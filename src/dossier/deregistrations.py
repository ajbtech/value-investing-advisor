"""Which filers stopped filing, and why.

Survivorship is the bias that manufactures an edge out of nothing. A universe built from
`company_tickers.json` lists the companies that trade today; every company that failed
has been quietly removed from it, and those are exactly the ones a value screen is most
likely to have flagged. `filer.status` has been in the schema since the first migration
for this reason, and until now nothing set it, because nothing knew which CIKs had died.

EDGAR's quarterly form index knows. It lists every filing of every form in a quarter, as
a fixed-column text file, free and without an account:

    https://www.sec.gov/Archives/edgar/full-index/2020/QTR1/form.idx

Two form families end a public company's reporting life, and they are not the same fact:

- **Form 15** deregisters the securities. The company stops filing. It is often voluntary
  — going private, a merger closing, falling under the holder threshold.
- **Form 25** delists a security from an exchange. When the exchange files it, the usual
  reason is failure to meet listing standards, which in a historical evaluation is a
  total loss rather than a missing value.

Both usually follow one failure within days of each other, so the more specific status
wins and is never softened by a later filing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from dossier.formindex import index_rows

#: The forms worth reading, mapped to the status each one can support on its own.
#:
#: Form 25 supports **nothing**, and learning that cost one live run. Scanning 2025 marked
#: 34 of the store's filers and 32 of them filed a 10-K afterwards — IBM, Procter &
#: Gamble, General Electric, Thermo Fisher. Form 25 delists *a security*: a company
#: retiring one note issue files it and carries on reporting. It is collected because it
#: is a useful pointer, and it is never a status by itself.
#:
#: `delisted_for_cause` appears nowhere here on purpose. In a historical evaluation it
#: means a total loss, and the index carries no reason for any filing, so no row built
#: from it may claim one.
TERMINAL_FORMS: dict[str, str | None] = {
    "15-12B": "deregistered",
    "15-12G": "deregistered",
    "15F-12B": "deregistered",
    "15F-12G": "deregistered",
    "25": None,
    "25-NSE": None,
}


@dataclass(frozen=True)
class Deregistration:
    cik: int
    form: str
    filed_date: str
    company_name: str | None = None

    @property
    def status(self) -> str | None:
        """The status this filing can support alone, or None if it cannot support one."""
        return TERMINAL_FORMS[self.form]


def parse_form_index(text: str) -> list[Deregistration]:
    """Every terminal filing in one quarterly index.

    Unrecognised forms are skipped rather than guessed at: a form this code has not been
    taught about is not evidence that a company died.
    """
    return [
        Deregistration(
            cik=row.cik,
            form=row.form,
            filed_date=row.filed_date,
            company_name=row.company_name,
        )
        for row in index_rows(text)
        if row.form in TERMINAL_FORMS
    ]


def mark_terminal_status(conn: sqlite3.Connection, records: list[Deregistration]) -> int:
    """Record a terminal status against filers the store already holds.

    A CIK the store does not hold is *not* inserted. A filer row with a status and no
    facts is a company in the universe that cannot be screened on anything, which is a
    worse lie than leaving it out; ingesting those CIKs is a separate decision, and their
    `companyfacts` are still on EDGAR long after they stop filing.

    A filer is marked only when its own filings agree. A deregistration followed by
    another annual report is a filer that deregistered one class of securities and went
    on reporting, which is a common and undramatic event; marking it dead would delete a
    live company from every later universe.

    Returns the number of filers whose status changed.
    """
    changed = 0
    with conn:
        for record in records:
            status = record.status
            if status is None:
                # Form 25 and its variants: a security was delisted, which says nothing
                # about the company. Collected for reference, never acted on alone.
                continue
            row = conn.execute(
                "SELECT status, status_date FROM filer WHERE cik = ?", (record.cik,)
            ).fetchone()
            if row is None:
                continue

            later_report = conn.execute(
                "SELECT MAX(filed_date) AS latest FROM filing "
                "WHERE cik = ? AND form_type IN ('10-K', '10-K/A', '20-F', '10-Q') "
                "AND filed_date > ?",
                (record.cik, record.filed_date),
            ).fetchone()
            if later_report and later_report["latest"]:
                # The filer's own subsequent report contradicts the inference. Its
                # evidence beats ours.
                continue

            if row["status"] == status and (row["status_date"] or "9999") <= record.filed_date:
                # Already recorded, and from an earlier filing. The first date is the one
                # that matters: it is when the filer stopped being investable.
                continue
            if row["status"] not in ("active", status):
                # Something more specific is already recorded — an acquisition, say,
                # established from evidence this function does not have.
                continue

            conn.execute(
                "UPDATE filer SET status = ?, status_date = ? WHERE cik = ?",
                (status, record.filed_date, record.cik),
            )
            changed += 1
    return changed


def unknown_ciks(conn: sqlite3.Connection, records: list[Deregistration]) -> list[Deregistration]:
    """The dead filers the store has never heard of.

    These are the survivorship gap itself: they are absent from the ticker map because
    they no longer trade, so no amount of ingesting from that map will ever find them.
    Their submissions and companyfacts remain on EDGAR, so they can be ingested by CIK.
    """
    held = {row["cik"] for row in conn.execute("SELECT cik FROM filer")}
    return [record for record in records if record.cik not in held]
