"""The point-in-time gateway.

Fundamental data is usually stamped to the fiscal period end, not the filing date, and
the gap has been measured at around 66 days. A backtest that ignores it "knows" results
roughly two months before the market did. That bias is enforced away here, in code, and
never in a prompt — because a prompt cannot be unit-tested.
"""

from datetime import date

import pytest

from dossier.asof import AsOfView, fact_count
from dossier.store import open_store

# Apple's FY2023 10-K: period ended 2023-09-30, filed 2023-11-03. Sixty-four days in
# which a naive query would have handed you results the market could not yet see.
APPLE_FY23 = {
    "accession": "0000320193-23-000106",
    "period_end": "2023-09-30",
    "filed_date": "2023-11-03",
    "revenue": 383_285_000_000.0,
}


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        conn.execute("INSERT INTO filer (cik, name, ticker) VALUES (320193, 'Apple Inc.', 'AAPL')")
        conn.execute(
            "INSERT INTO filing (accession_no, cik, form_type, filed_date, period_end) "
            "VALUES (?, 320193, '10-K', ?, ?)",
            (APPLE_FY23["accession"], APPLE_FY23["filed_date"], APPLE_FY23["period_end"]),
        )
        conn.execute(
            "INSERT INTO fact (accession_no, cik, tag, unit, period_start, period_end, "
            "value, filed_date) VALUES (?, 320193, 'Revenues', 'USD', '2022-10-01', ?, ?, ?)",
            (
                APPLE_FY23["accession"],
                APPLE_FY23["period_end"],
                APPLE_FY23["revenue"],
                APPLE_FY23["filed_date"],
            ),
        )
        conn.commit()
        yield conn


class TestLookaheadBias:
    """The test the build plan asks for by name, and it runs in CI."""

    def test_a_filing_is_invisible_the_day_before_it_was_filed(self, store):
        the_day_before = date.fromisoformat(APPLE_FY23["filed_date"]) - _one_day()
        view = AsOfView(store, as_of=the_day_before)
        assert view.facts(cik=320193, tag="Revenues") == []

    def test_a_filing_is_visible_on_the_day_it_was_filed(self, store):
        view = AsOfView(store, as_of=APPLE_FY23["filed_date"])
        facts = view.facts(cik=320193, tag="Revenues")
        assert [fact.value for fact in facts] == [APPLE_FY23["revenue"]]

    def test_the_period_end_does_not_make_a_fact_visible(self, store):
        """The fiscal period ended 2023-09-30 but nobody could see the number until
        2023-11-03. Querying by period end is exactly the mistake."""
        view = AsOfView(store, as_of=APPLE_FY23["period_end"])
        assert view.facts(cik=320193, tag="Revenues") == []

    def test_accepts_a_date_or_an_iso_string(self, store):
        as_date = AsOfView(store, as_of=date(2024, 1, 1)).facts(cik=320193)
        as_text = AsOfView(store, as_of="2024-01-01").facts(cik=320193)
        assert as_date == as_text
        assert as_date != []

    def test_rejects_an_unparseable_as_of(self, store):
        with pytest.raises(ValueError):
            AsOfView(store, as_of="last Tuesday")


class TestRestatementBias:
    """companyfacts returns today's restated figures, not what was originally reported.
    Screening on restated numbers is screening on data that did not exist at the time,
    and restatements are systematically biased because troubled companies restate more."""

    @pytest.fixture
    def restated(self, store):
        store.execute(
            "INSERT INTO filing (accession_no, cik, form_type, filed_date, period_end) "
            "VALUES ('0000320193-24-000123', 320193, '10-K', '2024-11-01', '2024-09-28')"
        )
        store.execute(
            "INSERT INTO fact (accession_no, cik, tag, unit, period_start, period_end, "
            "value, filed_date) VALUES ('0000320193-24-000123', 320193, 'Revenues', 'USD', "
            "'2022-10-01', '2023-09-30', 383000000000.0, '2024-11-01')"
        )
        store.commit()
        return store

    def test_returns_what_was_known_then_not_what_is_known_now(self, restated):
        view = AsOfView(restated, as_of="2024-01-01")
        fact = view.latest_fact(cik=320193, tag="Revenues", period_end="2023-09-30")
        assert fact.value == APPLE_FY23["revenue"]
        assert fact.accession_no == APPLE_FY23["accession"]

    def test_returns_the_restatement_once_it_has_been_filed(self, restated):
        view = AsOfView(restated, as_of="2025-01-01")
        fact = view.latest_fact(cik=320193, tag="Revenues", period_end="2023-09-30")
        assert fact.value == 383_000_000_000.0
        assert fact.accession_no == "0000320193-24-000123"

    def test_both_versions_survive_in_the_table(self, restated):
        assert restated.execute("SELECT COUNT(*) FROM fact").fetchone()[0] == 2

    def test_a_snapshot_holds_one_row_per_period(self, restated):
        """A point-in-time snapshot keeps the most recently filed version of each
        period, not every version ever reported."""
        snapshot = AsOfView(restated, as_of="2025-01-01").snapshot(cik=320193)
        assert len(snapshot) == 1
        assert snapshot[0].value == 383_000_000_000.0


class TestSurvivorshipBias:
    """Filers who went bankrupt stop filing. A universe built from current filers has
    quietly deleted its own failures — precisely the population a value screen flags."""

    @pytest.fixture
    def mixed(self, store):
        store.execute(
            "INSERT INTO filer (cik, name, status, status_date, first_seen) "
            "VALUES (1090727, 'Bed Bath & Beyond Inc.', 'delisted_for_cause', "
            "'2023-09-29', '1996-01-01')"
        )
        store.execute(
            "INSERT INTO filer (cik, name, status, first_seen) "
            "VALUES (1318605, 'Tesla, Inc.', 'active', '2010-01-29')"
        )
        store.execute("UPDATE filer SET first_seen = '1994-01-01' WHERE cik = 320193")
        store.commit()
        return store

    def test_includes_a_filer_that_later_failed(self, mixed):
        universe = AsOfView(mixed, as_of="2022-06-30").universe()
        assert 1090727 in {filer.cik for filer in universe}

    def test_excludes_a_filer_after_its_terminal_status(self, mixed):
        universe = AsOfView(mixed, as_of="2024-06-30").universe()
        assert 1090727 not in {filer.cik for filer in universe}

    def test_excludes_a_filer_that_had_not_started_filing_yet(self, mixed):
        universe = AsOfView(mixed, as_of="2005-01-01").universe()
        assert 1318605 not in {filer.cik for filer in universe}

    def test_a_delisting_for_cause_is_recoverable_as_a_total_loss(self, mixed):
        """Not a missing value. Treating it as one is how a backtest invents an edge."""
        failed = AsOfView(mixed, as_of="2024-06-30").terminated(since="2023-01-01")
        assert [(f.cik, f.status) for f in failed] == [(1090727, "delisted_for_cause")]


class TestFactCount:
    def test_counts_every_version_not_every_period(self, store):
        assert fact_count(store) == 1


class TestThereIsNoOtherReadPath:
    def test_no_module_outside_the_gateway_reads_the_fact_table(self):
        """The rule is that every read goes through this one function. A rule nobody
        checks is a rule that lasts until the first hurried afternoon."""
        import re
        from pathlib import Path

        import dossier

        package = Path(dossier.__file__).parent
        offenders = [
            path.name
            for path in package.rglob("*.py")
            if path.name != "asof.py"
            and re.search(r"\bfrom\s+fact\b", path.read_text(encoding="utf-8"), re.IGNORECASE)
        ]
        assert offenders == []


def _one_day():
    from datetime import timedelta

    return timedelta(days=1)
