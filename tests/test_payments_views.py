"""The recovery workspace — the screen the accountant lives in.

Three things are tested harder than the rest, because they are what "usable all
day" actually means here:

* **The sheet shows the four things a chase needs** — who, what they owe, how
  old it is, and when they last paid — and the four filters narrow it.
* **A row expands into its open invoices and takes money inline**, in one
  request, ending with the row redrawn from the ledger rather than from what the
  browser thought it had typed.
* **The client never computes money.** The templates are read here and the build
  fails if one of them ever learns arithmetic, the same way the sales entry
  screen's JavaScript is pinned.
"""

import datetime as dt
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from apps.accounting.enums import PartyType
from apps.accounting.services import party_balance
from apps.core.enums import DocumentStatus
from apps.core.money import to_paisa
from apps.masters.enums import Unit
from apps.masters.models import Client, Item, Route, Seller, Vendor
from apps.payments import services
from apps.payments.enums import ChequeStatus, PaymentDirection, PaymentMode
from apps.payments.forms import field_name
from apps.payments.models import ChequeEvent, Payment, PaymentAllocation
from apps.purchasing import services as purchasing
from apps.sales import services as sales
from apps.sales.models import SalesInvoiceLine
from tests.conftest import grant_cancel

pytestmark = pytest.mark.django_db

TODAY = dt.date(2026, 6, 30)
BIG_LIMIT = to_paisa("10000000")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
#: Long enough for the cancel form — see apps.core.forms.MIN_CANCEL_REASON.
CANCEL_REASON = "Receipt entered against the wrong shop"


@pytest.fixture
def operator(django_user_model, db):
    user = django_user_model.objects.create_user(username="counter", password="x", is_staff=True)
    return grant_cancel(user, Payment, ChequeEvent)


@pytest.fixture
def staff_client(client, operator):
    client.force_login(operator)
    return client


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
        credit_days=0,
    )


@pytest.fixture
def oil(db):
    return Item.objects.create(code="OIL-1000", name="Cooking Oil 1L", carton_size=12)


@pytest.fixture
def stocked(db, accounts, warehouses, oil, operator):
    vendor = Vendor.objects.create(code="V-01", name="Dalda Foods")
    invoice = purchasing.create_purchase_invoice(
        vendor=vendor, warehouse=warehouses.main, posting_date=dt.date(2024, 1, 1)
    )
    line = purchasing.update_line(
        purchasing.PurchaseInvoiceLine(document=invoice),
        item=oil,
        qty_input=1000,
        unit_input=Unit.CARTON,
        rate_input_paisa=to_paisa("2400"),
    )
    line.save()
    purchasing.post_purchase_invoice(invoice, user=operator)
    return invoice


def bill(shop, warehouse, oil, user, *, on, rupees="1000", cartons=1):
    invoice = sales.create_sales_invoice(client=shop, warehouse=warehouse, posting_date=on)
    line = sales.update_line(
        SalesInvoiceLine(document=invoice),
        item=oil,
        qty_input=cartons,
        unit_input=Unit.CARTON,
        rate_input_paisa=to_paisa(rupees),
    )
    line.save()
    return sales.post_sales_invoice(invoice, user=user)


@pytest.fixture
def overdue(db, stocked, shop, warehouses, oil, operator):
    """Rs 5,000 owed, seventy days past due."""
    return bill(
        shop, warehouses.main, oil, operator, on=TODAY - dt.timedelta(days=70), rupees="5000"
    )


def as_of(url: str) -> str:
    return f"{url}?as_of={TODAY:%Y-%m-%d}"


# ===========================================================================
# The sheet
# ===========================================================================
class TestWorkspace:
    def test_it_needs_a_login(self, client):
        response = client.get(reverse("payments:recovery"))
        assert response.status_code == 302

    def test_the_sheet_shows_the_shop_and_what_it_owes(self, staff_client, shop, overdue):
        response = staff_client.get(as_of(reverse("payments:recovery")))
        body = response.content.decode()

        assert response.status_code == 200
        assert "Al-Madina Kiryana" in body
        assert "0300-2214477" in body  # phone: how a shopkeeper identifies themselves
        assert "5,000.00" in body
        assert "61-90" in body  # the ageing bucket, seventy days out

    def test_the_ageing_strip_totals_the_sheet(self, staff_client, shop, overdue):
        response = staff_client.get(as_of(reverse("payments:recovery")))
        assert response.context["summary"]
        totals = {bucket: paisa for bucket, _label, paisa, _alarm in response.context["summary"]}
        assert totals["61-90"] == to_paisa("5000")
        assert response.context["total_overdue_paisa"] == to_paisa("5000")

    def test_overdue_money_is_in_the_alarm_colour(self, staff_client, shop, overdue):
        """`text-cancelled` is this project's alarm colour — see static/src/css."""
        body = staff_client.get(as_of(reverse("payments:recovery"))).content.decode()
        assert "text-cancelled" in body

    def test_the_route_filter_narrows_it(self, staff_client, shop, overdue, db):
        elsewhere = Route.objects.create(code="R-99", name="Nowhere")
        response = staff_client.get(
            f"{reverse('payments:recovery')}?as_of={TODAY:%Y-%m-%d}&route={elsewhere.pk}"
        )
        assert response.context["rows"] == []
        assert "Everybody is paid up" in response.content.decode()

    def test_the_bucket_filter_narrows_it(self, staff_client, shop, overdue):
        response = staff_client.get(
            f"{reverse('payments:recovery')}?as_of={TODAY:%Y-%m-%d}&bucket=1-30"
        )
        assert response.context["rows"] == []

    def test_the_over_90_bucket_link_survives_the_query_string(self, staff_client, shop, overdue):
        """A raw ``+`` in a URL decodes to a space, and "90 " matches no bucket.

        Without ``|urlencode`` on the link, clicking "Over 90 days" would filter
        on nothing and quietly show the whole sheet — the one bucket where being
        shown everything looks most like being shown the right thing.
        """
        body = staff_client.get(as_of(reverse("payments:recovery"))).content.decode()
        assert "bucket=90%2B" in body
        assert "bucket=90+" not in body

        response = staff_client.get(
            f"{reverse('payments:recovery')}?as_of={TODAY:%Y-%m-%d}&bucket=90%2B"
        )
        assert [row.client.code for row in response.context["rows"]] == []
        assert response.context["criteria"]["bucket"] == "90+"

    def test_the_rows_endpoint_returns_just_the_sheet(self, staff_client, shop, overdue):
        response = staff_client.get(as_of(reverse("payments:recovery-rows")))
        body = response.content.decode()
        assert response.status_code == 200
        assert body.strip().startswith('<div id="sheet"')
        assert "<html" not in body

    def test_todays_recovery_is_grouped_by_route(self, staff_client, shop, overdue, operator):
        services.post_payment(
            services.create_payment(
                party=shop,
                direction=PaymentDirection.RECEIVE,
                mode=PaymentMode.CASH,
                posting_date=TODAY,
                amount_paisa=to_paisa("2000"),
            ),
            user=operator,
        )
        response = staff_client.get(as_of(reverse("payments:recovery")))

        assert response.context["day_collected_paisa"] == to_paisa("2000")
        assert response.context["day_outstanding_paisa"] == to_paisa("3000")
        assert [line.route_code for line in response.context["day_lines"]] == ["R-01"]


# ===========================================================================
# The expanded row
# ===========================================================================
class TestExpandedRow:
    def test_it_lists_the_open_invoices(self, staff_client, shop, overdue):
        response = staff_client.get(
            as_of(reverse("payments:recovery-client", kwargs={"pk": shop.pk}))
        )
        body = response.content.decode()

        assert response.status_code == 200
        assert overdue.code in body
        assert "5,000.00" in body
        assert "Take a payment" in body

    def test_it_offers_a_box_per_open_invoice(self, staff_client, shop, overdue):
        response = staff_client.get(
            as_of(reverse("payments:recovery-client", kwargs={"pk": shop.pk}))
        )
        expected = field_name("SalesInvoice", overdue.pk)
        assert expected in response.content.decode()

    def test_taking_money_posts_and_allocates_in_one_request(
        self, staff_client, shop, overdue, warehouses
    ):
        response = staff_client.post(
            as_of(reverse("payments:recovery-receive", kwargs={"pk": shop.pk})),
            {
                "amount": "3,000",
                "mode": PaymentMode.CASH,
                "posting_date": f"{TODAY:%Y-%m-%d}",
                field_name("SalesInvoice", overdue.pk): "3000",
            },
        )

        assert response.status_code == 200
        payment = Payment.objects.get()
        assert payment.status == DocumentStatus.POSTED
        assert payment.amount_paisa == to_paisa("3000")
        assert payment.route_id == shop.route_id  # defaulted from the shop
        assert overdue.paid_paisa == to_paisa("3000")
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == to_paisa("2000")

    def test_the_row_comes_back_redrawn_from_the_ledger(self, staff_client, shop, overdue):
        response = staff_client.post(
            as_of(reverse("payments:recovery-receive", kwargs={"pk": shop.pk})),
            {
                "amount": "3,000",
                "mode": PaymentMode.CASH,
                "posting_date": f"{TODAY:%Y-%m-%d}",
                field_name("SalesInvoice", overdue.pk): "3000",
            },
        )
        assert response.context["row"].open_paisa == to_paisa("2000")
        assert "2,000.00" in response.content.decode()

    def test_money_taken_without_an_allocation_lands_on_account(self, staff_client, shop, overdue):
        response = staff_client.post(
            as_of(reverse("payments:recovery-receive", kwargs={"pk": shop.pk})),
            {"amount": "3,000", "mode": PaymentMode.CASH, "posting_date": f"{TODAY:%Y-%m-%d}"},
        )

        assert response.status_code == 200
        payment = Payment.objects.get()
        assert payment.unallocated_paisa == to_paisa("3000")
        assert response.context["row"].on_account_paisa == to_paisa("3000")
        assert "on account" in response.content.decode()

    def test_over_allocation_is_refused_and_nothing_is_posted(self, staff_client, shop, overdue):
        response = staff_client.post(
            as_of(reverse("payments:recovery-receive", kwargs={"pk": shop.pk})),
            {
                "amount": "3,000",
                "mode": PaymentMode.CASH,
                "posting_date": f"{TODAY:%Y-%m-%d}",
                field_name("SalesInvoice", overdue.pk): "9000",
            },
        )

        assert response.status_code == 422
        assert "too much" in response.content.decode()
        # The receipt and the allocation land together or not at all.
        assert Payment.objects.count() == 0
        assert PaymentAllocation.objects.count() == 0

    def test_a_cheque_taken_inline_needs_its_number(self, staff_client, shop, overdue):
        response = staff_client.post(
            as_of(reverse("payments:recovery-receive", kwargs={"pk": shop.pk})),
            {"amount": "3,000", "mode": PaymentMode.CHEQUE, "posting_date": f"{TODAY:%Y-%m-%d}"},
        )

        assert response.status_code == 422
        assert "cheque needs its number" in response.content.decode()
        assert Payment.objects.count() == 0

    def test_money_already_on_account_can_be_applied_inline(
        self, staff_client, shop, overdue, operator
    ):
        payment = services.post_payment(
            services.create_payment(
                party=shop,
                direction=PaymentDirection.RECEIVE,
                mode=PaymentMode.CASH,
                posting_date=TODAY - dt.timedelta(days=3),
                amount_paisa=to_paisa("2000"),
            ),
            user=operator,
        )

        response = staff_client.post(
            as_of(reverse("payments:recovery-allocate", kwargs={"pk": shop.pk})),
            {"payment": payment.pk, field_name("SalesInvoice", overdue.pk): "2000"},
        )

        assert response.status_code == 200
        assert overdue.paid_paisa == to_paisa("2000")
        assert payment.unallocated_paisa == 0


# ===========================================================================
# Payments and cheques
# ===========================================================================
class TestPaymentScreens:
    def test_the_list_renders(self, staff_client, shop, overdue, operator):
        services.post_payment(
            services.create_payment(
                party=shop,
                direction=PaymentDirection.RECEIVE,
                mode=PaymentMode.CASH,
                posting_date=TODAY,
                amount_paisa=to_paisa("1000"),
            ),
            user=operator,
        )
        body = staff_client.get(reverse("payments:list")).content.decode()
        assert "Al-Madina Kiryana" in body
        assert "1,000.00" in body

    def test_creating_a_receipt_from_the_form(self, staff_client, shop, overdue, seller):
        response = staff_client.post(
            reverse("payments:create"),
            {
                "client": shop.pk,
                "direction": PaymentDirection.RECEIVE,
                "mode": PaymentMode.CASH,
                "posting_date": f"{TODAY:%Y-%m-%d}",
                "amount": "1,500.50",
            },
        )

        payment = Payment.objects.get()
        assert response.status_code == 302
        assert payment.status == DocumentStatus.DRAFT
        assert payment.amount_paisa == to_paisa("1500.50")
        assert payment.code.startswith("RV-")

    def test_a_receipt_with_no_party_is_refused(self, staff_client, shop):
        response = staff_client.post(
            reverse("payments:create"),
            {
                "direction": PaymentDirection.RECEIVE,
                "mode": PaymentMode.CASH,
                "posting_date": f"{TODAY:%Y-%m-%d}",
                "amount": "100",
            },
        )
        assert response.status_code == 200
        assert Payment.objects.count() == 0
        assert "Choose the shop money came from" in response.content.decode()

    def test_posting_from_the_detail_screen(self, staff_client, shop, overdue, operator):
        payment = services.create_payment(
            party=shop,
            direction=PaymentDirection.RECEIVE,
            mode=PaymentMode.CASH,
            posting_date=TODAY,
            amount_paisa=to_paisa("1000"),
        )
        staff_client.post(reverse("payments:post", kwargs={"pk": payment.pk}))

        payment.refresh_from_db()
        assert payment.status == DocumentStatus.POSTED

    def test_the_detail_screen_shows_the_gl_it_posted(self, staff_client, shop, overdue, operator):
        payment = services.post_payment(
            services.create_payment(
                party=shop,
                direction=PaymentDirection.RECEIVE,
                mode=PaymentMode.CASH,
                posting_date=TODAY,
                amount_paisa=to_paisa("1000"),
            ),
            user=operator,
        )
        body = staff_client.get(
            reverse("payments:detail", kwargs={"pk": payment.pk})
        ).content.decode()

        assert "1110" in body  # Cash
        assert "1130" in body  # Accounts Receivable
        assert "General ledger" in body


class TestChequeScreens:
    @pytest.fixture
    def cheque(self, shop, overdue, operator):
        return services.post_payment(
            services.create_payment(
                party=shop,
                direction=PaymentDirection.RECEIVE,
                mode=PaymentMode.CHEQUE,
                posting_date=TODAY - dt.timedelta(days=10),
                amount_paisa=to_paisa("5000"),
                cheque_no="0091823",
                cheque_date=TODAY,
                bank_name="Meezan Bank",
            ),
            user=operator,
        )

    def test_the_register_lists_the_drawer(self, staff_client, cheque):
        response = staff_client.get(as_of(reverse("payments:cheques")))
        body = response.content.decode()

        assert "0091823" in body
        assert "Meezan Bank" in body
        assert response.context["total_paisa"] == to_paisa("5000")
        assert response.context["due_count"] == 1  # dated today, bankable now

    def test_clearing_it_from_the_screen(self, staff_client, cheque):
        staff_client.post(
            reverse("payments:cheque-clear", kwargs={"pk": cheque.pk}),
            {"posting_date": f"{TODAY:%Y-%m-%d}"},
        )

        cheque.refresh_from_db()
        assert cheque.cheque_status == ChequeStatus.CLEARED
        assert list(staff_client.get(as_of(reverse("payments:cheques"))).context["cheques"]) == []

    def test_bouncing_it_from_the_screen(self, staff_client, cheque, shop, overdue):
        staff_client.post(
            reverse("payments:cheque-bounce", kwargs={"pk": cheque.pk}),
            {"posting_date": f"{TODAY:%Y-%m-%d}"},
        )

        cheque.refresh_from_db()
        assert cheque.cheque_status == ChequeStatus.BOUNCED
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == to_paisa("5000")

    def test_a_bounced_shop_is_flagged_on_the_sheet(self, staff_client, cheque, shop):
        staff_client.post(
            reverse("payments:cheque-bounce", kwargs={"pk": cheque.pk}),
            {"posting_date": f"{TODAY:%Y-%m-%d}"},
        )

        response = staff_client.get(as_of(reverse("payments:recovery")))
        assert response.context["flagged_count"] == 1
        assert "bounced" in response.content.decode()

    def test_the_register_hides_a_cancelled_receipt_until_the_audit_toggle(
        self, staff_client, cheque, operator
    ):
        """Its entries have been reversed out of 1160, so the total must not count it."""
        services.cancel_payment(cheque, user=operator, reason=CANCEL_REASON)

        plain = staff_client.get(as_of(reverse("payments:cheques")))
        assert list(plain.context["cheques"]) == []
        assert plain.context["total_paisa"] == 0
        assert "Include cancelled (audit)" in plain.content.decode()

        audit = staff_client.get(as_of(reverse("payments:cheques")) + "&include_cancelled=1")
        assert list(audit.context["cheques"]) == [cheque]
        assert audit.context["cancelled_count"] == 1
        assert audit.context["total_paisa"] == 0, "the audit view still totals only live cheques"

    def test_reversing_a_cheque_event_goes_through_the_confirmation_screen(
        self, staff_client, cheque
    ):
        staff_client.post(
            reverse("payments:cheque-clear", kwargs={"pk": cheque.pk}),
            {"posting_date": f"{TODAY:%Y-%m-%d}"},
        )
        event = cheque.cheque_events.get()

        url = reverse("payments:cheque-event-cancel", kwargs={"pk": event.pk})
        preview = staff_client.get(url)
        assert preview.status_code == 200
        assert "Reversing general ledger entries" in preview.content.decode()

        staff_client.post(url, {"reason": CANCEL_REASON})
        cheque.refresh_from_db()
        assert cheque.cheque_status == ChequeStatus.PENDING


# ===========================================================================
# Cancelling a payment
# ===========================================================================
class TestCancelScreen:
    @pytest.fixture
    def receipt(self, shop, overdue, operator):
        return services.post_payment(
            services.create_payment(
                party=shop,
                direction=PaymentDirection.RECEIVE,
                mode=PaymentMode.CASH,
                posting_date=TODAY,
                amount_paisa=to_paisa("5000"),
            ),
            user=operator,
        )

    def _url(self, receipt):
        return reverse("payments:cancel", kwargs={"pk": receipt.pk})

    def test_it_previews_the_two_reversing_rows_and_writes_nothing(self, staff_client, receipt):
        response = staff_client.get(self._url(receipt))
        body = response.content.decode()

        assert response.status_code == 200
        assert "Reversing general ledger entries" in body
        assert response.context["preview"].balances
        assert len(response.context["preview"].ledger) == 2
        assert response.context["preview"].stock == [], "money moving is not goods moving"
        receipt.refresh_from_db()
        assert receipt.status == DocumentStatus.POSTED

    def test_a_typed_reason_cancels_and_reverses(self, staff_client, receipt, shop):
        before = party_balance(PartyType.CLIENT, shop.pk).paisa

        staff_client.post(self._url(receipt), {"reason": CANCEL_REASON})

        receipt.refresh_from_db()
        assert receipt.status == DocumentStatus.CANCELLED
        assert receipt.cancel_reason == CANCEL_REASON
        assert party_balance(PartyType.CLIENT, shop.pk).paisa == before + to_paisa("5000")

    def test_a_short_reason_is_refused(self, staff_client, receipt):
        response = staff_client.post(self._url(receipt), {"reason": "wrong"})
        receipt.refresh_from_db()

        assert response.status_code == 422
        assert receipt.status == DocumentStatus.POSTED
        assert "at least 10 characters" in response.content.decode()

    def test_it_needs_the_cancel_permission(self, staff_client, client, django_user_model, receipt):
        client.force_login(
            django_user_model.objects.create_user(username="junior", password="x", is_staff=True)
        )
        response = client.post(self._url(receipt), {"reason": CANCEL_REASON})

        receipt.refresh_from_db()
        assert response.status_code == 403
        assert receipt.status == DocumentStatus.POSTED

    def test_a_settled_cheque_blocks_the_button_and_names_the_event(
        self, staff_client, shop, overdue, operator
    ):
        payment = services.post_payment(
            services.create_payment(
                party=shop,
                direction=PaymentDirection.RECEIVE,
                mode=PaymentMode.CHEQUE,
                posting_date=TODAY,
                amount_paisa=to_paisa("5000"),
                cheque_no="0091823",
                cheque_date=TODAY,
            ),
            user=operator,
        )
        event = services.clear_cheque(payment, posting_date=TODAY, user=operator)

        response = staff_client.get(self._url(payment))
        body = response.content.decode()

        assert event.code in body
        assert "cannot be cancelled yet" in body
        assert "disabled" in body

    def test_the_detail_screen_shows_the_timeline_and_the_watermark(
        self, staff_client, receipt, operator
    ):
        url = reverse("payments:detail", kwargs={"pk": receipt.pk})
        assert "doc-watermark" not in staff_client.get(url).content.decode()

        services.cancel_payment(receipt, user=operator, reason=CANCEL_REASON)
        body = staff_client.get(url).content.decode()

        assert "doc-watermark" in body
        assert CANCEL_REASON in body, "the reason belongs on the timeline"
        assert "Not yet amended" in body


# ===========================================================================
# The client computes nothing
# ===========================================================================
class TestNoClientSideArithmetic:
    """The rule that keeps every figure on this screen the server's.

    A template that started adding up allocations in the browser would show one
    number and post another, and the one somebody trusted would be the wrong one.
    """

    ARITHMETIC = ("|add:", "|sub:", "widthratio", "|divisibleby")

    def test_no_template_does_money_arithmetic(self):
        offenders = []
        for path in (Path(settings.BASE_DIR) / "templates" / "payments").rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            for needle in self.ARITHMETIC:
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        assert not offenders, (
            "Money is computed by apps.payments.recovery on the server, never in a "
            "template:\n" + "\n".join(offenders)
        )

    def test_the_workspace_has_no_inline_script(self):
        """The only JavaScript on this screen is htmx, served from static/dist."""
        page = Path(settings.BASE_DIR) / "templates" / "payments" / "recovery.html"
        text = page.read_text(encoding="utf-8")
        assert "<script src=" in text
        assert "<script>" not in text
