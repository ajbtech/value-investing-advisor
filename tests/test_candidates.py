"""The screener's output contract.

The analysis layer receives the candidate list and nothing else. It never queries the
store, so every dossier has to be reproducible from this file alone. That means every
candidate carries which screens flagged it, at what rank, why, and the raw inputs to
every ratio with the filing they came from.
"""

import json

import pytest

from dossier.asof import AsOfView
from dossier.cli import main
from dossier.screens import build_candidates
from dossier.store import open_store
from tests.builders import StoreBuilder
from tests.test_screens import AS_OF, HEALTHY, eligible_filer


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        yield conn


def two_filers(store):
    b = StoreBuilder(store)
    eligible_filer(b, 1, name="Cheap Co", OperatingIncomeLoss=600_000_000)
    eligible_filer(b, 2, name="Tiny Co")
    store.execute("UPDATE price SET close = 1.0 WHERE cik = 2")
    store.commit()


def by_cik(run):
    return {c["cik"]: c for c in run["candidates"]}


class TestCandidates:
    def test_a_filer_flagged_by_several_screens_is_one_candidate(self, store):
        two_filers(store)
        run = build_candidates(store, AS_OF)
        cheap = by_cik(run)[1]
        screens = {f["screen"] for f in cheap["flagged_by"]}
        assert {"magic_formula", "owner_earnings"} <= screens
        assert len([c for c in run["candidates"] if c["cik"] == 1]) == 1

    def test_each_flag_carries_its_rank_out_of_how_many(self, store):
        """A rank of 1 means little in a universe of one. The count says so."""
        two_filers(store)
        magic = next(
            f
            for f in by_cik(build_candidates(store, AS_OF))[1]["flagged_by"]
            if f["screen"] == "magic_formula"
        )
        assert magic["rank"] == 1
        assert magic["ranked"] == 1

    def test_an_excluded_filer_is_not_a_candidate(self, store):
        two_filers(store)
        assert 2 not in by_cik(build_candidates(store, AS_OF))

    def test_inputs_cite_the_filing_they_came_from(self, store):
        two_filers(store)
        inputs = by_cik(build_candidates(store, AS_OF))[1]["inputs"]
        ebit = [i for i in inputs if i["tag"] == "OperatingIncomeLoss"]
        assert ebit, "EBIT feeds two screens and must be in the inputs"
        for item in ebit:
            assert item["accession_no"]
            assert item["filed_date"] <= AS_OF

    def test_inputs_never_include_a_later_restatement(self, store):
        two_filers(store)
        b = StoreBuilder(store)
        later = b._accession(1) + "9"
        store.execute(
            "INSERT INTO filing (accession_no, cik, form_type, filed_date) "
            "VALUES (?, 1, '10-K/A', '2025-09-01')",
            (later,),
        )
        b.fact(1, later, "OperatingIncomeLoss", 1.0, "2024-12-31", "2025-09-01", start="2024-01-02")
        store.commit()
        inputs = by_cik(build_candidates(store, AS_OF))[1]["inputs"]
        latest_ebit = [
            i
            for i in inputs
            if i["tag"] == "OperatingIncomeLoss" and i["period_end"] == "2024-12-31"
        ]
        assert [i["value"] for i in latest_ebit] == [600_000_000]

    def test_price_and_share_count_are_cited_too(self, store):
        two_filers(store)
        cheap = by_cik(build_candidates(store, AS_OF))[1]
        assert cheap["price"]["price_date"] == "2025-06-27"
        assert cheap["shares"]["accession_no"]
        assert cheap["shares"]["filed_date"] <= AS_OF

    def test_the_flag_reason_names_every_screen_that_flagged_it(self, store):
        two_filers(store)
        cheap = by_cik(build_candidates(store, AS_OF))[1]
        for flag in cheap["flagged_by"]:
            assert flag["label"] in cheap["flag_reason"]

    def test_the_list_is_capped(self, store):
        b = StoreBuilder(store)
        eligible_filer(b, 1)
        eligible_filer(b, 2, OperatingIncomeLoss=600_000_000)
        store.commit()
        assert len(build_candidates(store, AS_OF, limit=1)["candidates"]) == 1

    def test_the_summary_counts_every_exclusion_by_reason(self, store):
        two_filers(store)
        universe = build_candidates(store, AS_OF)["universe"]
        assert universe["filers"] == 2
        assert universe["eligible"] == 1
        assert universe["excluded"] == {"market cap below $300M": 1}


class TestHistory:
    """The plan's deliberate addition: run the screens as of 12 and 24 months ago too,
    and diff. A company that has been cheap and getting cheaper for two years is a
    different animal from one that just fell into the screen this quarter, and the
    difference routes to different prompts downstream."""

    def priced_history(self, store, cheap_from="2023-01-01"):
        """A filer eligible throughout, but only cheap enough to flag from a given date."""
        b = StoreBuilder(store)
        b.filer(1, name="Cheap Co")
        b.history(1, list(range(2017, 2025)), **{**HEALTHY, "OperatingIncomeLoss": 600_000_000})
        for year in range(2017, 2027):
            b.shares(1, 100_000_000, f"{year}-04-30", f"{year}-05-05")
        for day in ("2023-06-27", "2024-06-27", "2025-06-27"):
            b.price(1, day, 20.0 if day >= cheap_from else 20.0)
        store.commit()

    def test_each_candidate_carries_what_the_screens_said_before(self, store):
        self.priced_history(store)
        run = build_candidates(store, AS_OF, compare_months=(12, 24))
        history = by_cik(run)[1]["history"]
        assert [h["as_of"] for h in history] == ["2024-06-30", "2023-06-30"]
        assert all(h["eligible"] for h in history)

    def test_a_filer_flagged_at_every_date_is_persistent(self, store):
        self.priced_history(store)
        assert by_cik(build_candidates(store, AS_OF, compare_months=(12, 24)))[1]["trend"] == (
            "persistent"
        )

    def test_a_filer_that_was_not_eligible_before_is_new(self, store):
        """No price and no share count two years ago: it could not have been screened."""
        b = StoreBuilder(store)
        b.filer(1, name="Newcomer")
        b.history(1, list(range(2017, 2025)), **{**HEALTHY, "OperatingIncomeLoss": 600_000_000})
        b.shares(1, 100_000_000, "2025-04-30", "2025-05-05")
        b.price(1, "2025-06-27", 20.0)
        store.commit()
        candidate = by_cik(build_candidates(store, AS_OF, compare_months=(12, 24)))[1]
        assert candidate["trend"] == "new"
        assert candidate["history"][0]["eligible"] is False
        assert candidate["history"][0]["excluded_because"]

    def test_history_is_as_of_that_date_not_today(self, store):
        """The 12-month-ago run must not see a filing made since."""
        self.priced_history(store)
        StoreBuilder(store).annual(1, "2025-12-31", "2026-02-15", **HEALTHY)
        store.commit()
        history = by_cik(build_candidates(store, AS_OF, compare_months=(12,)))[1]["history"]
        assert history[0]["as_of"] == "2024-06-30"
        assert history[0]["fiscal_year_end"] < "2025-12-31"

    def test_no_compare_months_means_no_history(self, store):
        two_filers(store)
        candidate = by_cik(build_candidates(store, AS_OF))[1]
        assert candidate["history"] == []
        assert candidate["trend"] is None


class TestFingerprint:
    def test_adding_an_older_filing_changes_it(self, store):
        """Ingesting a new filer adds rows dated before the as-of date, so the same
        date can have a different answer. The fingerprint has to notice."""
        two_filers(store)
        before = AsOfView(store, AS_OF).fingerprint()
        StoreBuilder(store).annual(1, "2017-12-31", "2018-02-15", **HEALTHY)
        store.commit()
        assert AsOfView(store, AS_OF).fingerprint() != before

    def test_rows_after_the_as_of_date_do_not_change_it(self, store):
        two_filers(store)
        before = AsOfView(store, AS_OF).fingerprint()
        StoreBuilder(store).price(1, "2025-08-01", 50.0)
        store.commit()
        assert AsOfView(store, AS_OF).fingerprint() == before


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DOSSIER_DATA_DIR", str(tmp_path))
    with open_store(tmp_path / "edgar.sqlite") as conn:
        two_filers(conn)
    return tmp_path


class TestScreenCommand:
    def test_emits_the_run_as_json(self, data_dir, capsys):
        assert main(["screen", "--as-of", AS_OF, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "screen"
        assert payload["as_of"] == AS_OF
        assert [c["cik"] for c in payload["candidates"]] == [1]

    def test_writes_candidate_rows(self, data_dir, capsys):
        main(["screen", "--as-of", AS_OF, "--json"])
        with open_store(data_dir / "edgar.sqlite") as conn:
            rows = conn.execute("SELECT as_of, cik, flag_reason FROM candidate").fetchall()
        assert [(r["as_of"], r["cik"]) for r in rows] == [(AS_OF, 1)]
        assert rows[0]["flag_reason"]

    def test_the_same_store_and_date_is_a_cache_hit(self, data_dir, capsys):
        main(["screen", "--as-of", AS_OF, "--json"])
        first = json.loads(capsys.readouterr().out)
        main(["screen", "--as-of", AS_OF, "--json"])
        second = json.loads(capsys.readouterr().out)
        assert first["status"] == "screened"
        assert second["status"] == "cached"
        assert second["candidates"] == first["candidates"]

    def test_out_writes_the_candidate_array_alone(self, data_dir, tmp_path, capsys):
        """The analysis layer's input is this file and nothing else."""
        out = tmp_path / "candidates.json"
        main(["screen", "--as-of", AS_OF, "--out", str(out), "--json"])
        written = json.loads(out.read_text(encoding="utf-8"))
        assert isinstance(written, list)
        assert written[0]["cik"] == 1

    def test_a_bad_date_is_a_usage_error(self, data_dir, capsys):
        with pytest.raises(SystemExit):
            main(["screen", "--as-of", "last tuesday"])
