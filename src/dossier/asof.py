"""The point-in-time gateway: the only read path to the `fact` table.

Every query carries an as-of date and filters `filed_date <= as_of`. There is no other
way in, and a test asserts that no other module reads the table. Lookahead bias is a
property you either enforce structurally or discover, much later, as an edge that was
never there.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

#: Statuses that mean a filer has stopped filing. A universe built as of a date must
#: still contain the ones that had not failed yet.
TERMINAL_STATUSES = ("deregistered", "delisted_for_cause", "acquired")


def _as_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"as_of must be a date or an ISO date string, got {value!r}") from exc


@dataclass(frozen=True)
class Fact:
    cik: int
    tag: str
    unit: str
    period_start: str
    period_end: str
    value: float
    filed_date: str
    accession_no: str
    fiscal_year: int | None = None
    fiscal_period: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Fact:
        return cls(
            cik=row["cik"],
            tag=row["tag"],
            unit=row["unit"],
            period_start=row["period_start"],
            period_end=row["period_end"],
            value=row["value"],
            filed_date=row["filed_date"],
            accession_no=row["accession_no"],
            fiscal_year=row["fiscal_year"],
            fiscal_period=row["fiscal_period"],
        )


@dataclass(frozen=True)
class Price:
    cik: int
    ticker: str
    price_date: str
    close: float
    source: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Price:
        return cls(
            cik=row["cik"],
            ticker=row["ticker"],
            price_date=row["price_date"],
            close=row["close"],
            source=row["source"],
        )


@dataclass(frozen=True)
class Filer:
    cik: int
    name: str
    ticker: str | None
    sic: str | None
    status: str
    status_date: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Filer:
        return cls(
            cik=row["cik"],
            name=row["name"],
            ticker=row["ticker"],
            sic=row["sic"],
            status=row["status"],
            status_date=row["status_date"],
        )


class AsOfView:
    """A read-only view of the store as it could have been known on one date."""

    def __init__(self, conn: sqlite3.Connection, as_of: date | str) -> None:
        self.conn = conn
        self.as_of = _as_date(as_of)

    @property
    def _as_of(self) -> str:
        return self.as_of.isoformat()

    def __repr__(self) -> str:
        return f"AsOfView(as_of={self._as_of})"

    # -- facts ---------------------------------------------------------------

    def facts(
        self,
        cik: int,
        tag: str | None = None,
        *,
        period_end: str | None = None,
    ) -> list[Fact]:
        """Every version of every matching fact that had been filed by the as-of date.

        Ordered oldest filing first, so the last element is the most recent version
        that was knowable then.
        """
        sql = ["SELECT * FROM fact WHERE cik = ? AND filed_date <= ?"]
        params: list = [cik, self._as_of]
        if tag is not None:
            sql.append("AND tag = ?")
            params.append(tag)
        if period_end is not None:
            sql.append("AND period_end = ?")
            params.append(period_end)
        sql.append("ORDER BY period_end, filed_date, accession_no")
        rows = self.conn.execute(" ".join(sql), params).fetchall()
        return [Fact.from_row(row) for row in rows]

    def latest_fact(self, cik: int, tag: str, period_end: str) -> Fact | None:
        """The value for one period as most recently reported *as of* this date.

        Before a restatement was filed this is the original figure; afterwards it is
        the restated one. Both remain on disk either way.
        """
        matches = self.facts(cik, tag, period_end=period_end)
        return matches[-1] if matches else None

    def snapshot(self, cik: int, tags: list[str] | None = None) -> list[Fact]:
        """One row per (tag, unit, period): the state of knowledge on the as-of date."""
        latest: dict[tuple, Fact] = {}
        for fact in self.facts(cik):
            if tags is not None and fact.tag not in tags:
                continue
            latest[(fact.tag, fact.unit, fact.period_start, fact.period_end)] = fact
        return sorted(latest.values(), key=lambda f: (f.tag, f.period_end))

    # -- prices --------------------------------------------------------------

    def price(self, cik: int) -> Price | None:
        """The last close on or before the as-of date.

        The caller sees `price_date`, so a stale price — a filer that stopped trading —
        is visible as stale rather than silently standing in for today's.
        """
        row = self.conn.execute(
            "SELECT * FROM price WHERE cik = ? AND price_date <= ? "
            "ORDER BY price_date DESC LIMIT 1",
            (cik, self._as_of),
        ).fetchone()
        return Price.from_row(row) if row else None

    # -- filings -------------------------------------------------------------

    def filings(self, cik: int, form_type: str | None = None) -> list[sqlite3.Row]:
        sql = ["SELECT * FROM filing WHERE cik = ? AND filed_date <= ?"]
        params: list = [cik, self._as_of]
        if form_type is not None:
            sql.append("AND form_type = ?")
            params.append(form_type)
        sql.append("ORDER BY filed_date DESC")
        return self.conn.execute(" ".join(sql), params).fetchall()

    # -- universe ------------------------------------------------------------

    def universe(self) -> list[Filer]:
        """Filers that were filing as of this date, including ones that later failed.

        A filer counts if it had started filing by the as-of date and had not yet hit
        a terminal status. Building the universe from *today's* filers instead is how
        a value screen quietly deletes the failures it would have flagged.
        """
        placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
        rows = self.conn.execute(
            f"""
            SELECT * FROM filer
            WHERE (first_seen IS NULL OR first_seen <= ?)
              AND (
                    status NOT IN ({placeholders})
                    OR status_date IS NULL
                    OR status_date > ?
                  )
            ORDER BY cik
            """,
            [self._as_of, *TERMINAL_STATUSES, self._as_of],
        ).fetchall()
        return [Filer.from_row(row) for row in rows]

    def terminated(self, since: date | str | None = None) -> list[Filer]:
        """Filers that stopped filing on or before the as-of date.

        A delisting for cause is a total loss in any historical evaluation, not a
        missing value, so the population has to be recoverable.
        """
        placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
        params: list = [*TERMINAL_STATUSES, self._as_of]
        sql = f"""
            SELECT * FROM filer
            WHERE status IN ({placeholders})
              AND status_date IS NOT NULL
              AND status_date <= ?
        """
        if since is not None:
            sql += " AND status_date >= ?"
            params.append(_as_date(since).isoformat())
        sql += " ORDER BY status_date, cik"
        return [Filer.from_row(row) for row in self.conn.execute(sql, params)]


def fact_count(conn: sqlite3.Connection) -> int:
    """How many fact rows the store holds, for status output.

    This is not an as-of question, but it lives here anyway: the invariant is that
    this module is the *only* one that reads the fact table, and an exception for
    "harmless" reads is how that invariant would quietly die.
    """
    return conn.execute("SELECT COUNT(*) FROM fact").fetchone()[0]
