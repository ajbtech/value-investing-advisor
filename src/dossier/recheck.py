"""Milestone 10: re-check every open thesis against its own falsification conditions.

The plan calls this the highest-leverage feature in the system, and the reason is not
technical. Everything else here helps you decide; this is the only part that checks
whether you were right, on terms you set before you knew the answer. It is the part that
catches you rationalising.

Two properties decide the design.

**It must never report a thesis as holding when it could not check.** A condition whose
metric is not in the store comes back as `needs_a_human`, counted separately and
reported first. Silence would be indistinguishable from good news, which is exactly the
failure this job exists to prevent.

**It does not tell you to sell.** It tells you that you said you would, quotes the
condition back, and asks whether you still mean it. The decision was always yours; the
job's contribution is that you cannot quietly forget what you committed to.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from dossier.figures import annual_rows, maintenance_capex, prepare_figures
from dossier.journal import append_entry
from dossier.thesis import stored_thesis

RECHECK_VERSION = "1"

#: The metrics that can be computed from XBRL facts in the store, with the aliases a
#: thesis is likely to write them under. Anything outside this vocabulary is not a
#: failure of the thesis — "written same-store sales" is a perfectly good condition, and
#: filers report it in prose rather than in XBRL — but it cannot be checked mechanically,
#: and this job says so rather than passing it silently.
CHECKABLE_METRICS: dict[str, tuple[str, ...]] = {
    "owner_earnings_margin": ("owner earnings margin", "owner earnings"),
    "operating_margin": ("operating margin", "consolidated operating margin", "ebit margin"),
    "gross_margin": ("gross margin",),
    "revenue_growth": ("revenue growth", "sales growth", "revenue, year over year"),
    "revenue": ("revenue", "sales"),
    "operating_income": ("operating income", "ebit"),
    "net_income": ("net income", "earnings"),
}

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "a": 1,
    "an": 1,
    "single": 1,
}


#: Words that describe the same figure rather than a different one, so they can be
#: dropped before matching. "Consolidated operating margin" is the operating margin.
_HARMLESS_PREFIXES = ("consolidated ", "total ", "the ", "company ", "reported ")


def resolve_metric(metric: str) -> str | None:
    """Which computable metric, if any, this condition is written about.

    Matched from the start of the phrase, never anywhere inside it. The looser rule is
    actively dangerous: "written same-store sales" contains "sales", and treating it as
    revenue would report a condition as holding after checking a different number —
    which is the one outcome this whole job exists to prevent. Failing to recognise a
    metric only asks a human to read the filing, so that is the safe direction to err.
    """
    text = " ".join(str(metric).lower().split())
    changed = True
    while changed:
        changed = False
        for prefix in _HARMLESS_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix) :]
                changed = True

    if text in CHECKABLE_METRICS:
        return text
    best: tuple[int, str] | None = None
    for key, aliases in CHECKABLE_METRICS.items():
        for alias in (key.replace("_", " "), *aliases):
            if text == alias or text.startswith(alias + " ") or text.startswith(alias + ","):
                if best is None or len(alias) > best[0]:
                    best = (len(alias), key)
    return None if best is None else best[1]


def window_years(window: str) -> int:
    """How many consecutive fiscal years a condition has to hold before it is breached.

    A re-check that fires on one bad period is one you learn to ignore, so the window is
    taken literally: "two consecutive fiscal years" needs two.
    """
    for token in str(window).lower().replace("-", " ").split():
        if token.isdigit():
            return max(1, int(token))
        if token in _NUMBER_WORDS:
            return _NUMBER_WORDS[token]
    return 1


def _metric_series(rows: list[dict]) -> list[dict]:
    """Every computable metric, per fiscal year, newest first."""
    ordered = sorted(rows, key=lambda r: r["fy_end"], reverse=True)
    capex = maintenance_capex(ordered)
    series = []
    for n, row in enumerate(ordered):
        revenue = row.get("revenue")
        values: dict[str, float | None] = {
            "revenue": revenue,
            "operating_income": row.get("ebit"),
            "net_income": row.get("net_income"),
        }
        if revenue:
            values["operating_margin"] = (
                row["ebit"] / revenue if row.get("ebit") is not None else None
            )
            values["gross_margin"] = (
                row["gross_profit"] / revenue if row.get("gross_profit") is not None else None
            )
            if row.get("cfo") is not None and capex is not None:
                values["owner_earnings_margin"] = (row["cfo"] - capex["used"]) / revenue
            older = ordered[n + 1] if n + 1 < len(ordered) else None
            if older and older.get("revenue"):
                values["revenue_growth"] = revenue / older["revenue"] - 1
        series.append({"fy_end": row["fy_end"], "values": values})
    return series


def _evaluate(condition: dict, series: list[dict]) -> dict:
    """One condition against the filings, with the numbers it was judged on."""
    you_said = {
        "metric": condition.get("metric"),
        "direction": condition.get("direction"),
        "threshold": condition.get("threshold"),
        "window": condition.get("window"),
    }
    key = resolve_metric(condition.get("metric", ""))
    if key is None:
        return {
            "you_said": you_said,
            "status": "needs_a_human",
            "note": (
                f"{condition.get('metric')!r} is not reported in XBRL, so this cannot be "
                "checked from the store. Read the filing and decide: a condition nobody "
                "checks is the same as no condition at all."
            ),
            "observed": [],
        }

    needed = window_years(condition.get("window", ""))
    observed = [
        {"fy_end": point["fy_end"], "value": point["values"].get(key)} for point in series[:needed]
    ]
    if len(observed) < needed or any(point["value"] is None for point in observed):
        return {
            "you_said": you_said,
            "status": "needs_a_human",
            "metric_key": key,
            "note": (
                f"{key} is not available for all {needed} year(s) this condition covers, "
                "so it has not been checked rather than passed."
            ),
            "observed": observed,
        }

    direction = str(condition.get("direction", "")).lower()
    threshold = condition["threshold"]
    if direction.startswith("below") or direction.startswith("under"):
        breached = all(point["value"] < threshold for point in observed)
    elif direction.startswith("above") or direction.startswith("over"):
        breached = all(point["value"] > threshold for point in observed)
    else:
        return {
            "you_said": you_said,
            "status": "needs_a_human",
            "metric_key": key,
            "note": f"direction {direction!r} is neither below nor above, so nothing was tested",
            "observed": observed,
        }

    return {
        "you_said": you_said,
        "metric_key": key,
        "status": "breached" if breached else "holding",
        "observed": observed,
    }


def recheck_thesis(
    conn: sqlite3.Connection,
    cik: int,
    as_of: date | str,
    on: date | str | None = None,
    journal_dir: Path | None = None,
) -> dict:
    """Check the latest thesis for one company against what has been filed since."""
    thesis = stored_thesis(conn, cik, as_of)
    if thesis is None:
        raise ValueError(f"no thesis for CIK {cik} as of {as_of} to re-check")

    run_date = str(on or date.today().isoformat())
    prepare_figures(conn, run_date, ciks=[cik])
    series = _metric_series([dict(row) for row in annual_rows(conn, cik)])

    conditions = [_evaluate(c, series) for c in thesis["thesis"]["falsification"]]
    breached = sum(1 for c in conditions if c["status"] == "breached")
    needs_a_human = sum(1 for c in conditions if c["status"] == "needs_a_human")

    if breached:
        status = "breached"
        question = (
            f"You said you would treat {breached} of these as evidence you were wrong. "
            "Do you still mean it?"
        )
    elif needs_a_human:
        status = "needs_a_human"
        question = (
            f"{needs_a_human} condition(s) could not be checked from the store. Have you "
            "read the filing?"
        )
    else:
        status = "holding"
        question = "Nothing you named has been breached. Is the thesis still the one you wrote?"

    report = {
        "cik": cik,
        "as_of": str(as_of),
        "thesis_version": thesis["version"],
        "run_date": run_date,
        "recheck_version": RECHECK_VERSION,
        "status": status,
        "breached": breached,
        "needs_a_human": needs_a_human,
        "conditions": conditions,
        # The job reports; it never advises. A breach is a reminder of what you
        # committed to when you knew less, not an instruction.
        "question": question,
        "latest_fiscal_year": series[0]["fy_end"] if series else None,
    }

    with conn:
        conn.execute(
            "DELETE FROM recheck WHERE cik = ? AND as_of = ? AND thesis_version = ? "
            "AND run_date = ?",
            (cik, str(as_of), thesis["version"], run_date),
        )
        conn.execute(
            "INSERT INTO recheck (cik, as_of, thesis_version, run_date, status, payload, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                cik,
                str(as_of),
                thesis["version"],
                run_date,
                status,
                json.dumps(report),
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )

    append_entry(
        journal_dir,
        {"kind": "recheck", **report},
        f"{cik}-{as_of}-recheck-{run_date}",
    )
    return report


def open_theses(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Every company with a thesis, at its latest date."""
    return [
        (row["cik"], row["as_of"])
        for row in conn.execute(
            "SELECT cik, MAX(as_of) AS as_of FROM thesis GROUP BY cik ORDER BY cik"
        ).fetchall()
    ]


def recheck_all(
    conn: sqlite3.Connection, on: date | str | None = None, journal_dir: Path | None = None
) -> list[dict]:
    """Re-check every open thesis. A store with none is not an error."""
    return [
        recheck_thesis(conn, cik=cik, as_of=as_of, on=on, journal_dir=journal_dir)
        for cik, as_of in open_theses(conn)
    ]
