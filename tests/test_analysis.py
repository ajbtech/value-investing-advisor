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
