"""Pass B — footnote forensics.

The footnotes are where the accounting choices live, and they are enormous: Kodak's
Item 8 is 209,515 characters. Pass A could hand over both sections whole; Pass B cannot,
so `prepare` splits the notes and hands over an index, and a pass reads the notes it
needs rather than the filing it cannot.

The flags are not the diff types Pass A uses. A depreciation life that was extended is
not "added" or "softened" — it is a change of estimate, and calling it by its name is
what makes a list of flags worth reading.
"""

import pytest

from dossier.analysis import (
    load_findings,
    prepare_pass_b,
    prompt_version_for,
    split_notes,
)
from dossier.findings import FOOTNOTE_FLAGS, Finding
from dossier.store import open_store

CIK = 31235
CURRENT = "0001193125-26-104214"
PRIOR = "0000950170-25-040256"

NOTES = """
NOTE 1: BASIS OF PRESENTATION AND SIGNIFICANT ACCOUNTING POLICIES
Kodak prepares its financial statements in accordance with U.S. GAAP. Property, plant
and equipment is depreciated over estimated useful lives of three to forty years.

NOTE 17: RETIREMENT PLANS
On November 26, 2025, all pension obligations under KRIP had been fully settled and the
excess pension assets of $1.023 billion reverted to the Company. The discount rate used
to measure the obligation was 5.4%, compared with 4.9% in the prior year.

NOTE 22: RELATED PARTY TRANSACTIONS
The Company purchased $4 million of materials from an entity controlled by a member of
the Board of Directors during the year ended December 31, 2025.
"""

PRIOR_NOTES = """
NOTE 1: BASIS OF PRESENTATION AND SIGNIFICANT ACCOUNTING POLICIES
Kodak prepares its financial statements in accordance with U.S. GAAP. Property, plant
and equipment is depreciated over estimated useful lives of three to thirty years.

NOTE 17: RETIREMENT PLANS
The Board of Directors approved the termination of KRIP effective March 31, 2025. The
discount rate used to measure the obligation was 4.9%.
"""


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        conn.execute("INSERT INTO filer (cik, name) VALUES (?, 'EASTMAN KODAK CO')", (CIK,))
        for accession, filed, text in (
            (CURRENT, "2026-03-12", NOTES),
            (PRIOR, "2025-03-17", PRIOR_NOTES),
        ):
            conn.execute(
                "INSERT INTO filing (accession_no, cik, form_type, filed_date, "
                "primary_doc_url) VALUES (?, ?, '10-K', ?, 'https://example.com/f.htm')",
                (accession, CIK, filed),
            )
            conn.execute(
                "INSERT INTO document_section (accession_no, item, text, "
                "extraction_confidence, char_count) VALUES (?, '8', ?, 0.8, ?)",
                (accession, text, len(text)),
            )
        conn.commit()
        yield conn


class TestSplittingTheNotes:
    def test_it_finds_each_note_with_its_heading(self):
        notes = split_notes(NOTES)
        assert [n["number"] for n in notes] == ["1", "17", "22"]
        assert "RETIREMENT PLANS" in notes[1]["heading"]
        assert "1.023 billion" in notes[1]["text"]

    def test_a_section_with_no_note_headings_comes_back_whole(self):
        """An incorporation-by-reference stub, or a parse that went wrong. Returning
        nothing would look like a filing with no footnotes."""
        notes = split_notes("The financial statements are included in Part IV, Item 15.")
        assert len(notes) == 1
        assert notes[0]["number"] is None

    def test_headings_are_matched_in_several_house_styles(self):
        text = "Note 3 — Inventories\nFIFO.\nNOTE 4. Debt\nRevolver.\nNote 5: Leases\nASC 842."
        assert [n["number"] for n in split_notes(text)] == ["3", "4", "5"]


class TestPrepare:
    def test_it_hands_over_an_index_rather_than_one_wall_of_text(self, store):
        """Kodak's Item 8 is 209,515 characters. An index of notes with their sizes is
        what makes the pass navigable; the text is there for the notes that matter."""
        prepared = prepare_pass_b(store, cik=CIK)
        index = prepared["notes"]
        assert [n["number"] for n in index] == ["1", "17", "22"]
        assert all("char_count" in n for n in index)
        assert prepared["prompt_version"] == prompt_version_for("8", pass_name="b")

    def test_it_carries_the_prior_year_notes_for_comparison(self, store):
        """A depreciation life that was extended is only visible against last year."""
        prepared = prepare_pass_b(store, cik=CIK)
        assert prepared["prior"]["accession_no"] == PRIOR
        assert [n["number"] for n in prepared["prior_notes"]] == ["1", "17"]

    def test_a_filer_with_one_filing_still_prepares(self, store):
        """Unlike Pass A, this pass reads a filing rather than a diff, so one is enough
        — the prior year is context, not a requirement."""
        store.execute("DELETE FROM document_section WHERE accession_no = ?", (PRIOR,))
        store.commit()
        prepared = prepare_pass_b(store, cik=CIK)
        assert prepared["prior"] is None
        assert prepared["prior_notes"] == []


class TestTheFlagsAreFootnoteFlags:
    def test_a_footnote_flag_is_accepted(self):
        finding = Finding(
            accession_no=CURRENT,
            item="8",
            change_type="estimate_change",
            quote="depreciated over estimated useful lives of three to forty years",
            implication="Useful lives were extended from thirty to forty years.",
            severity="high",
        )
        assert finding.change_type == "estimate_change"

    def test_the_flag_vocabulary_names_what_the_plan_asks_for(self):
        for flag in ("policy_change", "estimate_change", "related_party", "off_balance_sheet"):
            assert flag in FOOTNOTE_FLAGS

    def test_an_invented_flag_is_still_refused(self):
        with pytest.raises(ValueError, match="change_type"):
            Finding(
                accession_no=CURRENT,
                item="8",
                change_type="looks_bad",
                quote="depreciated over estimated useful lives of three to forty years",
                implication="Something.",
                severity="high",
            )


class TestLoad:
    def test_a_flag_whose_quote_verifies_is_stored_against_pass_b(self, store):
        result = load_findings(
            store,
            cik=CIK,
            item="8",
            pass_name="b",
            payload={
                "findings": [
                    {
                        "accession_no": CURRENT,
                        "item": "8",
                        "change_type": "estimate_change",
                        "quote": "depreciated over estimated useful lives of three to forty years",
                        "prior_accession_no": PRIOR,
                        "prior_quote": (
                            "depreciated over estimated useful lives of three to thirty years"
                        ),
                        "implication": "Useful lives were extended, which lowers depreciation.",
                        "severity": "high",
                    }
                ]
            },
        )
        assert (result.kept, result.dropped) == (1, 0)
        row = store.execute("SELECT pass, prompt_version FROM finding").fetchone()
        assert row["pass"] == "b"
        assert row["prompt_version"] == prompt_version_for("8", pass_name="b")

    def test_the_prompt_warns_that_notes_are_renumbered(self, store):
        """Found live: Kodak's Note 13 is Guarantees this year and was Financial
        Instruments last year. Comparing by number compares two unrelated disclosures."""
        from dossier.analysis import prompt_text

        assert "by heading" in prompt_text(prompt_version_for("8", pass_name="b"))

    def test_pass_b_does_not_read_item_1a(self, store):
        """Each pass has its own sections. Running the footnote prompt over risk factors
        would produce flags about the wrong document."""
        with pytest.raises(ValueError, match="no Pass B prompt"):
            prompt_version_for("1A", pass_name="b")
