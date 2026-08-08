"""
Document numbering: format, per-year isolation, and — the point of the whole
exercise — that two simultaneous callers cannot be handed the same number.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection

from apps.core.exceptions import SequenceError
from apps.core.models import DocumentSequence
from apps.core.services import get_next_code, peek_next_code

pytestmark = pytest.mark.django_db


class TestCodeFormat:
    def test_first_code_starts_at_one(self):
        assert get_next_code("SI", 2026) == "SI-2026-000001"

    def test_numbers_increment(self):
        codes = [get_next_code("SI", 2026) for _ in range(3)]
        assert codes == ["SI-2026-000001", "SI-2026-000002", "SI-2026-000003"]

    def test_number_is_six_digits(self):
        DocumentSequence.objects.create(prefix="SI", fiscal_year=2026, last_number=999998)
        assert get_next_code("SI", 2026) == "SI-2026-999999"

    def test_number_widens_rather_than_truncating(self):
        """A million invoices is unlikely, but silently reusing 000001 is fatal."""
        DocumentSequence.objects.create(prefix="SI", fiscal_year=2026, last_number=999999)
        assert get_next_code("SI", 2026) == "SI-2026-1000000"

    def test_prefix_is_normalised(self):
        assert get_next_code("si", 2026) == "SI-2026-000001"
        assert get_next_code(" SI ", 2026) == "SI-2026-000002"

    @pytest.mark.parametrize("prefix", ["", "S", "TOOLONGPREFIX", "S1", "S-I", "si!", None])
    def test_invalid_prefixes_are_rejected(self, prefix):
        with pytest.raises(SequenceError):
            get_next_code(prefix, 2026)

    @pytest.mark.parametrize("year", [1899, 10000, "2026", 20.26, True, None])
    def test_invalid_years_are_rejected(self, year):
        with pytest.raises(SequenceError):
            get_next_code("SI", year)


class TestSequenceIsolation:
    def test_prefixes_are_independent(self):
        assert get_next_code("SI", 2026) == "SI-2026-000001"
        assert get_next_code("PI", 2026) == "PI-2026-000001"
        assert get_next_code("SI", 2026) == "SI-2026-000002"

    def test_fiscal_years_are_independent(self):
        """Numbering restarts each year; it does not run on from the last one."""
        assert get_next_code("SI", 2026) == "SI-2026-000001"
        assert get_next_code("SI", 2027) == "SI-2027-000001"
        assert get_next_code("SI", 2026) == "SI-2026-000002"

    def test_one_row_per_prefix_and_year(self):
        for _ in range(5):
            get_next_code("SI", 2026)
        assert DocumentSequence.objects.filter(prefix="SI", fiscal_year=2026).count() == 1
        assert DocumentSequence.objects.get(prefix="SI", fiscal_year=2026).last_number == 5


class TestPeek:
    def test_peek_does_not_consume(self):
        assert peek_next_code("SI", 2026) == "SI-2026-000001"
        assert peek_next_code("SI", 2026) == "SI-2026-000001"
        assert get_next_code("SI", 2026) == "SI-2026-000001"
        assert peek_next_code("SI", 2026) == "SI-2026-000002"


@pytest.mark.django_db(transaction=True)
class TestConcurrentAllocation:
    """Two simultaneous invoices must not collide.

    These need ``transaction=True`` so each thread gets a real connection
    against the real test database, and config/settings/test.py puts that
    database on disk — an in-memory SQLite database does not reproduce the
    ``BEGIN IMMEDIATE`` write lock that actually serialises production.
    """

    WORKERS = 8

    @staticmethod
    def _allocate(barrier, prefix="SI", year=2026):
        """Called in a worker thread. The barrier makes every thread hit the
        counter at the same instant instead of politely queueing."""
        try:
            barrier.wait(timeout=30)
            return get_next_code(prefix, year)
        finally:
            # Each thread opened its own connection; leaking them wedges teardown.
            connection.close()

    def test_no_two_callers_get_the_same_code(self):
        barrier = threading.Barrier(self.WORKERS)

        with ThreadPoolExecutor(max_workers=self.WORKERS) as pool:
            codes = [
                f.result(timeout=60)
                for f in [pool.submit(self._allocate, barrier) for _ in range(self.WORKERS)]
            ]

        assert len(set(codes)) == self.WORKERS, f"duplicate codes handed out: {sorted(codes)}"
        assert sorted(codes) == [f"SI-2026-{n:06d}" for n in range(1, self.WORKERS + 1)]

    def test_the_counter_matches_what_was_handed_out(self):
        barrier = threading.Barrier(self.WORKERS)

        with ThreadPoolExecutor(max_workers=self.WORKERS) as pool:
            list(pool.map(lambda _: self._allocate(barrier), range(self.WORKERS)))

        sequence = DocumentSequence.objects.get(prefix="SI", fiscal_year=2026)
        assert sequence.last_number == self.WORKERS

    def test_first_use_race_creates_exactly_one_row(self):
        """All threads arrive before the counter row exists.

        Whoever loses the create race must pick up the winner's row, not raise
        and not create a second one — the unique constraint is the backstop.
        """
        barrier = threading.Barrier(self.WORKERS)
        assert not DocumentSequence.objects.filter(prefix="XX").exists()

        with ThreadPoolExecutor(max_workers=self.WORKERS) as pool:
            codes = list(
                pool.map(lambda _: self._allocate(barrier, prefix="XX"), range(self.WORKERS))
            )

        assert DocumentSequence.objects.filter(prefix="XX", fiscal_year=2026).count() == 1
        assert len(set(codes)) == self.WORKERS

    def test_concurrent_allocation_across_prefixes(self):
        """Different prefixes must not block or contaminate each other."""
        prefixes = ["SI", "PI", "CN", "DN"]
        barrier = threading.Barrier(len(prefixes) * 2)

        with ThreadPoolExecutor(max_workers=len(prefixes) * 2) as pool:
            futures = [
                pool.submit(self._allocate, barrier, prefix)
                for prefix in prefixes
                for _ in range(2)
            ]
            codes = [f.result(timeout=60) for f in futures]

        assert len(set(codes)) == len(prefixes) * 2
        for prefix in prefixes:
            issued = sorted(c for c in codes if c.startswith(prefix))
            assert issued == [f"{prefix}-2026-000001", f"{prefix}-2026-000002"]
