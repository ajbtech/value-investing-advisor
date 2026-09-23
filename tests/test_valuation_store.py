"""The valuation's two halves: what the store hands the model, and what comes back.

Same seam as the analysis passes. `--prepare` writes the exact figures and the findings
that bear on them; the model proposes assumptions; `--load` validates every one of them
and does the arithmetic itself.
"""

import json

import pytest

from dossier.store import open_store
from dossier.valuation import (
    VALUATION_VERSION,
    load_valuation,
    prepare_valuation,
    stored_valuation,
)

CIK = 57131
FY = "2026-04-25"


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        conn.execute(
            "INSERT INTO filer (cik, name, ticker, sic, first_seen) "
            "VALUES (?, 'La-Z-Boy Incorporated', 'LZB', '2510', '2010-06-01')",
            (CIK,),
        )
        for n in range(8):
            year = 2026 - n
            accession = f"0000057131-{year % 100:02d}-000019"
            conn.execute(
                "INSERT INTO filing (accession_no, cik, form_type, filed_date, "
                "primary_doc_url) VALUES (?, ?, '10-K', ?, 'https://example.com/f.htm')",
                (accession, CIK, f"{year}-06-16"),
            )
            facts = {
                "Revenues": 2_126_635_000.0 - n * 20_000_000,
                "NetCashProvidedByUsedInOperatingActivities": 204_106_000.0,
                "PaymentsToAcquirePropertyPlantAndEquipment": 76_300_000.0,
                "DepreciationDepletionAndAmortization": 60_000_000.0,
                "NetIncomeLoss": 90_000_000.0,
                "OperatingIncomeLoss": 129_207_000.0,
                "Assets": 2_000_000_000.0,
                "AssetsCurrent": 900_000_000.0,
                "LiabilitiesCurrent": 500_000_000.0,
                "StockholdersEquity": 1_000_000_000.0,
                "CashAndCashEquivalentsAtCarryingValue": 300_000_000.0,
                "PropertyPlantAndEquipmentNet": 400_000_000.0,
            }
            for tag, value_ in facts.items():
                conn.execute(
                    "INSERT INTO fact (cik, tag, unit, period_start, period_end, "
                    "filed_date, accession_no, value, form_type) "
                    "VALUES (?, ?, 'USD', ?, ?, ?, ?, ?, '10-K')",
                    (
                        CIK,
                        tag,
                        f"{year - 1}-04-27",
                        f"{year}-04-25",
                        f"{year}-06-16",
                        accession,
                        value_,
                    ),
                )
            conn.execute(
                "INSERT INTO fact (cik, tag, unit, period_start, period_end, filed_date, "
                "accession_no, value, form_type) VALUES (?, "
                "'WeightedAverageNumberOfSharesOutstandingBasic', 'shares', ?, ?, ?, ?, "
                "42_000_000, '10-K')",
                (CIK, f"{year - 1}-04-27", f"{year}-04-25", f"{year}-06-16", accession),
            )
            conn.execute(
                "INSERT INTO fact (cik, tag, unit, period_start, period_end, filed_date, "
                "accession_no, value, form_type) VALUES (?, "
                "'EntityCommonStockSharesOutstanding', 'shares', '', ?, ?, ?, "
                "42_000_000, '10-K')",
                (CIK, f"{year}-04-25", f"{year}-06-16", accession),
            )
        conn.execute(
            "INSERT INTO price (cik, ticker, price_date, close, source, fetched_at) "
            "VALUES (?, 'LZB', '2026-09-21', 42.0, 'test', '2026-09-22T00:00:00+00:00')",
            (CIK,),
        )
        conn.commit()
        yield conn


def assumptions_payload(**overrides) -> dict:
    payload = {
        "valuation": {
            "revenue_growth": {
                "bear": -0.02,
                "base": 0.01,
                "bull": 0.04,
                "justification": "Consolidated sales rose 0.8% in fiscal 2026.",
            },
            "owner_earnings_margin": {
                "bear": 0.04,
                "base": 0.06,
                "bull": 0.08,
                "justification": (
                    "Owner earnings were 6% of revenue in fiscal 2026, including $11.5 "
                    "million of one-off gains (0000057131-26-000019)."
                ),
            },
        },
        "model": "claude-opus-5",
    }
    for name, fields in overrides.items():
        payload["valuation"][name] = fields
    return payload


class TestPrepare:
    def test_it_hands_over_the_figures_with_the_filings_behind_them(self, store):
        prepared = prepare_valuation(store, cik=CIK, as_of="2026-09-22")
        assert prepared["inputs"]["revenue"] == 2_126_635_000.0
        assert prepared["inputs"]["history"][0]["accession_no"] == "0000057131-26-000019"
        assert prepared["inputs"]["history"][0]["filed_date"] == "2026-06-16"

    def test_it_shows_both_maintenance_capex_estimates(self, store):
        """The one genuinely hard input. Hiding the spread inside a single number is
        how an estimate stops being read as one."""
        prepared = prepare_valuation(store, cik=CIK, as_of="2026-09-22")
        capex = prepared["maintenance_capex"]
        assert capex["total_capex"] == 76_300_000.0
        assert capex["revenue_scaled"] is not None
        assert capex["used"] == min(capex["total_capex"], capex["revenue_scaled"])

    def test_it_carries_the_findings_the_assumptions_must_answer_to(self, store):
        store.execute(
            "INSERT INTO finding (run_key, cik, accession_no, item, change_type, quote, "
            "implication, severity, pass, prompt_version, created_at) VALUES "
            "('k', ?, '0000057131-26-000019', '7', 'added', 'a quote from the filing', "
            "'Margins included one-off gains.', 'high', 'a', 'pass_a_mdna_v1', "
            "'2026-09-22T00:00:00+00:00')",
            (CIK,),
        )
        store.commit()
        prepared = prepare_valuation(store, cik=CIK, as_of="2026-09-22")
        assert prepared["findings"][0]["implication"] == "Margins included one-off gains."

    def test_it_carries_the_prompt_and_the_fixed_constants(self, store):
        prepared = prepare_valuation(store, cik=CIK, as_of="2026-09-22")
        assert "discount rate" in prepared["instructions"].lower()
        assert prepared["fixed"]["discount_rate"] == 0.10

    def test_a_filer_the_screens_cannot_see_is_refused_with_a_reason(self, store):
        with pytest.raises(ValueError, match="not in the eligible universe"):
            prepare_valuation(store, cik=999999, as_of="2026-09-22")


class TestLoad:
    def test_it_stores_a_valuation_and_returns_the_range(self, store):
        result = load_valuation(store, cik=CIK, as_of="2026-09-22", payload=assumptions_payload())
        assert result["bear"]["per_share"] < result["bull"]["per_share"]
        stored = stored_valuation(store, cik=CIK, as_of="2026-09-22")
        assert stored["valuation_version"] == VALUATION_VERSION
        assert stored["buy_below"] == result["buy_below"]

    def test_a_vague_justification_is_refused_rather_than_stored(self, store):
        payload = assumptions_payload(
            revenue_growth={
                "bear": -0.02,
                "base": 0.01,
                "bull": 0.04,
                "justification": "A conservative estimate.",
            }
        )
        with pytest.raises(ValueError, match="justification"):
            load_valuation(store, cik=CIK, as_of="2026-09-22", payload=payload)
        assert stored_valuation(store, cik=CIK, as_of="2026-09-22") is None

    def test_a_model_supplied_discount_rate_is_refused(self, store):
        payload = assumptions_payload(
            discount_rate={
                "bear": 0.12,
                "base": 0.10,
                "bull": 0.08,
                "justification": "The rate used for every filer is 10%.",
            }
        )
        with pytest.raises(ValueError, match="discount rate"):
            load_valuation(store, cik=CIK, as_of="2026-09-22", payload=payload)

    def test_re_running_replaces_rather_than_accumulates(self, store):
        load_valuation(store, cik=CIK, as_of="2026-09-22", payload=assumptions_payload())
        load_valuation(store, cik=CIK, as_of="2026-09-22", payload=assumptions_payload())
        rows = store.execute("SELECT COUNT(*) AS n FROM valuation").fetchone()
        assert rows["n"] == 1

    def test_the_stored_payload_is_the_whole_valuation(self, store):
        load_valuation(store, cik=CIK, as_of="2026-09-22", payload=assumptions_payload())
        row = store.execute("SELECT payload FROM valuation").fetchone()
        payload = json.loads(row["payload"])
        assert payload["discount_rate"] == 0.10
        assert {a["name"] for a in payload["assumptions"]} == {
            "revenue_growth",
            "owner_earnings_margin",
        }
