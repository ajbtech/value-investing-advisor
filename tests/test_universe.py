"""Building the universe from who actually filed, not from who trades today.

`company_tickers.json` lists current registrants only, so a universe built from it has
already deleted every company that failed — 447 deregistrations in 2025 alone that the
store could not see. EDGAR's quarterly form index lists every annual report filed in a
quarter, by every filer, dead or alive, and that list is the historical universe itself.

Finding them is half the job. The other half is saying what happens to them next: Yahoo
has no prices for tickers that stopped trading, so a dead filer drops out of the screen
for want of a market cap. That has to be counted under its own name rather than hidden
inside another exclusion, or the survivorship bias moves from the universe into the
price layer and becomes harder to see than it was before.
"""

import json
from pathlib import Path

import httpx
import pytest

from dossier.store import open_store
from dossier.universe import ANNUAL_REPORT_FORMS, AnnualFiler, parse_annual_filers, unheld
from tests.builders import StoreBuilder

FIXTURE = Path(__file__).parent / "fixtures" / "form_2020_QTR1.idx"
VALID_UA = "Jane Doe jane@example.com"


def _index(*rows: tuple[str, str, int, str]) -> str:
    """A form.idx body, column-aligned the way EDGAR writes it."""
    header = (
        "Form Type   Company Name                                                  "
        "CIK         Date Filed  File Name\n" + "-" * 120 + "\n"
    )
    lines = [
        f"{form:<12}{name:<62}{cik:010d}  {filed}  edgar/data/{cik}/{cik:010d}-20-000001.txt"
        for form, name, cik, filed in rows
    ]
    return header + "\n".join(lines) + "\n"


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        yield conn


class TestReadingAnnualFilersFromTheIndex:
    def test_the_committed_index_has_one_annual_report(self):
        filers = parse_annual_filers(FIXTURE.read_text(encoding="utf-8"))
        assert [(f.cik, f.form, f.filed_date) for f in filers] == [(1750, "10-K", "2020-02-14")]

    def test_a_proxy_or_an_8k_is_not_membership(self):
        """Filing an 8-K says a company exists. It does not say it produced the annual
        figures a screen reads, which is what being in the universe means here."""
        text = _index(
            ("8-K", "CURRENT REPORT CO", 1, "2020-01-05"),
            ("10-Q", "QUARTERLY ONLY CO", 2, "2020-01-06"),
        )
        assert parse_annual_filers(text) == []

    def test_a_transition_report_counts_and_an_amendment_does_not(self):
        """A 10-KT is an annual report for a changed fiscal year. A 10-K/A amends one
        that is already in the index, so it adds nobody and is left out."""
        text = _index(
            ("10-KT", "CHANGED YEAR END CO", 22222, "2020-02-01"),
            ("10-K/A", "AMENDING CO", 33333, "2020-02-02"),
        )
        assert [f.cik for f in parse_annual_filers(text)] == [22222]

    def test_the_forms_are_named(self):
        assert {"10-K", "10-KT"} <= ANNUAL_REPORT_FORMS
        assert "10-K/A" not in ANNUAL_REPORT_FORMS

    def test_a_filer_appears_once_at_its_earliest_filing(self):
        """Quarters are read one at a time and a filer can appear in several. The
        earliest date is the one kept, because it is when the filer entered the
        universe."""
        text = _index(
            ("10-K", "LATE THEN EARLY CO", 5, "2020-03-30"),
            ("10-K", "LATE THEN EARLY CO", 5, "2020-01-10"),
        )
        [filer] = parse_annual_filers(text)
        assert filer.filed_date == "2020-01-10"


class TestTheGap:
    def test_only_filers_the_store_does_not_hold_are_the_gap(self, store):
        StoreBuilder(store).filer(1750).done()
        found = [
            AnnualFiler(cik=1750, form="10-K", filed_date="2020-02-14"),
            AnnualFiler(cik=9876, form="10-K", filed_date="2020-02-20", company_name="DEAD CO"),
        ]
        assert [f.cik for f in unheld(store, found)] == [9876]

    def test_the_gap_is_reported_once_per_filer_across_quarters(self, store):
        found = [
            AnnualFiler(cik=9876, form="10-K", filed_date="2021-02-20"),
            AnnualFiler(cik=9876, form="10-K", filed_date="2020-02-20"),
        ]
        [filer] = unheld(store, found)
        assert filer.filed_date == "2020-02-20"


class TestADeadFilerIsCountedNotHidden:
    """A filer with no ticker cannot be priced, and cannot be screened. Before this, it
    was excluded as "listed ticker is not common stock: none", which is true of nothing:
    there was no listed ticker to judge. The reason has to say what happened."""

    def _eligible_but_unpriced(self, store, *, status="active", status_date=None):
        from dossier.figures import universe_rows
        from dossier.screens import prepare

        b = StoreBuilder(store)
        b.filer(1, name="Unpriced Co", ticker=None, status=status, status_date=status_date)
        b.history(1, [2019, 2020, 2021, 2022, 2023, 2024], Revenues=2e9, NetIncomeLoss=1e8)
        b.shares(1, 100_000_000, "2025-04-30", "2025-05-05")
        b.done()
        prepare(store, "2025-06-30")
        [row] = [r for r in universe_rows(store) if r["cik"] == 1]
        return row["excluded_because"]

    def test_no_ticker_is_its_own_reason(self, store):
        reason = self._eligible_but_unpriced(store)
        assert reason.startswith("no ticker on file")
        assert "common stock" not in reason

    def test_a_filer_that_later_stopped_filing_is_named_as_the_survivorship_gap(self, store):
        """On 2025-06-30 this company was filing. It deregistered in 2026, so no free
        source has its price, and that is precisely the company a historical screen
        must not quietly lose. The count of these is the size of the remaining bias."""
        reason = self._eligible_but_unpriced(store, status="deregistered", status_date="2026-03-01")
        assert reason.startswith("no price for a filer that later stopped filing")

    def test_a_ticker_that_is_not_common_stock_keeps_its_own_reason(self, store):
        from dossier.figures import universe_rows
        from dossier.screens import prepare

        b = StoreBuilder(store)
        b.filer(2, name="Preferred Listed Co", ticker="SCE-PG")
        b.history(2, [2019, 2020, 2021, 2022, 2023, 2024], Revenues=2e9, NetIncomeLoss=1e8)
        b.done()
        prepare(store, "2025-06-30")
        [row] = [r for r in universe_rows(store) if r["cik"] == 2]
        assert row["excluded_because"] == "listed ticker is not common stock: SCE-PG"


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DOSSIER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EDGAR_USER_AGENT", VALID_UA)
    return tmp_path


@pytest.fixture
def edgar():
    """EDGAR with one quarter's index, listing Apple and a filer nobody has heard of."""
    fixtures = Path(__file__).parent / "fixtures"
    submissions = (fixtures / "submissions_CIK0000320193.json").read_text()
    facts = (fixtures / "companyfacts_CIK0000320193.json").read_text()
    index = _index(
        ("10-K", "APPLE INC", 320193, "2020-01-31"),
        ("10-K", "APPLE INC", 320193, "2020-02-28"),
    )
    as_json = {"content-type": "application/json"}
    requested = []

    def handler(request):
        url = str(request.url)
        requested.append(url)
        if url.endswith("/full-index/2020/QTR1/form.idx"):
            return httpx.Response(200, text=index)
        if "/submissions/" in url:
            return httpx.Response(200, text=submissions, headers=as_json)
        if "/companyfacts/" in url:
            return httpx.Response(200, text=facts, headers=as_json)
        return httpx.Response(404)

    from dossier.edgar import EdgarClient

    client = EdgarClient(VALID_UA, transport=httpx.MockTransport(handler), sleep=lambda _: None)
    client.requested = requested
    return client


class TestUniverseCommand:
    def test_it_reports_the_gap_without_fetching_anyone(self, data_dir, edgar, capsys):
        from dossier.cli import main

        assert main(["universe", "--from-year", "2020", "--json"], client=edgar) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["annual_filers"] == 1
        assert payload["held"] == 0
        assert payload["not_held"] == 1
        assert payload["ingested"] == []
        assert not any("/submissions/" in url for url in edgar.requested)

    def test_a_missing_quarter_is_reported_not_fatal(self, data_dir, edgar, capsys):
        from dossier.cli import main

        main(["universe", "--from-year", "2020", "--json"], client=edgar)
        quarters = json.loads(capsys.readouterr().out)["quarters"]
        assert quarters[0] == {"year": 2020, "quarter": 1, "annual_filers": 1}
        assert all("error" in q for q in quarters[1:])

    def test_ingest_fetches_the_filers_the_store_does_not_hold(self, data_dir, edgar, capsys):
        from dossier.cli import main

        assert main(["universe", "--from-year", "2020", "--ingest", "--json"], client=edgar) == 0
        payload = json.loads(capsys.readouterr().out)
        assert [r["status"] for r in payload["ingested"]] == ["ingested"]
        with open_store(data_dir / "edgar.sqlite") as conn:
            assert conn.execute("SELECT cik FROM filer").fetchone()[0] == 320193

    def test_limit_caps_how_many_are_fetched(self, data_dir, edgar, capsys):
        from dossier.cli import main

        main(
            ["universe", "--from-year", "2020", "--ingest", "--limit", "0", "--json"], client=edgar
        )
        assert json.loads(capsys.readouterr().out)["ingested"] == []

    def test_to_year_before_from_year_is_a_usage_error(self, data_dir, edgar, capsys):
        from dossier.cli import main

        assert main(["universe", "--from-year", "2021", "--to-year", "2020"], client=edgar) == 2
        assert capsys.readouterr().out == ""
