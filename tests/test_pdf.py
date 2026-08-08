"""The printed output: five renderers, and the two ways to reach them.

Asserting on a PDF is asserting on a binary, so these tests check the three
things that actually go wrong and can be checked without a rendering engine:

* **it is a real PDF and it is not empty** — the header bytes, and a size that
  rules out an empty page with a letterhead and nothing under it;
* **the page count**, read back out of the file, because "page 1 of 3" is only
  right if there really are three, and because a long invoice that silently
  truncated at one page is the failure worth catching;
* **the text on it**, extracted from the content streams, so an invoice that
  printed no line items or lost its amount-in-words fails here rather than at a
  counter.

What is deliberately not tested is where anything sits on the page. A test that
pinned coordinates would fail on every layout tweak and pass on every layout
that was wrong.
"""

from __future__ import annotations

import base64
import datetime as dt
import re
import zlib

import pytest
from django.urls import reverse

from apps.core.money import to_paisa
from apps.masters.enums import Unit
from apps.masters.models import Client, Item, Route, Seller, Vendor
from apps.payments import services as payments
from apps.payments.enums import PaymentDirection, PaymentMode
from apps.purchasing import services as purchasing
from apps.purchasing.models import PurchaseInvoiceLine
from apps.reports.models import SINGLETON_PK, CompanyProfile
from apps.reports.pdf import (
    client_ledger_pdf,
    payment_receipt_pdf,
    purchase_invoice_pdf,
    route_day_sheet_pdf,
    sales_invoice_pdf,
)
from apps.sales import services as sales
from apps.sales.models import SalesInvoiceLine, SalesReturnLine
from tests.conftest import join_group

pytestmark = pytest.mark.django_db

APRIL = dt.date(2026, 4, 1)
MAY = dt.date(2026, 5, 1)
JUNE = dt.date(2026, 6, 1)

#: A letterhead, a line table with one row and a totals block is comfortably
#: over 2 KB even compressed. Anything under this is a page that failed to draw
#: its content — which is exactly the failure a "did it produce a file" test
#: would otherwise wave through.
MIN_PDF_BYTES = 2000


# ===========================================================================
# Reading a PDF back
# ===========================================================================
def page_count(pdf: bytes) -> int:
    """How many pages the file actually has.

    Counted from the ``/Type /Page`` objects rather than from ``/Count``, which
    a malformed page tree can overstate. Whitespace between the tokens varies,
    hence the regex rather than a substring search.
    """
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf))


def _content_streams(pdf: bytes):
    """Every stream in the file, decoded.

    ReportLab writes content as ``/Filter [ /ASCII85Decode /FlateDecode ]``, so
    a stream has to be un-85'd and then inflated. Both filters are tried and
    then the raw bytes, because an embedded image is neither.
    """
    for raw in re.findall(rb"stream(.*?)endstream", pdf, re.S):
        data = raw.strip(b"\r\n \t")
        body = data[:-2] if data.endswith(b"~>") else data
        for decode in (
            lambda value: zlib.decompress(base64.a85decode(value, adobe=False)),
            zlib.decompress,
            lambda value: value,
        ):
            try:
                yield decode(body)
            except Exception:
                continue
            break


def text_of(pdf: bytes) -> str:
    """Every string drawn into the file, concatenated.

    Crude — it makes no attempt at reading order or spacing — but it answers the
    only question these tests ask, which is "does this word appear on the page
    at all". Pinning coordinates would fail on every layout tweak and pass on
    every layout that was wrong.
    """
    text = []
    for chunk in _content_streams(pdf):
        # ( … ) Tj  and  [ ( … ) … ] TJ — the two ways ReportLab shows a string.
        for literal in re.findall(rb"\((?:\\.|[^\\()])*\)", chunk):
            text.append(literal[1:-1].decode("latin-1", errors="replace"))
    return "".join(text).replace("\\(", "(").replace("\\)", ")")


def assert_is_a_pdf(pdf: bytes) -> None:
    assert pdf[:5] == b"%PDF-", "not a PDF at all"
    assert pdf.rstrip().endswith(b"%%EOF"), "the file was truncated"
    assert len(pdf) > MIN_PDF_BYTES, f"only {len(pdf)} bytes — the page drew nothing"


# ===========================================================================
# Fixtures — a seeded set of documents to print
# ===========================================================================
@pytest.fixture
def profile(db):
    """A filled-in company profile. Every page reads this."""
    company = CompanyProfile.get()
    company.name = "Al-Noor Distributors"
    company.address = "Plot 14, Sector 7-A\nKorangi Industrial Area, Karachi"
    company.phone = "021-35120987"
    company.email = "billing@alnoor.test"
    company.ntn = "1234567-8"
    company.strn = "0987654321098"
    company.footer_text = "Goods once sold will not be taken back without this bill."
    company.invoice_terms = "Payment due within 15 days. Cheques in favour of Al-Noor Distributors."
    company.save()
    return company


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
        name="Al-Madina Kiryana Store",
        phone="0300-2214477",
        address="Shop 4, Tariq Road",
        city="Karachi",
        route=route,
        seller=seller,
        credit_limit_paisa=to_paisa("500000"),
        credit_days=15,
    )


@pytest.fixture
def vendor(db):
    return Vendor.objects.create(code="V-01", name="Dalda Foods (Pvt) Ltd", city="Karachi")


@pytest.fixture
def oil(db):
    return Item.objects.create(
        code="OIL-1000",
        name="Cooking Oil 1 Litre Bottle",
        carton_size=12,
        tax_rate_bp=1750,
        sale_rate_paisa=to_paisa("250"),
    )


@pytest.fixture
def rice(db):
    return Item.objects.create(
        code="RICE-25",
        name="Basmati Rice 25kg Bag",
        carton_size=1,
        sale_rate_paisa=to_paisa("7850"),
    )


@pytest.fixture
def purchase(db, accounts, warehouses, vendor, oil, rice, user):
    """A posted supplier bill: 1,200 bottles and 40 bags on hand."""
    bill = purchasing.create_purchase_invoice(
        vendor=vendor,
        warehouse=warehouses.main,
        posting_date=APRIL,
        vendor_bill_no="DF-88213",
        vendor_bill_date=APRIL,
        remarks="Delivered by their own van, two cartons short — credited separately.",
    )
    for item, qty, unit, rate in (
        (oil, 100, Unit.CARTON, "2400"),
        (rice, 40, Unit.PIECE, "7000"),
    ):
        purchasing.update_line(
            PurchaseInvoiceLine(document=bill),
            item=item,
            qty_input=qty,
            unit_input=unit,
            rate_input_paisa=to_paisa(rate),
        ).save()
    return purchasing.post_purchase_invoice(bill, user=user)


@pytest.fixture
def invoice(db, purchase, shop, warehouses, oil, rice, user):
    """A posted sales invoice with two lines, one of them cartoned."""
    document = sales.create_sales_invoice(
        client=shop, warehouse=warehouses.main, posting_date=MAY, remarks="Van delivery, Tuesday."
    )
    for item, qty, unit, rate in (
        (oil, 10, Unit.CARTON, "3000"),
        (rice, 5, Unit.PIECE, "8000"),
    ):
        sales.update_line(
            SalesInvoiceLine(document=document),
            item=item,
            qty_input=qty,
            unit_input=unit,
            rate_input_paisa=to_paisa(rate),
        ).save()
    return sales.post_sales_invoice(document, user=user)


@pytest.fixture
def receipt(db, invoice, shop, user):
    """A posted cash receipt, allocated against the invoice."""
    payment = payments.post_payment(
        payments.create_payment(
            party=shop,
            direction=PaymentDirection.RECEIVE,
            mode=PaymentMode.CASH,
            posting_date=JUNE,
            amount_paisa=to_paisa("20000"),
            remarks="Collected on the Tuesday round.",
        ),
        user=user,
    )
    payments.allocate_payment(payment, [(invoice, to_paisa("20000"))], user=user)
    return payment


# ===========================================================================
# The invoices
# ===========================================================================
class TestSalesInvoicePDF:
    def test_it_renders_a_one_page_pdf(self, invoice, profile):
        pdf = sales_invoice_pdf(invoice)
        assert_is_a_pdf(pdf)
        assert page_count(pdf) == 1

    def test_the_letterhead_is_on_it(self, invoice, profile):
        text = text_of(sales_invoice_pdf(invoice))
        assert "Al-Noor Distributors" in text
        assert "Korangi Industrial Area, Karachi" in text
        assert "021-35120987" in text
        assert "NTN 1234567-8" in text

    def test_it_names_the_document_and_the_shop(self, invoice, profile):
        text = text_of(sales_invoice_pdf(invoice))
        assert invoice.code in text
        assert "Al-Madina Kiryana Store" in text
        assert "SALES INVOICE" in text

    def test_every_line_is_on_it_with_the_quantity_the_warehouse_counts(self, invoice, profile):
        text = text_of(sales_invoice_pdf(invoice))
        assert "OIL-1000" in text
        assert "Cooking Oil 1 Litre Bottle" in text
        # 10 cartons of 12, printed the way a picker reads it — not "120".
        assert "10 ctn" in text
        assert "RICE-25" in text
        assert "5 pcs" in text

    def test_the_total_is_followed_by_the_amount_in_words(self, invoice, profile):
        from apps.core.words import amount_in_words

        text = text_of(sales_invoice_pdf(invoice))
        assert "Amount in words:" in text
        # The exact sentence, lakh and crore included.
        assert amount_in_words(invoice.total_paisa).split(" and ")[0] in text

    def test_the_footer_and_the_page_number_are_on_it(self, invoice, profile):
        text = text_of(sales_invoice_pdf(invoice))
        assert "Page 1 of 1" in text
        assert "Goods once sold will not be taken back" in text

    def test_it_carries_a_signature_line(self, invoice, profile):
        text = text_of(sales_invoice_pdf(invoice))
        assert "Received the goods in good order" in text
        assert "Authorised signature" in text

    def test_the_terms_are_printed_under_the_totals(self, invoice, profile):
        assert "Payment due within 15 days" in text_of(sales_invoice_pdf(invoice))

    def test_a_long_invoice_runs_to_more_than_one_page_and_says_so(
        self, purchase, shop, warehouses, oil, user, profile
    ):
        """The case "page x of y" exists for."""
        document = sales.create_sales_invoice(
            client=shop, warehouse=warehouses.main, posting_date=MAY
        )
        for _ in range(60):
            sales.update_line(
                SalesInvoiceLine(document=document),
                item=oil,
                qty_input=1,
                unit_input=Unit.CARTON,
                rate_input_paisa=to_paisa("3000"),
            ).save()
        sales.post_sales_invoice(document, user=user)

        pdf = sales_invoice_pdf(document)
        pages = page_count(pdf)
        text = text_of(pdf)

        assert pages >= 2, "sixty lines must not silently fit on one page"
        assert f"Page 1 of {pages}" in text
        assert f"Page {pages} of {pages}" in text
        # The letterhead repeats: a second sheet without one gets queried.
        assert text.count("Al-Noor Distributors") >= pages

    def test_a_cancelled_invoice_is_watermarked_and_says_why(self, invoice, user, profile):
        sales.cancel_sales_invoice(invoice, user=user, reason="Keyed against the wrong shop")
        text = text_of(sales_invoice_pdf(invoice))

        assert "CANCELLED" in text
        assert "Keyed against the wrong shop" in text
        assert "entries have been reversed" in text

    def test_an_amendment_names_what_it_replaces(self, invoice, user, profile):
        sales.cancel_sales_invoice(invoice, user=user, reason="Quantity on line 1 was wrong")
        amendment = sales.amend_sales_invoice(invoice, user=user)
        sales.post_sales_invoice(amendment, user=user)

        text = text_of(sales_invoice_pdf(amendment))
        assert "Amends" in text
        assert invoice.code in text, "the meta block must name the original"

    def test_a_credit_note_prints_as_one(self, invoice, shop, warehouses, oil, user, profile):
        note = sales.create_sales_return(
            client=shop, warehouse=warehouses.main, posting_date=JUNE, against_invoice=invoice
        )
        sales.update_line(
            SalesReturnLine(document=note),
            item=oil,
            qty_input=2,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("3000"),
        ).save()
        sales.post_sales_return(note, user=user)

        text = text_of(sales_invoice_pdf(note))
        assert "CREDIT NOTE" in text
        assert invoice.code in text, "it must name the invoice it is against"

    def test_it_prints_on_a5_too(self, invoice, profile):
        """The delivery book is half-sheet; the same column spec has to fit."""
        pdf = sales_invoice_pdf(invoice, paper="a5")
        assert_is_a_pdf(pdf)
        assert invoice.code in text_of(pdf)


class TestPurchaseInvoicePDF:
    def test_it_renders_and_names_the_supplier(self, purchase, profile):
        pdf = purchase_invoice_pdf(purchase)
        assert_is_a_pdf(pdf)
        assert page_count(pdf) == 1

        text = text_of(pdf)
        assert "PURCHASE INVOICE" in text
        assert "Dalda Foods (Pvt) Ltd" in text
        assert purchase.code in text

    def test_it_carries_the_suppliers_own_bill_number(self, purchase, profile):
        """What a query to the supplier is made against — not our code."""
        text = text_of(purchase_invoice_pdf(purchase))
        assert "DF-88213" in text

    def test_the_remarks_are_printed(self, purchase, profile):
        assert "two cartons short" in text_of(purchase_invoice_pdf(purchase))


# ===========================================================================
# The receipt, on both kinds of printer
# ===========================================================================
class TestPaymentReceiptPDF:
    def test_the_sheet_layout_renders(self, receipt, profile):
        pdf = payment_receipt_pdf(receipt, layout="a5")
        assert_is_a_pdf(pdf)
        assert page_count(pdf) == 1

        text = text_of(pdf)
        assert "RECEIPT" in text
        assert receipt.code in text
        assert "Al-Madina Kiryana Store" in text

    def test_the_sheet_shows_what_the_money_settled(self, receipt, invoice, profile):
        text = text_of(payment_receipt_pdf(receipt, layout="a5"))
        assert "Applied to" in text
        assert invoice.code in text

    def test_the_sheet_carries_the_amount_in_words(self, receipt, profile):
        assert "Rupees Twenty Thousand Only" in text_of(payment_receipt_pdf(receipt, layout="a5"))

    def test_the_thermal_layout_is_80mm_wide_and_has_no_page_furniture(self, receipt, profile):
        from reportlab.lib.units import mm

        pdf = payment_receipt_pdf(receipt, layout="80mm")
        assert_is_a_pdf(pdf)
        assert page_count(pdf) == 1

        # The MediaBox is the roll width, not A4.
        box = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
        assert box, "no MediaBox in the file"
        width = float(box.group(1))
        assert abs(width - 72 * mm) < 1, f"expected a 72mm printable width, got {width}pt"

        text = text_of(pdf)
        assert receipt.code in text
        assert "TOTAL" in text
        # A page number on a continuous roll is noise.
        assert "Page 1 of 1" not in text

    def test_the_58mm_roll_is_narrower(self, receipt, profile):
        from reportlab.lib.units import mm

        pdf = payment_receipt_pdf(receipt, layout="58mm")
        box = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
        assert abs(float(box.group(1)) - 48 * mm) < 1

    def test_the_layout_comes_from_settings_when_none_is_given(self, receipt, profile, settings):
        from reportlab.lib.units import mm

        settings.RECEIPT_LAYOUT = "80mm"
        pdf = payment_receipt_pdf(receipt)
        box = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
        assert abs(float(box.group(1)) - 72 * mm) < 1, "the printer setting was ignored"

    def test_an_unknown_layout_falls_back_rather_than_failing_to_print(self, receipt, profile):
        assert_is_a_pdf(payment_receipt_pdf(receipt, layout="fax"))

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Al-Madina Kiryana", "Al-Madina Kiryana"),
            # Cut on a space, not mid-word: "KiryanaStor" reads as another shop.
            (
                "New Sabir Kiryana Store & General Merchant",
                "New Sabir Kiryana Store & General..",
            ),
            # No space to break on, so it has to cut somewhere.
            ("A" * 50, "A" * 34 + ".."),
            ("", ""),
        ],
    )
    def test_a_long_name_is_trimmed_on_a_word_for_the_roll(self, name, expected):
        from apps.reports.pdf.receipts import _shorten

        assert _shorten(name) == expected

    def test_the_shops_full_name_reaches_the_roll(self, invoice, shop, accounts, user, profile):
        """The name is half the point of a receipt somebody keeps.

        Compared with the spaces squeezed out of both sides: the name wraps to a
        second line on a 72mm roll, and ``text_of`` concatenates each drawn
        string with no idea where the line breaks were.
        """
        payment = payments.post_payment(
            payments.create_payment(
                party=shop,
                direction=PaymentDirection.RECEIVE,
                mode=PaymentMode.CASH,
                posting_date=JUNE,
                amount_paisa=to_paisa("1000"),
            ),
            user=user,
        )
        squeezed = text_of(payment_receipt_pdf(payment, layout="80mm")).replace(" ", "")
        assert shop.name.replace(" ", "") in squeezed, "the shop's name was cut short"

    def test_a_cancelled_receipt_says_so_on_the_roll(self, receipt, user, profile):
        payments.cancel_payment(receipt, user=user, reason="Counted the cash twice")
        text = text_of(payment_receipt_pdf(receipt, layout="80mm"))
        assert "CANCELLED" in text

    def test_a_payment_out_prints_as_a_voucher(self, vendor, accounts, user, profile):
        payment = payments.post_payment(
            payments.create_payment(
                party=vendor,
                direction=PaymentDirection.PAY,
                mode=PaymentMode.BANK,
                posting_date=JUNE,
                amount_paisa=to_paisa("50000"),
            ),
            user=user,
        )
        text = text_of(payment_receipt_pdf(payment, layout="a5"))
        assert "PAYMENT VOUCHER" in text
        assert "Dalda Foods" in text


# ===========================================================================
# The two ledger reports
# ===========================================================================
class TestClientLedgerPDF:
    def test_it_renders_a_statement_with_the_movement_on_it(self, receipt, invoice, shop, profile):
        pdf = client_ledger_pdf(shop, APRIL, JUNE)
        assert_is_a_pdf(pdf)
        assert page_count(pdf) >= 1

        text = text_of(pdf)
        assert "STATEMENT OF ACCOUNT" in text
        assert "Al-Madina Kiryana Store" in text
        assert invoice.code in text, "the invoice must appear as a debit"
        assert receipt.code in text, "the receipt must appear as a credit"
        assert "Opening balance" in text
        assert "Closing balance" in text

    def test_the_closing_balance_is_checked_against_the_ledger(self, receipt, shop, profile):
        """The check is printed either way — that is what makes the page trustworthy."""
        text = text_of(client_ledger_pdf(shop, APRIL, JUNE))
        assert "agrees with the ledger" in text
        assert "does not tie out" not in text

    def test_a_cancelled_invoice_appears_twice_and_nets_to_zero(self, invoice, shop, user, profile):
        """A statement is the audit trail: the line, and the line that took it back."""
        sales.cancel_sales_invoice(invoice, user=user, reason="Keyed against the wrong shop")
        text = text_of(client_ledger_pdf(shop, APRIL, JUNE))

        assert text.count(invoice.code) >= 2
        assert "agrees with the ledger" in text

    def test_an_empty_period_still_prints_a_page(self, shop, profile, accounts):
        pdf = client_ledger_pdf(shop, dt.date(2020, 1, 1), dt.date(2020, 1, 31))
        assert_is_a_pdf(pdf)
        assert "Nothing moved on this account" in text_of(pdf)


class TestRouteDaySheetPDF:
    def test_it_lists_the_shops_that_owe_money(self, invoice, route, shop, profile):
        pdf = route_day_sheet_pdf(route, JUNE)
        assert_is_a_pdf(pdf)
        assert page_count(pdf) >= 1

        text = text_of(pdf)
        assert "ROUTE DAY SHEET" in text
        assert "R-01" in text
        assert "Al-Madina Kiryana Store" in text
        assert "0300-2214477" in text, "the phone is the point of the sheet"

    def test_it_leaves_a_column_for_what_is_collected(self, invoice, route, profile):
        assert "Collected" in text_of(route_day_sheet_pdf(route, JUNE))

    def test_an_empty_route_still_prints_a_page(self, route, profile, accounts):
        pdf = route_day_sheet_pdf(route, JUNE)
        assert_is_a_pdf(pdf)
        assert "No shop on this route has anything outstanding" in text_of(pdf)


# ===========================================================================
# The company profile
# ===========================================================================
class TestCompanyProfile:
    def test_it_is_a_singleton_however_it_is_created(self, db):
        first = CompanyProfile.objects.create(name="One")
        second = CompanyProfile.objects.create(name="Two")

        assert first.pk == second.pk == SINGLETON_PK
        assert CompanyProfile.objects.count() == 1
        assert CompanyProfile.get().name == "Two"

    def test_get_creates_it_empty_on_a_fresh_installation(self, db):
        assert not CompanyProfile.objects.exists()
        assert CompanyProfile.get().pk == SINGLETON_PK

    def test_it_refuses_to_be_deleted(self, db):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="every printed page"):
            CompanyProfile.get().delete()

    def test_a_url_cannot_get_into_the_logo_field(self, db):
        """CLAUDE.md §7: a hotlinked logo is a blank box on an offline PC."""
        from django.core.exceptions import ValidationError

        from apps.reports.models import logo_path_is_local

        with pytest.raises(ValidationError, match="no internet"):
            logo_path_is_local("https://example.com/logo.png")

    def test_an_unconfigured_profile_still_prints_a_page(self, invoice, db):
        """A fresh installation must not fail to print its first bill."""
        pdf = sales_invoice_pdf(invoice)
        assert_is_a_pdf(pdf)
        assert "company profile not set" in text_of(pdf)

    def test_a_missing_logo_file_does_not_stop_the_print(self, invoice, profile):
        profile.logo.name = "company/deleted-by-somebody.png"
        profile.save()

        pdf = sales_invoice_pdf(invoice)
        assert_is_a_pdf(pdf)
        assert "Al-Noor Distributors" in text_of(pdf)

    @pytest.mark.parametrize("suffix", [".png", ".jpg"])
    def test_a_real_logo_is_embedded_from_disk(self, invoice, profile, tmp_path, suffix, settings):
        """The one path where a hotlink would have been easier and is refused.

        The image is written to MEDIA_ROOT and read back off the filesystem —
        there is no code path in this system that fetches one over a network
        (CLAUDE.md §7).
        """
        from PIL import Image

        settings.MEDIA_ROOT = str(tmp_path)
        directory = tmp_path / "company"
        directory.mkdir()
        source = directory / f"logo{suffix}"
        Image.new("RGB", (600, 200), (0, 78, 149)).save(source)

        profile.logo.name = f"company/logo{suffix}"
        profile.save()

        pdf = sales_invoice_pdf(invoice)
        assert_is_a_pdf(pdf)
        assert "Al-Noor Distributors" in text_of(pdf), "the name still prints beside the logo"
        # An embedded image object, which is only there if the file was read.
        assert b"/Subtype /Image" in pdf or b"/Subtype/Image" in pdf

    def test_the_logo_makes_room_for_itself_in_a_sparse_header(self, tmp_path, settings):
        """A company with a logo, a name and nothing else.

        With an address and a tax line the text block is already taller than the
        logo and the logo changes nothing. Strip those away and the logo is what
        sets the depth — and if the header did not count it, it would print
        through the first row of the table.
        """
        from PIL import Image

        from apps.reports.pdf.blocks import header_depth

        settings.MEDIA_ROOT = str(tmp_path)
        bare = CompanyProfile.get()
        bare.name = "Al-Noor Distributors"
        bare.address = bare.phone = bare.email = bare.ntn = bare.strn = ""
        bare.save()

        meta = [("Document", "SI-2026-000001")]
        without = header_depth(bare, meta)

        (tmp_path / "company").mkdir()
        Image.new("RGB", (600, 200), "white").save(tmp_path / "company" / "logo.png")
        bare.logo.name = "company/logo.png"
        bare.save()

        assert header_depth(bare, meta) > without


# ===========================================================================
# The two output paths
# ===========================================================================
@pytest.fixture
def staff_client(client, django_user_model, db):
    """An accountant, who can open every screen these two paths print from.

    The group rather than a hand-built set: these tests are about what reaches
    paper, and who may reach the screen is asserted in
    ``tests/test_permissions.py``.
    """
    user = django_user_model.objects.create_user(username="counter", password="x", is_staff=True)
    join_group(user, "Accountant")
    client.force_login(user)
    return client


class TestFormatPdfEndpoint:
    def test_a_sales_invoice_streams_as_a_pdf(self, staff_client, invoice, profile):
        url = reverse("sales:detail", kwargs={"slug": "invoices", "pk": invoice.pk})
        response = staff_client.get(url, {"format": "pdf"})

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert invoice.code in response["Content-Disposition"]
        assert response["Content-Disposition"].startswith("inline")
        assert_is_a_pdf(response.content)

    def test_download_asks_for_the_file_instead_of_the_viewer(self, staff_client, invoice, profile):
        url = reverse("sales:detail", kwargs={"slug": "invoices", "pk": invoice.pk})
        response = staff_client.get(url, {"format": "pdf", "download": "1"})
        assert response["Content-Disposition"].startswith("attachment")

    def test_without_the_flag_the_screen_is_html(self, staff_client, invoice, profile):
        url = reverse("sales:detail", kwargs={"slug": "invoices", "pk": invoice.pk})
        response = staff_client.get(url)
        assert "text/html" in response["Content-Type"]

    def test_a_purchase_invoice_streams_as_a_pdf(self, staff_client, purchase, profile):
        url = reverse("purchasing:detail", kwargs={"slug": "invoices", "pk": purchase.pk})
        response = staff_client.get(url, {"format": "pdf"})
        assert response["Content-Type"] == "application/pdf"
        assert_is_a_pdf(response.content)

    def test_a_receipt_streams_and_takes_a_layout(self, staff_client, receipt, profile):
        from reportlab.lib.units import mm

        url = reverse("payments:detail", kwargs={"pk": receipt.pk})
        response = staff_client.get(url, {"format": "pdf", "layout": "80mm"})

        assert response["Content-Type"] == "application/pdf"
        box = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", response.content)
        assert abs(float(box.group(1)) - 72 * mm) < 1

    def test_the_filename_carries_the_document_code(self, staff_client, invoice, profile):
        url = reverse("sales:detail", kwargs={"slug": "invoices", "pk": invoice.pk})
        disposition = staff_client.get(url, {"format": "pdf"})["Content-Disposition"]
        assert disposition.endswith('.pdf"')
        assert "/" not in disposition.split("filename=")[1], "a code with a slash became a path"


class TestTheBrowserPrintPath:
    """The fast path: Ctrl+P on the screen, no PDF step.

    It is the one the counter uses a hundred times a day, so what it prints has
    to be a bill — which means the screen has to carry a letterhead, a total and
    a signature line that are hidden until it is printed, and has to hide every
    control that is not part of the document.
    """

    def test_the_screen_carries_a_print_letterhead(self, staff_client, invoice, profile):
        url = reverse("sales:detail", kwargs={"slug": "invoices", "pk": invoice.pk})
        body = staff_client.get(url).content.decode()

        assert "print-header" in body
        assert "Al-Noor Distributors" in body
        assert "NTN 1234567-8" in body

    def test_the_screen_carries_a_print_only_total_and_the_amount_in_words(
        self, staff_client, invoice, profile
    ):
        """The on-screen totals live in the posting strip, which is `.no-print`."""
        from apps.core.words import amount_in_words

        url = reverse("sales:detail", kwargs={"slug": "invoices", "pk": invoice.pk})
        body = staff_client.get(url).content.decode()

        assert "print-only" in body
        assert "Amount in words:" in body
        assert amount_in_words(invoice.total_paisa) in body
        assert "Payment due within 15 days" in body, "the terms print too"

    def test_the_workspace_controls_are_marked_no_print(self, staff_client, invoice, profile):
        url = reverse("sales:detail", kwargs={"slug": "invoices", "pk": invoice.pk})
        body = staff_client.get(url).content.decode()

        assert "no-print" in body
        # The posting strip carries the Post button and the ledger preview.
        assert 'class="no-print sticky' in body

    def test_both_routes_to_paper_are_offered(self, staff_client, invoice, profile):
        url = reverse("sales:detail", kwargs={"slug": "invoices", "pk": invoice.pk})
        body = staff_client.get(url).content.decode()

        assert "window.print()" in body, "the fast path must be one click"
        assert "?format=pdf" in body

    def test_the_stylesheet_actually_defines_the_print_rules(self):
        """A `print-only` class with no @media print rule behind it prints nothing."""
        from pathlib import Path

        from django.conf import settings

        css = (Path(settings.BASE_DIR) / "static" / "dist" / "app.css").read_text()
        assert "@media print" in css
        assert "print-only" in css
        assert "no-print" in css
        assert "doc-watermark" in css

    def test_the_payment_screen_offers_the_till_roll(self, staff_client, receipt, profile):
        url = reverse("payments:detail", kwargs={"pk": receipt.pk})
        body = staff_client.get(url).content.decode()
        assert "layout=80mm" in body


class TestReportEndpoints:
    def test_the_client_statement(self, staff_client, receipt, shop, profile):
        response = staff_client.get(
            reverse("reports:client-ledger", kwargs={"pk": shop.pk}),
            {"from": "2026-04-01", "to": "2026-06-30"},
        )
        assert response.status_code == 200
        assert_is_a_pdf(response.content)
        assert "statement" in response["Content-Disposition"]

    def test_a_bad_date_is_refused_rather_than_silently_ignored(self, staff_client, shop, profile):
        response = staff_client.get(
            reverse("reports:client-ledger", kwargs={"pk": shop.pk}), {"from": "01-04-2026"}
        )
        assert response.status_code == 404

    def test_a_backwards_period_is_refused(self, staff_client, shop, profile):
        response = staff_client.get(
            reverse("reports:client-ledger", kwargs={"pk": shop.pk}),
            {"from": "2026-06-30", "to": "2026-04-01"},
        )
        assert response.status_code == 404

    def test_the_route_day_sheet(self, staff_client, invoice, route, profile):
        response = staff_client.get(
            reverse("reports:route-day-sheet", kwargs={"pk": route.pk}), {"date": "2026-06-30"}
        )
        assert response.status_code == 200
        assert_is_a_pdf(response.content)

    def test_both_need_a_login(self, client, shop, route):
        for url in (
            reverse("reports:client-ledger", kwargs={"pk": shop.pk}),
            reverse("reports:route-day-sheet", kwargs={"pk": route.pk}),
        ):
            assert client.get(url).status_code == 302


# ===========================================================================
# Fonts and theme
# ===========================================================================
class TestFonts:
    def test_it_falls_back_to_a_built_in_face_when_nothing_is_vendored(self):
        """No webfont ships with this installation yet — see apps/reports/pdf/fonts.py."""
        from apps.reports.pdf.fonts import fonts

        face = fonts()
        assert face.body and face.mono
        if not face.is_vendored:
            assert face.body == "Helvetica"
            assert face.mono == "Courier", "amounts must be monospaced whatever happens"

    def test_the_urdu_line_is_only_printed_when_a_font_can_draw_it(self, invoice, profile):
        """A row of empty boxes on a bill is worse than one language."""
        from apps.core.words import amount_in_words_urdu
        from apps.reports.pdf.fonts import fonts

        text = text_of(sales_invoice_pdf(invoice))
        if not fonts().has_urdu:
            assert amount_in_words_urdu(invoice.total_paisa) not in text

    def test_no_font_is_ever_fetched_from_a_network(self):
        """CLAUDE.md §7. The font module reads a directory and nothing else."""
        from pathlib import Path

        from django.conf import settings

        source = (Path(settings.BASE_DIR) / "apps" / "reports" / "pdf" / "fonts.py").read_text()
        for banned in ("http://", "https://", "urlopen", "requests."):
            assert banned not in source, f"fonts.py must not reach the network ({banned})"


class TestHeaderGeometry:
    """The letterhead must never print through the first rows of the table.

    The header grows with the number of meta pairs a document type carries, and
    a sales invoice carries seven of them. A fixed top margin fits three.
    """

    @pytest.mark.parametrize("count", [1, 3, 5, 8, 12])
    def test_the_content_frame_always_starts_below_the_header_rule(self, count, profile):
        from apps.reports.pdf.base import PDFDocument
        from apps.reports.pdf.blocks import header_depth

        meta = [(f"Label {index}", f"Value {index}") for index in range(count)]
        pdf = PDFDocument(title="t", header_title="TEST", header_meta=meta, profile=profile)

        rule_from_top = header_depth(profile, meta)
        assert pdf.topMargin >= rule_from_top, (
            f"with {count} meta pairs the header reaches {rule_from_top / 2.83:.1f}mm "
            f"but the content starts at {pdf.topMargin / 2.83:.1f}mm — they overlap"
        )

    def test_a_real_sales_invoice_leaves_room_for_its_own_header(self, invoice, profile):
        """Seven meta pairs: document, date, client, route, booker, due, status."""
        from apps.reports.pdf.base import PDFDocument
        from apps.reports.pdf.blocks import header_depth
        from apps.reports.pdf.documents import _meta_pairs

        meta = _meta_pairs(invoice, party_label="Client", party_name=invoice.client.name)
        assert len(meta) >= 4
        pdf = PDFDocument(title="t", header_title="TEST", header_meta=meta, profile=profile)
        assert pdf.topMargin >= header_depth(profile, meta)

    def test_the_first_row_of_the_table_is_below_the_rule_on_the_page(self, invoice, profile):
        """Checked on the drawn page, not just in the arithmetic.

        The header rule is the lowest horizontal line the header draws; the
        table's own rules are below it. If the two orders ever swapped, the
        letterhead would be sitting on top of the lines.
        """
        pdf = sales_invoice_pdf(invoice)
        y_values = []
        for chunk in _content_streams(pdf):
            # "x1 y1 m x2 y2 l" — a straight line in the content stream.
            for match in re.finditer(rb"([\d.]+) ([\d.]+) m ([\d.]+) ([\d.]+) l", chunk):
                y_values.append(float(match.group(2)))

        assert y_values, "no rules were drawn at all"
        from reportlab.lib.pagesizes import A4

        from apps.reports.pdf.blocks import header_depth
        from apps.reports.pdf.documents import _meta_pairs

        meta = _meta_pairs(invoice, party_label="Client", party_name=invoice.client.name)
        rule_y = A4[1] - header_depth(profile, meta)
        below = [y for y in y_values if y < rule_y - 1]
        assert below, "nothing was drawn below the header rule — the content is behind it"


class TestTheme:
    def test_the_alarm_colour_matches_the_screen(self):
        """--color-cancelled in static/src/css/app.css, converted to sRGB."""
        from apps.reports.pdf.theme import ALARM

        assert ALARM.hexval() == "0xbd413f"

    def test_every_receipt_layout_can_actually_be_rendered(self, receipt, profile):
        from apps.reports.pdf.theme import RECEIPT_LAYOUTS

        for layout in RECEIPT_LAYOUTS:
            assert_is_a_pdf(payment_receipt_pdf(receipt, layout=layout))
