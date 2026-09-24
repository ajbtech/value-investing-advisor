"""Finding the filers that stopped filing.

A universe built from `company_tickers.json` has quietly deleted its own failures: the map
lists filers that trade today, and a company that went bankrupt in 2023 is simply not in
it. That is precisely the population a value screen is most likely to have flagged, which
makes survivorship the one bias here that manufactures an edge out of nothing.

The `filer.status` column has been ready for this since the first migration. Nothing ever
set it, because nothing knew which CIKs had died. EDGAR's quarterly form index does:
Form 15 is a company deregistering, Form 25 is a security being delisted.
"""

from pathlib import Path

import pytest

from dossier.deregistrations import (
    TERMINAL_FORMS,
    Deregistration,
    mark_terminal_status,
    parse_form_index,
)
from dossier.store import open_store
from tests.builders import StoreBuilder

FIXTURE = Path(__file__).parent / "fixtures" / "form_2020_QTR1.idx"


@pytest.fixture
def index_text():
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        yield conn


class TestParsingTheIndex:
    def test_it_finds_every_terminal_filing_and_nothing_else(self, index_text):
        found = parse_form_index(index_text)
        assert sorted(d.cik for d in found) == [9876, 12345, 31235, 54321, 77777]

    def test_a_10k_is_not_a_deregistration(self, index_text):
        assert all(d.form != "10-K" for d in parse_form_index(index_text))

    def test_it_keeps_the_form_and_the_date_because_they_mean_different_things(self, index_text):
        by_cik = {d.cik: d for d in parse_form_index(index_text)}
        assert by_cik[9876].form == "15-12B"
        assert by_cik[9876].filed_date == "2020-03-02"
        assert by_cik[77777].form == "25-NSE"

    def test_form_15_deregisters_and_form_25_only_delists_a_security(self, index_text):
        by_cik = {d.cik: d for d in parse_form_index(index_text)}
        assert by_cik[12345].status == "deregistered"
        assert by_cik[31235].status is None

    def test_the_terminal_forms_are_named_rather_than_guessed(self):
        assert set(TERMINAL_FORMS) == {"15-12B", "15-12G", "15F-12B", "15F-12G", "25", "25-NSE"}

    def test_a_header_only_index_yields_nothing(self):
        assert parse_form_index("Form Type Company Name CIK Date Filed File Name\n---\n") == []


class TestMarkingTheStore:
    def test_it_records_the_status_and_the_date(self, store):
        b = StoreBuilder(store)
        b.filer(9876, name="DEFUNCT RETAIL HOLDINGS CORP")
        b.done()
        marked = mark_terminal_status(
            store, [Deregistration(cik=9876, form="15-12B", filed_date="2020-03-02")]
        )
        row = store.execute("SELECT status, status_date FROM filer WHERE cik = 9876").fetchone()
        assert (row["status"], row["status_date"]) == ("deregistered", "2020-03-02")
        assert marked == 1

    def test_a_filer_the_store_does_not_hold_is_reported_not_invented(self, store):
        """A row with a status and no facts would be a company in the universe that
        cannot be screened on anything. Those CIKs are worth ingesting, which is a
        separate decision from recording a status."""
        result = mark_terminal_status(
            store, [Deregistration(cik=404404, form="25", filed_date="2020-03-20")]
        )
        assert result == 0
        assert store.execute("SELECT COUNT(*) n FROM filer").fetchone()["n"] == 0

    def test_the_earliest_deregistration_date_is_the_one_kept(self, store):
        """Several Form 15s can follow one wind-down, a class at a time. The date that
        matters for a historical universe is when the filer stopped being investable, so
        a later filing must not push it forward."""
        b = StoreBuilder(store)
        b.filer(9876, name="DEFUNCT RETAIL HOLDINGS CORP")
        b.done()
        mark_terminal_status(
            store, [Deregistration(cik=9876, form="15-12G", filed_date="2020-04-01")]
        )
        mark_terminal_status(
            store, [Deregistration(cik=9876, form="15-12B", filed_date="2020-03-02")]
        )
        row = store.execute("SELECT status, status_date FROM filer WHERE cik = 9876").fetchone()
        assert row["status"] == "deregistered"
        assert row["status_date"] == "2020-03-02"

    def test_an_active_filer_is_left_alone(self, store):
        b = StoreBuilder(store)
        b.filer(1750, name="WIDGET WORKS INC")
        b.done()
        mark_terminal_status(store, [])
        row = store.execute("SELECT status FROM filer WHERE cik = 1750").fetchone()
        assert row["status"] == "active"


class TestAFormIsNotProofOfDeath:
    """Found live, immediately, and it was my own bug. Scanning 2025 marked 34 of the
    store's filers, and 32 of them filed a 10-K afterwards — IBM, Procter & Gamble, GE,
    Thermo Fisher. Form 25 delists *a security*, not a company: a firm retiring one note
    issue files it and carries on reporting. The index gives no reason either, so
    "delisted for cause" was never something these rows could support."""

    def test_a_form_25_alone_never_marks_a_filer(self, store):
        b = StoreBuilder(store)
        b.filer(51143, name="INTERNATIONAL BUSINESS MACHINES")
        b.done()
        assert (
            mark_terminal_status(
                store, [Deregistration(cik=51143, form="25", filed_date="2025-01-31")]
            )
            == 0
        )
        row = store.execute("SELECT status FROM filer WHERE cik = 51143").fetchone()
        assert row["status"] == "active"

    def test_a_form_15_from_a_filer_that_kept_reporting_is_not_death_either(self, store):
        """A Form 15 deregisters a class of securities. A filer with several classes can
        file one and go on reporting, and its own later 10-K says so."""
        b = StoreBuilder(store)
        b.filer(1750, name="WIDGET WORKS INC")
        b.annual(1750, "2025-12-31", "2026-02-14", NetIncomeLoss=5.0)
        b.done()
        assert (
            mark_terminal_status(
                store, [Deregistration(cik=1750, form="15-12B", filed_date="2025-03-02")]
            )
            == 0
        )

    def test_a_form_15_with_no_later_annual_report_marks_the_filer(self, store):
        b = StoreBuilder(store)
        b.filer(9876, name="DEFUNCT RETAIL HOLDINGS CORP")
        b.annual(9876, "2019-12-31", "2020-02-14", NetIncomeLoss=1.0)
        b.done()
        assert (
            mark_terminal_status(
                store, [Deregistration(cik=9876, form="15-12B", filed_date="2020-03-02")]
            )
            == 1
        )
        row = store.execute("SELECT status FROM filer WHERE cik = 9876").fetchone()
        assert row["status"] == "deregistered"

    def test_nothing_in_the_index_can_produce_a_cause(self, store):
        """`delisted_for_cause` means a total loss in a historical evaluation. The index
        carries no reason for any filing, so no row here may claim one."""
        assert "delisted_for_cause" not in set(TERMINAL_FORMS.values())


class TestTheUniverseStillSeesThem:
    def test_a_filer_that_died_after_the_as_of_date_is_still_in_the_universe(self, store):
        """The whole point. On 2025-06-30 this company was trading and screening; that it
        deregistered in 2026 must not delete it from a 2025 universe, or every historical
        run silently evaluates only the survivors."""
        from dossier.screens import prepare, universe_rows

        b = StoreBuilder(store)
        b.filer(1, name="Died Later Co", sic="3571")
        b.history(1, [2019, 2020, 2021, 2022, 2023, 2024], Revenues=2e9, NetIncomeLoss=1e8)
        b.shares(1, 100_000_000, "2025-04-30", "2025-05-05")
        b.price(1, "2025-06-27", 20.0)
        b.done()
        mark_terminal_status(store, [Deregistration(cik=1, form="25", filed_date="2026-03-01")])

        prepare(store, "2025-06-30")
        assert 1 in {row["cik"] for row in universe_rows(store)}
