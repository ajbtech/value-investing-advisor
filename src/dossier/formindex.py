"""EDGAR's quarterly form index: every filing of every form in a quarter.

One fixed-column text file per quarter, free and without an account:

    https://www.sec.gov/Archives/edgar/full-index/2020/QTR1/form.idx

It is the only list of filers that includes the ones that stopped filing, which is why
two things read it: `dossier.deregistrations` for who died, and `dossier.universe` for
who was there at all. They share this parser so the two can never disagree about what a
row says.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

#: `form.idx` is column-aligned, not delimited: form type, company name, CIK, date, path.
#: Splitting on runs of two or more spaces survives company names containing single
#: spaces, which splitting on whitespace does not. A form type with a space in it (`DEF
#: 14A`) does not match, and nothing that reads this index wants one.
_ROW = re.compile(r"^(\S+)\s{2,}(.+?)\s{2,}(\d{1,10})\s{2,}(\d{4}-\d{2}-\d{2})\s{2,}(\S+)\s*$")


@dataclass(frozen=True)
class IndexRow:
    form: str
    company_name: str | None
    cik: int
    filed_date: str
    path: str


def form_index_url(year: int, quarter: int) -> str:
    """The quarterly index of every filing by form type."""
    if quarter not in (1, 2, 3, 4):
        raise ValueError(f"quarter must be 1-4, got {quarter}")
    return f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/form.idx"


def index_rows(text: str) -> Iterator[IndexRow]:
    """Every filing row in one quarterly index; header lines are skipped."""
    for line in text.splitlines():
        match = _ROW.match(line)
        if match is None:
            continue
        form, name, cik, filed, path = match.groups()
        yield IndexRow(
            form=form.upper(),
            company_name=name.strip() or None,
            cik=int(cik),
            filed_date=filed,
            path=path,
        )
