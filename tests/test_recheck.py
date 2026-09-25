"""Milestone 10: the quarterly re-check of every open thesis.

The plan calls this the highest-leverage feature in the system, and it is the one that
catches you rationalising — so the thing it must never do is report that a thesis is
holding when it has not actually been able to check it. A condition whose metric cannot
be computed comes back as needing a human, loudly, not as a pass.

It also does not tell you to sell. It tells you that you said you would, and quotes you.
"""

import json

import pytest

from dossier.prompt_files import prompt_text
from dossier.recheck import CHECKABLE_METRICS, recheck_all, recheck_thesis
from dossier.store import open_store
from dossier.thesis import THESIS_PROMPT, load_thesis
from dossier.valuation import load_valuation

CIK = 57131


def facts_for(conn, year, revenue, cfo, capex, ebit):
    accession = f"0000057131-{year % 100:02d}-000019"
    conn.execute(
        "INSERT INTO filing (accession_no, cik, form_type, filed_date, primary_doc_url) "
        "VALUES (?, ?, '10-K', ?, 'https://example.com/f.htm')",
        (accession, CIK, f"{year}-06-16"),
    )
    figures = {
        "Revenues": revenue,
        "NetCashProvidedByUsedInOperatingActivities": cfo,
        "PaymentsToAcquirePropertyPlantAndEquipment": capex,
        "OperatingIncomeLoss": ebit,
        "NetIncomeLoss": ebit * 0.7,
        "Assets": 2_000_000_000.0,
        "AssetsCurrent": 900_000_000.0,
        "LiabilitiesCurrent": 500_000_000.0,
        "StockholdersEquity": 1_000_000_000.0,
        "CashAndCashEquivalentsAtCarryingValue": 300_000_000.0,
        "PropertyPlantAndEquipmentNet": 400_000_000.0,
    }
    for tag, figure in figures.items():
        conn.execute(
            "INSERT INTO fact (cik, tag, unit, period_start, period_end, filed_date, "
            "accession_no, value, form_type) VALUES (?, ?, 'USD', ?, ?, ?, ?, ?, '10-K')",
            (CIK, tag, f"{year - 1}-04-27", f"{year}-04-25", f"{year}-06-16", accession, figure),
        )
    conn.execute(
        "INSERT INTO fact (cik, tag, unit, period_start, period_end, filed_date, "
        "accession_no, value, form_type) VALUES (?, 'EntityCommonStockSharesOutstanding', "
        "'shares', '', ?, ?, ?, 40_000_000, '10-K')",
        (CIK, f"{year}-04-25", f"{year}-06-16", accession),
    )


@pytest.fixture
def store(tmp_path):
    """A filer whose owner earnings margin has fallen for the last two years."""
    with open_store(tmp_path / "test.sqlite") as conn:
        conn.execute(
            "INSERT INTO filer (cik, name, ticker, sic, first_seen) VALUES "
            "(?, 'La-Z-Boy Incorporated', 'LZB', '2510', '2010-06-01')",
            (CIK,),
        )
        # Healthy years, then two that fall through a 4% owner earnings margin.
        for year in range(2019, 2025):
            facts_for(
                conn,
                year,
                revenue=2_000_000_000.0,
                cfo=200_000_000.0,
                capex=60_000_000.0,
                ebit=140_000_000.0,
            )
        facts_for(
            conn,
            2025,
            revenue=2_000_000_000.0,
            cfo=120_000_000.0,
            capex=60_000_000.0,
            ebit=80_000_000.0,
        )
        facts_for(
            conn,
            2026,
            revenue=2_000_000_000.0,
            cfo=110_000_000.0,
            capex=60_000_000.0,
            ebit=70_000_000.0,
        )
        conn.execute(
            "INSERT INTO price (cik, ticker, price_date, close, source, fetched_at) "
            "VALUES (?, 'LZB', '2026-09-21', 30.33, 'test', '2026-09-22T00:00:00+00:00')",
            (CIK,),
        )
        conn.commit()
        yield conn


def with_thesis(conn, conditions, journal_dir=None):
    load_valuation(
        conn,
        cik=CIK,
        as_of="2026-09-22",
        payload={
            "valuation": {
                "revenue_growth": {
                    "bear": -0.03,
                    "base": 0.01,
                    "bull": 0.04,
                    "justification": "Revenue grew 0.8% in fiscal 2026.",
                },
                "owner_earnings_margin": {
                    "bear": 0.04,
                    "base": 0.06,
                    "bull": 0.075,
                    "justification": "Owner earnings were 6.6% of revenue in fiscal 2026.",
                },
            }
        },
    )
    load_thesis(
        conn,
        cik=CIK,
        as_of="2026-09-22",
        journal_dir=journal_dir,
        payload={
            "model": "claude-opus-5",
            "thesis": {
                "business": "It makes chairs. It sells them through its own stores. Some "
                "are sold by independent dealers.",
                "mispricing": {
                    "reason_type": "a temporary earnings trough",
                    "explanation": "Furniture demand tracks housing turnover, which is at "
                    "a multi-decade low, and the price implies -2.6% growth forever.",
                },
                "must_go_right": ["The margin holds near 6%."],
                "falsification": conditions,
                "holding_period_years": 4,
                "sell_early_if": "A second reporting unit is impaired after Joybird.",
                "pre_mortem": "Demand never recovered and the store count grew into it, so "
                "revenue was flat on a larger fixed cost base and the margin fell to 0.1%.",
            },
        },
    )


MARGIN_CONDITION = {
    "metric": "owner earnings margin",
    "direction": "below",
    "threshold": 0.04,
    "window": "two consecutive fiscal years",
}
OPERATING_CONDITION = {
    "metric": "consolidated operating margin",
    "direction": "below",
    "threshold": 0.05,
    "window": "one fiscal year",
}


class TestBreaches:
    def test_a_condition_that_has_been_breached_is_reported(self, store):
        with_thesis(store, [MARGIN_CONDITION, OPERATING_CONDITION])
        report = recheck_thesis(store, cik=CIK, as_of="2026-09-22")
        margin = report["conditions"][0]
        assert margin["status"] == "breached"
        assert margin["observed"][0]["value"] == pytest.approx(0.025, abs=1e-4)
        assert report["status"] == "breached"

    def test_a_condition_that_is_holding_says_so_with_the_number(self, store):
        with_thesis(
            store,
            [MARGIN_CONDITION, {**OPERATING_CONDITION, "threshold": 0.01}],
        )
        holding = recheck_thesis(store, cik=CIK, as_of="2026-09-22")["conditions"][1]
        assert holding["status"] == "holding"
        assert holding["observed"][0]["value"] == pytest.approx(0.035, abs=1e-4)

    def test_a_window_needs_consecutive_years(self, store):
        """One bad year inside a two-year window is not a breach. A re-check that fires
        on a single quarter is one you learn to ignore."""
        store.execute("DELETE FROM fact WHERE period_end = '2025-04-25'")
        store.execute("DELETE FROM filing WHERE accession_no = '0000057131-25-000019'")
        facts_for(
            store,
            2025,
            revenue=2_000_000_000.0,
            cfo=200_000_000.0,
            capex=60_000_000.0,
            ebit=140_000_000.0,
        )
        store.commit()
        with_thesis(store, [MARGIN_CONDITION, OPERATING_CONDITION])
        report = recheck_thesis(store, cik=CIK, as_of="2026-09-22")
        assert report["conditions"][0]["status"] == "holding"


class TestItNeverClaimsToHaveCheckedWhatItCannot:
    def test_an_uncomputable_metric_is_flagged_for_a_human(self, store):
        """The failure that would matter most: reporting a thesis as holding because the
        condition could not be evaluated. Silence here is indistinguishable from good
        news, which is the whole reason this job exists."""
        with_thesis(
            store,
            [
                {
                    "metric": "written same-store sales",
                    "direction": "below",
                    "threshold": -0.03,
                    "window": "two consecutive fiscal years",
                },
                OPERATING_CONDITION,
            ],
        )
        report = recheck_thesis(store, cik=CIK, as_of="2026-09-22")
        same_store = report["conditions"][0]
        assert same_store["status"] == "needs_a_human"
        assert "same-store" in same_store["note"] or "not reported" in same_store["note"]
        assert report["needs_a_human"] == 1

    def test_the_checkable_vocabulary_is_stated_so_a_thesis_can_use_it(self):
        assert "owner_earnings_margin" in CHECKABLE_METRICS
        assert "operating_margin" in CHECKABLE_METRICS

    def test_the_thesis_prompt_names_every_metric_this_job_can_check(self):
        """The first live re-check could evaluate two of four conditions. A thesis
        cannot aim for something it was never told exists, so the vocabulary and the
        prompt stay in step — and this test fails if a metric is added to one alone."""
        prompt = prompt_text(THESIS_PROMPT)
        assert [key for key in CHECKABLE_METRICS if key not in prompt] == []


class TestItReportsRatherThanAdvises:
    def test_a_breach_quotes_the_thesis_back(self, store):
        """It does not tell you to sell. It tells you that you said you would, and asks
        whether you still mean it."""
        with_thesis(store, [MARGIN_CONDITION, OPERATING_CONDITION])
        report = recheck_thesis(store, cik=CIK, as_of="2026-09-22")
        assert report["question"].endswith("?")
        assert "sell" not in report["question"].lower() or "you said" in report["question"].lower()
        assert report["conditions"][0]["you_said"]["threshold"] == 0.04

    def test_only_the_latest_version_of_a_thesis_is_checked(self, store):
        with_thesis(store, [MARGIN_CONDITION, OPERATING_CONDITION])
        with_thesis(store, [{**MARGIN_CONDITION, "threshold": 0.01}, OPERATING_CONDITION])
        report = recheck_thesis(store, cik=CIK, as_of="2026-09-22")
        assert report["thesis_version"] == 2
        assert report["conditions"][0]["status"] == "holding"


class TestTheRunIsRecorded:
    def test_it_stores_the_report_and_journals_it(self, store, tmp_path):
        journal = tmp_path / "journal"
        with_thesis(store, [MARGIN_CONDITION, OPERATING_CONDITION])
        recheck_thesis(store, cik=CIK, as_of="2026-09-22", journal_dir=journal)
        row = store.execute("SELECT payload FROM recheck").fetchone()
        assert json.loads(row["payload"])["status"] == "breached"
        entry = json.loads(next(journal.glob("*recheck*.json")).read_text(encoding="utf-8"))
        assert entry["kind"] == "recheck"

    def test_every_open_thesis_is_checked_in_one_run(self, store):
        with_thesis(store, [MARGIN_CONDITION, OPERATING_CONDITION])
        reports = recheck_all(store)
        assert [r["cik"] for r in reports] == [CIK]

    def test_a_store_with_no_theses_is_not_an_error(self, store):
        assert recheck_all(store) == []
