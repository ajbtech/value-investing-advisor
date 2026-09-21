"""The SQLite store and its migrations.

Opening the store applies any migrations it is missing. There is no separate migrate
command to forget to run, and no state that can drift between the schema and the code
that reads it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _migrations() -> list[Path]:
    """Migration files in application order. Names begin with a zero-padded number."""
    return sorted(MIGRATIONS_DIR.glob("[0-9]*.sql"))


SCHEMA_VERSION = len(_migrations())


def _apply_migrations(conn: sqlite3.Connection) -> None:
    applied = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, path in enumerate(_migrations(), start=1):
        if version <= applied:
            continue
        with conn:  # one transaction per migration
            conn.executescript(path.read_text(encoding="utf-8"))
            # PRAGMA does not take a bound parameter; version is a loop counter, not input.
            conn.execute(f"PRAGMA user_version = {version}")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with the settings the rest of the code assumes, and migrate it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL survives a crash mid-write, which is the whole point of "write then mark".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    _apply_migrations(conn)
    return conn


@contextmanager
def open_store(path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open the store for the duration of a block, closing it afterwards."""
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()
