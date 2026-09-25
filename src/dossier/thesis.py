"""Milestone 9: the thesis, the bear pass, and the journal.

The journal is the part that compounds. A dossier is disposable — it can be regenerated
from the filings any time — but the record of what was believed, when, and on what
evidence cannot be reconstructed after the fact. That asymmetry decides every rule here.

- A thesis is written for a company that has already been valued, so the argument comes
  after the price rather than reaching for it.
- Its falsification conditions have to be measurable, because the quarterly re-check is
  the feature that catches you rationalising and it cannot check a sentence.
- A revision appends. The earlier version stays exactly as written.
- The bear pass cites filings like every other pass, and attaches to the version it
  attacked. You do not get to delete the counterargument once you have bought.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

from dossier.findings import quote_appears_in, reads_as_recommendation
from dossier.journal import append_entry
from dossier.prompt_files import prompt_text
from dossier.valuation import stored_valuation

#: v2 names the metrics `dossier recheck` can evaluate on its own. The first live
#: re-check could check two of four conditions and said so; the prompt now asks for at
#: least one that a quarter nobody has time to read still gets checked on.
THESIS_PROMPT = "thesis_v2"
BEAR_PROMPT = "bear_v1"

#: Every section the plan requires. A thesis missing one is not a shorter thesis; it is
#: one that skipped the question it found hardest.
REQUIRED_SECTIONS = (
    "business",
    "mispricing",
    "must_go_right",
    "falsification",
    "holding_period_years",
    "sell_early_if",
    "pre_mortem",
)

#: Two, because a single condition is a thesis with one way to be wrong, and no company
#: has only one.
MIN_FALSIFICATION_CONDITIONS = 2

#: A reason has to name a mechanism. These are restatements of the conclusion: they say
#: the price is wrong, which is the claim, not the reason for it.
_NOT_A_REASON = {
    "mispriced",
    "undervalued",
    "overlooked",
    "cheap",
    "the market is wrong",
    "market inefficiency",
    "value",
    "unknown",
}

#: What the plan gives as examples of real reasons. Not a closed list — a new mechanism
#: is allowed — but it has to be a mechanism and not a synonym for "cheap".
REASON_EXAMPLES = (
    "forced selling",
    "a misunderstood segment",
    "a temporary earnings trough",
    "index exclusion",
    "a spin-off nobody covers",
    "a legal overhang being resolved",
)


def _fail(section: str, message: str) -> None:
    raise ValueError(f"{section}: {message}")


def _check_mispricing(mispricing: object) -> None:
    if not isinstance(mispricing, dict):
        _fail("mispricing", "expected an object with reason_type and explanation")

    reason = str(mispricing.get("reason_type", "")).strip().casefold()
    explanation = str(mispricing.get("explanation", "")).strip()

    if not reason or reason in _NOT_A_REASON:
        _fail(
            "mispricing",
            f"{reason or 'nothing'!r} names the conclusion, not a mechanism. A reason is "
            f"something like {', '.join(REASON_EXAMPLES[:3])} — a specific answer to why "
            "this is available at this price, which is the question the whole thesis "
            "rests on.",
        )
    if len(explanation) < 40:
        _fail("mispricing", "the explanation has to say what is actually happening")
    if "market is wrong" in explanation.casefold():
        _fail(
            "mispricing",
            "'the market is wrong' restates the position rather than explaining it. "
            "Name what the market is looking at, and what it is missing.",
        )


def _check_falsification(conditions: object) -> None:
    if not isinstance(conditions, list) or len(conditions) < MIN_FALSIFICATION_CONDITIONS:
        _fail(
            "falsification",
            "at least two measurable conditions are required, so there is more than one "
            "way to find out you were wrong",
        )
    for n, condition in enumerate(conditions, start=1):
        if not isinstance(condition, dict):
            _fail(f"falsification[{n}]", "expected an object")
        for key in ("metric", "direction", "window"):
            if not str(condition.get(key, "")).strip():
                _fail(f"falsification[{n}]", f"missing {key}")
        threshold = condition.get("threshold")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            _fail(
                f"falsification[{n}]",
                "needs a numeric threshold. 'I am wrong if the thesis stops working' "
                "cannot be checked by the quarterly re-check, which is the one feature "
                "that catches you rationalising.",
            )


def _check_thesis(thesis: dict) -> None:
    for section in REQUIRED_SECTIONS:
        value = thesis.get(section)
        if value is None or (isinstance(value, (str, list)) and not value):
            _fail(section, "is missing, and every section is required")

    _check_mispricing(thesis["mispricing"])
    _check_falsification(thesis["falsification"])

    if not isinstance(thesis["must_go_right"], list) or not thesis["must_go_right"]:
        _fail("must_go_right", "expected a list of conditions, not hopes")
    if not isinstance(thesis["holding_period_years"], (int, float)):
        _fail("holding_period_years", "expected a number of years")

    for section in ("business", "sell_early_if", "pre_mortem"):
        if reads_as_recommendation(str(thesis[section])):
            _fail(
                section,
                "reads as a recommendation to buy or sell. A thesis records what is "
                "believed and what would falsify it; this tool makes no individualised "
                "recommendations, and the decision is the reader's.",
            )


# -- the thesis ----------------------------------------------------------------------


def prepare_thesis(conn: sqlite3.Connection, cik: int, as_of: date | str) -> dict:
    """Hand over the valuation, the findings and the prompt the thesis answers to."""
    valuation = stored_valuation(conn, cik, as_of)
    if valuation is None:
        raise ValueError(
            f"no valuation for CIK {cik} as of {as_of}. The thesis is written for a "
            "company that has already been valued: writing one first is arguing for a "
            "company before pricing it, and the argument then reaches for the price. "
            "Run `dossier value` first."
        )

    findings = [
        dict(row)
        for row in conn.execute(
            "SELECT accession_no, item, change_type, quote, implication, severity "
            "FROM finding WHERE cik = ? ORDER BY "
            "CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END",
            (cik,),
        ).fetchall()
    ]
    row = conn.execute("SELECT name, ticker FROM filer WHERE cik = ?", (cik,)).fetchone()
    return {
        "cik": cik,
        "as_of": str(as_of),
        "name": row["name"] if row else None,
        "ticker": row["ticker"] if row else None,
        "prompt_version": THESIS_PROMPT,
        "instructions": prompt_text(THESIS_PROMPT),
        "valuation": valuation,
        "findings": findings,
        "required_sections": list(REQUIRED_SECTIONS),
        "reason_examples": list(REASON_EXAMPLES),
    }


def _next_version(conn: sqlite3.Connection, cik: int, as_of: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM thesis WHERE cik = ? AND as_of = ?",
        (cik, as_of),
    ).fetchone()
    return int(row["v"]) + 1


def load_thesis(
    conn: sqlite3.Connection,
    cik: int,
    as_of: date | str,
    payload: dict,
    journal_dir: Path | None = None,
) -> dict:
    """Validate a thesis, append it, and write the journal entry.

    Nothing is stored unless every section passes. A thesis with an unmeasurable
    falsification condition looks exactly like one that can be checked, right up to the
    quarter when it matters.
    """
    thesis = payload.get("thesis")
    if not isinstance(thesis, dict):
        raise ValueError("no thesis in the payload: expected a 'thesis' object")
    _check_thesis(thesis)

    as_of = str(as_of)
    version = _next_version(conn, cik, as_of)
    created = datetime.now(UTC).isoformat(timespec="seconds")
    model = payload.get("model")

    with conn:
        conn.execute(
            "INSERT INTO thesis (cik, as_of, version, payload, prompt_version, model, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cik, as_of, version, json.dumps(thesis), THESIS_PROMPT, model, created),
        )

    entry = {
        "kind": "thesis",
        "cik": cik,
        "as_of": as_of,
        "version": version,
        "prompt_version": THESIS_PROMPT,
        "model": model,
        "created_at": created,
        "thesis": thesis,
        "valuation": stored_valuation(conn, cik, as_of),
    }
    path = append_entry(journal_dir, entry, f"{cik}-{as_of}-thesis-v{version}")
    return {"cik": cik, "as_of": as_of, "version": version, "journal_entry": path}


def stored_thesis(
    conn: sqlite3.Connection, cik: int, as_of: date | str, version: int | None = None
) -> dict | None:
    """The latest thesis for a date, with the bear points attached to it."""
    if version is None:
        row = conn.execute(
            "SELECT * FROM thesis WHERE cik = ? AND as_of = ? ORDER BY version DESC LIMIT 1",
            (cik, str(as_of)),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM thesis WHERE cik = ? AND as_of = ? AND version = ?",
            (cik, str(as_of), version),
        ).fetchone()
    if row is None:
        return None

    bear = [
        dict(point)
        for point in conn.execute(
            "SELECT attacks, claim, accession_no, item, quote FROM bear_point "
            "WHERE cik = ? AND as_of = ? AND thesis_version = ? ORDER BY rowid",
            (cik, str(as_of), row["version"]),
        ).fetchall()
    ]
    return {
        "cik": cik,
        "as_of": row["as_of"],
        "version": row["version"],
        "prompt_version": row["prompt_version"],
        "model": row["model"],
        "created_at": row["created_at"],
        "thesis": json.loads(row["payload"]),
        "bear": bear,
    }


def latest_as_of(conn: sqlite3.Connection, cik: int) -> str | None:
    """The as-of date of the filer's most recent thesis, or None if it has none."""
    row = conn.execute("SELECT MAX(as_of) AS as_of FROM thesis WHERE cik = ?", (cik,)).fetchone()
    return row["as_of"] if row else None


# -- the bear pass -------------------------------------------------------------------


def prepare_bear_pass(conn: sqlite3.Connection, cik: int, as_of: date | str) -> dict:
    """Hand the bear the thesis, the findings and the sections it must quote from."""
    thesis = stored_thesis(conn, cik, as_of)
    if thesis is None:
        raise ValueError(
            f"no thesis for CIK {cik} as of {as_of}. The bear pass attacks a written "
            "argument; without one it is just another read of the filings."
        )

    sections = [
        dict(row)
        for row in conn.execute(
            "SELECT d.accession_no, d.item, f.filed_date, d.char_count "
            "FROM document_section d JOIN filing f ON f.accession_no = d.accession_no "
            "WHERE f.cik = ? ORDER BY f.filed_date DESC",
            (cik,),
        ).fetchall()
    ]
    findings = [
        dict(row)
        for row in conn.execute(
            "SELECT accession_no, item, change_type, quote, implication, severity "
            "FROM finding WHERE cik = ? ORDER BY severity",
            (cik,),
        ).fetchall()
    ]
    return {
        "cik": cik,
        "as_of": str(as_of),
        "thesis_version": thesis["version"],
        "prompt_version": BEAR_PROMPT,
        "instructions": prompt_text(BEAR_PROMPT),
        "thesis": thesis["thesis"],
        "valuation": stored_valuation(conn, cik, as_of),
        "findings": findings,
        "available_sections": sections,
    }


def load_bear_pass(
    conn: sqlite3.Connection,
    cik: int,
    as_of: date | str,
    payload: dict,
    journal_dir: Path | None = None,
) -> dict:
    """Validate every bear point's quote against its filing, and attach what survives."""
    thesis = stored_thesis(conn, cik, as_of)
    if thesis is None:
        raise ValueError(f"no thesis for CIK {cik} as of {as_of} to attack")

    points = payload.get("bear") or []
    if not isinstance(points, list):
        raise ValueError("expected 'bear' to be a list of points, each with a quote")

    as_of = str(as_of)
    kept: list[dict] = []
    reasons: Counter[str] = Counter()
    for point in points:
        quote = str(point.get("quote", "")).strip()
        accession = str(point.get("accession_no", "")).strip()
        item = str(point.get("item", "")).strip()
        claim = str(point.get("claim", "")).strip()
        if not claim or not quote or not accession:
            reasons["malformed"] += 1
            continue
        if reads_as_recommendation(claim):
            reasons["recommendation"] += 1
            continue
        row = conn.execute(
            "SELECT text FROM document_section WHERE accession_no = ? AND item = ?",
            (accession, item),
        ).fetchone()
        if row is None:
            reasons["source_unavailable"] += 1
            continue
        if not quote_appears_in(quote, row["text"]):
            reasons["quote_not_found"] += 1
            continue
        kept.append({**point, "claim": claim, "quote": quote})

    created = datetime.now(UTC).isoformat(timespec="seconds")
    model = payload.get("model")
    dropped = sum(reasons.values())

    with conn:
        for point in kept:
            conn.execute(
                "INSERT INTO bear_point (cik, as_of, thesis_version, attacks, claim, "
                "accession_no, item, quote, prompt_version, model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cik,
                    as_of,
                    thesis["version"],
                    str(point.get("attacks", "thesis")),
                    point["claim"],
                    point["accession_no"],
                    str(point.get("item", "")),
                    point["quote"],
                    BEAR_PROMPT,
                    model,
                    created,
                ),
            )
        conn.execute(
            "INSERT INTO bear_pass (cik, as_of, thesis_version, kept, dropped, "
            "drop_reasons, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(cik, as_of, thesis_version) DO UPDATE SET "
            "kept = kept + excluded.kept, dropped = dropped + excluded.dropped",
            (cik, as_of, thesis["version"], len(kept), dropped, json.dumps(dict(reasons)), created),
        )

    total = len(kept) + dropped
    result = {
        "cik": cik,
        "as_of": as_of,
        "thesis_version": thesis["version"],
        "kept": len(kept),
        "dropped": dropped,
        "drop_reasons": dict(reasons),
        # None, not 0.0, when there was nothing to measure: a pass that produced no
        # points has not earned a clean bill of health.
        "fabrication_rate": None if total == 0 else dropped / total,
    }
    append_entry(
        journal_dir,
        {
            "kind": "bear_pass",
            "created_at": created,
            "prompt_version": BEAR_PROMPT,
            "model": model,
            "points": kept,
            **result,
        },
        f"{cik}-{as_of}-bear-v{thesis['version']}",
    )
    return result


# -- pass-overs ----------------------------------------------------------------------


def record_pass_over(
    journal_dir: Path | None, cik: int, as_of: date | str, reason: str, **context
) -> str | None:
    """Record a candidate that cleared screening and was passed over, and why.

    The passes are where you learn most and they are what everyone forgets to record. A
    journal of only the decisions taken is a record of a different process than the one
    actually run.
    """
    if not reason.strip():
        raise ValueError(
            "a pass-over needs a reason. 'Did not like it' recorded now is worth more "
            "in two years than nothing recorded at all, but a blank is worth nothing."
        )
    return append_entry(
        journal_dir,
        {
            "kind": "pass_over",
            "cik": cik,
            "as_of": str(as_of),
            "reason": reason,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            **context,
        },
        f"{cik}-{as_of}-pass-over",
    )
