"""The five questions that decide whether the books can be trusted.

Nothing in the application can produce a failure here. ``LedgerEntry`` and
``StockEntry`` are append-only (CLAUDE.md §3), every posting balances inside its
own transaction (§4), and a POSTED document cannot be edited (§5). So a failure
means something reached the database from outside those rules: a half-finished
restore, a corrupted SQLite file, a hand-edited row, a disk that lied about a
write.

That is exactly why it runs nightly. The cost of finding out in March that
February was wrong is the whole of March's reconciliation.

Each check returns a :class:`Finding`, and each one is written to be *actionable*
— it names the voucher, the account and the amount, because "the trial balance
is out" is not something anybody can act on and "SI-2026-000412 is out by 1
paisa on 4100 Sales" is.

Read-only throughout. This module never writes to the ledger, not even to
correct something it finds: a correction is a reversing entry somebody decides
to post, not a repair a nightly job makes on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import F, Q, Sum

from apps.accounting.models import LedgerEntry, StockEntry


@dataclass
class Finding:
    """One check's verdict.

    ``details`` are the offending rows, capped by the caller for display — a
    corrupt restore can produce thousands and a nightly log nobody can read is
    a nightly log nobody reads.
    """

    name: str
    ok: bool
    summary: str
    details: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "PASS" if self.ok else "FAIL"


#: How many offending rows a finding carries. The count in the summary is the
#: true total; this only bounds what gets printed.
MAX_DETAILS = 20


def check_trial_balance() -> Finding:
    """Total debits equal total credits across the whole ledger."""
    totals = LedgerEntry.objects.aggregate(debit=Sum("debit_paisa"), credit=Sum("credit_paisa"))
    debit, credit = totals["debit"] or 0, totals["credit"] or 0
    difference = debit - credit
    return Finding(
        name="Trial balance is zero",
        ok=difference == 0,
        summary=(
            f"debits {debit:,} paisa, credits {credit:,} paisa"
            if difference == 0
            else f"OUT BY {difference:,} paisa (debits {debit:,}, credits {credit:,})"
        ),
    )


def check_every_voucher_balances() -> Finding:
    """Each document's own entries balance.

    Stronger than the trial balance and far more useful: a system can total to
    zero with two documents wrong in opposite directions, and the trial balance
    will never say so. This one names the document.
    """
    rows = (
        LedgerEntry.objects.values("voucher_type", "voucher_code")
        .annotate(debit=Sum("debit_paisa"), credit=Sum("credit_paisa"))
        .filter(~Q(debit=F("credit")))
        .order_by("voucher_code")
    )
    offenders = [
        f"{row['voucher_code']} ({row['voucher_type']}) is out by "
        f"{(row['debit'] or 0) - (row['credit'] or 0):,} paisa"
        for row in rows[: MAX_DETAILS + 1]
    ]
    total = rows.count()
    return Finding(
        name="Every voucher balances on its own",
        ok=total == 0,
        summary=("every voucher balances" if total == 0 else f"{total} voucher(s) do not balance"),
        details=offenders[:MAX_DETAILS],
    )


def check_no_orphaned_entries() -> Finding:
    """Every ledger entry points at a document that still exists.

    Walks the voucher types actually present rather than a hard-coded list, so
    a document type added next year is checked without anybody remembering.
    """
    orphans: list[str] = []
    total = 0
    by_name = _models_by_voucher_type()

    voucher_types = LedgerEntry.objects.values_list("voucher_type", flat=True).distinct().order_by()
    for voucher_type in voucher_types:
        model = by_name.get(voucher_type)
        if model is None:
            # A voucher type with no model at all: every entry under it is
            # orphaned, and the label is the useful thing to report.
            count = LedgerEntry.objects.filter(voucher_type=voucher_type).count()
            total += count
            orphans.append(f"{count} entries reference unknown document type {voucher_type!r}")
            continue

        missing = (
            LedgerEntry.objects.filter(voucher_type=voucher_type)
            .exclude(voucher_id__in=model.objects.values("pk"))
            .values_list("voucher_code", "voucher_id")
            .order_by("voucher_code")
        )
        for code, pk in missing[: MAX_DETAILS + 1]:
            orphans.append(f"{code} (row {pk} of {voucher_type}) no longer exists")
        total += missing.count()

    return Finding(
        name="No orphaned ledger entries",
        ok=total == 0,
        summary=(
            "every entry points at a document that exists"
            if total == 0
            else f"{total} orphaned entr(ies)"
        ),
        details=orphans[:MAX_DETAILS],
    )


def check_stock_matches_entries() -> Finding:
    """The stock position equals the sum of its entries.

    Computed twice and compared: once by the database aggregate the reports use,
    once row by row in Python. They can only disagree if a row is unreadable or
    the aggregate is wrong, and either is worth a phone call.
    """
    from collections import defaultdict

    by_hand: dict[tuple[int, int], int] = defaultdict(int)
    for item_id, warehouse_id, qty in StockEntry.objects.values_list(
        "item_id", "warehouse_id", "qty_base"
    ).iterator(chunk_size=5000):
        by_hand[(item_id, warehouse_id)] += qty

    aggregated = {
        (row["item_id"], row["warehouse_id"]): row["qty"] or 0
        for row in StockEntry.objects.values("item_id", "warehouse_id")
        .annotate(qty=Sum("qty_base"))
        .order_by()
    }

    mismatches = [
        f"item {item_id} in warehouse {warehouse_id}: aggregate says "
        f"{aggregated.get((item_id, warehouse_id), 0)}, its rows sum to {qty}"
        for (item_id, warehouse_id), qty in sorted(by_hand.items())
        if aggregated.get((item_id, warehouse_id), 0) != qty
    ]
    return Finding(
        name="Stock balances match their entries",
        ok=not mismatches,
        summary=(
            f"{len(by_hand)} item/warehouse position(s) agree"
            if not mismatches
            else f"{len(mismatches)} position(s) disagree"
        ),
        details=mismatches[:MAX_DETAILS],
    )


def check_posted_documents_have_entries() -> Finding:
    """A POSTED document has ledger entries. All of them, every type.

    A posted document with nothing in the ledger is money the business thinks it
    has recorded and has not — the failure that a report cannot show you,
    because reports read the ledger and the ledger is where it is missing from.
    """
    from django.apps import apps as django_apps

    from apps.core.enums import DocumentStatus
    from apps.core.models import DocumentModel

    offenders: list[str] = []
    total = 0

    for model in django_apps.get_models():
        if not issubclass(model, DocumentModel) or model._meta.abstract:
            continue
        # The bare class name, because that is what VoucherRef.of writes —
        # "SalesInvoice", not "sales.SalesInvoice", so a ledger listing reads
        # without a lookup table. Matching on the app-qualified label here found
        # nothing and reported every posted document in the system as missing,
        # which is how this comment came to be written.
        voucher_type = model.__name__
        posted = model.objects.filter(status=DocumentStatus.POSTED)
        if not posted.exists():
            continue

        missing = (
            posted.exclude(
                pk__in=LedgerEntry.objects.filter(voucher_type=voucher_type).values("voucher_id")
            )
            .values_list("code", flat=True)
            .order_by("code")
        )
        for code in missing[: MAX_DETAILS + 1]:
            offenders.append(f"{code} ({model._meta.label}) is POSTED but has no ledger entries")
        total += missing.count()

    return Finding(
        name="Every posted document has ledger entries",
        ok=total == 0,
        summary=(
            "every posted document is in the ledger"
            if total == 0
            else f"{total} posted document(s) wrote nothing"
        ),
        details=offenders[:MAX_DETAILS],
    )


def _models_by_voucher_type() -> dict[str, type]:
    """``{"SalesInvoice": <model>}`` — keyed the way the ledger stores it.

    ``LedgerEntry.voucher_type`` holds the **bare class name**, not the
    app-qualified label: see :meth:`apps.accounting.refs.VoucherRef.of`, which
    does it that way so a ledger listing reads without a lookup table.

    Only :class:`~apps.core.models.DocumentModel` subclasses are included. A
    name that appears twice across two apps is dropped rather than guessed at —
    reporting "unknown document type" is honest, and silently checking against
    the wrong model would not be.
    """
    from django.apps import apps as django_apps

    from apps.core.models import DocumentModel

    found: dict[str, type] = {}
    ambiguous: set[str] = set()
    for model in django_apps.get_models():
        if not issubclass(model, DocumentModel) or model._meta.abstract:
            continue
        name = model.__name__
        if name in found:
            ambiguous.add(name)
        found[name] = model

    for name in ambiguous:
        del found[name]
    return found


#: Run in this order: cheapest and broadest first, so a catastrophically wrong
#: database says so on line one rather than after the slowest check.
CHECKS = (
    check_trial_balance,
    check_every_voucher_balances,
    check_no_orphaned_entries,
    check_stock_matches_entries,
    check_posted_documents_have_entries,
)


def run_all() -> list[Finding]:
    """Every check, in order. Read-only; never raises for a *finding*."""
    return [check() for check in CHECKS]


__all__ = ["CHECKS", "Finding", "run_all"]
