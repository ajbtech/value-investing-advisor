"""Queries the CLI used to write itself, now owned by the module that owns each table.

Review finding 3: the command-line layer ran SQL against `filer`, `filing` and `thesis`
directly, so a schema change had to be made in the presentation layer as well as the
module that owns the table. Each query now lives beside the code that writes the rows.
"""

import pytest

from dossier.store import open_store
from tests.builders import StoreBuilder


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        b = StoreBuilder(conn)
        b.filer(3, ticker=None)
        b.filer(1, ticker="AAA")
        b.filer(2, ticker="BBB")
        b.annual(1, "2023-12-31", "2024-02-15")
        b.annual(1, "2024-12-31", "2025-02-15")
        b.annual(2, "2024-12-31", "2025-02-15", form="10-K/A")
        b.done()
        yield conn


def test_held_ciks_are_every_filer_in_order(store):
    from dossier.ingest import held_ciks

    assert held_ciks(store) == [1, 2, 3]


class TestFilingsToExtract:
    def test_a_filers_filings_of_one_form_newest_first(self, store):
        from dossier.extract import filings_to_extract

        rows = filings_to_extract(store, cik=1, form="10-K")
        assert [r["filed_date"] for r in rows] == ["2025-02-15", "2024-02-15"]

    def test_one_named_filing(self, store):
        from dossier.extract import filings_to_extract

        accession = filings_to_extract(store, cik=1, form="10-K")[0]["accession_no"]
        [row] = filings_to_extract(store, accession=accession)
        assert row["accession_no"] == accession

    def test_needs_a_filer_or_an_accession(self, store):
        from dossier.extract import filings_to_extract

        with pytest.raises(ValueError):
            filings_to_extract(store)


class TestPriceQueries:
    def test_priceable_ciks_are_those_with_a_ticker(self, store):
        from dossier.prices import priceable_ciks

        assert priceable_ciks(store) == [1, 2]

    def test_ticker_of_distinguishes_no_ticker_from_no_filer(self, store):
        from dossier.prices import NOT_INGESTED, ticker_of

        assert ticker_of(store, 1) == "AAA"
        assert ticker_of(store, 3) is None
        assert ticker_of(store, 999) is NOT_INGESTED


def test_latest_thesis_date_is_none_without_a_thesis(store):
    from dossier.thesis import latest_as_of

    assert latest_as_of(store, 1) is None


def test_row_counts_for_status(store):
    from dossier.store import row_counts

    assert row_counts(store) == {"filers": 3, "filings": 3}
