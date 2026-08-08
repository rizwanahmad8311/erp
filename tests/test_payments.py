"""Receipts, payments, allocation and the cheque lifecycle.

The properties this file exists to pin, in the order they matter:

* **A payment moves money and nothing else.** Two ledger rows, party-tagged, and
  the client's balance comes down by exactly what was taken.
* **Allocation is bookkeeping about documents, not about money.** A partial
  allocation leaves the rest **on account**, which is a normal state and must be
  visible; allocating more than there is — of the payment, or of the bill — is
  refused with the numbers in the message.
* **A cheque is not money until the bank says so.** It sits in Cheques in Hand,
  clearing is a separate posting on a later date, and a bounce writes the whole
  thing back and takes the invoices with it.

Every balance assertion reads the ledger through ``apps.accounting.services``
rather than a header field — a test that asserted ``payment.amount_paisa`` would
pass just as happily if nothing had been posted at all.
"""

import datetime as dt

import pytest

from apps.accounting import chart as coa
from apps.accounting.enums import PartyType
from apps.accounting.exceptions import AlreadyPosted
from apps.accounting.models import LedgerEntry
from apps.accounting.services import account_balance, party_balance, post_entries
from apps.core.enums import DocumentStatus
from apps.core.exceptions import DocumentImmutable, IllegalTransition
from apps.core.money import to_paisa
from apps.masters.enums import Unit
from apps.masters.models import Client, Item, Route, Seller, Vendor
from apps.payments import services
from apps.payments.enums import ChequeEventKind, ChequeStatus, PaymentDirection, PaymentMode
from apps.payments.exceptions import ChequeStateError, InvalidPayment, NotAllocatable, OverAllocated
from apps.payments.models import ChequeEvent, Payment, PaymentAllocation
from apps.purchasing import services as purchasing
from apps.purchasing.exceptions import PaymentAllocated
from apps.sales import services as sales
from apps.sales.models import SalesInvoiceLine, SalesReturnLine

pytestmark = pytest.mark.django_db

APRIL = dt.date(2026, 4, 1)
MAY = dt.date(2026, 5, 1)
JUNE = dt.date(2026, 6, 1)

#: Generous enough that the credit limit is never the thing under test here.
BIG_LIMIT = to_paisa("10000000")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def route(db):
    return Route.objects.create(code="R-01", name="Saddar & City")


@pytest.fixture
def seller(db):
    return Seller.objects.create(code="S-01", name="Imran Qureshi")


@pytest.fixture
def shop(db, route, seller):
    return Client.objects.create(
        code="C-0001",
        name="Al-Madina Kiryana",
        phone="0300-2214477",
        route=route,
        seller=seller,
        credit_limit_paisa=BIG_LIMIT,
        credit_days=15,
    )


@pytest.fixture
def other_shop(db, route):
    return Client.objects.create(
        code="C-0002", name="Madina Store", route=route, credit_limit_paisa=BIG_LIMIT
    )


@pytest.fixture
def oil(db):
    """Twelve to a carton, no tax — the arithmetic here is about money moving."""
    return Item.objects.create(code="OIL-1000", name="Cooking Oil 1L", carton_size=12)


@pytest.fixture
def stocked(db, accounts, warehouses, oil, user):
    """1,200 pieces of oil on hand, bought through a real purchase invoice."""
    vendor = Vendor.objects.create(code="V-01", name="Dalda Foods")
    bill = purchasing.create_purchase_invoice(
        vendor=vendor, warehouse=warehouses.main, posting_date=APRIL
    )
    line = purchasing.update_line(
        purchasing.PurchaseInvoiceLine(document=bill),
        item=oil,
        qty_input=100,
        unit_input=Unit.CARTON,
        rate_input_paisa=to_paisa("2400"),
    )
    line.save()
    purchasing.post_purchase_invoice(bill, user=user)
    return bill


def make_invoice(shop, warehouse, oil, user, *, posting_date=APRIL, cartons=10, rupees="2500"):
    """A posted sales invoice, the thing money gets allocated against."""
    invoice = sales.create_sales_invoice(
        client=shop, warehouse=warehouse, posting_date=posting_date
    )
    line = sales.update_line(
        SalesInvoiceLine(document=invoice),
        item=oil,
        qty_input=cartons,
        unit_input=Unit.CARTON,
        rate_input_paisa=to_paisa(rupees),
    )
    line.save()
    sales.post_sales_invoice(invoice, user=user)
    return invoice


@pytest.fixture
def invoice(db, stocked, shop, warehouses, oil, user):
    """Rs 25,000 owed by the shop."""
    return make_invoice(shop, warehouses.main, oil, user)


def receipt(shop, *, rupees, posting_date=APRIL, mode=PaymentMode.CASH, user=None, **fields):
    """Create and post a receipt in one step."""
    payment = services.create_payment(
        party=shop,
        direction=PaymentDirection.RECEIVE,
        mode=mode,
        posting_date=posting_date,
        amount_paisa=to_paisa(rupees),
        **fields,
    )
    return services.post_payment(payment, user=user)


def cheque(shop, *, rupees, posting_date=APRIL, cheque_date=MAY, user=None, **fields):
    return receipt(
        shop,
        rupees=rupees,
        posting_date=posting_date,
        mode=PaymentMode.CHEQUE,
        user=user,
        cheque_no="0091823",
        cheque_date=cheque_date,
        bank_name="Meezan Bank",
        **fields,
    )


# ===========================================================================
# The posting
# ===========================================================================
class TestPosting:
    def test_a_cash_receipt_debits_cash_and_credits_the_client(self, accounts, shop, invoice, user):
        payment = receipt(shop, rupees="10000", user=user)

        rows = LedgerEntry.objects.filter(voucher_type="Payment", voucher_id=payment.pk)
        assert rows.count() == 2

        debit = rows.get(debit_paisa__gt=0)
        credit = rows.get(credit_paisa__gt=0)
        assert debit.account.code == coa.CASH
        assert debit.debit_paisa == to_paisa("10000")
        assert credit.account.code == coa.ACCOUNTS_RECEIVABLE
        assert credit.credit_paisa == to_paisa("10000")

    def test_only_the_receivable_row_carries_the_party(self, shop, invoice, user):
        """Tagging the cash row too would double every party balance."""
        payment = receipt(shop, rupees="10000", user=user)
        rows = LedgerEntry.objects.filter(voucher_type="Payment", voucher_id=payment.pk)

        tagged = [row for row in rows if row.party_type is not None]
        assert len(tagged) == 1
        assert tagged[0].account.code == coa.ACCOUNTS_RECEIVABLE
        assert tagged[0].party_type == PartyType.CLIENT
        assert tagged[0].party_id == shop.pk

    def test_the_clients_balance_comes_down_by_what_was_taken(self, shop, invoice, user):
        before = party_balance(PartyType.CLIENT, shop.pk).paisa
        assert before == to_paisa("25000")

        receipt(shop, rupees="10000", user=user)

        assert party_balance(PartyType.CLIENT, shop.pk).paisa == to_paisa("15000")

    def test_a_bank_receipt_lands_in_the_bank(self, accounts, shop, invoice, user):
        receipt(shop, rupees="4000", mode=PaymentMode.BANK, user=user)
        assert account_balance(accounts.bank).paisa == to_paisa("4000")
        assert account_balance(accounts.cash).paisa == 0

    def test_paying_a_vendor_debits_the_payable(self, accounts, db, user):
        vendor = Vendor.objects.create(code="V-99", name="Shan Foods")
        payment = services.create_payment(
            party=vendor,
            direction=PaymentDirection.PAY,
            mode=PaymentMode.BANK,
            posting_date=APRIL,
            amount_paisa=to_paisa("7500"),
        )
        services.post_payment(payment, user=user)

        rows = LedgerEntry.objects.filter(voucher_type="Payment", voucher_id=payment.pk)
        assert rows.get(debit_paisa__gt=0).account.code == coa.ACCOUNTS_PAYABLE
        assert rows.get(credit_paisa__gt=0).account.code == coa.BANK
        # A vendor we have paid but do not owe is in debit: we are ahead of them.
        assert party_balance(PartyType.VENDOR, vendor.pk).paisa == to_paisa("-7500")

    def test_the_code_says_which_way_the_money_went(self, shop, db):
        received = services.create_payment(
            party=shop,
            direction=PaymentDirection.RECEIVE,
            mode=PaymentMode.CASH,
            posting_date=APRIL,
            amount_paisa=to_paisa("100"),
        )
        vendor = Vendor.objects.create(code="V-77", name="Someone")
        paid = services.create_payment(
            party=vendor,
            direction=PaymentDirection.PAY,
            mode=PaymentMode.CASH,
            posting_date=APRIL,
            amount_paisa=to_paisa("100"),
        )
        assert received.code.startswith("RV-2026-")
        assert paid.code.startswith("PV-2026-")

    def test_the_route_and_the_collector_default_from_the_client(self, shop, route, seller, db):
        payment = services.create_payment(
            party=shop,
            direction=PaymentDirection.RECEIVE,
            mode=PaymentMode.CASH,
            posting_date=APRIL,
            amount_paisa=to_paisa("500"),
        )
        assert payment.route_id == route.pk
        assert payment.collected_by_id == seller.pk

    def test_an_override_of_the_collector_survives(self, shop, db):
        covering = Seller.objects.create(code="S-02", name="Bilal")
        payment = services.create_payment(
            party=shop,
            direction=PaymentDirection.RECEIVE,
            mode=PaymentMode.CASH,
            posting_date=APRIL,
            amount_paisa=to_paisa("500"),
            collected_by=covering,
        )
        assert payment.collected_by_id == covering.pk

    def test_posting_twice_is_refused(self, shop, invoice, user):
        payment = receipt(shop, rupees="1000", user=user)
        with pytest.raises(IllegalTransition):
            services.post_payment(payment, user=user)

    def test_a_second_posting_through_the_ledger_is_refused(self, accounts, shop, invoice, user):
        """Belt and braces: even bypassing the status guard, the ledger refuses.

        The status guard is the readable error an operator sees. This is the one
        underneath it, and it is the one that matters — double-posting a receipt
        is silent, and is found weeks later by a bank reconciliation.
        """
        payment = receipt(shop, rupees="1000", user=user)
        with pytest.raises(AlreadyPosted):
            post_entries(
                payment,
                [
                    {"account": accounts.cash, "debit_paisa": to_paisa("1000")},
                    {"account": accounts.receivable, "credit_paisa": to_paisa("1000")},
                ],
                APRIL,
            )

    def test_a_posted_payment_cannot_be_edited(self, shop, invoice, user):
        payment = receipt(shop, rupees="1000", user=user)
        payment.amount_paisa = to_paisa("9999")
        with pytest.raises(DocumentImmutable):
            payment.save()

    def test_a_zero_payment_is_refused(self, shop, db):
        with pytest.raises(InvalidPayment):
            services.create_payment(
                party=shop,
                direction=PaymentDirection.RECEIVE,
                mode=PaymentMode.CASH,
                posting_date=APRIL,
                amount_paisa=0,
            )

    def test_cash_cannot_carry_a_cheque_number(self, shop, db):
        with pytest.raises(InvalidPayment):
            services.create_payment(
                party=shop,
                direction=PaymentDirection.RECEIVE,
                mode=PaymentMode.CASH,
                posting_date=APRIL,
                amount_paisa=to_paisa("100"),
                cheque_no="0091823",
            )

    def test_a_cheque_needs_a_number_and_a_date(self, shop, db):
        with pytest.raises(InvalidPayment):
            services.create_payment(
                party=shop,
                direction=PaymentDirection.RECEIVE,
                mode=PaymentMode.CHEQUE,
                posting_date=APRIL,
                amount_paisa=to_paisa("100"),
            )


class TestCancellation:
    def test_cancelling_reverses_both_rows_and_touches_neither(self, shop, invoice, user):
        payment = receipt(shop, rupees="10000", user=user)
        originals = list(
            LedgerEntry.objects.filter(voucher_type="Payment", voucher_id=payment.pk).order_by("pk")
        )

        services.cancel_payment(payment, user=user, reason="Wrong shop")

        payment.refresh_from_db()
        assert payment.status == DocumentStatus.CANCELLED
        rows = LedgerEntry.objects.filter(voucher_type="Payment", voucher_id=payment.pk)
        assert rows.count() == 4
        assert rows.filter(is_reversal=True).count() == 2
        # The originals are untouched, exactly as written.
        for original in originals:
            fresh = LedgerEntry.objects.get(pk=original.pk)
            assert (fresh.debit_paisa, fresh.credit_paisa) == (
                original.debit_paisa,
                original.credit_paisa,
            )
        # And the client owes the whole invoice again.
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == to_paisa("25000")

    def test_a_cancelled_payment_settles_nothing(self, shop, invoice, user):
        payment = receipt(shop, rupees="10000", user=user)
        services.allocate_payment(payment, [(invoice, to_paisa("10000"))])
        assert invoice.paid_paisa == to_paisa("10000")

        services.cancel_payment(payment, user=user, reason="Wrong shop")

        # The rows are still there as a record; they just stop counting.
        assert PaymentAllocation.objects.filter(payment=payment).count() == 1
        assert invoice.paid_paisa == 0

    def test_an_amendment_carries_the_allocations(self, shop, invoice, user):
        payment = receipt(shop, rupees="10000", user=user)
        services.allocate_payment(payment, [(invoice, to_paisa("10000"))])
        services.cancel_payment(payment, user=user, reason="Wrong date")

        amendment = services.amend_payment(payment, user=user)

        assert amendment.status == DocumentStatus.DRAFT
        assert amendment.code == f"{payment.code}-1"
        assert amendment.allocations.count() == 1
        assert amendment.allocated_paisa == to_paisa("10000")

    def test_an_amendment_that_no_longer_covers_its_allocations_will_not_post(
        self, shop, invoice, user
    ):
        payment = receipt(shop, rupees="10000", user=user)
        services.allocate_payment(payment, [(invoice, to_paisa("10000"))])
        services.cancel_payment(payment, user=user, reason="Amount was wrong")

        amendment = services.amend_payment(payment, user=user)
        amendment.amount_paisa = to_paisa("6000")
        amendment.save()

        with pytest.raises(OverAllocated) as exc:
            services.post_payment(amendment, user=user)
        assert "4,000.00" in str(exc.value)


# ===========================================================================
# Allocation
# ===========================================================================
class TestAllocation:
    def test_a_partial_allocation_leaves_the_invoice_part_paid(self, shop, invoice, user):
        payment = receipt(shop, rupees="10000", user=user)
        services.allocate_payment(payment, [(invoice, to_paisa("10000"))])

        assert invoice.paid_paisa == to_paisa("10000")
        assert invoice.outstanding_paisa == to_paisa("15000")
        assert not invoice.is_paid

    def test_a_payment_split_across_two_invoices(self, shop, warehouses, oil, stocked, user):
        first = make_invoice(shop, warehouses.main, oil, user, cartons=4, rupees="2500")
        second = make_invoice(shop, warehouses.main, oil, user, cartons=6, rupees="2500")
        payment = receipt(shop, rupees="20000", user=user)

        services.allocate_payment(
            payment, [(first, to_paisa("10000")), (second, to_paisa("10000"))]
        )

        assert first.outstanding_paisa == 0
        assert second.outstanding_paisa == to_paisa("5000")
        assert payment.unallocated_paisa == 0

    def test_the_remainder_stays_on_account_and_is_visible(self, shop, invoice, user):
        payment = receipt(shop, rupees="20000", user=user)
        services.allocate_payment(payment, [(invoice, to_paisa("5000"))])

        assert payment.allocated_paisa == to_paisa("5000")
        assert payment.unallocated_paisa == to_paisa("15000")
        assert not payment.is_fully_allocated
        # The ledger already knows: the client owes 25,000 less 20,000.
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == to_paisa("5000")

    def test_allocating_nothing_at_all_is_a_whole_payment_on_account(self, shop, invoice, user):
        payment = receipt(shop, rupees="20000", user=user)
        assert payment.allocated_paisa == 0
        assert payment.unallocated_paisa == to_paisa("20000")

    def test_more_than_the_payment_is_refused(self, shop, invoice, user):
        payment = receipt(shop, rupees="10000", user=user)
        with pytest.raises(OverAllocated) as exc:
            services.allocate_payment(payment, [(invoice, to_paisa("12000"))])

        assert exc.value.limit_paisa == to_paisa("10000")
        assert exc.value.requested_paisa == to_paisa("12000")
        assert exc.value.excess_paisa == to_paisa("2000")
        assert PaymentAllocation.objects.count() == 0

    def test_more_than_the_invoice_is_refused(self, shop, invoice, user):
        payment = receipt(shop, rupees="40000", user=user)
        with pytest.raises(OverAllocated) as exc:
            services.allocate_payment(payment, [(invoice, to_paisa("30000"))])

        assert exc.value.limit_paisa == to_paisa("25000")
        assert PaymentAllocation.objects.count() == 0

    def test_two_payments_cannot_between_them_overpay_one_invoice(self, shop, invoice, user):
        first = receipt(shop, rupees="20000", user=user)
        services.allocate_payment(first, [(invoice, to_paisa("20000"))])

        second = receipt(shop, rupees="20000", posting_date=MAY, user=user)
        with pytest.raises(OverAllocated) as exc:
            services.allocate_payment(second, [(invoice, to_paisa("10000"))])

        assert exc.value.limit_paisa == to_paisa("5000")
        assert "20,000.00 is already allocated" in str(exc.value)

    def test_re_allocating_the_same_payment_to_the_same_invoice_is_not_double_counted(
        self, shop, invoice, user
    ):
        payment = receipt(shop, rupees="20000", user=user)
        services.allocate_payment(payment, [(invoice, to_paisa("15000"))])
        services.allocate_payment(payment, [(invoice, to_paisa("20000"))])

        assert payment.allocations.count() == 1
        assert invoice.paid_paisa == to_paisa("20000")

    def test_replacing_drops_the_rows_that_were_left_out(
        self, shop, warehouses, oil, stocked, user
    ):
        first = make_invoice(shop, warehouses.main, oil, user, cartons=4, rupees="2500")
        second = make_invoice(shop, warehouses.main, oil, user, cartons=4, rupees="2500")
        payment = receipt(shop, rupees="10000", user=user)

        services.allocate_payment(payment, [(first, to_paisa("5000")), (second, to_paisa("5000"))])
        assert payment.allocations.count() == 2

        services.allocate_payment(payment, [(second, to_paisa("10000"))])

        assert payment.allocations.count() == 1
        assert first.paid_paisa == 0
        assert second.paid_paisa == to_paisa("10000")

    def test_a_zero_row_is_an_absence_not_an_allocation(self, shop, invoice, user):
        payment = receipt(shop, rupees="10000", user=user)
        services.allocate_payment(payment, [(invoice, 0)])
        assert payment.allocations.count() == 0

    def test_another_shops_invoice_is_refused(self, shop, other_shop, invoice, user):
        payment = receipt(other_shop, rupees="10000", user=user)
        with pytest.raises(NotAllocatable) as exc:
            services.allocate_payment(payment, [(invoice, to_paisa("5000"))])
        assert "Al-Madina Kiryana" in str(exc.value)

    def test_a_draft_invoice_is_refused(self, shop, warehouses, oil, stocked, user):
        draft = sales.create_sales_invoice(
            client=shop, warehouse=warehouses.main, posting_date=APRIL
        )
        payment = receipt(shop, rupees="10000", user=user)
        with pytest.raises(NotAllocatable):
            services.allocate_payment(payment, [(draft, to_paisa("1000"))])

    def test_money_coming_in_cannot_settle_a_credit_note(
        self, shop, invoice, warehouses, oil, user
    ):
        note = sales.create_sales_return(
            client=shop, warehouse=warehouses.main, posting_date=MAY, against_invoice=invoice
        )
        line = sales.update_line(
            SalesReturnLine(document=note),
            item=oil,
            qty_input=1,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("2500"),
        )
        line.save()
        sales.post_sales_return(note, user=user)

        payment = receipt(shop, rupees="1000", posting_date=JUNE, user=user)
        with pytest.raises(NotAllocatable) as exc:
            services.allocate_payment(payment, [(note, to_paisa("1000"))])
        assert "not a bill" in str(exc.value)

    def test_unallocate_puts_it_all_back_on_account(self, shop, invoice, user):
        payment = receipt(shop, rupees="10000", user=user)
        services.allocate_payment(payment, [(invoice, to_paisa("10000"))])
        services.unallocate(payment)

        assert payment.allocated_paisa == 0
        assert invoice.paid_paisa == 0

    def test_auto_allocate_takes_the_oldest_bill_first(self, shop, warehouses, oil, stocked, user):
        old = make_invoice(shop, warehouses.main, oil, user, posting_date=APRIL, cartons=4)
        new = make_invoice(shop, warehouses.main, oil, user, posting_date=MAY, cartons=4)
        payment = receipt(shop, rupees="12000", posting_date=JUNE, user=user)

        services.auto_allocate(payment, user=user)

        assert old.outstanding_paisa == 0
        assert new.outstanding_paisa == to_paisa("8000")
        assert payment.unallocated_paisa == 0

    def test_auto_allocate_leaves_the_surplus_on_account(self, shop, invoice, user):
        payment = receipt(shop, rupees="40000", posting_date=MAY, user=user)
        services.auto_allocate(payment, user=user)

        assert invoice.outstanding_paisa == 0
        assert payment.unallocated_paisa == to_paisa("15000")

    def test_an_allocated_invoice_cannot_be_cancelled(self, shop, invoice, user):
        """The seam apps.purchasing.services.assert_not_paid has been waiting for."""
        payment = receipt(shop, rupees="10000", user=user)
        services.allocate_payment(payment, [(invoice, to_paisa("10000"))])

        with pytest.raises(PaymentAllocated) as exc:
            sales.cancel_sales_invoice(invoice, user=user, reason="Typo")
        assert payment.code in str(exc.value)

        invoice.refresh_from_db()
        assert invoice.status == DocumentStatus.POSTED


# ===========================================================================
# Cheques
# ===========================================================================
class TestCheques:
    def test_a_cheque_posts_to_cheques_in_hand_not_to_the_bank(self, accounts, shop, invoice, user):
        payment = cheque(shop, rupees="25000", user=user)

        rows = LedgerEntry.objects.filter(voucher_type="Payment", voucher_id=payment.pk)
        assert rows.get(debit_paisa__gt=0).account.code == coa.CHEQUES_IN_HAND
        assert account_balance(accounts.by_code[coa.CHEQUES_IN_HAND]).paisa == to_paisa("25000")
        assert account_balance(accounts.bank).paisa == 0
        # The shop's account is settled the moment the cheque is taken.
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == 0

    def test_a_fresh_cheque_is_pending(self, shop, invoice, user):
        payment = cheque(shop, rupees="25000", user=user)
        assert payment.cheque_status == ChequeStatus.PENDING
        assert payment.is_pending_cheque

    def test_clearing_moves_it_from_the_drawer_to_the_bank(self, accounts, shop, invoice, user):
        payment = cheque(shop, rupees="25000", user=user)
        event = services.clear_cheque(payment, posting_date=MAY, user=user)

        assert event.code.startswith("CHQ-2026-")
        assert event.status == DocumentStatus.POSTED
        assert account_balance(accounts.bank).paisa == to_paisa("25000")
        assert account_balance(accounts.by_code[coa.CHEQUES_IN_HAND]).paisa == 0
        # And the client's balance is untouched — it was settled when the cheque
        # was taken, not when it cleared.
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == 0

        payment.refresh_from_db()
        assert payment.cheque_status == ChequeStatus.CLEARED

    def test_clearing_defaults_to_the_date_on_the_cheque(self, shop, invoice, user):
        payment = cheque(shop, rupees="25000", cheque_date=MAY, user=user)
        event = services.clear_cheque(payment, user=user)
        assert event.posting_date == MAY

    def test_clearing_twice_is_refused(self, shop, invoice, user):
        payment = cheque(shop, rupees="25000", user=user)
        services.clear_cheque(payment, posting_date=MAY, user=user)
        with pytest.raises(ChequeStateError):
            services.clear_cheque(payment, posting_date=MAY, user=user)

    def test_bouncing_a_cleared_cheque_is_refused(self, shop, invoice, user):
        payment = cheque(shop, rupees="25000", user=user)
        services.clear_cheque(payment, posting_date=MAY, user=user)
        with pytest.raises(ChequeStateError):
            services.bounce_cheque(payment, posting_date=JUNE, user=user)

    def test_clearing_cash_is_refused(self, shop, invoice, user):
        payment = receipt(shop, rupees="1000", user=user)
        with pytest.raises(ChequeStateError) as exc:
            services.clear_cheque(payment, user=user)
        assert "nothing to clear" in str(exc.value)

    def test_a_cheque_cannot_clear_before_it_was_taken(self, shop, invoice, user):
        payment = cheque(shop, rupees="25000", posting_date=MAY, cheque_date=MAY, user=user)
        event = services.create_cheque_event(
            payment, kind=ChequeEventKind.CLEARED, posting_date=APRIL, user=user
        )
        with pytest.raises(ChequeStateError) as exc:
            services.post_cheque_event(event, user=user)
        assert "cannot clear before it is taken" in str(exc.value)

    def test_a_settled_cheque_payment_cannot_be_cancelled(self, shop, invoice, user):
        payment = cheque(shop, rupees="25000", user=user)
        event = services.clear_cheque(payment, posting_date=MAY, user=user)

        with pytest.raises(ChequeStateError) as exc:
            services.cancel_payment(payment, user=user, reason="Oops")
        assert event.code in str(exc.value)


class TestBounce:
    def test_a_bounce_writes_the_posting_back(self, accounts, shop, invoice, user):
        payment = cheque(shop, rupees="25000", user=user)
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == 0

        event = services.bounce_cheque(payment, posting_date=JUNE, user=user)

        rows = LedgerEntry.objects.filter(voucher_type="ChequeEvent", voucher_id=event.pk)
        assert rows.get(debit_paisa__gt=0).account.code == coa.ACCOUNTS_RECEIVABLE
        assert rows.get(credit_paisa__gt=0).account.code == coa.CHEQUES_IN_HAND

        # The drawer is empty and the shop owes the whole invoice again.
        assert account_balance(accounts.by_code[coa.CHEQUES_IN_HAND]).paisa == 0
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == to_paisa("25000")

    def test_the_bounce_carries_the_party_tag(self, shop, invoice, user):
        """Without it the ledger and the shop's statement would disagree forever."""
        payment = cheque(shop, rupees="25000", user=user)
        event = services.bounce_cheque(payment, posting_date=JUNE, user=user)

        tagged = LedgerEntry.objects.filter(
            voucher_type="ChequeEvent", voucher_id=event.pk, party_type=PartyType.CLIENT
        )
        assert tagged.count() == 1
        assert tagged.first().party_id == shop.pk

    def test_a_bounce_reopens_every_invoice_the_cheque_was_paying(self, shop, invoice, user):
        payment = cheque(shop, rupees="25000", user=user)
        services.allocate_payment(payment, [(invoice, to_paisa("25000"))])
        assert invoice.outstanding_paisa == 0

        services.bounce_cheque(payment, posting_date=JUNE, user=user)

        # Nothing was unallocated by hand; the allocation simply stopped counting.
        assert PaymentAllocation.objects.filter(payment=payment).count() == 1
        assert invoice.paid_paisa == 0
        assert invoice.outstanding_paisa == to_paisa("25000")

    def test_the_payment_itself_is_not_touched(self, shop, invoice, user):
        """It is a true record that a cheque was taken on a day (CLAUDE.md §5)."""
        payment = cheque(shop, rupees="25000", user=user)
        services.bounce_cheque(payment, posting_date=JUNE, user=user)

        payment.refresh_from_db()
        assert payment.status == DocumentStatus.POSTED
        assert payment.amount_paisa == to_paisa("25000")
        assert payment.cheque_status == ChequeStatus.BOUNCED
        assert payment.is_bounced
        assert not payment.is_live

    def test_a_bounced_payment_is_off_every_live_query(self, shop, invoice, user):
        payment = cheque(shop, rupees="25000", user=user)
        services.bounce_cheque(payment, posting_date=JUNE, user=user)

        assert Payment.objects.live().filter(pk=payment.pk).count() == 0
        assert Payment.objects.filter(pk=payment.pk).count() == 1

    def test_cancelling_the_bounce_makes_the_money_good_again(self, shop, invoice, user):
        payment = cheque(shop, rupees="25000", user=user)
        services.allocate_payment(payment, [(invoice, to_paisa("25000"))])
        event = services.bounce_cheque(payment, posting_date=JUNE, user=user)

        services.cancel_cheque_event(event, user=user, reason="Bank had it wrong")

        rows = LedgerEntry.objects.filter(voucher_type="ChequeEvent", voucher_id=event.pk)
        assert rows.filter(is_reversal=True).count() == 2
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == 0

        payment.refresh_from_db()
        assert payment.cheque_status == ChequeStatus.PENDING
        assert payment.is_live
        assert invoice.paid_paisa == to_paisa("25000")

    def test_a_cheque_is_settled_once(self, shop, invoice, user):
        payment = cheque(shop, rupees="25000", user=user)
        services.bounce_cheque(payment, posting_date=JUNE, user=user)
        assert (
            ChequeEvent.objects.filter(payment=payment, status=DocumentStatus.POSTED).count() == 1
        )

    def test_a_bounced_cheque_can_be_re_presented_after_the_bounce_is_cancelled(
        self, accounts, shop, invoice, user
    ):
        payment = cheque(shop, rupees="25000", user=user)
        event = services.bounce_cheque(payment, posting_date=JUNE, user=user)
        services.cancel_cheque_event(event, user=user, reason="Re-presented")

        services.clear_cheque(payment, posting_date=JUNE, user=user)

        payment.refresh_from_db()
        assert payment.cheque_status == ChequeStatus.CLEARED
        assert account_balance(accounts.bank).paisa == to_paisa("25000")
        assert account_balance(accounts.by_code[coa.CHEQUES_IN_HAND]).paisa == 0


class TestPaidCheques:
    """The mirror: a cheque we write to a supplier."""

    def test_a_cheque_out_posts_to_cheques_issued(self, accounts, db, user):
        vendor = Vendor.objects.create(code="V-05", name="Tapal Tea")
        payment = services.create_payment(
            party=vendor,
            direction=PaymentDirection.PAY,
            mode=PaymentMode.CHEQUE,
            posting_date=APRIL,
            amount_paisa=to_paisa("50000"),
            cheque_no="0044120",
            cheque_date=MAY,
            bank_name="HBL",
        )
        services.post_payment(payment, user=user)

        rows = LedgerEntry.objects.filter(voucher_type="Payment", voucher_id=payment.pk)
        assert rows.get(debit_paisa__gt=0).account.code == coa.ACCOUNTS_PAYABLE
        assert rows.get(credit_paisa__gt=0).account.code == coa.CHEQUES_ISSUED
        assert account_balance(accounts.by_code[coa.CHEQUES_ISSUED]).paisa == to_paisa("50000")

    def test_clearing_it_takes_the_money_out_of_the_bank(self, accounts, db, user):
        vendor = Vendor.objects.create(code="V-05", name="Tapal Tea")
        payment = services.create_payment(
            party=vendor,
            direction=PaymentDirection.PAY,
            mode=PaymentMode.CHEQUE,
            posting_date=APRIL,
            amount_paisa=to_paisa("50000"),
            cheque_no="0044120",
            cheque_date=MAY,
        )
        services.post_payment(payment, user=user)
        services.clear_cheque(payment, posting_date=MAY, user=user)

        assert account_balance(accounts.by_code[coa.CHEQUES_ISSUED]).paisa == 0
        assert account_balance(accounts.bank).paisa == to_paisa("-50000")

    def test_bouncing_our_own_cheque_puts_the_payable_back(self, accounts, db, user):
        vendor = Vendor.objects.create(code="V-05", name="Tapal Tea")
        payment = services.create_payment(
            party=vendor,
            direction=PaymentDirection.PAY,
            mode=PaymentMode.CHEQUE,
            posting_date=APRIL,
            amount_paisa=to_paisa("50000"),
            cheque_no="0044120",
            cheque_date=MAY,
        )
        services.post_payment(payment, user=user)
        services.bounce_cheque(payment, posting_date=MAY, user=user)

        assert account_balance(accounts.by_code[coa.CHEQUES_ISSUED]).paisa == 0
        # Back to square one: nothing was paid, so nothing is owed either way.
        # The payable the cheque discharged has been put straight back.
        assert party_balance(PartyType.VENDOR, vendor.pk).paisa == 0


# ===========================================================================
# The ledger, end to end
# ===========================================================================
class TestLedgerIntegrity:
    def test_every_payment_posting_balances(self, shop, invoice, user):
        receipt(shop, rupees="4000", user=user)
        payment = cheque(shop, rupees="6000", posting_date=MAY, user=user)
        services.clear_cheque(payment, posting_date=JUNE, user=user)

        rows = LedgerEntry.objects.all()
        debits = sum(row.debit_paisa for row in rows)
        credits = sum(row.credit_paisa for row in rows)
        assert debits == credits

    def test_the_clients_balance_is_the_ledger_and_nothing_else(self, shop, invoice, user):
        payment = receipt(shop, rupees="10000", user=user)
        services.allocate_payment(payment, [(invoice, to_paisa("10000"))])

        # Allocation moved no money. The balance is what the two documents did.
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == to_paisa("15000")
        services.unallocate(payment)
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == to_paisa("15000")
