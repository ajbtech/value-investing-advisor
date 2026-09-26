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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")

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


def read_quarters(
    fetch: Callable[[int, int], str],
    from_year: int,
    to_year: int,
    *,
    parse: Callable[[str], list[T]],
    label: str,
) -> tuple[list[T], list[dict]]:
    """Fetch and parse every quarterly index from `from_year` to `to_year` inclusive.

    Returns everything parsed, and one report per quarter: how many records it
    yielded under `label`, or the error that stopped it. A quarter that has not
    happened yet, or a gap in the archive, is not a reason to lose the quarters that
    did parse.
    """
    found: list[T] = []
    quarters: list[dict] = []
    for year in range(from_year, to_year + 1):
        for quarter in (1, 2, 3, 4):
            try:
                text = fetch(year, quarter)
            except Exception as exc:
                quarters.append({"year": year, "quarter": quarter, "error": str(exc)[:120]})
                continue
            parsed = parse(text)
            found.extend(parsed)
            quarters.append({"year": year, "quarter": quarter, label: len(parsed)})
    return found, quarters


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
