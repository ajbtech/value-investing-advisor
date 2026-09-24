"""The universe, from the form index rather than from today's tickers.

`company_tickers.json` answers "who trades now", which is the wrong question: the plan
asks for the universe *as of the test date*, including the filers that later
deregistered. The same quarterly index that finds the dead also lists every annual
report filed in the quarter, which is the historical universe itself — no correction
needed, because nothing was ever selected for having survived.

This records who filed, not what they filed. Screening still needs facts and prices per
filer, so the point of the table is to say precisely who is missing and what ingesting
them would cost.
"""

from pathlib import Path

import pytest

from dossier.registrants import (
    ANNUAL_FORMS,
    Registrant,
    coverage,
    parse_annual_filers,
    store_registrants,
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


class TestParsing:
    def test_it_finds_the_annual_report_filers(self, index_text):
        found = parse_annual_filers(index_text)
        assert [r.cik for r in found] == [1750]
        assert found[0].company_name == "WIDGET WORKS INC"
        assert found[0].filed_date == "2020-02-14"

    def test_a_proxy_or_an_8k_is_not_an_annual_report(self, index_text):
        """The universe filter is a 10-K in the last fifteen months. A filer that only
        files 8-Ks is not a registrant this pipeline can screen."""
        assert all(r.form in ANNUAL_FORMS for r in parse_annual_filers(index_text))

    def test_the_annual_forms_include_the_foreign_and_amended_variants(self):
        assert {"10-K", "10-K/A", "10-KSB", "20-F", "40-F"} <= set(ANNUAL_FORMS)


class TestStoring:
    def test_it_records_a_filer_the_store_has_never_ingested(self, store):
        """This is the whole point: a row here does not claim any facts about the
        company, only that it filed an annual report on a date."""
        added = store_registrants(
            store,
            [Registrant(cik=999, form="10-K", filed_date="2020-02-14", company_name="Dead Co")],
        )
        assert added == 1
        row = store.execute("SELECT * FROM registrant WHERE cik = 999").fetchone()
        assert row["first_annual"] == "2020-02-14"
        assert row["last_annual"] == "2020-02-14"
        assert store.execute("SELECT COUNT(*) n FROM filer").fetchone()["n"] == 0

    def test_repeated_years_widen_the_span_rather_than_duplicating(self, store):
        store_registrants(store, [Registrant(cik=999, form="10-K", filed_date="2020-02-14")])
        store_registrants(store, [Registrant(cik=999, form="10-K", filed_date="2023-02-10")])
        store_registrants(store, [Registrant(cik=999, form="10-K", filed_date="2018-02-09")])
        row = store.execute("SELECT * FROM registrant WHERE cik = 999").fetchone()
        assert (row["first_annual"], row["last_annual"]) == ("2018-02-09", "2023-02-10")
        assert row["annual_reports"] == 3

    def test_re_reading_the_same_quarter_changes_nothing(self, store):
        """Quarters get re-read: a run is interrupted, a year is scanned twice. Counting
        one filing as two would inflate a filer's history and move nothing that is true."""
        records = [Registrant(cik=999, form="10-K", filed_date="2020-02-14")]
        assert store_registrants(store, records) == 1
        assert store_registrants(store, records) == 0
        row = store.execute("SELECT * FROM registrant WHERE cik = 999").fetchone()
        assert row["annual_reports"] == 1

    def test_a_name_is_kept_when_one_is_offered(self, store):
        store_registrants(store, [Registrant(cik=999, form="10-K", filed_date="2020-02-14")])
        store_registrants(
            store,
            [Registrant(cik=999, form="10-K", filed_date="2021-02-14", company_name="Named Co")],
        )
        row = store.execute("SELECT name FROM registrant WHERE cik = 999").fetchone()
        assert row["name"] == "Named Co"


class TestCoverage:
    def test_it_says_how_much_of_the_real_universe_the_store_holds(self, store):
        """The number that matters. Screening 289 filers means nothing until it is 289
        of something, and the ticker map could never say what."""
        b = StoreBuilder(store)
        b.filer(1, name="Held Co")
        b.done()
        store_registrants(
            store,
            [
                Registrant(cik=1, form="10-K", filed_date="2020-02-14"),
                Registrant(cik=2, form="10-K", filed_date="2020-02-15"),
                Registrant(cik=3, form="10-K", filed_date="2020-02-16"),
            ],
        )
        report = coverage(store)
        assert report["registrants"] == 3
        assert report["ingested"] == 1
        assert report["missing"] == 2

    def test_coverage_can_be_asked_about_one_window(self, store):
        """A universe is always as of a date, so coverage is too: a filer that last filed
        in 2019 is not missing from a 2026 universe, it is correctly absent from it."""
        store_registrants(
            store,
            [
                Registrant(cik=1, form="10-K", filed_date="2019-02-14"),
                Registrant(cik=2, form="10-K", filed_date="2025-02-15"),
            ],
        )
        assert coverage(store, since="2024-01-01")["registrants"] == 1
