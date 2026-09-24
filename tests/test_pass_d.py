"""Pass D — proxy incentives.

Compensation metrics predict behaviour better than strategy slides. The pass answers one
question — what is management paid on — and that is a standing fact, not only a change,
which is why `metric_mix` exists alongside `metric_change`.
"""

import pytest

from dossier.analysis import load_findings, prepare_pass_d, prompt_version_for
from dossier.findings import INCENTIVE_FLAGS
from dossier.store import open_store

CIK = 57131
CURRENT = "0000057131-26-000024"
PRIOR = "0000057131-25-000070"
CDA = (
    "The Compensation Committee selected sales and operating margin as the financial "
    "performance metrics to focus management on growing the business profitably."
)


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        conn.execute("INSERT INTO filer (cik, name) VALUES (?, 'LA-Z-BOY INC')", (CIK,))
        for accession, filed in ((CURRENT, "2026-07-15"), (PRIOR, "2025-07-16")):
            conn.execute(
                "INSERT INTO filing (accession_no, cik, form_type, filed_date, "
                "primary_doc_url) VALUES (?, ?, 'DEF 14A', ?, 'https://example.com/p.htm')",
                (accession, CIK, filed),
            )
            for item, text in (("CDA", CDA), ("RELATED_PERSON", "No transactions.")):
                conn.execute(
                    "INSERT INTO document_section (accession_no, item, text, "
                    "extraction_confidence, char_count) VALUES (?, ?, ?, 0.9, ?)",
                    (accession, item, text, len(text)),
                )
        # A 10-K as well, to prove the pass does not reach for the wrong form.
        conn.execute(
            "INSERT INTO filing (accession_no, cik, form_type, filed_date) "
            "VALUES ('0000057131-26-000019', ?, '10-K', '2026-06-16')",
            (CIK,),
        )
        conn.execute(
            "INSERT INTO document_section (accession_no, item, text, "
            "extraction_confidence, char_count) VALUES "
            "('0000057131-26-000019', '7', 'MD&A text', 1.0, 9)"
        )
        conn.commit()
        yield conn


class TestPrepare:
    def test_it_reads_the_proxy_not_the_annual_report(self, store):
        prepared = prepare_pass_d(store, cik=CIK)
        assert prepared["proxy"]["accession_no"] == CURRENT
        assert "CDA" in prepared["proxy"]["sections"]
        assert "7" not in prepared["proxy"]["sections"]

    def test_it_carries_last_years_proxy_because_the_pass_is_a_comparison(self, store):
        """A weighting means nothing until the reader can see what it was."""
        prepared = prepare_pass_d(store, cik=CIK)
        assert prepared["prior_proxy"]["accession_no"] == PRIOR
        assert prepared["prior_proxy"]["sections"]["CDA"]["text"] == CDA

    def test_a_filer_with_one_proxy_still_prepares(self, store):
        store.execute("DELETE FROM document_section WHERE accession_no = ?", (PRIOR,))
        store.commit()
        assert prepare_pass_d(store, cik=CIK)["prior_proxy"] is None

    def test_a_filer_with_no_extracted_proxy_is_told_which_command_to_run(self, store):
        store.execute("DELETE FROM document_section WHERE accession_no IN (?, ?)", (CURRENT, PRIOR))
        store.commit()
        with pytest.raises(ValueError, match="DEF 14A"):
            prepare_pass_d(store, cik=CIK)


class TestTheFlags:
    def test_the_standing_mix_has_a_flag_of_its_own(self):
        """What management is paid on is the question this pass asks, and the answer is
        worth reporting whether or not it changed this year."""
        assert "metric_mix" in INCENTIVE_FLAGS

    def test_a_finding_is_stored_against_pass_d(self, store):
        result = load_findings(
            store,
            cik=CIK,
            item="CDA",
            pass_name="d",
            payload={
                "findings": [
                    {
                        "accession_no": CURRENT,
                        "item": "CDA",
                        "change_type": "metric_mix",
                        "quote": (
                            "selected sales and operating margin as the financial "
                            "performance metrics"
                        ),
                        "implication": "The annual incentive pays on absolute sales.",
                        "severity": "high",
                    }
                ]
            },
        )
        assert (result.kept, result.dropped) == (1, 0)
        row = store.execute("SELECT pass, prompt_version FROM finding").fetchone()
        assert row["pass"] == "d"
        assert row["prompt_version"] == prompt_version_for("CDA", pass_name="d")

    def test_pass_d_does_not_read_the_footnotes(self, store):
        with pytest.raises(ValueError, match="no Pass D prompt"):
            prompt_version_for("8", pass_name="d")
