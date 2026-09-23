"""Pass A: prepare an input, load the findings back, validate every quote.

The model is a Claude Code session, not an API call, so this runs in two halves.
`prepare` writes what the pass needs to read; `load` takes the findings back, checks
every quote against the filing it claims to come from, and stores only what survives.

The seam is the point. Whatever produced the findings — a subscription session, an API
call, a person typing them by hand — they pass through the same validator, because the
guarantee was never about where the text came from.
"""

import json

import pytest

from dossier.analysis import (
    PASS_A_VERSION,
    AnalysisInput,
    load_findings,
    prepare_pass_a,
    prompt_text,
)
from dossier.store import open_store

CURRENT = "0000320193-24-000123"
PRIOR = "0000320193-23-000106"

CURRENT_TEXT = (
    "Our business is concentrated: three customers accounted for 62% of revenue in the "
    "year, up from 51% in the prior year. The loss of any one of them would materially "
    "harm our results of operations. We have not qualified a second source for the "
    "titanium alloy used in our primary product line."
)
PRIOR_TEXT = (
    "Our business is concentrated: three customers accounted for 51% of revenue in the "
    "year. The loss of any one of them could affect our results of operations."
)


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        conn.execute("INSERT INTO filer (cik, name) VALUES (320193, 'Acme Widgets Inc.')")
        for accession, filed, text in (
            (CURRENT, "2024-11-01", CURRENT_TEXT),
            (PRIOR, "2023-11-03", PRIOR_TEXT),
        ):
            conn.execute(
                "INSERT INTO filing (accession_no, cik, form_type, filed_date) "
                "VALUES (?, 320193, '10-K', ?)",
                (accession, filed),
            )
            conn.execute(
                "INSERT INTO document_section "
                "(accession_no, item, text, extraction_confidence, char_count) "
                "VALUES (?, '1A', ?, 0.95, ?)",
                (accession, text, len(text)),
            )
        conn.commit()
        yield conn


def finding(**overrides) -> dict:
    payload = dict(
        accession_no=CURRENT,
        item="1A",
        change_type="strengthened",
        quote="three customers accounted for 62% of revenue",
        prior_accession_no=PRIOR,
        prior_quote="three customers accounted for 51% of revenue",
        implication="Customer concentration rose by eleven points year over year.",
        severity="high",
    )
    payload.update(overrides)
    return payload


class TestThePrompt:
    def test_is_versioned_and_lives_in_the_repo(self):
        """Pinned in the repo so a result can be traced to the words that produced it.
        When output shifts you need to know whether the world changed or the prompt did."""
        assert PASS_A_VERSION
        assert "risk-factor" in prompt_text(PASS_A_VERSION).lower()

    def test_tells_the_model_to_quote_verbatim(self):
        assert "verbatim" in prompt_text(PASS_A_VERSION).lower()

    def test_forbids_buy_and_sell_views(self):
        text = prompt_text(PASS_A_VERSION).lower()
        assert "not views" in text or "observations, not" in text

    def test_an_unknown_version_fails_loudly(self):
        with pytest.raises(FileNotFoundError):
            prompt_text("pass_a_v99")


class TestPrepare:
    def test_pairs_the_two_most_recent_filings(self, store):
        prepared = prepare_pass_a(store, cik=320193)
        assert prepared.current["accession_no"] == CURRENT
        assert prepared.prior["accession_no"] == PRIOR

    def test_carries_the_section_text_the_pass_reads(self, store):
        prepared = prepare_pass_a(store, cik=320193)
        assert "62% of revenue" in prepared.current["text"]
        assert "51% of revenue" in prepared.prior["text"]

    def test_carries_the_extraction_confidence(self, store):
        """The pass should know how much to trust what it is reading."""
        assert prepare_pass_a(store, cik=320193).current["extraction_confidence"] == 0.95

    def test_includes_the_prompt_and_its_version(self, store):
        prepared = prepare_pass_a(store, cik=320193)
        assert prepared.prompt_version == PASS_A_VERSION
        assert "Quote or stay silent" in prepared.instructions

    def test_serialises_to_json(self, store):
        payload = json.loads(json.dumps(prepare_pass_a(store, cik=320193).to_dict()))
        assert payload["pass"] == "a"
        assert payload["cik"] == 320193

    def test_refuses_when_there_is_only_one_filing(self, store):
        """A diff needs two. Producing a one-sided 'diff' would invite findings about
        changes that were never compared against anything."""
        store.execute("DELETE FROM document_section WHERE accession_no = ?", (PRIOR,))
        store.commit()
        with pytest.raises(ValueError, match="two"):
            prepare_pass_a(store, cik=320193)

    def test_refuses_a_filer_with_nothing_extracted(self, store):
        with pytest.raises(ValueError):
            prepare_pass_a(store, cik=999999)

    def test_warns_when_the_extraction_is_weak(self, store):
        """Analysing a bad parse produces confident findings about text the filing does
        not contain. The pass should refuse rather than launder a broken extraction."""
        store.execute("UPDATE document_section SET extraction_confidence = 0.2")
        store.commit()
        with pytest.raises(ValueError, match="confidence"):
            prepare_pass_a(store, cik=320193, min_confidence=0.6)


def add_candidate(conn, as_of="2026-09-22", cik=320193, **overrides):
    """A candidate row as `dossier screen` writes one."""
    payload = {
        "cik": cik,
        "ticker": "ACME",
        "name": "Acme Widgets Inc.",
        "market_cap": 2_000_000_000,
        "flag_reason": "Net-net: market cap 0.80x net current assets",
        "flagged_by": [
            {
                "screen": "net_net",
                "label": "Net-net",
                "rank": 2,
                "ranked": 59,
                "metrics": {"net_current_assets": 2.5e9, "price_to_ncav": 0.8},
                "reason": "Net-net: market cap 0.80x net current assets",
            }
        ],
    }
    payload.update(overrides)
    conn.execute(
        "INSERT INTO candidate (as_of, cik, screener_version, screens, flag_reason, "
        "payload, created_at) VALUES (?, ?, '2', ?, ?, ?, '2026-09-22T00:00:00+00:00')",
        (
            as_of,
            cik,
            ",".join(f["screen"] for f in payload["flagged_by"]),
            payload["flag_reason"],
            json.dumps(payload),
        ),
    )
    conn.commit()
    return payload


def add_section(conn, item, current_text, prior_text, confidence=0.95):
    """The same filings, with another item extracted from each."""
    for accession, text in ((CURRENT, current_text), (PRIOR, prior_text)):
        conn.execute(
            "INSERT INTO document_section "
            "(accession_no, item, text, extraction_confidence, char_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (accession, item, text, confidence, len(text)),
        )
    conn.commit()


MDNA_CURRENT = (
    "Gross margin was 34.1%, compared to 38.2% in the prior year, reflecting higher "
    "titanium alloy costs which we do not expect to reverse in fiscal 2025. We now "
    "present adjusted operating income excluding restructuring charges, a measure we "
    "did not previously report."
)
MDNA_PRIOR = (
    "Gross margin was 38.2%, compared to 37.9% in the prior year. We expect input costs "
    "to remain stable."
)


class TestPassAOverMDNA:
    """The plan's Pass A is a risk-factor *and MD&A* diff. Item 7 is extracted and was
    never paired, and the two sections do not take the same reading: Item 1A is a list of
    risks management chose to name, Item 7 is management explaining its own numbers."""

    def test_item_7_uses_a_prompt_written_for_mdna(self, store):
        add_section(store, "7", MDNA_CURRENT, MDNA_PRIOR)
        prepared = prepare_pass_a(store, cik=320193, item="7")
        assert prepared.prompt_version != PASS_A_VERSION
        assert "non-gaap" in prepared.instructions.lower()

    def test_a_finding_records_the_prompt_that_produced_it(self, store):
        """Storing an MD&A finding under the risk-factor prompt version would break the
        one thing prompt pinning is for: knowing whether the world changed or the words
        did."""
        add_section(store, "7", MDNA_CURRENT, MDNA_PRIOR)
        expected = prepare_pass_a(store, cik=320193, item="7").prompt_version
        load_findings(
            store,
            cik=320193,
            item="7",
            payload={
                "findings": [
                    finding(
                        item="7",
                        change_type="strengthened",
                        quote="Gross margin was 34.1%, compared to 38.2% in the prior year",
                        prior_quote="Gross margin was 38.2%, compared to 37.9% in the prior year",
                        implication="Gross margin fell four points on input costs.",
                    )
                ]
            },
        )
        stored = store.execute("SELECT prompt_version FROM finding").fetchall()
        assert [row["prompt_version"] for row in stored] == [expected]

    def test_an_item_with_no_prompt_of_its_own_is_refused(self, store):
        """Item 8 is the footnotes, which is Pass B's job. Falling back to the
        risk-factor prompt would run a pass whose instructions describe another
        section."""
        add_section(store, "8", "Note 1. Summary.", "Note 1. Summary.")
        with pytest.raises(ValueError, match="no Pass A prompt"):
            prepare_pass_a(store, cik=320193, item="8")


class TestTheScreenReasonReachesThePass:
    """The plan asks for this by name: the model should know whether a company surfaced
    as a net-net or as a quality compounder, because the interesting questions differ."""

    def test_the_prepared_input_carries_the_flag_reason(self, store):
        add_candidate(store)
        prepared = prepare_pass_a(store, cik=320193)
        assert prepared.screen["flag_reason"] == "Net-net: market cap 0.80x net current assets"

    def test_it_carries_which_screens_flagged_it_and_their_metrics(self, store):
        add_candidate(store)
        screen = prepare_pass_a(store, cik=320193).screen
        assert [f["screen"] for f in screen["flagged_by"]] == ["net_net"]
        assert screen["flagged_by"][0]["metrics"]["price_to_ncav"] == 0.8
        assert screen["as_of"] == "2026-09-22"

    def test_it_carries_the_trend_and_the_history_behind_it(self, store):
        """`--compare` decides whether a company has been cheap for two years or just
        fell in, and the plan routes that distinction to the prompt. Carrying the trend
        without the history would be a label the model cannot check: Flexsteel reads as
        `new` because it was ineligible earlier, not because it newly became cheap."""
        history = [
            {"as_of": "2025-09-22", "flagged_by": [], "excluded": "no recent price"},
            {"as_of": "2024-09-22", "flagged_by": [], "excluded": "no recent price"},
        ]
        add_candidate(store, trend="new", history=history)
        screen = prepare_pass_a(store, cik=320193).screen
        assert screen["trend"] == "new"
        assert [point["excluded"] for point in screen["history"]] == [
            "no recent price",
            "no recent price",
        ]

    def test_a_candidate_screened_without_compare_has_no_trend(self, store):
        """`--compare` is optional, and its absence is not a missing value to invent."""
        add_candidate(store)
        screen = prepare_pass_a(store, cik=320193).screen
        assert screen["trend"] is None
        assert screen["history"] == []

    def test_the_prompt_explains_what_a_trend_means(self, store):
        assert "persistent" in prompt_text(PASS_A_VERSION).lower()

    def test_the_most_recent_screen_run_wins(self, store):
        add_candidate(store, as_of="2025-01-31", flag_reason="older run")
        add_candidate(store, as_of="2026-09-22", flag_reason="newer run")
        assert prepare_pass_a(store, cik=320193).screen["flag_reason"] == "newer run"

    def test_a_company_nobody_screened_still_prepares(self, store):
        """A CIK analysed directly was never a candidate. That is a fact to state, not a
        reason to refuse the pass."""
        prepared = prepare_pass_a(store, cik=320193)
        assert prepared.screen is None

    def test_the_prompt_asks_the_model_to_use_it(self, store):
        assert "screen" in prompt_text(PASS_A_VERSION).lower()

    def test_it_survives_the_round_trip_to_json(self, store):
        add_candidate(store)
        payload = json.loads(json.dumps(prepare_pass_a(store, cik=320193).to_dict()))
        assert AnalysisInput.from_dict(payload).screen["flag_reason"]


class TestLoad:
    def test_stores_a_finding_whose_quotes_verify(self, store):
        result = load_findings(store, cik=320193, payload={"findings": [finding()]})
        assert result.kept == 1
        assert result.dropped == 0
        row = store.execute("SELECT * FROM finding").fetchone()
        assert row["change_type"] == "strengthened"
        assert row["accession_no"] == CURRENT

    def test_drops_a_finding_whose_quote_is_not_in_the_filing(self, store):
        bad = finding(quote="four customers accounted for 81% of revenue")
        result = load_findings(store, cik=320193, payload={"findings": [bad]})
        assert result.kept == 0
        assert result.dropped == 1
        assert store.execute("SELECT COUNT(*) FROM finding").fetchone()[0] == 0

    def test_drops_a_finding_whose_prior_quote_is_invented(self, store):
        bad = finding(prior_quote="language that was never in the earlier filing at all")
        result = load_findings(store, cik=320193, payload={"findings": [bad]})
        assert result.kept == 0
        assert result.drop_reasons["prior_quote_not_found"] == 1

    def test_records_the_fabrication_rate_for_the_run(self, store):
        payload = {"findings": [finding(), finding(quote="invented text, nowhere at all")]}
        load_findings(store, cik=320193, payload=payload)
        run = store.execute("SELECT * FROM analysis_run").fetchone()
        assert run["findings_kept"] == 1
        assert run["findings_dropped"] == 1

    def test_the_run_records_which_prompt_and_model_produced_it(self, store):
        load_findings(
            store, cik=320193, payload={"findings": [finding()], "model": "claude-opus-5"}
        )
        run = store.execute("SELECT prompt_version, model FROM analysis_run").fetchone()
        assert run["prompt_version"] == PASS_A_VERSION
        assert run["model"] == "claude-opus-5"

    def test_reloading_replaces_a_run_rather_than_duplicating_it(self, store):
        for _ in range(2):
            load_findings(store, cik=320193, payload={"findings": [finding()]})
        assert store.execute("SELECT COUNT(*) FROM finding").fetchone()[0] == 1
        assert store.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0] == 1

    def test_rejects_a_finding_that_recommends_a_trade(self, store):
        """The prompt forbids it; the loader enforces it. A rule only in the prompt is
        a rule that holds until the model has an off day."""
        result = load_findings(
            store,
            cik=320193,
            payload={
                "findings": [finding(implication="Investors should buy before the next filing.")]
            },
        )
        assert result.kept == 0
        assert result.drop_reasons["malformed"] == 1

    def test_a_malformed_finding_does_not_abort_the_load(self, store):
        """One bad entry must not cost the good ones beside it."""
        payload = {"findings": [{"nonsense": True}, finding()]}
        result = load_findings(store, cik=320193, payload=payload)
        assert result.kept == 1
        assert result.drop_reasons["malformed"] == 1

    def test_accepts_the_shape_the_prompt_asks_for(self, store):
        """Round trip: what the prompt tells the model to emit is what load accepts."""
        payload = {"pass": "a", "findings": [finding()], "commentary": "Nothing else."}
        assert load_findings(store, cik=320193, payload=payload).kept == 1

    def test_stores_nothing_when_every_finding_fails(self, store):
        payload = {"findings": [finding(quote="entirely invented text, not in any filing")]}
        result = load_findings(store, cik=320193, payload=payload)
        assert result.kept == 0
        assert store.execute("SELECT COUNT(*) FROM finding").fetchone()[0] == 0
        # The run is still recorded: a pass that produced nothing usable is a fact worth
        # keeping, not an absence to be inferred later from missing rows.
        assert store.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0] == 1


class TestAnalysisInputShape:
    def test_round_trips(self, store):
        prepared = prepare_pass_a(store, cik=320193)
        assert AnalysisInput.from_dict(prepared.to_dict()).cik == prepared.cik
