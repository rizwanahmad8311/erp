"""Template filters: |money, |qty, |doc_status."""

import pytest
from django.template import Context, Template

from apps.core.enums import DocumentStatus


def render(template_string: str, **context) -> str:
    return Template("{% load core_tags %}" + template_string).render(Context(context))


class TestMoneyFilter:
    @pytest.mark.parametrize(
        ("paisa", "expected"),
        [
            (0, "0.00"),
            (5, "0.05"),
            (12345, "123.45"),
            (123450, "1,234.50"),
            (-123450, "-1,234.50"),
            (123456789, "1,234,567.89"),
        ],
    )
    def test_formats_paisa(self, paisa, expected):
        assert render("{{ v|money }}", v=paisa) == expected

    def test_blank_stays_blank(self):
        """A blank cell must not read as a real zero balance."""
        assert render("{{ v|money }}", v=None) == ""
        assert render("{{ v|money }}", v="") == ""
        assert render("{{ v|money }}") == ""

    def test_no_currency_symbol(self):
        assert "Rs" not in render("{{ v|money }}", v=123450)


class TestQtyFilter:
    @pytest.mark.parametrize(
        ("pieces", "expected"),
        [(0, "0"), (7, "7"), (1200, "1,200"), (-12, "-12"), (1234567, "1,234,567")],
    )
    def test_formats_pieces(self, pieces, expected):
        assert render("{{ v|qty }}", v=pieces) == expected

    def test_blank_stays_blank(self):
        assert render("{{ v|qty }}", v=None) == ""

    def test_never_renders_a_decimal_point(self):
        assert "." not in render("{{ v|qty }}", v=1200)


class TestDocStatusFilter:
    @pytest.mark.parametrize(
        ("status", "label"),
        [
            (DocumentStatus.DRAFT, "Draft"),
            (DocumentStatus.POSTED, "Posted"),
            (DocumentStatus.CANCELLED, "Cancelled"),
        ],
    )
    def test_renders_a_badge_with_the_label(self, status, label):
        out = render("{{ v|doc_status }}", v=status)
        assert label in out
        assert out.startswith("<span")

    def test_each_status_gets_its_own_styling(self):
        rendered = {
            s: render("{{ v|doc_status }}", v=s)
            for s in (DocumentStatus.DRAFT, DocumentStatus.POSTED, DocumentStatus.CANCELLED)
        }
        assert len(set(rendered.values())) == 3

    def test_unknown_status_degrades_instead_of_raising(self):
        out = render("{{ v|doc_status }}", v="WEIRD")
        assert "WEIRD" in out
        assert out.startswith("<span")

    def test_empty_status_renders_a_placeholder(self):
        assert "—" in render("{{ v|doc_status }}", v=None)

    def test_output_is_escaped(self):
        """The filter marks its output safe, so its inputs must be escaped."""
        out = render("{{ v|doc_status }}", v="<script>alert(1)</script>")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out
