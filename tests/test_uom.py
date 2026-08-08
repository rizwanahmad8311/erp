"""Unit conversion: ``to_base``, ``from_base`` and ``fmt_qty``.

This is the arithmetic that turns what an operator typed into the integer a
stock row stores, and the integer back into what a picker reads. Two cases carry
most of the weight and get most of the tests:

* **carton_size = 1** — an item that is not cartoned at all. Every naive
  implementation divides by one, gets a clean answer, and prints "17 ctn" for
  seventeen 25kg rice bags.
* **remainders** — 41 pieces of a 12-pack is three cartons and five loose, and
  the five must never be dropped. Quantities have no rounding site; the
  remainder is carried, exactly, in both directions and in both signs.
"""

import pytest

from apps.masters.enums import Unit
from apps.masters.exceptions import InvalidPacking, InvalidQuantity, UnknownUnit
from apps.masters.models import Item
from apps.masters.services import fmt_qty, from_base, to_base, unit_factor

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Items under test
# ---------------------------------------------------------------------------
@pytest.fixture
def carton12(db):
    """The ordinary case: pieces that come twelve to a carton."""
    return Item.objects.create(code="OIL-1000", name="Cooking Oil 1L", carton_size=12)


@pytest.fixture
def carton24(db):
    return Item.objects.create(code="TEA-190", name="Tea 190g", carton_size=24)


@pytest.fixture
def carton48(db):
    return Item.objects.create(code="SOAP-LUX", name="Lux Soap 100g", carton_size=48)


@pytest.fixture
def loose(db):
    """The case everything gets wrong: sold singly, no carton at all."""
    return Item.objects.create(code="RICE-25", name="Basmati Rice 25kg Bag", carton_size=1)


@pytest.fixture
def by_carton(db):
    """Counted in cartons: the carton IS the base unit, so there is no piece."""
    return Item.objects.create(
        code="WATER-PALLET",
        name="Bottled Water, sealed carton",
        base_unit=Unit.CARTON,
        carton_size=1,
    )


# ---------------------------------------------------------------------------
# allows_carton
# ---------------------------------------------------------------------------
class TestAllowsCarton:
    """The one question a quantity widget should ask before offering CARTON."""

    def test_true_when_the_carton_holds_more_than_one(self, carton12):
        assert carton12.allows_carton is True

    def test_false_at_carton_size_one(self, loose):
        assert loose.allows_carton is False

    def test_false_for_an_item_counted_in_cartons(self, by_carton):
        """The carton is already the base unit; there is no second level."""
        assert by_carton.allows_carton is False


# ---------------------------------------------------------------------------
# to_base
# ---------------------------------------------------------------------------
class TestToBase:
    def test_pieces_pass_through_unchanged(self, carton12):
        assert to_base(carton12, 5, Unit.PIECE) == 5

    def test_defaults_to_pieces(self, carton12):
        assert to_base(carton12, 5) == 5

    @pytest.mark.parametrize(
        ("fixture", "qty", "expected"),
        [
            ("carton12", 3, 36),
            ("carton24", 3, 72),
            ("carton48", 3, 144),
            ("carton12", 1, 12),
            ("carton48", 10, 480),
        ],
    )
    def test_cartons_multiply_by_the_packing(self, request, fixture, qty, expected):
        item = request.getfixturevalue(fixture)
        assert to_base(item, qty, Unit.CARTON) == expected

    def test_zero_is_zero_in_either_unit(self, carton12):
        assert to_base(carton12, 0, Unit.CARTON) == 0
        assert to_base(carton12, 0, Unit.PIECE) == 0

    def test_negative_quantities_convert_the_same_way(self, carton12):
        """A return of two cartons is minus twenty-four pieces, not an error.

        Direction is the caller's business — the conversion does not care which
        way stock is moving.
        """
        assert to_base(carton12, -2, Unit.CARTON) == -24
        assert to_base(carton12, -5, Unit.PIECE) == -5

    def test_units_are_case_insensitive_and_stripped(self, carton12):
        """This is a boundary: "carton" out of a CSV import means CARTON."""
        assert to_base(carton12, 2, "carton") == 24
        assert to_base(carton12, 2, "  Carton ") == 24
        assert to_base(carton12, 2, "piece") == 2

    # -- carton_size = 1 ---------------------------------------------------
    def test_loose_item_is_unchanged_by_either_unit(self, loose):
        """A carton of one is arithmetically unambiguous, so CARTON is allowed
        and is worth 1. The distinction that matters is a *display* one, and
        from_base is where it is enforced."""
        assert to_base(loose, 17, Unit.PIECE) == 17
        assert to_base(loose, 17, Unit.CARTON) == 17

    # -- rejections --------------------------------------------------------
    @pytest.mark.parametrize("qty", [2.5, 3.0, "3", None, [3]])
    def test_a_quantity_that_is_not_a_whole_int_is_refused(self, carton12, qty):
        """Including 3.0, which is a whole number and still a float. There is no
        fractional quantity, so there is no reason for a float to be here at
        all, and accepting the ones that happen to be whole invites the ones
        that are not."""
        with pytest.raises(InvalidQuantity):
            to_base(carton12, qty, Unit.PIECE)

    def test_a_bool_is_not_a_quantity(self, carton12):
        """``True`` is an int subclass. One piece is never what was meant."""
        with pytest.raises(InvalidQuantity):
            to_base(carton12, True, Unit.PIECE)

    @pytest.mark.parametrize("unit", ["BOX", "DOZEN", "", "kg", 12, None])
    def test_an_unknown_unit_is_refused_rather_than_guessed(self, carton12, unit):
        with pytest.raises(UnknownUnit):
            to_base(carton12, 1, unit)

    def test_pieces_of_a_carton_item_are_refused(self, by_carton):
        """A fraction of a stored unit is not a quantity (CLAUDE.md §2)."""
        with pytest.raises(UnknownUnit):
            to_base(by_carton, 5, Unit.PIECE)

    def test_cartons_of_a_carton_item_are_the_base_unit(self, by_carton):
        assert to_base(by_carton, 5, Unit.CARTON) == 5

    def test_a_carton_size_below_one_is_refused_before_anything_divides(self):
        """Unsaved and hand-built: the model and a CHECK constraint both refuse
        to store this, so the helper is the last line of defence against a
        division by zero halfway through a posting."""
        broken = Item(code="BROKEN", name="Impossible packing", carton_size=0)
        with pytest.raises(InvalidPacking):
            to_base(broken, 1, Unit.CARTON)
        with pytest.raises(InvalidPacking):
            from_base(broken, 1)


class TestUnitFactor:
    """``to_base`` is a multiplication by this; a few direct cases pin it."""

    def test_base_unit_is_always_one(self, carton12, loose, by_carton):
        assert unit_factor(carton12, Unit.PIECE) == 1
        assert unit_factor(loose, Unit.PIECE) == 1
        assert unit_factor(by_carton, Unit.CARTON) == 1

    def test_carton_is_the_packing(self, carton24):
        assert unit_factor(carton24, Unit.CARTON) == 24


# ---------------------------------------------------------------------------
# from_base
# ---------------------------------------------------------------------------
class TestFromBase:
    @pytest.mark.parametrize(
        ("qty_base", "expected"),
        [
            (0, (0, 0)),
            (5, (0, 5)),  # under a carton
            (11, (0, 11)),  # one short of a carton
            (12, (1, 0)),  # exactly one
            (17, (1, 5)),  # the remainder case
            (24, (2, 0)),
            (41, (3, 5)),
            (143, (11, 11)),  # one short of a round dozen dozen
        ],
    )
    def test_remainders_are_carried_not_dropped(self, carton12, qty_base, expected):
        assert from_base(carton12, qty_base) == expected

    @pytest.mark.parametrize("qty_base", [0, 1, 11, 12, 17, 41, 143, 1000, -1, -17, -144])
    def test_the_split_always_adds_back_up(self, carton12, qty_base):
        """The invariant the whole module rests on. If this ever fails, a
        delivery note and the stock ledger disagree about what left the godown.
        """
        cartons, loose_pieces = from_base(carton12, qty_base)
        assert cartons * carton12.carton_size + loose_pieces == qty_base

    @pytest.mark.parametrize(
        ("qty_base", "expected"),
        [
            (-5, (0, -5)),
            (-12, (-1, 0)),
            (-17, (-1, -5)),
            (-41, (-3, -5)),
        ],
    )
    def test_negatives_split_by_magnitude_not_by_floor(self, carton12, qty_base, expected):
        """Python floors, so ``divmod(-17, 12)`` is ``(-2, 7)``. Nobody means
        "minus two cartons plus seven loose" by minus seventeen pieces."""
        assert from_base(carton12, qty_base) == expected

    # -- carton_size = 1 ---------------------------------------------------
    @pytest.mark.parametrize("qty_base", [0, 1, 17, 250, -17])
    def test_a_loose_item_never_reports_cartons(self, loose, qty_base):
        """Dividing by one would say ``(17, 0)`` — seventeen cartons of a 25kg
        bag — and someone would load seventeen pallets."""
        assert from_base(loose, qty_base) == (0, qty_base)

    def test_the_invariant_still_holds_at_carton_size_one(self, loose):
        cartons, loose_pieces = from_base(loose, 17)
        assert cartons * loose.carton_size + loose_pieces == 17

    def test_an_item_counted_in_cartons_reports_no_split_either(self, by_carton):
        assert from_base(by_carton, 17) == (0, 17)

    def test_round_trips_through_to_base(self, carton12):
        """``to_base`` and ``from_base`` are inverses across the carton boundary."""
        for cartons, pieces in [(0, 0), (0, 7), (3, 0), (3, 5), (12, 11)]:
            qty_base = to_base(carton12, cartons, Unit.CARTON) + to_base(
                carton12, pieces, Unit.PIECE
            )
            assert from_base(carton12, qty_base) == (cartons, pieces)

    @pytest.mark.parametrize("qty_base", [2.5, "17", None, True])
    def test_a_fractional_or_non_int_quantity_is_refused(self, carton12, qty_base):
        with pytest.raises(InvalidQuantity):
            from_base(carton12, qty_base)


# ---------------------------------------------------------------------------
# fmt_qty
# ---------------------------------------------------------------------------
class TestFmtQty:
    @pytest.mark.parametrize(
        ("qty_base", "expected"),
        [
            (0, "0 pcs"),
            (1, "1 pcs"),
            (5, "5 pcs"),
            (11, "11 pcs"),
            (12, "1 ctn"),
            (17, "1 ctn + 5 pcs"),
            (24, "2 ctn"),
            (41, "3 ctn + 5 pcs"),
            (143, "11 ctn + 11 pcs"),
        ],
    )
    def test_cartons_and_the_remainder(self, carton12, qty_base, expected):
        assert fmt_qty(carton12, qty_base) == expected

    def test_a_whole_number_of_cartons_omits_the_pieces(self, carton24):
        assert fmt_qty(carton24, 72) == "3 ctn"

    def test_under_a_carton_omits_the_cartons(self, carton48):
        assert fmt_qty(carton48, 7) == "7 pcs"

    # -- carton_size = 1 ---------------------------------------------------
    @pytest.mark.parametrize(
        ("qty_base", "expected"),
        [(0, "0 pcs"), (1, "1 pcs"), (17, "17 pcs"), (250, "250 pcs")],
    )
    def test_a_loose_item_is_always_plain_pieces(self, loose, qty_base, expected):
        """Never "17 ctn", and never "17 ctn + 0 pcs"."""
        assert fmt_qty(loose, qty_base) == expected

    def test_an_item_counted_in_cartons_reads_in_cartons(self, by_carton):
        assert fmt_qty(by_carton, 17) == "17 ctn"

    # -- sign --------------------------------------------------------------
    @pytest.mark.parametrize(
        ("qty_base", "expected"),
        [(-5, "-5 pcs"), (-12, "-1 ctn"), (-17, "-1 ctn + 5 pcs"), (-41, "-3 ctn + 5 pcs")],
    )
    def test_one_minus_sign_in_front_of_the_whole_quantity(self, carton12, qty_base, expected):
        """ "-3 ctn + -5 pcs" reads as a subtraction that it is not."""
        assert fmt_qty(carton12, qty_base) == expected

    def test_a_fractional_quantity_is_refused(self, carton12):
        with pytest.raises(InvalidQuantity):
            fmt_qty(carton12, 2.5)


# ---------------------------------------------------------------------------
# The packing rules the conversion depends on
# ---------------------------------------------------------------------------
class TestPackingIsGuardedAtTheModel:
    """``services`` may assume a sane ``carton_size`` because ``Item`` enforces one."""

    @pytest.mark.parametrize("carton_size", [0, -1])
    def test_a_carton_must_hold_at_least_one_base_unit(self, carton_size):
        with pytest.raises(InvalidPacking):
            Item.objects.create(code="BAD-1", name="Bad packing", carton_size=carton_size)

    def test_an_item_counted_in_cartons_must_have_a_carton_size_of_one(self):
        """Otherwise carton_size names a packing level with no unit to count it in."""
        with pytest.raises(InvalidPacking):
            Item.objects.create(
                code="BAD-2",
                name="Cartons of cartons",
                base_unit=Unit.CARTON,
                carton_size=12,
            )

    def test_the_default_item_is_a_loose_piece(self):
        """A code and a name is all the stock ledger has ever needed. The
        defaults must keep that call site working and must not invent packing.
        """
        item = Item.objects.create(code="PLAIN", name="Just a thing")
        assert item.base_unit == Unit.PIECE
        assert item.carton_size == 1
        assert item.allows_carton is False
        assert fmt_qty(item, 17) == "17 pcs"
