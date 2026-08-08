"""
The four ledger services: posting, reversal, and the two balances derived from
what they wrote.

Every figure this system will ever print comes out of ``account_balance`` or
``party_balance``, and both of them are pure aggregation over rows written by
``post_entries`` and ``reverse_entries``. Nothing is cached, so these tests are
the whole story: if a balance is right here it is right in the report.
"""

import datetime as dt

import pytest
from django.db import transaction

from apps.accounting.enums import PartyType
from apps.accounting.exceptions import (
    AlreadyPosted,
    AlreadyReversed,
    GroupAccountPosting,
    InactiveAccount,
    InvalidPosting,
    UnbalancedEntry,
)
from apps.accounting.models import LedgerEntry
from apps.accounting.refs import PartyRef
from apps.accounting.services import (
    account_balance,
    party_balance,
    post_entries,
    reverse_entries,
)
from apps.core.money import Money
from tests.testapp.models import SampleDocument

pytestmark = pytest.mark.django_db

APRIL = dt.date(2026, 4, 1)
MAY = dt.date(2026, 5, 1)
JUNE = dt.date(2026, 6, 1)

CLIENT = 7
OTHER_CLIENT = 8
VENDOR = 3


# ---------------------------------------------------------------------------
# Helpers shaped like the posting services the sales/payments apps will have.
# ---------------------------------------------------------------------------
def document(code: str) -> SampleDocument:
    return SampleDocument.objects.create(code=code, party_name="Ali Traders")


def post_sale(doc, accounts, *, client_id, total_paisa, tax_paisa=0, on=APRIL, user=None):
    """Receivable debited, income (and tax) credited — one invoice, posted."""
    lines = [
        {
            "account": accounts.receivable,
            "debit_paisa": total_paisa,
            "party": PartyRef(PartyType.CLIENT, client_id),
            "remarks": f"{doc.code} total",
        },
        {"account": accounts.sales, "credit_paisa": total_paisa - tax_paisa},
    ]
    if tax_paisa:
        lines.append({"account": accounts.tax_payable, "credit_paisa": tax_paisa})

    with transaction.atomic():
        post_entries(doc, lines, on, user=user)
        doc.mark_posted(user=user)
        doc.save()
    return doc


def post_receipt(doc, accounts, *, client_id, amount_paisa, on=APRIL, user=None):
    """Cash in, receivable down."""
    with transaction.atomic():
        post_entries(
            doc,
            [
                {"account": accounts.cash, "debit_paisa": amount_paisa},
                {
                    "account": accounts.receivable,
                    "credit_paisa": amount_paisa,
                    "party": PartyRef(PartyType.CLIENT, client_id),
                },
            ],
            on,
            user=user,
        )
        doc.mark_posted(user=user)
        doc.save()
    return doc


def cancel(doc, *, user=None, reason="correction"):
    """What every ``cancel()`` will do: reverse, then freeze the header."""
    with transaction.atomic():
        reverse_entries(doc, user=user)
        doc.mark_cancelled(user=user, reason=reason)
        doc.save()
    return doc


@pytest.fixture
def simple_lines(accounts):
    """Rs 500 of rent paid in cash."""
    return [
        {"account": accounts.rent, "debit_paisa": 50000, "remarks": "April rent"},
        {"account": accounts.cash, "credit_paisa": 50000},
    ]


# ---------------------------------------------------------------------------
# post_entries
# ---------------------------------------------------------------------------
class TestBalancedPostingSucceeds:
    def test_rows_are_written(self, accounts, ledger_voucher, simple_lines):
        created = post_entries(ledger_voucher, simple_lines, APRIL)

        assert len(created) == 2
        assert LedgerEntry.objects.count() == 2

    def test_each_row_carries_the_voucher_reference(self, accounts, ledger_voucher, simple_lines):
        post_entries(ledger_voucher, simple_lines, APRIL)

        for row in LedgerEntry.objects.all():
            assert row.voucher_type == "SampleDocument"
            assert row.voucher_id == ledger_voucher.pk
            assert row.voucher_code == "SI-2026-000001"

    def test_the_sides_land_where_they_were_put(self, accounts, ledger_voucher, simple_lines):
        post_entries(ledger_voucher, simple_lines, APRIL)

        debit = LedgerEntry.objects.get(account=accounts.rent)
        credit = LedgerEntry.objects.get(account=accounts.cash)

        assert (debit.debit_paisa, debit.credit_paisa) == (50000, 0)
        assert (credit.debit_paisa, credit.credit_paisa) == (0, 50000)
        assert debit.remarks == "April rent"
        assert credit.remarks == ""

    def test_nothing_is_flagged_as_a_reversal(self, accounts, ledger_voucher, simple_lines):
        post_entries(ledger_voucher, simple_lines, APRIL)

        assert not LedgerEntry.objects.filter(is_reversal=True).exists()
        assert not LedgerEntry.objects.filter(reverses__isnull=False).exists()

    def test_the_posting_date_is_the_one_given_not_today(
        self, accounts, ledger_voucher, simple_lines
    ):
        post_entries(ledger_voucher, simple_lines, APRIL)

        assert {row.posting_date for row in LedgerEntry.objects.all()} == {APRIL}

    def test_the_author_is_recorded(self, accounts, ledger_voucher, simple_lines, user):
        post_entries(ledger_voucher, simple_lines, APRIL, user=user)

        assert all(row.created_by == user for row in LedgerEntry.objects.all())

    def test_a_party_is_stored_as_a_pair(self, accounts, ledger_voucher):
        post_entries(
            ledger_voucher,
            [
                {
                    "account": accounts.receivable,
                    "debit_paisa": 50000,
                    "party": PartyRef(PartyType.CLIENT, CLIENT),
                },
                {"account": accounts.sales, "credit_paisa": 50000},
            ],
            APRIL,
        )

        receivable = LedgerEntry.objects.get(account=accounts.receivable)
        sales = LedgerEntry.objects.get(account=accounts.sales)

        assert (receivable.party_type, receivable.party_id) == (PartyType.CLIENT, CLIENT)
        assert (sales.party_type, sales.party_id) == (None, None)

    def test_a_party_may_be_given_as_a_plain_pair(self, accounts, ledger_voucher):
        post_entries(
            ledger_voucher,
            [
                {
                    "account": accounts.receivable,
                    "debit_paisa": 50000,
                    "party": (PartyType.CLIENT, CLIENT),
                },
                {"account": accounts.sales, "credit_paisa": 50000},
            ],
            APRIL,
        )
        assert LedgerEntry.objects.filter(party_type="CLIENT", party_id=CLIENT).count() == 1

    def test_many_lines_balance_in_aggregate_not_pairwise(self, accounts, ledger_voucher):
        """A real invoice is one debit against several credits."""
        post_entries(
            ledger_voucher,
            [
                {"account": accounts.receivable, "debit_paisa": 118000},
                {"account": accounts.sales, "credit_paisa": 95000},
                {"account": accounts.tax_payable, "credit_paisa": 18000},
                {"account": accounts.discount_received, "credit_paisa": 5000},
            ],
            APRIL,
        )
        assert LedgerEntry.objects.count() == 4


class TestUnbalancedPostingRaises:
    def test_it_raises(self, accounts, ledger_voucher):
        with pytest.raises(UnbalancedEntry):
            post_entries(
                ledger_voucher,
                [
                    {"account": accounts.rent, "debit_paisa": 50000},
                    {"account": accounts.cash, "credit_paisa": 49900},
                ],
                APRIL,
            )

    def test_the_message_states_the_difference_in_paisa(self, accounts, ledger_voucher):
        with pytest.raises(UnbalancedEntry) as exc:
            post_entries(
                ledger_voucher,
                [
                    {"account": accounts.rent, "debit_paisa": 50000},
                    {"account": accounts.cash, "credit_paisa": 49900},
                ],
                APRIL,
            )

        message = str(exc.value)
        assert "100 paisa" in message, "the difference must be stated in paisa"
        assert "excess debit" in message
        assert "SI-2026-000001" in message, "the error must name the voucher"

    def test_an_excess_credit_is_reported_as_such(self, accounts, ledger_voucher):
        with pytest.raises(UnbalancedEntry, match="excess credit"):
            post_entries(
                ledger_voucher,
                [
                    {"account": accounts.rent, "debit_paisa": 49900},
                    {"account": accounts.cash, "credit_paisa": 50000},
                ],
                APRIL,
            )

    def test_off_by_a_single_paisa_is_still_unbalanced(self, accounts, ledger_voucher):
        """Exactly equal means exactly. There is no tolerance."""
        with pytest.raises(UnbalancedEntry, match="1 paisa"):
            post_entries(
                ledger_voucher,
                [
                    {"account": accounts.rent, "debit_paisa": 50000},
                    {"account": accounts.cash, "credit_paisa": 49999},
                ],
                APRIL,
            )

    def test_nothing_at_all_is_written(self, accounts, ledger_voucher):
        with pytest.raises(UnbalancedEntry):
            post_entries(
                ledger_voucher,
                [
                    {"account": accounts.rent, "debit_paisa": 50000},
                    {"account": accounts.cash, "credit_paisa": 49900},
                ],
                APRIL,
            )

        assert LedgerEntry.objects.count() == 0, "a rejected posting leaves no partial rows"


class TestPostingIsRejected:
    def test_against_a_group_account(self, accounts, ledger_voucher):
        with pytest.raises(GroupAccountPosting, match="5000"):
            post_entries(
                ledger_voucher,
                [
                    {"account": accounts.expenses_group, "debit_paisa": 50000},
                    {"account": accounts.cash, "credit_paisa": 50000},
                ],
                APRIL,
            )
        assert LedgerEntry.objects.count() == 0

    def test_against_an_inactive_account(self, accounts, ledger_voucher, simple_lines):
        accounts.cash.is_active = False
        accounts.cash.save()

        with pytest.raises(InactiveAccount, match="1110"):
            post_entries(ledger_voucher, simple_lines, APRIL)

    def test_with_no_lines(self, ledger_voucher):
        with pytest.raises(InvalidPosting, match="no lines"):
            post_entries(ledger_voucher, [], APRIL)

    def test_with_a_zero_line(self, accounts, ledger_voucher):
        with pytest.raises(InvalidPosting, match="moves no money"):
            post_entries(
                ledger_voucher,
                [
                    {"account": accounts.rent, "debit_paisa": 50000},
                    {"account": accounts.cash, "credit_paisa": 50000},
                    {"account": accounts.bank, "debit_paisa": 0},
                ],
                APRIL,
            )

    def test_with_both_sides_on_one_line(self, accounts, ledger_voucher):
        with pytest.raises(InvalidPosting, match="sets both sides"):
            post_entries(
                ledger_voucher,
                [
                    {"account": accounts.rent, "debit_paisa": 50000, "credit_paisa": 50000},
                    {"account": accounts.cash, "credit_paisa": 50000},
                ],
                APRIL,
            )

    def test_with_a_negative_amount(self, accounts, ledger_voucher):
        """A refund is a line on the other side, never a minus."""
        with pytest.raises(InvalidPosting, match="never negative"):
            post_entries(
                ledger_voucher,
                [
                    {"account": accounts.rent, "debit_paisa": -50000},
                    {"account": accounts.cash, "debit_paisa": 50000},
                ],
                APRIL,
            )

    def test_with_a_float_amount(self, accounts, ledger_voucher):
        with pytest.raises(InvalidPosting, match="whole paisa"):
            post_entries(
                ledger_voucher,
                [
                    {"account": accounts.rent, "debit_paisa": 500.0},
                    {"account": accounts.cash, "credit_paisa": 50000},
                ],
                APRIL,
            )

    def test_with_a_money_object_instead_of_paisa(self, accounts, ledger_voucher):
        """The key is named debit_paisa; it holds paisa. The error says so."""
        with pytest.raises(InvalidPosting, match=r"\.paisa"):
            post_entries(
                ledger_voucher,
                [
                    {"account": accounts.rent, "debit_paisa": Money(50000)},
                    {"account": accounts.cash, "credit_paisa": 50000},
                ],
                APRIL,
            )

    def test_with_a_mistyped_key(self, accounts, ledger_voucher):
        """`debit` instead of `debit_paisa` would otherwise post a silent zero."""
        with pytest.raises(InvalidPosting, match="unknown key"):
            post_entries(
                ledger_voucher,
                [
                    {"account": accounts.rent, "debit": 50000},
                    {"account": accounts.cash, "credit_paisa": 50000},
                ],
                APRIL,
            )

    def test_with_a_missing_account(self, accounts, ledger_voucher):
        with pytest.raises(InvalidPosting, match="needs an Account"):
            post_entries(
                ledger_voucher,
                [
                    {"debit_paisa": 50000},
                    {"account": accounts.cash, "credit_paisa": 50000},
                ],
                APRIL,
            )

    def test_with_a_half_set_party(self, accounts, ledger_voucher):
        with pytest.raises(InvalidPosting, match="PartyRef"):
            post_entries(
                ledger_voucher,
                [
                    {"account": accounts.receivable, "debit_paisa": 50000, "party": "CLIENT"},
                    {"account": accounts.sales, "credit_paisa": 50000},
                ],
                APRIL,
            )

    def test_with_an_unknown_party_type(self, accounts, ledger_voucher):
        with pytest.raises(InvalidPosting, match="Unknown party type"):
            post_entries(
                ledger_voucher,
                [
                    {
                        "account": accounts.receivable,
                        "debit_paisa": 50000,
                        "party": ("SUPPLIER", 7),
                    },
                    {"account": accounts.sales, "credit_paisa": 50000},
                ],
                APRIL,
            )

    def test_with_a_datetime_posting_date(self, accounts, ledger_voucher, simple_lines):
        """A timezone conversion can move a datetime to the previous day; which
        day a sale hit the books may not depend on that."""
        from django.utils import timezone

        with pytest.raises(InvalidPosting, match="not a datetime"):
            post_entries(ledger_voucher, simple_lines, timezone.now())

    def test_with_a_string_posting_date(self, accounts, ledger_voucher, simple_lines):
        with pytest.raises(InvalidPosting, match="must be a date"):
            post_entries(ledger_voucher, simple_lines, "2026-04-01")

    def test_against_an_unsaved_voucher(self, accounts, simple_lines):
        with pytest.raises(InvalidPosting, match="no primary key"):
            post_entries(SampleDocument(code="SI-2026-000999"), simple_lines, APRIL)

    def test_against_a_voucher_with_no_code(self, accounts, ledger_voucher, simple_lines):
        ledger_voucher.code = ""
        with pytest.raises(InvalidPosting, match="no code"):
            post_entries(ledger_voucher, simple_lines, APRIL)

    def test_twice_for_the_same_voucher(self, accounts, ledger_voucher, simple_lines):
        post_entries(ledger_voucher, simple_lines, APRIL)

        with pytest.raises(AlreadyPosted, match="already has ledger entries"):
            post_entries(ledger_voucher, simple_lines, APRIL)

        assert LedgerEntry.objects.count() == 2, "the second attempt wrote nothing"

    def test_twice_even_after_a_reversal(self, accounts, ledger_voucher, simple_lines):
        """A cancelled document is never re-posted; it is replaced by an
        amendment, which is a different voucher."""
        post_entries(ledger_voucher, simple_lines, APRIL)
        reverse_entries(ledger_voucher)

        with pytest.raises(AlreadyPosted):
            post_entries(ledger_voucher, simple_lines, APRIL)


# ---------------------------------------------------------------------------
# reverse_entries
# ---------------------------------------------------------------------------
class TestReversal:
    @pytest.fixture
    def posted(self, accounts, ledger_voucher, simple_lines, user):
        post_entries(ledger_voucher, simple_lines, APRIL, user=user)
        return ledger_voucher

    def test_originals_are_not_touched(self, posted):
        """Not updated, not flagged, not re-dated. Byte for byte identical."""
        before = list(LedgerEntry.objects.filter(is_reversal=False).order_by("pk").values())

        reverse_entries(posted)

        after = list(LedgerEntry.objects.filter(is_reversal=False).order_by("pk").values())
        assert before == after

    def test_a_mirror_is_written_for_every_row(self, posted):
        mirrors = reverse_entries(posted)

        assert len(mirrors) == 2
        assert LedgerEntry.objects.count() == 4

    def test_the_sides_are_swapped_never_negated(self, accounts, posted):
        reverse_entries(posted)

        rent = LedgerEntry.objects.get(account=accounts.rent, is_reversal=True)
        cash = LedgerEntry.objects.get(account=accounts.cash, is_reversal=True)

        assert (rent.debit_paisa, rent.credit_paisa) == (0, 50000)
        assert (cash.debit_paisa, cash.credit_paisa) == (50000, 0)
        assert not LedgerEntry.objects.filter(debit_paisa__lt=0).exists()
        assert not LedgerEntry.objects.filter(credit_paisa__lt=0).exists()

    def test_each_mirror_points_at_what_it_reverses(self, posted):
        originals = list(LedgerEntry.objects.filter(is_reversal=False).order_by("pk"))

        reverse_entries(posted)

        for original in originals:
            mirror = LedgerEntry.objects.get(reverses=original)
            assert mirror.is_reversal
            assert mirror.account_id == original.account_id
            assert mirror.debit_paisa == original.credit_paisa
            assert mirror.credit_paisa == original.debit_paisa

    def test_the_voucher_reference_and_party_are_carried_over(self, accounts, ledger_voucher):
        post_entries(
            ledger_voucher,
            [
                {
                    "account": accounts.receivable,
                    "debit_paisa": 50000,
                    "party": PartyRef(PartyType.CLIENT, CLIENT),
                },
                {"account": accounts.sales, "credit_paisa": 50000},
            ],
            APRIL,
        )

        reverse_entries(ledger_voucher)

        mirror = LedgerEntry.objects.get(account=accounts.receivable, is_reversal=True)
        assert mirror.voucher_code == "SI-2026-000001"
        assert mirror.voucher_id == ledger_voucher.pk
        assert (mirror.party_type, mirror.party_id) == (PartyType.CLIENT, CLIENT)

    def test_the_account_nets_to_zero(self, accounts, posted):
        assert account_balance(accounts.rent) == Money(50000)

        reverse_entries(posted)

        assert account_balance(accounts.rent) == Money(0)
        assert account_balance(accounts.cash) == Money(0)

    def test_it_nets_to_zero_on_any_as_of_date(self, accounts, posted):
        """The mirror takes the original's date, so looking back at a moment
        before the cancellation still shows a cancelled document as cancelled —
        which is what you want when the reason was "this never happened"."""
        reverse_entries(posted)

        for as_of in (APRIL, MAY, JUNE):
            assert account_balance(accounts.rent, as_of=as_of) == Money(0)

    def test_the_reversal_date_can_be_pushed_to_a_later_period(self, accounts, posted):
        """When the original period must not be reopened, the earlier balance
        correctly still shows the original amount."""
        reverse_entries(posted, posting_date=JUNE)

        assert account_balance(accounts.rent, as_of=MAY) == Money(50000)
        assert account_balance(accounts.rent, as_of=JUNE) == Money(0)

    def test_the_author_of_the_reversal_is_recorded(self, posted, user):
        reverse_entries(posted, user=user)

        assert all(m.created_by == user for m in LedgerEntry.objects.filter(is_reversal=True))

    def test_default_remarks_name_the_voucher(self, posted):
        reverse_entries(posted)

        assert all(
            m.remarks == "Reversal of SI-2026-000001"
            for m in LedgerEntry.objects.filter(is_reversal=True)
        )

    def test_remarks_can_be_supplied(self, posted):
        reverse_entries(posted, remarks="Cancelled: wrong party")

        assert all(
            m.remarks == "Cancelled: wrong party"
            for m in LedgerEntry.objects.filter(is_reversal=True)
        )

    def test_a_deactivated_account_does_not_trap_the_document(self, accounts, posted):
        """An account deactivated after posting must not make cancellation
        impossible — that would strand the document in POSTED forever."""
        accounts.cash.is_active = False
        accounts.cash.save()

        reverse_entries(posted)

        assert account_balance(accounts.cash) == Money(0)


class TestDoubleReversalIsRefused:
    @pytest.fixture
    def posted(self, accounts, ledger_voucher, simple_lines):
        post_entries(ledger_voucher, simple_lines, APRIL)
        return ledger_voucher

    def test_reversing_twice_raises(self, posted):
        reverse_entries(posted)

        with pytest.raises(AlreadyReversed, match="already been reversed"):
            reverse_entries(posted)

    def test_nothing_is_written_by_the_second_attempt(self, posted):
        reverse_entries(posted)
        with pytest.raises(AlreadyReversed):
            reverse_entries(posted)

        assert LedgerEntry.objects.count() == 4

    def test_the_balance_stays_at_zero_rather_than_bouncing_back(self, accounts, posted):
        """This is the failure the refusal exists to prevent: reversing a
        reversal would restore the original amounts and silently un-cancel."""
        reverse_entries(posted)
        with pytest.raises(AlreadyReversed):
            reverse_entries(posted)

        assert account_balance(accounts.rent) == Money(0)

    def test_reversing_a_voucher_that_was_never_posted_raises(self, ledger_voucher):
        with pytest.raises(AlreadyReversed, match="nothing to reverse"):
            reverse_entries(ledger_voucher)

    def test_a_second_voucher_is_unaffected(self, accounts, posted, simple_lines):
        """The "already reversed" test is per voucher, not global."""
        other = document("SI-2026-000002")
        post_entries(other, simple_lines, APRIL)
        reverse_entries(posted)

        reverse_entries(other)  # must not raise

        assert LedgerEntry.objects.filter(is_reversal=True).count() == 4


# ---------------------------------------------------------------------------
# account_balance
# ---------------------------------------------------------------------------
class TestAccountBalance:
    def test_an_empty_account_is_zero(self, accounts):
        assert account_balance(accounts.cash) == Money(0)

    def test_a_debit_normal_account_is_positive_when_debited(self, accounts, ledger_voucher):
        post_entries(
            ledger_voucher,
            [
                {"account": accounts.cash, "debit_paisa": 50000},
                {"account": accounts.owners_equity, "credit_paisa": 50000},
            ],
            APRIL,
        )

        assert account_balance(accounts.cash) == Money(50000)

    def test_a_credit_normal_account_is_positive_when_credited(self, accounts, ledger_voucher):
        """Rs 500 owed to suppliers reads as +500, not -500."""
        post_entries(
            ledger_voucher,
            [
                {"account": accounts.inventory, "debit_paisa": 50000},
                {"account": accounts.payable, "credit_paisa": 50000},
            ],
            APRIL,
        )

        assert account_balance(accounts.payable) == Money(50000)
        assert account_balance(accounts.inventory) == Money(50000)

    def test_income_is_positive_when_earned(self, accounts, ledger_voucher):
        post_sale(ledger_voucher, accounts, client_id=CLIENT, total_paisa=100000)

        assert account_balance(accounts.sales) == Money(100000)

    def test_a_contra_account_reports_negative(self, accounts, ledger_voucher):
        """Sales Returns sits under Income but is written up with debits, so it
        nets against its siblings when the group is totalled."""
        post_entries(
            ledger_voucher,
            [
                {"account": accounts.sales_returns, "debit_paisa": 5000},
                {"account": accounts.receivable, "credit_paisa": 5000},
            ],
            APRIL,
        )

        assert account_balance(accounts.sales_returns) == Money(-5000)

    def test_debits_and_credits_on_one_account_net(self, accounts, ledger_voucher):
        post_sale(ledger_voucher, accounts, client_id=CLIENT, total_paisa=100000)
        post_receipt(document("RC-2026-000001"), accounts, client_id=CLIENT, amount_paisa=40000)

        assert account_balance(accounts.receivable) == Money(60000)

    def test_as_of_is_inclusive_and_ignores_the_future(self, accounts):
        post_sale(
            document("SI-2026-000010"), accounts, client_id=CLIENT, total_paisa=100000, on=APRIL
        )
        post_sale(
            document("SI-2026-000011"), accounts, client_id=CLIENT, total_paisa=30000, on=JUNE
        )

        assert account_balance(accounts.sales, as_of=dt.date(2026, 3, 31)) == Money(0)
        assert account_balance(accounts.sales, as_of=APRIL) == Money(100000)
        assert account_balance(accounts.sales, as_of=MAY) == Money(100000)
        assert account_balance(accounts.sales, as_of=JUNE) == Money(130000)
        assert account_balance(accounts.sales) == Money(130000)

    def test_a_datetime_as_of_is_refused(self, accounts):
        from django.utils import timezone

        with pytest.raises(InvalidPosting, match="not a datetime"):
            account_balance(accounts.sales, as_of=timezone.now())

    def test_a_group_totals_its_children(self, accounts, ledger_voucher):
        post_entries(
            ledger_voucher,
            [
                {"account": accounts.cogs, "debit_paisa": 60000},
                {"account": accounts.inventory, "credit_paisa": 60000},
            ],
            APRIL,
        )
        post_entries(
            document("PV-2026-000001"),
            [
                {"account": accounts.rent, "debit_paisa": 20000},
                {"account": accounts.cash, "credit_paisa": 20000},
            ],
            APRIL,
        )

        # Rent is two levels down: Expenses -> Operating Expenses -> Rent.
        assert account_balance(accounts.operating_expenses_group) == Money(20000)
        assert account_balance(accounts.expenses_group) == Money(80000)

    def test_a_group_with_no_entries_below_it_is_zero_not_wrong(self, accounts):
        assert account_balance(accounts.expenses_group) == Money(0)

    def test_a_contra_child_nets_against_its_siblings_in_the_group(self, accounts, ledger_voucher):
        post_sale(ledger_voucher, accounts, client_id=CLIENT, total_paisa=100000)
        post_entries(
            document("SR-2026-000001"),
            [
                {"account": accounts.sales_returns, "debit_paisa": 5000},
                {"account": accounts.receivable, "credit_paisa": 5000},
            ],
            APRIL,
        )

        assert account_balance(accounts.income_group) == Money(95000)

    def test_the_whole_ledger_always_balances(self, accounts, ledger_voucher):
        """The invariant behind every trial balance: across all accounts, the
        debits and the credits are equal."""
        post_sale(ledger_voucher, accounts, client_id=CLIENT, total_paisa=118000, tax_paisa=18000)
        post_receipt(document("RC-2026-000002"), accounts, client_id=CLIENT, amount_paisa=40000)
        reverse_entries(ledger_voucher)

        rows = LedgerEntry.objects.all()
        assert sum(r.debit_paisa for r in rows) == sum(r.credit_paisa for r in rows)


# ---------------------------------------------------------------------------
# party_balance
# ---------------------------------------------------------------------------
class TestPartyBalance:
    def test_a_party_with_no_entries_is_zero(self, accounts):
        assert party_balance(PartyType.CLIENT, CLIENT) == Money(0)

    def test_a_client_who_owes_us_is_positive(self, accounts, ledger_voucher):
        post_sale(ledger_voucher, accounts, client_id=CLIENT, total_paisa=118000, tax_paisa=18000)

        assert party_balance(PartyType.CLIENT, CLIENT) == Money(118000)

    def test_a_vendor_we_owe_is_positive(self, accounts, ledger_voucher):
        post_entries(
            ledger_voucher,
            [
                {"account": accounts.inventory, "debit_paisa": 75000},
                {
                    "account": accounts.payable,
                    "credit_paisa": 75000,
                    "party": PartyRef(PartyType.VENDOR, VENDOR),
                },
            ],
            APRIL,
        )

        assert party_balance(PartyType.VENDOR, VENDOR) == Money(75000)

    def test_parties_do_not_bleed_into_each_other(self, accounts, ledger_voucher):
        post_sale(ledger_voucher, accounts, client_id=CLIENT, total_paisa=100000)

        assert party_balance(PartyType.CLIENT, OTHER_CLIENT) == Money(0)
        assert party_balance(PartyType.VENDOR, CLIENT) == Money(0), "same id, different type"

    def test_a_receipt_reduces_it(self, accounts, ledger_voucher):
        post_sale(ledger_voucher, accounts, client_id=CLIENT, total_paisa=100000)
        post_receipt(document("RC-2026-000001"), accounts, client_id=CLIENT, amount_paisa=40000)

        assert party_balance(PartyType.CLIENT, CLIENT) == Money(60000)

    def test_as_of_ignores_the_future(self, accounts):
        post_sale(
            document("SI-2026-000010"), accounts, client_id=CLIENT, total_paisa=100000, on=APRIL
        )
        post_sale(
            document("SI-2026-000011"), accounts, client_id=CLIENT, total_paisa=30000, on=JUNE
        )

        assert party_balance(PartyType.CLIENT, CLIENT, as_of=MAY) == Money(100000)
        assert party_balance(PartyType.CLIENT, CLIENT, as_of=JUNE) == Money(130000)

    def test_an_unknown_party_type_raises_rather_than_returning_zero(self, accounts):
        with pytest.raises(InvalidPosting, match="Unknown party type"):
            party_balance("SUPPLIER", CLIENT)

    def test_across_posted_cancelled_and_amended_documents(self, accounts, user):
        """The whole point of reversal-instead-of-deletion, in one test.

        A cancelled invoice keeps its rows *and* their mirrors, which net to
        zero, so the party balance is right without anything downstream having
        to know a cancellation ever happened. An amendment is a separate
        document with its own rows, so it simply adds.
        """
        original = document("SI-2026-000123")
        post_sale(
            original, accounts, client_id=CLIENT, total_paisa=118000, tax_paisa=18000, user=user
        )
        assert party_balance(PartyType.CLIENT, CLIENT) == Money(118000)

        # Cancelled: reversed out, back to nothing owed.
        cancel(original, user=user)
        assert party_balance(PartyType.CLIENT, CLIENT) == Money(0)
        assert LedgerEntry.objects.filter(voucher_id=original.pk).count() == 6, (
            "three rows and their three mirrors — nothing was deleted"
        )

        # Amended: a new document, correcting the amount downwards.
        amended = original.amend(user=user)
        assert amended.code == "SI-2026-000123-1"
        post_sale(amended, accounts, client_id=CLIENT, total_paisa=90000, user=user)
        assert party_balance(PartyType.CLIENT, CLIENT) == Money(90000)

        # A second, ordinary invoice.
        post_sale(
            document("SI-2026-000124"), accounts, client_id=CLIENT, total_paisa=50000, user=user
        )
        assert party_balance(PartyType.CLIENT, CLIENT) == Money(140000)

        # A payment against the account.
        post_receipt(
            document("RC-2026-000001"),
            accounts,
            client_id=CLIENT,
            amount_paisa=40000,
            user=user,
        )
        assert party_balance(PartyType.CLIENT, CLIENT) == Money(100000)

        # The party ledger and the control account agree, because both are the
        # same rows aggregated two ways.
        assert account_balance(accounts.receivable) == Money(100000)
        assert party_balance(PartyType.CLIENT, OTHER_CLIENT) == Money(0)

    def test_the_amended_chain_leaves_the_cancelled_rows_readable(self, accounts, user):
        """History is not rewritten: the cancelled document's rows are still
        there, still say what they said, and are still findable by its code."""
        original = document("SI-2026-000200")
        post_sale(original, accounts, client_id=CLIENT, total_paisa=100000, user=user)
        cancel(original, user=user)
        amended = original.amend(user=user)
        post_sale(amended, accounts, client_id=CLIENT, total_paisa=80000, user=user)

        cancelled_rows = LedgerEntry.objects.filter(voucher_code="SI-2026-000200")
        assert cancelled_rows.filter(is_reversal=False).count() == 2
        assert cancelled_rows.filter(is_reversal=True).count() == 2
        assert LedgerEntry.objects.filter(voucher_code="SI-2026-000200-1").count() == 2
        assert party_balance(PartyType.CLIENT, CLIENT) == Money(80000)
