"""Who has ever filed an annual report, from EDGAR's quarterly form index.

The universe filter the plan specifies is "all CIKs with a 10-K filed in the last 15
months", built as of the test date and including filers that later deregistered.
`company_tickers.json` cannot answer that: it lists what trades today. Every company that
failed has been removed from it, and those are exactly the ones a value screen is most
likely to have flagged — which is how survivorship bias manufactures an edge out of
nothing.

The same index that finds the dead lists every annual report filed in a quarter. That is
the historical universe directly, with nothing selected for having survived and no
correction to apply afterwards. Correcting a survivor list by adding the dead inverts the
bias rather than removing it: a universe of survivors plus a cohort chosen for dying is
not a market either.

A `registrant` row claims nothing except that a CIK filed an annual report on a date.
Screening needs facts and prices per filer, so the table's job is to say precisely who is
missing and what ingesting them would cost.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from dossier.deregistrations import _ROW

#: The forms that make a filer screenable: an annual report with financial statements.
#: `10-KSB` is the small-business form retired in 2009, `20-F` and `40-F` are the foreign
#: private issuer and Canadian equivalents — a historical universe needs all of them,
#: since a 2010 universe is full of forms nobody files now.
ANNUAL_FORMS: frozenset[str] = frozenset(
    {"10-K", "10-K/A", "10-K405", "10-KSB", "10-KSB405", "20-F", "20-F/A", "40-F", "40-F/A"}
)


@dataclass(frozen=True)
class Registrant:
    cik: int
    form: str
    filed_date: str
    company_name: str | None = None


def parse_annual_filers(text: str) -> list[Registrant]:
    """Every annual report in one quarterly index."""
    found = []
    for line in text.splitlines():
        match = _ROW.match(line)
        if match is None:
            continue
        form, name, cik, filed, _path = match.groups()
        if form.upper() not in ANNUAL_FORMS:
            continue
        found.append(
            Registrant(
                cik=int(cik),
                form=form.upper(),
                filed_date=filed,
                company_name=name.strip() or None,
            )
        )
    return found


def store_registrants(conn: sqlite3.Connection, records: list[Registrant]) -> int:
    """Record the annual reports. Returns the number of CIKs not seen before.

    Re-reading a quarter is a no-op: one row per (filer, filing date), so a second pass
    cannot inflate a filer's history. The count returned is of genuinely new filers,
    which is the number worth watching as the years are read.
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    known = {row["cik"] for row in conn.execute("SELECT DISTINCT cik FROM registrant_annual")}
    added = 0
    with conn:
        for record in records:
            conn.execute(
                "INSERT INTO registrant_annual (cik, filed_date, form, name, seen_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(cik, filed_date) DO UPDATE SET "
                "    name = COALESCE(excluded.name, registrant_annual.name)",
                (record.cik, record.filed_date, record.form, record.company_name, now),
            )
            if record.cik not in known:
                known.add(record.cik)
                added += 1
    return added


def coverage(conn: sqlite3.Connection, since: str | None = None) -> dict:
    """How much of the real universe the store actually holds.

    The number that gives every other number its meaning: screening 289 filers says
    nothing until it is 289 *of something*, and the ticker map could never say what.

    `since` narrows it to filers whose most recent annual report is on or after a date,
    because a universe is always as of a date: a filer that last reported in 2019 is not
    missing from a 2026 universe, it is correctly absent from it.
    """
    where = "WHERE last_annual >= ?" if since else ""
    params = (since,) if since else ()
    registrants = conn.execute(f"SELECT COUNT(*) AS n FROM registrant {where}", params).fetchone()[
        "n"
    ]
    ingested = conn.execute(
        f"SELECT COUNT(*) AS n FROM registrant r {where} "
        f"{'AND' if since else 'WHERE'} EXISTS (SELECT 1 FROM filer f WHERE f.cik = r.cik)",
        params,
    ).fetchone()["n"]
    return {
        "since": since,
        "registrants": registrants,
        "ingested": ingested,
        "missing": registrants - ingested,
        "fraction_held": None if registrants == 0 else round(ingested / registrants, 4),
    }
