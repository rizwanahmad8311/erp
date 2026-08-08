"""The accounting reports. Every figure is aggregated from the ledger.

Eight reports, and not one of them reads a total off a document header
(CLAUDE.md §6). They all go through :mod:`apps.reports.ledger`, which is the
only module in this app that touches :class:`~apps.accounting.models.LedgerEntry`
at all.

Two of them exist to be checked rather than read:

* the **Trial Balance** must sum to zero, and prints the difference in the alarm
  colour when it does not rather than hiding it. That single figure catches
  almost every ledger bug this system could develop, which is why
  ``tests/test_reports.py`` asserts it across a dataset containing posted,
  cancelled and amended documents;
* the **Balance Sheet** must satisfy ``assets = liabilities + equity + profit``,
  and prints its difference the same way.

Both differences are computed from the ledger's own rows and are never
suppressed. A report that quietly showed the prettier of two disagreeing numbers
would be worse than no report.
"""

from __future__ import annotations

from django.urls import reverse

from apps.accounting.enums import AccountType, PartyType, account_sign, party_sign
from apps.accounting.models import Account
from apps.accounting.services import account_balance, party_balance
from apps.core.money import Money, fmt
from apps.masters.models import Client, Vendor
from apps.payments import recovery
from apps.payments.enums import AGEING_BUCKETS, OVERDUE_BUCKETS, AgeingBucket

from .. import ledger
from ..columns import CODE, DATE, MONEY, TEXT, Column, ReportRow
from ..registry import Report, ReportResult, register

GROUP = "Accounting"


def _client_ledger_url(client_id: int) -> str:
    """Where a client-shaped row drills to: that shop's statement.

    A client has no screen of its own in this system — the shop *is* its ledger,
    and "what does this row mean" is always answered by the statement behind it.
    """
    return f"{reverse('reports:report', kwargs={'slug': 'client-ledger'})}?client={client_id}"


def _vendor_ledger_url(vendor_id: int) -> str:
    return f"{reverse('reports:report', kwargs={'slug': 'vendor-ledger'})}?vendor={vendor_id}"


# ===========================================================================
# General ledger
# ===========================================================================
GENERAL_LEDGER_COLUMNS = (
    Column("date", "Date", DATE, width=10),
    Column("voucher", "Voucher", CODE, width=14, link=True),
    Column("type", "Type", TEXT, width=14),
    Column("particulars", "Particulars", TEXT, width=28),
    Column("debit", "Debit", MONEY, width=12, total=True, blank_zero=True),
    Column("credit", "Credit", MONEY, width=12, total=True, blank_zero=True),
    Column("balance", "Balance", MONEY, width=13),
)


def _statement_rows(queryset, *, opening_paisa: int, sign: int, particulars):
    """The shared body of every running-balance statement.

    A general ledger, a client ledger and a vendor ledger differ in what they
    are filtered by and in nothing else: an opening figure, a row per ledger
    entry, and a balance that moves in the account's or the party's natural
    sign. Written once so the three cannot drift.
    """
    running = Money(opening_paisa)
    rows = [
        ReportRow(
            values={"particulars": "Opening balance", "balance": running.paisa},
            emphasis="opening",
        )
    ]

    entries = list(queryset.select_related("account").order_by("posting_date", "id"))
    targets = ledger.voucher_targets((entry.voucher_type, entry.voucher_id) for entry in entries)

    for entry in entries:
        running = running + Money(sign * (entry.debit_paisa - entry.credit_paisa))
        target = targets.get((entry.voucher_type, entry.voucher_id), ledger.VoucherTarget())
        rows.append(
            ReportRow(
                values={
                    "date": entry.posting_date,
                    "voucher": entry.voucher_code,
                    "type": ledger.voucher_label(entry.voucher_type),
                    "particulars": particulars(entry),
                    "debit": entry.debit_paisa,
                    "credit": entry.credit_paisa,
                    "balance": running.paisa,
                },
                url=target.url,
                status=target.status,
            )
        )
    return rows, running


def _tie_out(printed: int, expected: int, *, subject: str) -> str:
    """The one note that matters on a statement, printed either way.

    Same reasoning as :func:`apps.reports.pdf.ledgers._tie_out_note` and
    :meth:`apps.payments.recovery.ClientRecovery.ties_out`: the check being
    visible is what makes the rest of the page trustworthy.
    """
    if printed == expected:
        return f"Closing balance {fmt(printed)} agrees with the ledger."
    return (
        f"This statement does not tie out. It closes at {fmt(printed)}; the ledger says "
        f"{fmt(expected)} for {subject}. Do not act on this page — report it."
    )


def _build_general_ledger(criteria) -> ReportResult:
    account = criteria.account
    account_ids = account.subtree_ids()
    shared = {"account_ids": account_ids, "include_cancelled": criteria.include_cancelled}

    opening_map = ledger.account_totals(date_to=criteria.day_before, **shared)
    opening = sum(opening_map.get(pk, ledger.Totals()).net_paisa for pk in account_ids)
    opening *= account_sign(account.type)

    rows, running = _statement_rows(
        ledger.entries(date_from=criteria.date_from, date_to=criteria.date_to, **shared),
        opening_paisa=opening,
        sign=account_sign(account.type),
        particulars=lambda entry: entry.remarks or entry.account.name,
    )

    expected = account_balance(account, criteria.date_to).paisa
    note = _tie_out(running.paisa, expected, subject=str(account))

    totals = {
        "particulars": "Closing balance",
        "debit": sum(int(row.get("debit") or 0) for row in rows),
        "credit": sum(int(row.get("credit") or 0) for row in rows),
        "balance": running.paisa,
    }
    return ReportResult(
        rows=rows,
        totals=totals,
        subtitle=f"{account} · {criteria.period_label}",
        notes=(note,),
        alarm="" if running.paisa == expected else note,
    )


register(
    Report(
        slug="general-ledger",
        title="General Ledger",
        group=GROUP,
        description=(
            "Every entry on one account between two dates, with a running balance. "
            "Each row links to the voucher that wrote it."
        ),
        columns=GENERAL_LEDGER_COLUMNS,
        filters=("account", "date_from", "date_to"),
        requires=("account",),
        build=_build_general_ledger,
    )
)


# ===========================================================================
# Trial balance
# ===========================================================================
TRIAL_BALANCE_COLUMNS = (
    Column("code", "Code", CODE, width=8),
    Column("account", "Account", TEXT, width=34),
    Column("type", "Type", TEXT, width=12),
    Column("debit", "Debit", MONEY, width=14, total=True, blank_zero=True),
    Column("credit", "Credit", MONEY, width=14, total=True, blank_zero=True),
)


def _build_trial_balance(criteria) -> ReportResult:
    """Every postable account's net balance as at a date, split into two columns.

    The columns must add up to the same figure, because every posting is
    balanced inside its own transaction (CLAUDE.md §4) and a cancellation writes
    the exact mirror of what it reverses (§3). So the difference is zero — and
    when it is not, this is where it becomes visible, which is the entire point
    of running it.
    """
    totals = ledger.account_totals(
        date_to=criteria.as_of, include_cancelled=criteria.include_cancelled
    )
    accounts = {
        account.pk: account for account in Account.objects.filter(pk__in=totals).order_by("code")
    }

    rows = []
    zero_netted = 0
    for pk, account in accounts.items():
        net = totals[pk].net_paisa
        if net == 0:
            zero_netted += 1
            continue
        rows.append(
            ReportRow(
                values={
                    "code": account.code,
                    "account": account.name,
                    "type": AccountType(account.type).label,
                    "debit": net if net > 0 else 0,
                    "credit": -net if net < 0 else 0,
                }
            )
        )

    debit_total = sum(int(row.get("debit")) for row in rows)
    credit_total = sum(int(row.get("credit")) for row in rows)
    difference = debit_total - credit_total

    notes = [
        "Debits and credits are net per account: an account is in one column or the other, "
        "never both. The two columns must total the same figure.",
    ]
    if zero_netted:
        notes.append(
            f"{zero_netted} account{'s' if zero_netted != 1 else ''} netted to zero and "
            f"{'are' if zero_netted != 1 else 'is'} not listed."
        )
    if not criteria.include_cancelled:
        notes.append(
            "Cancelled documents are left out. Their entries and the reversals net to zero, "
            "so including them changes the listing and not this total."
        )

    return ReportResult(
        rows=rows,
        totals={"account": "Total", "debit": debit_total, "credit": credit_total},
        subtitle=f"As at {criteria.as_of:%d %b %Y}",
        notes=tuple(notes),
        alarm=(
            ""
            if difference == 0
            else f"This trial balance does not balance. Debits exceed credits by "
            f"{fmt(difference)}. The ledger has an unbalanced posting in it — "
            f"do not close the period, and report this."
        ),
    )


register(
    Report(
        slug="trial-balance",
        title="Trial Balance",
        group=GROUP,
        description=(
            "Every account's net balance as at a date. The two columns must total the same "
            "figure; the difference is shown when they do not."
        ),
        columns=TRIAL_BALANCE_COLUMNS,
        filters=("as_of",),
        build=_build_trial_balance,
    )
)


# ===========================================================================
# Profit and loss, and the balance sheet
# ===========================================================================
STATEMENT_COLUMNS = (
    Column("code", "Code", CODE, width=8),
    Column("account", "Account", TEXT, width=44),
    Column("amount", "Amount", MONEY, width=16),
)


def _section(account_type: str, totals, *, indent="   ") -> tuple[list[ReportRow], int]:
    """One block of a financial statement: a heading, its leaves, a sub-total.

    Balances are in the account's **natural sign**
    (:func:`apps.accounting.enums.account_sign`), so income reads positive when
    it was credited and a contra account like Sales Returns reads negative and
    nets against its siblings — which is exactly what a contra account is for.
    """
    sign = account_sign(account_type)
    accounts = Account.objects.filter(type=account_type, is_group=False).order_by("code")

    rows = [ReportRow(values={"account": AccountType(account_type).label}, emphasis="heading")]
    subtotal = 0
    for account in accounts:
        balance = sign * totals.get(account.pk, ledger.Totals()).net_paisa
        if balance == 0:
            continue
        subtotal += balance
        rows.append(
            ReportRow(
                values={
                    "code": account.code,
                    "account": f"{indent}{account.name}",
                    "amount": balance,
                }
            )
        )

    rows.append(
        ReportRow(
            values={
                "account": f"Total {AccountType(account_type).label.lower()}",
                "amount": subtotal,
            },
            emphasis="subtotal",
        )
    )
    return rows, subtotal


def _build_profit_and_loss(criteria) -> ReportResult:
    totals = ledger.account_totals(
        date_from=criteria.date_from,
        date_to=criteria.date_to,
        include_cancelled=criteria.include_cancelled,
    )
    income_rows, income = _section(AccountType.INCOME, totals)
    expense_rows, expenses = _section(AccountType.EXPENSE, totals)
    profit = income - expenses

    return ReportResult(
        rows=[*income_rows, *expense_rows],
        totals={
            "account": "Profit for the period" if profit >= 0 else "Loss for the period",
            "amount": profit,
        },
        subtitle=criteria.period_label,
        notes=(
            "Movement in the period only — nothing is carried forward. Cost of goods sold is "
            "the stock ledger's own valuation of what left the warehouse, not a purchase figure.",
        ),
    )


register(
    Report(
        slug="profit-and-loss",
        title="Profit & Loss",
        group=GROUP,
        description="Income and expenses over a period, and what is left.",
        columns=STATEMENT_COLUMNS,
        filters=("date_from", "date_to"),
        build=_build_profit_and_loss,
    )
)


def _build_balance_sheet(criteria) -> ReportResult:
    """What is owned, what is owed and what is left, as at a date.

    The check at the bottom is ``assets = liabilities + equity + profit``, and it
    holds for the same arithmetic reason the trial balance does: every posting
    balances, so summing ``debit - credit`` over every account gives zero, and
    rearranging that identity by account type gives this one. The profit line is
    the period-to-date result that has not been closed to equity yet — without
    it the sheet is out by exactly the year's trading.
    """
    totals = ledger.account_totals(
        date_to=criteria.as_of, include_cancelled=criteria.include_cancelled
    )

    asset_rows, assets = _section(AccountType.ASSET, totals)
    liability_rows, liabilities = _section(AccountType.LIABILITY, totals)
    equity_rows, equity = _section(AccountType.EQUITY, totals)

    income = sum(
        -totals.get(pk, ledger.Totals()).net_paisa
        for pk in Account.objects.filter(type=AccountType.INCOME, is_group=False).values_list(
            "pk", flat=True
        )
    )
    expenses = sum(
        totals.get(pk, ledger.Totals()).net_paisa
        for pk in Account.objects.filter(type=AccountType.EXPENSE, is_group=False).values_list(
            "pk", flat=True
        )
    )
    profit = income - expenses

    profit_rows = [
        ReportRow(
            values={"account": "Profit and loss (not yet closed to equity)", "amount": profit},
            emphasis="subtotal",
        )
    ]
    funding = liabilities + equity + profit
    difference = assets - funding

    return ReportResult(
        rows=[*asset_rows, *liability_rows, *equity_rows, *profit_rows],
        totals={"account": "Liabilities + equity + profit", "amount": funding},
        subtitle=f"As at {criteria.as_of:%d %b %Y}",
        notes=(
            f"Assets total {fmt(assets)}. Liabilities, equity and the period result total "
            f"{fmt(funding)}.",
            "The profit line is this year's trading, which has not been closed to equity. "
            "Without it the sheet would be out by exactly that figure.",
        ),
        alarm=(
            ""
            if difference == 0
            else f"This balance sheet does not balance. Assets exceed liabilities, equity and "
            f"profit by {fmt(difference)}. Run the Trial Balance — the fault is in the "
            f"ledger, not on this page."
        ),
    )


register(
    Report(
        slug="balance-sheet",
        title="Balance Sheet",
        group=GROUP,
        description="What is owned, what is owed and what is left, as at a date.",
        columns=STATEMENT_COLUMNS,
        filters=("as_of",),
        build=_build_balance_sheet,
    )
)


# ===========================================================================
# Party statements
# ===========================================================================
PARTY_LEDGER_COLUMNS = (
    Column("date", "Date", DATE, width=10),
    Column("voucher", "Voucher", CODE, width=15, link=True),
    Column("type", "Type", TEXT, width=15),
    Column("particulars", "Particulars", TEXT, width=28),
    Column("debit", "Debit", MONEY, width=12, total=True, blank_zero=True),
    Column("credit", "Credit", MONEY, width=12, total=True, blank_zero=True),
    Column("balance", "Balance", MONEY, width=13),
)


def _party_statement(party_type: str, party, criteria) -> ReportResult:
    """One party's account between two dates, opening to closing.

    Deliberately the plainest arithmetic available::

        opening  = party_balance(as_of = the day before the window)
        movement = every ledger row tagged with this party in the window
        closing  = opening + movement

    and the closing figure is checked against
    :func:`apps.accounting.services.party_balance` at the far end of the window
    before it is printed.
    """
    opening = party_balance(party_type, party.pk, criteria.day_before).paisa
    rows, running = _statement_rows(
        ledger.entries(
            date_from=criteria.date_from,
            date_to=criteria.date_to,
            party_type=party_type,
            party_ids=[party.pk],
            include_cancelled=criteria.include_cancelled,
        ),
        opening_paisa=opening,
        sign=party_sign(party_type),
        particulars=lambda entry: entry.remarks or entry.account.name,
    )

    expected = party_balance(party_type, party.pk, criteria.date_to).paisa
    note = _tie_out(running.paisa, expected, subject=f"{party.code} {party.name}")

    return ReportResult(
        rows=rows,
        totals={
            "particulars": "Closing balance",
            "debit": sum(int(row.get("debit") or 0) for row in rows),
            "credit": sum(int(row.get("credit") or 0) for row in rows),
            "balance": running.paisa,
        },
        subtitle=f"{party.code} — {party.name} · {criteria.period_label}",
        notes=(
            note,
            "A cancelled document appears twice — the original and the entry that took it "
            "back — when the audit toggle is on, and nets to zero either way.",
        ),
        alarm="" if running.paisa == expected else note,
    )


register(
    Report(
        slug="client-ledger",
        title="Client Ledger",
        group=GROUP,
        description="One shop's statement of account, with a running balance.",
        columns=PARTY_LEDGER_COLUMNS,
        filters=("client", "date_from", "date_to"),
        requires=("client",),
        build=lambda criteria: _party_statement(PartyType.CLIENT, criteria.client, criteria),
    )
)

register(
    Report(
        slug="vendor-ledger",
        title="Vendor Ledger",
        group=GROUP,
        description="One supplier's statement of account, with a running balance.",
        columns=PARTY_LEDGER_COLUMNS,
        filters=("vendor", "date_from", "date_to"),
        requires=("vendor",),
        build=lambda criteria: _party_statement(PartyType.VENDOR, criteria.vendor, criteria),
    )
)


# ===========================================================================
# Ageing
# ===========================================================================
def _bucket_columns() -> tuple[Column, ...]:
    """One money column per ageing band, in ladder order.

    Built from :data:`apps.payments.enums.AGEING_BUCKETS` rather than typed out,
    so a band added to the ladder appears on both ageing reports, in the CSV and
    on the PDF, without anything here changing.
    """
    return tuple(
        Column(
            f"bucket_{bucket}",
            AgeingBucket(bucket).label,
            MONEY,
            width=11,
            total=True,
            blank_zero=True,
        )
        for bucket in AGEING_BUCKETS
    )


RECEIVABLE_AGEING_COLUMNS = (
    Column("code", "Code", CODE, width=8),
    Column("client", "Client", TEXT, width=22),
    Column("route", "Route", TEXT, width=9),
    Column("seller", "Seller", TEXT, width=11),
    *_bucket_columns(),
    Column("outstanding", "Outstanding", MONEY, width=12, total=True),
    Column("on_account", "On account", MONEY, width=11, total=True, blank_zero=True),
)


def _build_receivable_ageing(criteria) -> ReportResult:
    """What every shop owes, laid out by how overdue it is.

    Straight from :func:`apps.payments.recovery.recovery_rows`, which aggregates
    the ledger and takes its due dates from the documents — the one fact a
    ledger row has never heard of. Nothing here re-derives an amount; this
    report is the recovery workspace with a filter bar and three output formats
    on it.
    """
    rows_data = recovery.recovery_rows(
        as_of=criteria.as_of, route=criteria.route, seller=criteria.seller
    )

    rows = []
    for row in rows_data:
        buckets = row.buckets
        values = {
            "code": row.client.code,
            "client": row.client.name,
            "route": row.client.route.code if row.client.route_id else "—",
            "seller": row.client.seller.name if row.client.seller_id else "—",
            "outstanding": row.open_paisa,
            "on_account": row.on_account_paisa,
        }
        for bucket in AGEING_BUCKETS:
            values[f"bucket_{bucket}"] = buckets[bucket]

        rows.append(
            ReportRow(
                values=values,
                url=_client_ledger_url(row.client.pk),
                alarm=frozenset(
                    f"bucket_{bucket}" for bucket in OVERDUE_BUCKETS if buckets[bucket]
                ),
            )
        )

    totals = {"client": "Total"}
    for column in RECEIVABLE_AGEING_COLUMNS:
        if column.total:
            totals[column.key] = sum(int(row.get(column.key) or 0) for row in rows)

    flagged = sum(1 for row in rows_data if row.is_flagged)
    notes = [
        "Ageing is measured in days past the **due** date, not days since the invoice: "
        "a bill on fifteen days' credit is not overdue on day fourteen.",
        "On-account money is a receipt or a credit note nobody has applied to a bill yet. "
        "It is real and it reduces what the shop owes.",
        "The cancelled toggle does not change this report. Every figure is a sum over the "
        "shop's ledger rows, where a cancelled document and its reversal are both present "
        "and net to zero — so the bill simply is not open, either way.",
    ]
    if flagged:
        notes.append(
            f"{flagged} of these shops {'have' if flagged != 1 else 'has'} handed over a cheque "
            f"that bounced. The recovery workspace flags them by name."
        )

    return ReportResult(
        rows=rows,
        totals=totals,
        subtitle=f"As at {criteria.as_of:%d %b %Y}",
        notes=tuple(notes),
    )


register(
    Report(
        slug="receivable-ageing",
        title="Accounts Receivable Ageing",
        group=GROUP,
        description="What every shop owes, by how overdue it is. Filterable by route and seller.",
        columns=RECEIVABLE_AGEING_COLUMNS,
        filters=("as_of", "route", "seller"),
        landscape=True,
        build=_build_receivable_ageing,
    )
)


PAYABLE_AGEING_COLUMNS = (
    Column("code", "Code", CODE, width=8),
    Column("vendor", "Vendor", TEXT, width=30),
    Column("city", "City", TEXT, width=12),
    *_bucket_columns(),
    Column("outstanding", "Outstanding", MONEY, width=13, total=True),
)


def _build_payable_ageing(criteria) -> ReportResult:
    """What we owe every supplier, laid out the same way.

    Built on :func:`apps.payments.recovery.open_items`, one call per vendor that
    the ledger has actually heard of. That is a query per supplier, which is
    fine here and is not fine on the receivable side: a distribution business
    buys from dozens of suppliers and sells to thousands of shops, which is why
    the client version goes through the batched
    :func:`~apps.payments.recovery.recovery_rows` instead.
    """
    vendor_ids = ledger.parties_with_movement(PartyType.VENDOR, date_to=criteria.as_of)
    vendors = {
        vendor.pk: vendor for vendor in Vendor.objects.filter(pk__in=vendor_ids).order_by("code")
    }

    rows = []
    for pk, vendor in vendors.items():
        items, credits = recovery.open_items(PartyType.VENDOR, pk, as_of=criteria.as_of)
        if not items and not credits:
            continue

        buckets = dict.fromkeys(AGEING_BUCKETS, 0)
        for item in items:
            buckets[item.bucket] += item.outstanding_paisa

        values = {
            "code": vendor.code,
            "vendor": vendor.name,
            "city": vendor.city or "—",
            "outstanding": sum(item.outstanding_paisa for item in items),
        }
        for bucket in AGEING_BUCKETS:
            values[f"bucket_{bucket}"] = buckets[bucket]

        rows.append(
            ReportRow(
                values=values,
                url=_vendor_ledger_url(pk),
                alarm=frozenset(
                    f"bucket_{bucket}" for bucket in OVERDUE_BUCKETS if buckets[bucket]
                ),
            )
        )

    rows.sort(key=lambda row: -int(row.get("outstanding") or 0))

    totals = {"vendor": "Total"}
    for column in PAYABLE_AGEING_COLUMNS:
        if column.total:
            totals[column.key] = sum(int(row.get(column.key) or 0) for row in rows)

    return ReportResult(
        rows=rows,
        totals=totals,
        subtitle=f"As at {criteria.as_of:%d %b %Y}",
        notes=(
            "A supplier bill has no due date in this system, so it ages from the day it hit "
            "the books — which is what 'due on sight' means on paper.",
            "The cancelled toggle does not change this report: a cancelled bill and its "
            "reversal are both in the ledger and net to zero, so it is not open either way.",
        ),
    )


register(
    Report(
        slug="payable-ageing",
        title="Accounts Payable Ageing",
        group=GROUP,
        description="What we owe every supplier, by how long it has been outstanding.",
        columns=PAYABLE_AGEING_COLUMNS,
        filters=("as_of",),
        landscape=True,
        build=_build_payable_ageing,
    )
)


# ===========================================================================
# Day book
# ===========================================================================
DAY_BOOK_COLUMNS = (
    Column("voucher", "Voucher", CODE, width=14, link=True),
    Column("type", "Type", TEXT, width=14),
    Column("account", "Account", TEXT, width=26),
    Column("party", "Party", TEXT, width=16),
    Column("particulars", "Particulars", TEXT, width=18),
    Column("debit", "Debit", MONEY, width=12, total=True, blank_zero=True),
    Column("credit", "Credit", MONEY, width=12, total=True, blank_zero=True),
)


def _build_day_book(criteria) -> ReportResult:
    """Everything that hit the books on one day, voucher by voucher.

    Ordered by voucher rather than by account, because that is how it is read:
    somebody is looking for what a particular bill did, and the two sides of a
    posting sitting next to each other is what makes it checkable by eye. The
    two columns must total the same figure — every posting balances inside its
    own transaction (CLAUDE.md §4), so a day that does not balance is a day with
    a bug in it.
    """
    entries = list(
        ledger.entries(
            date_from=criteria.as_of,
            date_to=criteria.as_of,
            include_cancelled=criteria.include_cancelled,
        )
        .select_related("account")
        .order_by("voucher_type", "voucher_code", "id")
    )
    targets = ledger.voucher_targets((entry.voucher_type, entry.voucher_id) for entry in entries)

    parties = _party_names(entries)
    rows = []
    for entry in entries:
        target = targets.get((entry.voucher_type, entry.voucher_id), ledger.VoucherTarget())
        rows.append(
            ReportRow(
                values={
                    "voucher": entry.voucher_code,
                    "type": ledger.voucher_label(entry.voucher_type),
                    "account": f"{entry.account.code} {entry.account.name}",
                    "party": parties.get((entry.party_type, entry.party_id), "—"),
                    "particulars": entry.remarks or "",
                    "debit": entry.debit_paisa,
                    "credit": entry.credit_paisa,
                },
                url=target.url,
                status=target.status,
            )
        )

    debit_total = sum(int(row.get("debit")) for row in rows)
    credit_total = sum(int(row.get("credit")) for row in rows)
    voucher_count = len({(entry.voucher_type, entry.voucher_id) for entry in entries})

    return ReportResult(
        rows=rows,
        totals={"particulars": "Total", "debit": debit_total, "credit": credit_total},
        subtitle=f"{criteria.as_of:%d %b %Y} · {voucher_count} voucher{'s' if voucher_count != 1 else ''}",
        notes=(
            "Both sides of every posting are here, which is why the two columns total the same "
            "figure. Rows are grouped by voucher, not by account.",
        ),
        alarm=(
            ""
            if debit_total == credit_total
            else f"This day does not balance: debits {fmt(debit_total)} against credits "
            f"{fmt(credit_total)}. One of the day's postings is broken — report it."
        ),
    )


def _party_names(entries) -> dict[tuple[str, int], str]:
    """``{(party_type, party_id): "CODE — Name"}`` for a page of ledger rows.

    Two queries whatever the page holds. The ledger's party link is soft — a
    type and an id, no foreign key (CLAUDE.md §3) — so this is the join that
    would otherwise be one query per row.
    """
    wanted: dict[str, set[int]] = {PartyType.CLIENT: set(), PartyType.VENDOR: set()}
    for entry in entries:
        if entry.party_type in wanted and entry.party_id:
            wanted[entry.party_type].add(entry.party_id)

    names: dict[tuple[str, int], str] = {}
    for party_type, model in ((PartyType.CLIENT, Client), (PartyType.VENDOR, Vendor)):
        if not wanted[party_type]:
            continue
        for code, name, pk in model.objects.filter(pk__in=wanted[party_type]).values_list(
            "code", "name", "pk"
        ):
            names[(party_type, pk)] = f"{code} — {name}"
    return names


register(
    Report(
        slug="day-book",
        title="Day Book",
        group=GROUP,
        description="Every voucher that hit the books on one day, both sides of each posting.",
        columns=DAY_BOOK_COLUMNS,
        filters=("as_of",),
        landscape=True,
        build=_build_day_book,
    )
)


__all__ = ["GROUP"]
