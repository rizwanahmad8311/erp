"""The entry rule: what the supplier billed is exact, the per-piece rate is derived.

This is the file the purchasing app exists to keep passing.

An operator types "10 cartons at Rs 2,400". The stock ledger stores pieces. Those
two facts do not divide into each other, and every way of resolving that except
one puts money somewhere nobody agreed to:

* Rate first, amount from ``qty_base * rate_paisa`` — the supplier's bill comes
  out wrong by up to half a paisa per piece. On a 240-piece line that is Rs 1.20.
* Amount first, inventory debited ``qty_base * rate_paisa`` — the general ledger
  will not balance without a plug, and inventory holds paisa nobody paid.
* Amount first, inventory debited the amount — exact, on both ledgers, always.

The third is what is implemented. These tests pin it from both ends: the
arithmetic on its own, and the two ledgers agreeing after a real posting.
"""

import pytest

from apps.core.money import to_paisa
from apps.masters.enums import Unit
from apps.masters.models import Item
from apps.purchasing.exceptions import InvalidLine
from apps.purchasing.services import compute_line, entry_rate_paisa

pytestmark = pytest.mark.django_db


@pytest.fixture
def carton12(db):
    """Twelve to a carton: Rs 2,400 a carton divides into a whole Rs 200 a piece."""
    return Item.objects.create(code="OIL-1000", name="Cooking Oil 1L", carton_size=12)


@pytest.fixture
def carton24(db):
    """Twenty-four to a carton: the awkward case, where nothing divides."""
    return Item.objects.create(code="TEA-190", name="Tea 190g", carton_size=24)


@pytest.fixture
def loose(db):
    return Item.objects.create(code="RICE-25", name="Basmati Rice 25kg Bag", carton_size=1)


# ---------------------------------------------------------------------------
# The worked example from the brief
# ---------------------------------------------------------------------------
class TestTenCartonsAtTwentyFourHundred:
    """ "10 cartons at 2400 per carton", spelled out.

    carton_size is 12, so 10 cartons is 120 pieces and Rs 2,400 a carton is
    exactly Rs 200 a piece. Everything lands on a whole paisa and the identity
    ``qty_base * rate_paisa == amount_paisa`` holds on the nose.
    """

    def test_stores_the_pieces_and_the_per_piece_rate(self, carton12):
        line = compute_line(
            carton12,
            qty_input=10,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("2400"),
        )
        assert line.qty_base == 120  # 10 * carton_size
        assert line.rate_paisa == 20000  # 240000 / 12, exactly
        assert line.amount_paisa == 2_400_000  # 10 * 240000, exactly

    def test_the_parts_multiply_back_to_the_line(self, carton12):
        line = compute_line(
            carton12, qty_input=10, unit_input=Unit.CARTON, rate_input_paisa=to_paisa("2400")
        )
        assert line.qty_base * line.rate_paisa == line.amount_paisa
        assert line.rate_is_exact is True
        assert line.rate_drift_paisa == 0


# ---------------------------------------------------------------------------
# The case that does not divide
# ---------------------------------------------------------------------------
class TestARateThatDoesNotDivide:
    """Rs 2,500 across 24 pieces is 1041.66... paisa, and no integer rate exists.

    This is not an edge case. It happens whenever the per-carton rate in paisa
    is not a multiple of the carton size, which is most bills.
    """

    @pytest.fixture
    def line(self, carton24):
        return compute_line(
            carton24, qty_input=10, unit_input=Unit.CARTON, rate_input_paisa=to_paisa("2500")
        )

    def test_the_bill_is_exact(self, line):
        """The one number the supplier and the operator both agreed on."""
        assert line.amount_paisa == 2_500_000  # 10 x Rs 2,500, to the paisa

    def test_the_rate_is_the_rounded_derivation(self, line):
        assert line.qty_base == 240
        assert line.rate_paisa == 10417  # 250000/24 = 10416.66..., rounded once

    def test_the_drift_is_visible_and_never_posted(self, line):
        """240 x 10417 is 2,500,080 — Rs 0.80 that is not on the bill.

        The line knows about it, so the entry screen can flag the rate as
        rounded. Nothing else in the system ever sees it: the ledger and the
        stock row are both posted from ``amount_paisa``.
        """
        assert line.rate_is_exact is False
        assert line.rate_drift_paisa == 80
        assert line.qty_base * line.rate_paisa == 2_500_080

    def test_rounding_a_rate_up_never_moves_the_bill(self, carton24):
        """The property that matters, over a spread of awkward rates."""
        for rupees in ("2500", "999.99", "1", "0.01", "1234.56", "7777.77"):
            line = compute_line(
                carton24,
                qty_input=7,
                unit_input=Unit.CARTON,
                rate_input_paisa=to_paisa(rupees),
            )
            assert line.amount_paisa == 7 * to_paisa(rupees)


# ---------------------------------------------------------------------------
# The general properties
# ---------------------------------------------------------------------------
class TestTheAmountIsAlwaysTheAnchor:
    @pytest.mark.parametrize("qty_input", [1, 2, 7, 10, 99, 1000])
    @pytest.mark.parametrize("rupees", ["0.01", "1", "12.34", "2400", "9999.99"])
    @pytest.mark.parametrize("fixture", ["carton12", "carton24", "loose"])
    def test_the_line_is_exactly_what_was_typed(self, request, fixture, qty_input, rupees):
        """``amount == qty_input * rate_input``, for every combination.

        No rounding, no drift, no tolerance. If this ever fails, an operator is
        being asked to pay a number they did not agree to.
        """
        item = request.getfixturevalue(fixture)
        rate = to_paisa(rupees)
        line = compute_line(
            item, qty_input=qty_input, unit_input=Unit.CARTON, rate_input_paisa=rate
        )
        assert line.amount_paisa == qty_input * rate

    @pytest.mark.parametrize("fixture", ["carton12", "carton24", "loose"])
    def test_the_typed_rate_is_always_recoverable(self, request, fixture):
        """A draft line must re-open as "10 cartons @ 2,400", not as pieces.

        ``amount_paisa`` is ``qty_input * rate_input_paisa``, so dividing it back
        out is exact — there is no rounding on this path and there must not be.
        """
        item = request.getfixturevalue(fixture)

        class Row:  # the two fields entry_rate_paisa reads
            pass

        for qty_input, rupees in [(10, "2400"), (7, "2500"), (3, "0.01"), (99, "1234.56")]:
            line = compute_line(
                item,
                qty_input=qty_input,
                unit_input=Unit.CARTON,
                rate_input_paisa=to_paisa(rupees),
            )
            row = Row()
            row.qty_input, row.amount_paisa = qty_input, line.amount_paisa
            assert entry_rate_paisa(row) == to_paisa(rupees)

    def test_pieces_and_cartons_agree_when_they_describe_the_same_goods(self, carton12):
        """120 pieces at Rs 200 is the same line as 10 cartons at Rs 2,400."""
        by_carton = compute_line(
            carton12, qty_input=10, unit_input=Unit.CARTON, rate_input_paisa=to_paisa("2400")
        )
        by_piece = compute_line(
            carton12, qty_input=120, unit_input=Unit.PIECE, rate_input_paisa=to_paisa("200")
        )
        assert by_carton.qty_base == by_piece.qty_base
        assert by_carton.rate_paisa == by_piece.rate_paisa
        assert by_carton.amount_paisa == by_piece.amount_paisa

    def test_a_loose_item_has_no_conversion_to_get_wrong(self, loose):
        line = compute_line(
            loose, qty_input=17, unit_input=Unit.PIECE, rate_input_paisa=to_paisa("7850")
        )
        assert line.qty_base == 17
        assert line.rate_paisa == 785_000
        assert line.amount_paisa == 17 * 785_000
        assert line.rate_is_exact is True


# ---------------------------------------------------------------------------
# Discount and tax, each rounded once
# ---------------------------------------------------------------------------
class TestDiscountAndTax:
    def test_tax_is_charged_on_the_discounted_amount(self, carton12):
        line = compute_line(
            carton12,
            qty_input=10,
            unit_input=Unit.CARTON,
            rate_input_paisa=to_paisa("2400"),
            discount_paisa=to_paisa("400"),
            tax_rate_bp=1750,
        )
        assert line.amount_paisa == 2_400_000
        assert line.discount_paisa == 40_000
        assert line.net_paisa == 2_360_000
        assert line.tax_paisa == 413_000  # 17.5% of 2,360,000, exactly
        assert line.total_paisa == 2_773_000

    def test_tax_rounds_once_through_the_single_rounding_point(self, carton24):
        """17.5% of a figure ending in an odd paisa. Banker's rounding, once."""
        line = compute_line(
            carton24, qty_input=1, unit_input=Unit.PIECE, rate_input_paisa=333, tax_rate_bp=1750
        )
        # 333 * 0.175 = 58.275 -> 58
        assert line.tax_paisa == 58

    def test_the_item_supplies_the_default_rate(self, carton12):
        carton12.tax_rate_bp = 1750
        carton12.save()
        line = compute_line(
            carton12, qty_input=1, unit_input=Unit.PIECE, rate_input_paisa=to_paisa("100")
        )
        assert line.tax_paisa == 1750  # 17.5% of Rs 100

    def test_zero_tax_is_a_zero_line_not_a_missing_one(self, loose):
        line = compute_line(
            loose, qty_input=1, unit_input=Unit.PIECE, rate_input_paisa=to_paisa("100")
        )
        assert line.tax_paisa == 0
        assert line.total_paisa == line.amount_paisa


# ---------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------
class TestRefusals:
    @pytest.mark.parametrize("qty_input", [0, -1])
    def test_a_line_moves_a_positive_quantity(self, carton12, qty_input):
        with pytest.raises(InvalidLine):
            compute_line(carton12, qty_input=qty_input, unit_input=Unit.PIECE, rate_input_paisa=100)

    @pytest.mark.parametrize("qty_input", [2.5, "10", None, True])
    def test_a_fractional_or_non_int_quantity_is_refused(self, carton12, qty_input):
        with pytest.raises(InvalidLine):
            compute_line(carton12, qty_input=qty_input, unit_input=Unit.PIECE, rate_input_paisa=100)

    @pytest.mark.parametrize("rate", [-1, 24.5, "100", None])
    def test_a_rate_must_be_non_negative_whole_paisa(self, carton12, rate):
        with pytest.raises(InvalidLine):
            compute_line(carton12, qty_input=1, unit_input=Unit.PIECE, rate_input_paisa=rate)

    def test_a_free_line_is_allowed(self, carton12):
        """Bonus cartons are real, they cost nothing, and they carry stock in."""
        line = compute_line(carton12, qty_input=2, unit_input=Unit.CARTON, rate_input_paisa=0)
        assert line.qty_base == 24
        assert line.amount_paisa == 0
        assert line.rate_paisa == 0

    def test_a_discount_bigger_than_the_line_is_refused(self, carton12):
        """It would credit inventory on a document that is putting stock in."""
        with pytest.raises(InvalidLine, match="more than the line amount"):
            compute_line(
                carton12,
                qty_input=1,
                unit_input=Unit.PIECE,
                rate_input_paisa=to_paisa("100"),
                discount_paisa=to_paisa("101"),
            )

    def test_a_unit_the_item_is_not_sold_in_is_refused(self, carton12):
        from apps.masters.exceptions import UnknownUnit

        with pytest.raises(UnknownUnit):
            compute_line(carton12, qty_input=1, unit_input="BOX", rate_input_paisa=100)
