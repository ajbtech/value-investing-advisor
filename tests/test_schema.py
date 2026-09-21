"""The store's schema is where two of the three data-integrity rules are enforced.

`filed_date` lives on every fact, and a restated fact never overwrites the original.
Both are structural properties, so they are tested against the schema itself rather
than against the code that happens to write through it.
"""

import sqlite3

import pytest

from dossier.store import SCHEMA_VERSION, open_store


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        yield conn


def table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def columns(conn, table):
    return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}


class TestMigrations:
    def test_creates_the_core_tables(self, store):
        assert {"filer", "filing", "fact", "document_section", "dossier"} <= table_names(store)

    def test_records_the_schema_version(self, store):
        assert store.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    def test_is_idempotent(self, tmp_path):
        path = tmp_path / "twice.sqlite"
        with open_store(path) as conn:
            conn.execute("INSERT INTO filer (cik, name) VALUES (?, ?)", (320193, "Apple Inc."))
            conn.commit()
        with open_store(path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert conn.execute("SELECT COUNT(*) FROM filer").fetchone()[0] == 1

    def test_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "store.sqlite"
        with open_store(path) as conn:
            assert table_names(conn)
        assert path.exists()

    def test_enforces_foreign_keys(self, store):
        with pytest.raises(sqlite3.IntegrityError):
            store.execute(
                "INSERT INTO filing (accession_no, cik, form_type, filed_date) VALUES (?, ?, ?, ?)",
                ("0000320193-24-000123", 999999, "10-K", "2024-11-01"),
            )

    def test_rows_come_back_by_name(self, store):
        store.execute("INSERT INTO filer (cik, name) VALUES (?, ?)", (320193, "Apple Inc."))
        assert store.execute("SELECT name FROM filer").fetchone()["name"] == "Apple Inc."


class TestFactTable:
    """The `filed_date` column is what separates an honest backtest from a fantasy one."""

    def test_fact_carries_filed_date_and_period_end(self, store):
        cols = columns(store, "fact")
        assert "filed_date" in cols
        assert "period_end" in cols

    def test_filed_date_is_mandatory(self, store):
        assert columns(store, "fact")["filed_date"]["notnull"] == 1

    def test_a_restatement_does_not_overwrite_the_original(self, store):
        """companyfacts returns today's restated figures. Keep every version, keyed by
        the accession number that reported it, so a screen can ask what was known then."""
        store.execute("INSERT INTO filer (cik, name) VALUES (320193, 'Apple Inc.')")
        for accession, filed, value in [
            ("0000320193-23-000106", "2023-11-03", 383_285_000_000),
            ("0000320193-24-000123", "2024-11-01", 383_000_000_000),
        ]:
            store.execute(
                "INSERT INTO filing (accession_no, cik, form_type, filed_date) "
                "VALUES (?, 320193, '10-K', ?)",
                (accession, filed),
            )
            store.execute(
                "INSERT INTO fact "
                "(accession_no, cik, tag, unit, period_start, period_end, value, filed_date) "
                "VALUES (?, 320193, 'Revenues', 'USD', '2022-09-25', '2023-09-30', ?, ?)",
                (accession, value, filed),
            )

        values = [
            row["value"]
            for row in store.execute(
                "SELECT value FROM fact WHERE period_end = '2023-09-30' ORDER BY filed_date"
            )
        ]
        assert values == [383_285_000_000, 383_000_000_000]

    def test_the_same_fact_from_the_same_filing_is_stored_once(self, store):
        store.execute("INSERT INTO filer (cik, name) VALUES (320193, 'Apple Inc.')")
        store.execute(
            "INSERT INTO filing (accession_no, cik, form_type, filed_date) "
            "VALUES ('0000320193-24-000123', 320193, '10-K', '2024-11-01')"
        )
        insert = (
            "INSERT INTO fact "
            "(accession_no, cik, tag, unit, period_start, period_end, value, filed_date) "
            "VALUES ('0000320193-24-000123', 320193, 'Revenues', 'USD', "
            "'2023-10-01', '2024-09-28', 391000000000, '2024-11-01')"
        )
        store.execute(insert)
        with pytest.raises(sqlite3.IntegrityError):
            store.execute(insert)


class TestFilerTable:
    """Filers who went bankrupt stop filing. A universe built from current filers has
    quietly deleted its own failures — exactly the population a value screen flags."""

    def test_retains_delisted_filers_with_a_terminal_status(self, store):
        store.execute(
            "INSERT INTO filer (cik, name, status, status_date) VALUES (?, ?, ?, ?)",
            (1090727, "Bed Bath & Beyond Inc.", "deregistered", "2023-09-29"),
        )
        row = store.execute("SELECT status, status_date FROM filer").fetchone()
        assert row["status"] == "deregistered"
        assert row["status_date"] == "2023-09-29"

    def test_defaults_to_active(self, store):
        store.execute("INSERT INTO filer (cik, name) VALUES (320193, 'Apple Inc.')")
        assert store.execute("SELECT status FROM filer").fetchone()["status"] == "active"

    def test_rejects_an_unknown_status(self, store):
        with pytest.raises(sqlite3.IntegrityError):
            store.execute(
                "INSERT INTO filer (cik, name, status) VALUES (?, ?, ?)",
                (320193, "Apple Inc.", "probably fine"),
            )


class TestDocumentSectionTable:
    """Item boundaries are inconsistent across filers and years, so the analysis layer
    needs to be able to refuse to reason over a bad parse."""

    def test_carries_an_extraction_confidence(self, store):
        store.execute("INSERT INTO filer (cik, name) VALUES (320193, 'Apple Inc.')")
        store.execute(
            "INSERT INTO filing (accession_no, cik, form_type, filed_date) "
            "VALUES ('0000320193-24-000123', 320193, '10-K', '2024-11-01')"
        )
        store.execute(
            "INSERT INTO document_section "
            "(accession_no, item, text, extraction_confidence) VALUES (?, ?, ?, ?)",
            ("0000320193-24-000123", "1A", "Risk Factors ...", 0.93),
        )
        row = store.execute("SELECT item, extraction_confidence FROM document_section").fetchone()
        assert row["item"] == "1A"
        assert row["extraction_confidence"] == pytest.approx(0.93)
