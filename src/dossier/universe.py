"""The universe as of a date: every filer that produced an annual report, dead or alive.

`company_tickers.json` lists current registrants only. A universe built from it has
already deleted every company that failed, and those are exactly the ones a value screen
is most likely to have flagged. EDGAR's quarterly form index lists every annual report
filed in a quarter by every filer, which makes it the historical universe itself rather
than today's tickers plus a correction.

This module finds the filers. It does not fetch them: whether to spend two requests on
each of several thousand CIKs is the caller's decision, and `dossier universe --ingest`
is where that decision is made.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from dossier.formindex import index_rows

#: What makes a filer a member of the universe: it produced the annual figures a screen
#: reads. `10-K405` and `10-KSB` are retired forms that were annual reports while they
#: existed. An amendment (`10-K/A`) is left out because the report it amends is already
#: in the index, so it can add nobody.
ANNUAL_REPORT_FORMS = frozenset({"10-K", "10-KT", "10-K405", "10-KSB"})


@dataclass(frozen=True)
class AnnualFiler:
    cik: int
    form: str
    filed_date: str
    company_name: str | None = None


def earliest_per_filer(filers: list[AnnualFiler]) -> list[AnnualFiler]:
    """One entry per CIK, at its earliest filing: when it entered the universe."""
    first: dict[int, AnnualFiler] = {}
    for filer in filers:
        held = first.get(filer.cik)
        if held is None or filer.filed_date < held.filed_date:
            first[filer.cik] = filer
    return sorted(first.values(), key=lambda f: f.cik)


def parse_annual_filers(text: str) -> list[AnnualFiler]:
    """Every filer with an annual report in one quarterly index, once each."""
    return earliest_per_filer(
        [
            AnnualFiler(
                cik=row.cik,
                form=row.form,
                filed_date=row.filed_date,
                company_name=row.company_name,
            )
            for row in index_rows(text)
            if row.form in ANNUAL_REPORT_FORMS
        ]
    )


def unheld(conn: sqlite3.Connection, filers: list[AnnualFiler]) -> list[AnnualFiler]:
    """The annual filers the store has never heard of: the survivorship gap, by name."""
    held = {row["cik"] for row in conn.execute("SELECT cik FROM filer")}
    return [filer for filer in earliest_per_filer(filers) if filer.cik not in held]
