"""Ingest: EDGAR's JSON into the point-in-time store.

The whole point-in-time design rests on one detail of the companyfacts shape: every
datapoint carries its own `filed` date and the accession that reported it. That is what
makes an honest as-of query possible from free data, so it is what these tests guard.
"""

import json
from pathlib import Path

import pytest

from dossier.asof import AsOfView
from dossier.ingest import (
    SCREEN_TAGS,
    ingest_filer,
    parse_company_facts,
    parse_submissions,
    tags_version,
)
from dossier.store import open_store

FIXTURES = Path(__file__).parent / "fixtures"


class TestTagsVersion:
    def test_changes_when_a_tag_is_added(self):
        assert tags_version(SCREEN_TAGS) != tags_version(SCREEN_TAGS | {"SomeNewTag"})

    def test_ignores_the_order_tags_were_listed_in(self):
        assert tags_version(["B", "A"]) == tags_version(["A", "B"])

    def test_covers_cost_of_goods_and_services_sold(self):
        """Apple, among many others, reports cost of sales under this tag rather than
        CostOfRevenue. Without it their gross margin cannot be computed at all."""
        assert "CostOfGoodsAndServicesSold" in SCREEN_TAGS


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def submissions():
    return fixture("submissions_CIK0000320193.json")


@pytest.fixture
def company_facts():
    return fixture("companyfacts_CIK0000320193.json")


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        yield conn


@pytest.fixture
def ingested(store, submissions, company_facts):
    ingest_filer(store, submissions, company_facts)
    return store


class TestParseSubmissions:
    def test_reads_the_filer(self, submissions):
        filer, _ = parse_submissions(submissions)
        assert filer.cik == 320193
        assert filer.name == "Apple Inc."
        assert filer.ticker == "AAPL"
        assert filer.exchange == "Nasdaq"
        assert filer.sic == "3571"

    def test_reads_every_filing_in_the_recent_block(self, submissions):
        _, filings = parse_submissions(submissions)
        assert len(filings) == 3

    def test_transposes_the_column_oriented_arrays(self, submissions):
        """`recent` is parallel arrays, not a list of records. Getting the transpose
        wrong silently pairs each filing with another filing's date."""
        _, filings = parse_submissions(submissions)
        by_accession = {filing.accession_no: filing for filing in filings}
        fy23 = by_accession["0000320193-23-000106"]
        assert fy23.form_type == "10-K"
        assert fy23.filed_date == "2023-11-03"
        assert fy23.period_end == "2023-09-30"

    def test_builds_the_primary_document_url(self, submissions):
        _, filings = parse_submissions(submissions)
        url = {f.accession_no: f.primary_doc_url for f in filings}["0000320193-24-000123"]
        assert url == (
            "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm"
        )

    def test_records_the_last_filing_date(self, submissions):
        """Phase 1 builds its universe from filers with a recent 10-K, so this is the
        column that lets a dead filer be recognised as dead."""
        filer, _ = parse_submissions(submissions)
        assert filer.last_filing_date == "2024-11-01"

    def test_survives_a_filer_with_no_ticker(self, submissions):
        submissions["tickers"] = []
        submissions["exchanges"] = []
        filer, _ = parse_submissions(submissions)
        assert filer.ticker is None
        assert filer.exchange is None


class TestParseCompanyFacts:
    def test_stamps_each_fact_with_when_it_was_filed(self, company_facts):
        """Not the period end. This is the entire ballgame."""
        facts = parse_company_facts(company_facts)
        fy23 = [f for f in facts if f.tag == "Revenues" and f.period_end == "2023-09-30"]
        assert {f.filed_date for f in fy23} == {"2023-11-03", "2024-11-01"}

    def test_keeps_every_version_of_a_restated_fact(self, company_facts):
        facts = parse_company_facts(company_facts)
        fy23 = [f for f in facts if f.tag == "Revenues" and f.period_end == "2023-09-30"]
        assert sorted(f.value for f in fy23) == [383_000_000_000.0, 383_285_000_000.0]
        assert len({f.accession_no for f in fy23}) == 2

    def test_records_the_reporting_accession(self, company_facts):
        facts = parse_company_facts(company_facts)
        original = next(f for f in facts if f.tag == "Revenues" and f.filed_date == "2023-11-03")
        assert original.accession_no == "0000320193-23-000106"

    def test_an_instant_fact_has_an_empty_period_start(self, company_facts):
        """Balance-sheet items are instants, not durations. Storing NULL there would
        break the primary key that stops duplicate facts."""
        facts = parse_company_facts(company_facts)
        assets = next(f for f in facts if f.tag == "Assets")
        assert assets.period_start == ""
        assert assets.period_end == "2024-09-28"

    def test_keeps_only_the_tags_the_screens_use(self, company_facts):
        """companyfacts carries several thousand tags; the screens use about forty.
        Ingesting the rest is a weekend and tens of gigabytes for nothing."""
        tags = {f.tag for f in parse_company_facts(company_facts)}
        assert "SomeTagNoScreenUses" not in tags
        assert "Revenues" in tags

    def test_an_explicit_tag_list_overrides_the_default(self, company_facts):
        facts = parse_company_facts(company_facts, tags={"Assets"})
        assert {f.tag for f in facts} == {"Assets"}

    def test_carries_the_unit(self, company_facts):
        facts = parse_company_facts(company_facts, tags={"Revenues"})
        assert {f.unit for f in facts} == {"USD"}

    def test_the_default_tag_list_covers_the_screens(self):
        for needed in ["Revenues", "Assets", "Liabilities", "NetIncomeLoss"]:
            assert needed in SCREEN_TAGS


class TestIngest:
    def test_writes_the_filer(self, ingested):
        row = ingested.execute("SELECT * FROM filer WHERE cik = 320193").fetchone()
        assert row["name"] == "Apple Inc."
        assert row["ticker"] == "AAPL"
        assert row["status"] == "active"

    def test_writes_the_filings(self, ingested):
        count = ingested.execute("SELECT COUNT(*) FROM filing WHERE cik = 320193").fetchone()[0]
        assert count == 4  # three from submissions, one stub for an older accession

    def test_writes_the_facts(self, ingested):
        view = AsOfView(ingested, as_of="2025-01-01")
        revenue = view.latest_fact(cik=320193, tag="Revenues", period_end="2024-09-28")
        assert revenue.value == 391_035_000_000.0

    def test_reports_what_it_wrote(self, store, submissions, company_facts):
        result = ingest_filer(store, submissions, company_facts)
        assert result.filings == 3
        # Four kept tags: Revenues (three datapoints, two of them versions of the
        # same period), Assets, NetIncomeLoss, and the dei share count.
        assert result.facts == 6
        assert result.cik == 320193

    def test_is_idempotent(self, store, submissions, company_facts):
        first = ingest_filer(store, submissions, company_facts)
        second = ingest_filer(store, submissions, company_facts)

        assert second.facts_inserted == 0
        assert first.facts == second.facts
        assert store.execute("SELECT COUNT(*) FROM fact").fetchone()[0] == 6
        assert store.execute("SELECT COUNT(*) FROM filer").fetchone()[0] == 1

    def test_a_later_restatement_is_added_not_substituted(self, store, submissions, company_facts):
        """Re-ingesting after a restatement must grow the table, never rewrite it."""
        original = json.loads(json.dumps(company_facts))
        original["facts"]["us-gaap"]["Revenues"]["units"]["USD"] = [
            original["facts"]["us-gaap"]["Revenues"]["units"]["USD"][0]
        ]
        ingest_filer(store, submissions, original)
        before = store.execute("SELECT COUNT(*) FROM fact WHERE tag = 'Revenues'").fetchone()[0]

        ingest_filer(store, submissions, company_facts)
        after = store.execute("SELECT COUNT(*) FROM fact WHERE tag = 'Revenues'").fetchone()[0]

        assert before == 1
        assert after == 3
        assert (
            AsOfView(store, "2024-01-01").latest_fact(320193, "Revenues", "2023-09-30").value
            == 383_285_000_000.0
        )

    def test_a_fact_from_an_unlisted_filing_still_lands(self, ingested):
        """companyfacts reaches further back than the `recent` submissions window. A
        fact whose filing is not in that window must not be dropped on the floor."""
        view = AsOfView(ingested, as_of="2025-01-01")
        assert view.latest_fact(320193, "NetIncomeLoss", "2024-09-28").value == 93_736_000_000.0

    def test_the_stub_filing_is_marked_as_one(self, ingested):
        row = ingested.execute(
            "SELECT * FROM filing WHERE accession_no = '0000320193-99-999999'"
        ).fetchone()
        assert row is not None
        assert row["filed_date"] == "2024-11-01"
        assert row["form_type"] == "10-K"
        assert row["primary_doc_url"] is None

    def test_updates_a_filer_whose_details_changed(self, store, submissions, company_facts):
        ingest_filer(store, submissions, company_facts)
        submissions["tickers"] = ["AAPL2"]
        submissions["name"] = "Apple Inc. (renamed)"
        ingest_filer(store, submissions, company_facts)

        row = store.execute("SELECT * FROM filer WHERE cik = 320193").fetchone()
        assert row["ticker"] == "AAPL2"
        assert row["name"] == "Apple Inc. (renamed)"
        assert store.execute("SELECT COUNT(*) FROM filer").fetchone()[0] == 1

    def test_preserves_a_terminal_status_across_re_ingest(self, store, submissions, company_facts):
        """A filer marked as having failed must not be quietly reactivated by a later
        bulk ingest. That is survivorship bias creeping back in through the side door."""
        ingest_filer(store, submissions, company_facts)
        store.execute(
            "UPDATE filer SET status = 'delisted_for_cause', status_date = '2025-01-01' "
            "WHERE cik = 320193"
        )
        store.commit()
        ingest_filer(store, submissions, company_facts)
        row = store.execute("SELECT status, status_date FROM filer").fetchone()
        assert row["status"] == "delisted_for_cause"
        assert row["status_date"] == "2025-01-01"

    def test_point_in_time_survives_the_round_trip(self, ingested):
        """The end-to-end property: ingest, then ask what was knowable the day before
        the FY2023 10-K was filed."""
        assert AsOfView(ingested, "2023-11-02").facts(320193, "Revenues") == []
        assert AsOfView(ingested, "2023-11-03").facts(320193, "Revenues") != []
