"""`dossier extract`: filings in, document_section rows out.

Stages talk only through the store, so this one reads the filing table and writes
document_section. Extraction is one job per filing, which is the right size: filings are
immutable once filed, so a completed extraction is valid forever and a re-run costs
nothing for work already done.
"""

from pathlib import Path

import pytest

from dossier.extract import extract_filing, store_sections
from dossier.store import open_store

FILINGS = Path(__file__).parent / "fixtures" / "filings"
ACCESSION = "0000320193-24-000123"


def filing_html(name: str) -> str:
    return (FILINGS / f"{name}.html").read_text(encoding="utf-8")


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        conn.execute("INSERT INTO filer (cik, name) VALUES (320193, 'Acme Widgets Inc.')")
        conn.execute(
            "INSERT INTO filing (accession_no, cik, form_type, filed_date, primary_doc_url) "
            "VALUES (?, 320193, '10-K', '2024-11-01', 'https://www.sec.gov/Archives/x.htm')",
            (ACCESSION,),
        )
        conn.commit()
        yield conn


class TestStoringSections:
    def test_writes_a_row_per_section(self, store):
        result = extract_filing(store, ACCESSION, filing_html("tenk_with_toc"))
        rows = store.execute(
            "SELECT item FROM document_section WHERE accession_no = ?", (ACCESSION,)
        ).fetchall()
        assert {row["item"] for row in rows} >= {"1A", "7", "7A"}
        assert result.sections == len(rows)

    def test_stores_the_text_and_its_length(self, store):
        extract_filing(store, ACCESSION, filing_html("tenk_with_toc"))
        row = store.execute(
            "SELECT text, char_count FROM document_section WHERE item = '1A'"
        ).fetchone()
        assert "SENTINEL_1A_START" in row["text"]
        assert row["char_count"] == len(row["text"])

    def test_stores_the_confidence(self, store):
        extract_filing(store, ACCESSION, filing_html("tenk_with_toc"))
        row = store.execute(
            "SELECT extraction_confidence FROM document_section WHERE item = '1A'"
        ).fetchone()
        assert row["extraction_confidence"] >= 0.8

    def test_stores_how_the_section_was_bounded(self, store):
        """A low confidence score says a parse is doubtful. These say *why*, which is
        what an operator actually needs when a section comes out wrong."""
        extract_filing(store, ACCESSION, filing_html("tenk_with_toc"))
        row = store.execute(
            "SELECT heading, ended_at FROM document_section WHERE item = '1A'"
        ).fetchone()
        assert row["heading"] == "Item 1A. Risk Factors"
        assert row["ended_at"] == "1B"

    def test_records_when_it_was_extracted(self, store):
        extract_filing(store, ACCESSION, filing_html("tenk_with_toc"))
        row = store.execute("SELECT extracted_at FROM document_section LIMIT 1").fetchone()
        assert row["extracted_at"]

    def test_keeps_a_low_confidence_section_rather_than_dropping_it(self, store):
        """Dropping it would leave the analysis layer unable to tell a bad parse from a
        section the filer never wrote. Store it, flagged, and let that layer refuse it."""
        html = (
            "<p>PART I</p><p>Item 1A. Risk Factors</p><p>None.</p>"
            "<p>Item 1B. Unresolved Staff Comments</p><p>None.</p>"
        )
        extract_filing(store, ACCESSION, html)
        row = store.execute(
            "SELECT extraction_confidence FROM document_section WHERE item = '1A'"
        ).fetchone()
        assert row is not None
        assert row["extraction_confidence"] < 0.6

    def test_re_extracting_replaces_rather_than_duplicates(self, store):
        """Filings never change, but the extractor does. A better parse must be able to
        supersede a worse one without leaving both behind."""
        extract_filing(store, ACCESSION, filing_html("tenk_with_toc"))
        extract_filing(store, ACCESSION, filing_html("tenk_with_toc"))
        count = store.execute("SELECT COUNT(*) FROM document_section WHERE item = '1A'").fetchone()[
            0
        ]
        assert count == 1

    def test_refuses_a_filing_the_store_does_not_have(self, store):
        """The foreign key is doing real work: a section with no filing has no citation,
        and a finding without a citation is a bug by the first non-negotiable."""
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            store_sections(store, "0000000000-00-000000", {})
            extract_filing(store, "0000000000-00-000000", filing_html("tenk_with_toc"))

    def test_reports_the_weakest_section_it_stored(self, store):
        """So a caller can spot a filing worth looking at by hand without re-querying."""
        result = extract_filing(store, ACCESSION, filing_html("tenk_with_toc"))
        assert 0.0 <= result.lowest_confidence <= 1.0
        assert result.items
