"""Enumerations for money in and money out, and the ageing ladder.

Four of these describe a payment — which way the money went, what it moved in,
and what happened to the cheque afterwards. The fifth, :class:`AgeingBucket`, is
the recovery workspace's whole vocabulary and is the reason this module exists
rather than a handful of string literals scattered across a template.

Document prefixes live here for the same reason they live in
:mod:`apps.sales.enums`: ``RV-2026-000123`` is a receipt and nothing else, for
as long as the ledger row that names it exists.
"""

from __future__ import annotations

from django.db import models

#: Receipt voucher: money coming in.
RECEIPT_PREFIX = "RV"

#: Payment voucher: money going out.
PAYMENT_PREFIX = "PV"

#: Cheque event: a cheque clearing, or bouncing.
CHEQUE_EVENT_PREFIX = "CHQ"


class PaymentDirection(models.TextChoices):
    """Which way the money moved.

    Deliberately independent of the party type. Receiving from a vendor is a
    refund of an overpayment and paying a client is a refund of a credit note;
    both are ordinary, and a design that inferred the direction from the party
    could not express either.
    """

    RECEIVE = "RECEIVE", "Received"
    PAY = "PAY", "Paid"


class PaymentMode(models.TextChoices):
    """What the money moved in, which is what decides the account it lands in.

    ``CHEQUE`` is not a flavour of ``BANK``. A cheque posts to Cheques in Hand
    (or Cheques Issued) and only reaches the bank when it clears — see
    :class:`~apps.payments.models.ChequeEvent`. Treating it as a bank
    transaction on the day it is taken overstates the bank by every post-dated
    cheque in the drawer, which in this business is most of them.
    """

    CASH = "CASH", "Cash"
    BANK = "BANK", "Bank"
    CHEQUE = "CHEQUE", "Cheque"


class ChequeStatus(models.TextChoices):
    """Where a cheque has got to. **Derived, never stored.**

    There is no ``cheque_status`` column on :class:`~apps.payments.models.Payment`
    and there must not be. The status is a fact about the cheque's
    :class:`~apps.payments.models.ChequeEvent` rows, and a column would be a
    second answer that can disagree with them (CLAUDE.md §6) — as well as a
    field somebody would have to write onto a POSTED document, which the
    lifecycle forbids outright.
    """

    PENDING = "PENDING", "Not yet cleared"
    CLEARED = "CLEARED", "Cleared"
    BOUNCED = "BOUNCED", "Bounced"


class ChequeEventKind(models.TextChoices):
    """What the bank did with the cheque.

    Both are postings, not flags. Clearing moves the money from Cheques in Hand
    to Bank; bouncing puts the receivable back where it was. Neither edits the
    payment that wrote the cheque — CLAUDE.md §3 and §5 leave no room for that.
    """

    CLEARED = "CLEARED", "Cleared"
    BOUNCED = "BOUNCED", "Bounced"


class AgeingBucket(models.TextChoices):
    """How overdue a receivable is, in the five bands the office uses.

    Measured in **days past due**, not days since the invoice: an invoice on
    fifteen days' credit is not overdue on day fourteen, and a report that said
    it was would have the accountant chasing shops that owe nothing yet.

    The bands are inclusive at both ends and butt up against each other with no
    gap, so a day belongs to exactly one of them. Day 30 is the last day of
    ``1-30``, day 60 the last of ``31-60``, day 90 the last of ``61-90``. Those
    three boundaries are the ones everybody argues about, so
    ``tests/test_recovery.py`` pins all three.
    """

    CURRENT = "CURRENT", "Current"
    DAYS_1_30 = "1-30", "1-30 days"
    DAYS_31_60 = "31-60", "31-60 days"
    DAYS_61_90 = "61-90", "61-90 days"
    DAYS_90_PLUS = "90+", "Over 90 days"


#: ``(bucket, first day, last day)`` — the ladder, in report order. ``None`` as
#: the last day means "and everything beyond". The overdue bands only; CURRENT
#: is what is left when nothing here matches.
AGEING_LADDER: tuple[tuple[str, int, int | None], ...] = (
    (AgeingBucket.DAYS_1_30, 1, 30),
    (AgeingBucket.DAYS_31_60, 31, 60),
    (AgeingBucket.DAYS_61_90, 61, 90),
    (AgeingBucket.DAYS_90_PLUS, 91, None),
)

#: Every bucket in report order, CURRENT first. What the workspace iterates.
AGEING_BUCKETS: tuple[str, ...] = (
    AgeingBucket.CURRENT,
    *(bucket for bucket, _first, _last in AGEING_LADDER),
)

#: Buckets that mean somebody is late. Rendered in the alarm colour.
OVERDUE_BUCKETS: frozenset[str] = frozenset(AGEING_BUCKETS) - {AgeingBucket.CURRENT}


def bucket_for(days_overdue: int) -> str:
    """Which band a number of days past due falls in.

        bucket_for(0)   -> CURRENT      (due today; not late)
        bucket_for(1)   -> 1-30
        bucket_for(30)  -> 1-30         (the boundary, inclusive)
        bucket_for(31)  -> 31-60
        bucket_for(60)  -> 31-60
        bucket_for(61)  -> 61-90
        bucket_for(90)  -> 61-90
        bucket_for(91)  -> 90+

    Negative days — an invoice not yet due — are CURRENT, which is why the
    ladder starts at 1 rather than at 0.
    """
    if days_overdue < 1:
        return AgeingBucket.CURRENT
    for bucket, first, last in AGEING_LADDER:
        if days_overdue >= first and (last is None or days_overdue <= last):
            return bucket
    # Unreachable: the ladder's last band is open-ended. Kept so that narrowing
    # it in future fails here rather than returning None into a template.
    raise ValueError(f"No ageing bucket covers {days_overdue} days overdue.")


def bucket_label(bucket: str) -> str:
    """The human label for a bucket value, for a heading or a filter chip."""
    try:
        return AgeingBucket(bucket).label
    except ValueError:
        return str(bucket)
