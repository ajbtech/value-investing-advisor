"""Milestone 9: the thesis, the bear pass that tries to kill it, and the journal.

The journal is the part that compounds. Dossiers are disposable; the record of what was
believed and why is not. So the rules that make a thesis auditable later are enforced
here rather than asked for in a prompt: conditions that can actually be measured, a
named reason for the mispricing, and a bear case that cites filings like everyone else.
"""

import json

import pytest

from dossier.store import open_store
from dossier.thesis import (
    THESIS_PROMPT,
    load_bear_pass,
    load_thesis,
    prepare_bear_pass,
    prepare_thesis,
    record_pass_over,
    stored_thesis,
)
from dossier.valuation import load_valuation

CIK = 57131
ACCESSION = "0000057131-26-000019"
SECTION_TEXT = (
    "Gross margin increased 10 basis points during fiscal 2026 compared with fiscal "
    "2025, as a 50 basis point benefit from a change in our consolidated mix due to "
    "growth in our Retail segment was largely offset by higher distribution costs."
)


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        conn.execute(
            "INSERT INTO filer (cik, name, ticker, sic, first_seen) VALUES "
            "(?, 'La-Z-Boy Incorporated', 'LZB', '2510', '2010-06-01')",
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
                "Revenues": 2_126_635_000.0,
                "NetCashProvidedByUsedInOperatingActivities": 204_106_000.0,
                "PaymentsToAcquirePropertyPlantAndEquipment": 76_300_000.0,
                "NetIncomeLoss": 90_000_000.0,
                "OperatingIncomeLoss": 129_207_000.0,
                "Assets": 2_000_000_000.0,
                "AssetsCurrent": 900_000_000.0,
                "LiabilitiesCurrent": 500_000_000.0,
                "StockholdersEquity": 1_000_000_000.0,
                "CashAndCashEquivalentsAtCarryingValue": 300_000_000.0,
                "PropertyPlantAndEquipmentNet": 400_000_000.0,
            }
            for tag, figure in facts.items():
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
                        figure,
                    ),
                )
            conn.execute(
                "INSERT INTO fact (cik, tag, unit, period_start, period_end, filed_date, "
                "accession_no, value, form_type) VALUES (?, "
                "'EntityCommonStockSharesOutstanding', 'shares', '', ?, ?, ?, "
                "40_000_000, '10-K')",
                (CIK, f"{year}-04-25", f"{year}-06-16", accession),
            )
        conn.execute(
            "INSERT INTO document_section (accession_no, item, text, "
            "extraction_confidence, char_count) VALUES (?, '7', ?, 1.0, ?)",
            (ACCESSION, SECTION_TEXT, len(SECTION_TEXT)),
        )
        conn.execute(
            "INSERT INTO price (cik, ticker, price_date, close, source, fetched_at) "
            "VALUES (?, 'LZB', '2026-09-21', 30.33, 'test', '2026-09-22T00:00:00+00:00')",
            (CIK,),
        )
        conn.commit()
        yield conn


@pytest.fixture
def valued(store):
    load_valuation(
        store,
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
            },
            "model": "claude-opus-5",
        },
    )
    return store


def thesis_payload(**overrides) -> dict:
    thesis = {
        "business": (
            "La-Z-Boy manufactures and retails upholstered furniture under its own "
            "brand. It owns 61% of the stores that sell it. The rest are independent "
            "dealers and wholesale accounts."
        ),
        "mispricing": {
            "reason_type": "temporary earnings trough",
            "explanation": (
                "Written same-store sales fell 3% in fiscal 2026 on weak furniture "
                "demand, and the price implies -2.6% revenue growth in perpetuity."
            ),
        },
        "must_go_right": [
            "Same-store sales stop declining as the store count grows toward 450.",
            "The owner earnings margin holds near 6% without the fiscal 2026 inventory "
            "release repeating.",
        ],
        "falsification": [
            {
                "metric": "written same-store sales",
                "direction": "below",
                "threshold": -0.05,
                "window": "two consecutive fiscal years",
            },
            {
                "metric": "owner earnings margin",
                "direction": "below",
                "threshold": 0.04,
                "window": "one fiscal year",
            },
        ],
        "holding_period_years": 4,
        "sell_early_if": (
            "The company funds the buyback authorized in April 2026 with borrowing "
            "while same-store sales are still falling."
        ),
        "pre_mortem": (
            "Three years on, the store count reached 450 while same-store sales kept "
            "falling, so revenue was flat on a larger fixed cost base and the margin "
            "fell to the fiscal 2022 level of 0.1%."
        ),
    }
    thesis.update(overrides)
    return {"thesis": thesis, "model": "claude-opus-5"}


class TestPrepare:
    def test_it_carries_the_valuation_and_the_findings(self, valued):
        prepared = prepare_thesis(valued, cik=CIK, as_of="2026-09-22")
        assert prepared["valuation"]["base"]["per_share"] > 0
        assert prepared["valuation"]["implied"]["growth"] is not None
        assert prepared["prompt_version"] == THESIS_PROMPT

    def test_a_company_without_a_valuation_is_refused(self, store):
        """The thesis is written for a candidate that cleared valuation. Writing one
        first would be arguing for a company before pricing it."""
        with pytest.raises(ValueError, match="no valuation"):
            prepare_thesis(store, cik=CIK, as_of="2026-09-22")


class TestTheThesisMustBeAuditableLater:
    def test_it_stores_a_complete_thesis(self, valued):
        result = load_thesis(valued, cik=CIK, as_of="2026-09-22", payload=thesis_payload())
        assert result["version"] == 1
        assert stored_thesis(valued, cik=CIK, as_of="2026-09-22")["thesis"]["business"]

    def test_a_missing_section_is_refused(self, valued):
        payload = thesis_payload()
        del payload["thesis"]["pre_mortem"]
        with pytest.raises(ValueError, match="pre_mortem"):
            load_thesis(valued, cik=CIK, as_of="2026-09-22", payload=payload)

    def test_the_market_is_wrong_is_not_a_reason(self, valued):
        payload = thesis_payload(
            mispricing={
                "reason_type": "mispriced",
                "explanation": "The market is wrong about this company.",
            }
        )
        with pytest.raises(ValueError, match="mispricing"):
            load_thesis(valued, cik=CIK, as_of="2026-09-22", payload=payload)

    def test_a_falsification_condition_must_be_measurable(self, valued):
        """'I am wrong if the thesis stops working' cannot be checked by the quarterly
        job, which is the one feature that catches you rationalising."""
        payload = thesis_payload(
            falsification=[
                {
                    "metric": "owner earnings margin",
                    "direction": "below",
                    "threshold": 0.04,
                    "window": "one fiscal year",
                },
                {"metric": "the thesis", "direction": "worse", "window": "a while"},
            ]
        )
        with pytest.raises(ValueError, match="threshold"):
            load_thesis(valued, cik=CIK, as_of="2026-09-22", payload=payload)

    def test_at_least_two_conditions_are_required(self, valued):
        payload = thesis_payload(
            falsification=[
                {
                    "metric": "owner earnings margin",
                    "direction": "below",
                    "threshold": 0.04,
                    "window": "one fiscal year",
                }
            ]
        )
        with pytest.raises(ValueError, match="two"):
            load_thesis(valued, cik=CIK, as_of="2026-09-22", payload=payload)

    def test_a_thesis_that_recommends_is_refused(self, valued):
        payload = thesis_payload(
            sell_early_if="Buy more if the price falls below $20 and hold for the dividend."
        )
        with pytest.raises(ValueError, match="observation|recommend"):
            load_thesis(valued, cik=CIK, as_of="2026-09-22", payload=payload)

    def test_revising_a_thesis_appends_rather_than_overwrites(self, valued):
        """You do not get to quietly rewrite what you believed. The old version stays."""
        first = load_thesis(valued, cik=CIK, as_of="2026-09-22", payload=thesis_payload())
        second = load_thesis(
            valued,
            cik=CIK,
            as_of="2026-09-22",
            payload=thesis_payload(holding_period_years=2),
        )
        assert (first["version"], second["version"]) == (1, 2)
        rows = valued.execute("SELECT COUNT(*) AS n FROM thesis").fetchone()
        assert rows["n"] == 2


class TestTheBearPass:
    def test_it_is_given_the_thesis_to_attack(self, valued):
        load_thesis(valued, cik=CIK, as_of="2026-09-22", payload=thesis_payload())
        prepared = prepare_bear_pass(valued, cik=CIK, as_of="2026-09-22")
        assert prepared["thesis"]["mispricing"]["reason_type"] == "temporary earnings trough"
        assert "destroy" in prepared["instructions"].lower() or (
            "kill" in prepared["instructions"].lower()
        )

    def test_a_bear_point_must_cite_the_filing_like_everyone_else(self, valued):
        load_thesis(valued, cik=CIK, as_of="2026-09-22", payload=thesis_payload())
        result = load_bear_pass(
            valued,
            cik=CIK,
            as_of="2026-09-22",
            payload={
                "bear": [
                    {
                        "attacks": "must_go_right",
                        "claim": "The margin benefit came from mix, not from cost control.",
                        "accession_no": ACCESSION,
                        "item": "7",
                        "quote": "a 50 basis point benefit from a change in our consolidated mix",
                    },
                    {
                        "attacks": "mispricing",
                        "claim": "Invented support for a claim the filing does not make.",
                        "accession_no": ACCESSION,
                        "item": "7",
                        "quote": "management expects margins to decline materially next year",
                    },
                ],
                "model": "claude-opus-5",
            },
        )
        assert result["kept"] == 1
        assert result["dropped"] == 1
        assert result["fabrication_rate"] == 0.5

    def test_the_bear_case_attaches_to_the_thesis_permanently(self, valued):
        load_thesis(valued, cik=CIK, as_of="2026-09-22", payload=thesis_payload())
        load_bear_pass(
            valued,
            cik=CIK,
            as_of="2026-09-22",
            payload={
                "bear": [
                    {
                        "attacks": "must_go_right",
                        "claim": "The margin benefit came from mix.",
                        "accession_no": ACCESSION,
                        "item": "7",
                        "quote": "a 50 basis point benefit from a change in our consolidated mix",
                    }
                ]
            },
        )
        stored = stored_thesis(valued, cik=CIK, as_of="2026-09-22")
        assert stored["bear"][0]["claim"].startswith("The margin benefit")

    def test_a_bear_pass_without_a_thesis_is_refused(self, valued):
        with pytest.raises(ValueError, match="no thesis"):
            prepare_bear_pass(valued, cik=CIK, as_of="2026-09-22")


class TestTheJournal:
    def test_a_decision_is_written_outside_the_repository(self, valued, tmp_path):
        journal = tmp_path / "journal"
        load_thesis(
            valued,
            cik=CIK,
            as_of="2026-09-22",
            payload=thesis_payload(),
            journal_dir=journal,
        )
        entries = sorted(journal.glob("*.json"))
        assert len(entries) == 1
        entry = json.loads(entries[0].read_text(encoding="utf-8"))
        assert entry["cik"] == CIK
        assert entry["prompt_version"] == THESIS_PROMPT
        assert entry["model"] == "claude-opus-5"

    def test_a_candidate_passed_over_is_recorded_too(self, tmp_path):
        """The passes are where you learn most, and they are what everyone forgets to
        record. A journal of only the decisions taken describes a different process than
        the one that was run."""
        journal = tmp_path / "journal"
        record_pass_over(
            journal,
            cik=56679,
            as_of="2026-09-22",
            reason="Named four AI-enabled competitors in its own risk factors.",
            screens=["magic_formula", "quality_at_price"],
        )
        entry = json.loads(next(journal.glob("*.json")).read_text(encoding="utf-8"))
        assert entry["kind"] == "pass_over"
        assert entry["screens"] == ["magic_formula", "quality_at_price"]

    def test_a_pass_over_without_a_reason_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="reason"):
            record_pass_over(tmp_path, cik=56679, as_of="2026-09-22", reason="   ")

    def test_a_second_decision_does_not_overwrite_the_first(self, valued, tmp_path):
        journal = tmp_path / "journal"
        load_thesis(
            valued, cik=CIK, as_of="2026-09-22", payload=thesis_payload(), journal_dir=journal
        )
        load_thesis(
            valued,
            cik=CIK,
            as_of="2026-09-22",
            payload=thesis_payload(holding_period_years=2),
            journal_dir=journal,
        )
        assert len(sorted(journal.glob("*.json"))) == 2
