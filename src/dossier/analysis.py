"""Pass A, run in two halves with a person in the middle.

The model is a Claude Code session on an existing subscription, not an API call, so the
pass splits: `prepare_pass_a` writes what the pass needs to read, and `load_findings`
takes the findings back, checks every quote against the filing it claims to come from,
and stores only what survives.

The seam is the point. Whatever produced the findings — a subscription session, an API
call, someone typing them by hand — they go through the same validator, because the
guarantee was never about where the text came from.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from dossier.findings import Finding, fabrication_rate, validate_findings
from dossier.jobs import idempotency_key

PROMPTS_DIR = Path(__file__).parent / "prompts"

#: Pinned so a result can be traced to the words that produced it. When output shifts,
#: you need to know whether the world changed or the prompt did.
PASS_A_VERSION = "pass_a_v3"

#: Pass A is a risk-factor *and MD&A* diff, and the two sections do not take the same
#: reading: Item 1A is the risks management chose to name, Item 7 is management
#: explaining its own numbers. One prompt each, so a finding always records the words
#: that produced it. An item absent here has no Pass A prompt and is refused rather than
#: run against instructions written for another section.
PASS_A_PROMPTS = {"1A": PASS_A_VERSION, "7": "pass_a_mdna_v1"}

#: Below this, the extraction is too doubtful to reason over. Analysing a bad parse
#: produces confident findings about text the filing does not contain — the pass should
#: refuse rather than launder a broken extraction into a dossier.
MIN_SECTION_CONFIDENCE = 0.6


def prompt_text(version: str) -> str:
    path = PROMPTS_DIR / f"{version}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"no prompt {version!r} in {PROMPTS_DIR}. Prompt versions are pinned in the "
            "repository; add the file rather than inlining the text."
        )
    return path.read_text(encoding="utf-8")


def prompt_version_for(item: str) -> str:
    """The Pass A prompt written for this section."""
    try:
        return PASS_A_PROMPTS[item]
    except KeyError:
        known = ", ".join(sorted(PASS_A_PROMPTS))
        raise ValueError(
            f"no Pass A prompt for Item {item}. Pass A reads {known}; the footnotes are "
            "Pass B's job. Running a pass against instructions written for another "
            "section produces findings about the wrong thing."
        ) from None


@dataclass
class AnalysisInput:
    """Everything Pass A reads, in the shape it is handed over."""

    cik: int
    item: str
    prompt_version: str
    instructions: str
    current: dict
    prior: dict
    #: Why the screener surfaced this company, or None if nobody screened it.
    screen: dict | None = None
    pass_name: str = "a"

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["pass"] = payload.pop("pass_name")
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> AnalysisInput:
        payload = dict(payload)
        payload["pass_name"] = payload.pop("pass", "a")
        return cls(**payload)


def _section(conn, accession_no: str, item: str) -> dict | None:
    row = conn.execute(
        "SELECT d.accession_no, d.item, d.text, d.extraction_confidence, d.char_count, "
        "       f.filed_date, f.primary_doc_url "
        "FROM document_section d JOIN filing f ON f.accession_no = d.accession_no "
        "WHERE d.accession_no = ? AND d.item = ?",
        (accession_no, item),
    ).fetchone()
    return dict(row) if row else None


def _screen_reason(conn, cik: int) -> dict | None:
    """The most recent screen run that flagged this company, if any.

    The build plan asks for this by name: the model should know whether a company
    surfaced as a net-net or as a quality compounder, because the interesting questions
    differ. A company analysed directly was never a candidate, which is not an error.
    """
    row = conn.execute(
        "SELECT as_of, flag_reason, payload FROM candidate WHERE cik = ? "
        "ORDER BY as_of DESC LIMIT 1",
        (cik,),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"])
    return {
        "as_of": row["as_of"],
        "flag_reason": row["flag_reason"],
        "flagged_by": payload.get("flagged_by", []),
        "market_cap": payload.get("market_cap"),
        # From `screen --compare`. The trend is a label over the history, so both travel
        # together: "new" means nothing on its own when the company was merely ineligible
        # a year ago, and only the history says which it was.
        "trend": payload.get("trend"),
        "history": payload.get("history", []),
    }


def prepare_pass_a(
    conn,
    cik: int,
    item: str = "1A",
    min_confidence: float = MIN_SECTION_CONFIDENCE,
) -> AnalysisInput:
    """Pair a filer's two most recent extracted sections for comparison."""
    prompt_version = prompt_version_for(item)
    rows = conn.execute(
        "SELECT d.accession_no FROM document_section d "
        "JOIN filing f ON f.accession_no = d.accession_no "
        "WHERE f.cik = ? AND d.item = ? AND f.form_type = '10-K' "
        "ORDER BY f.filed_date DESC",
        (cik, item),
    ).fetchall()

    if len(rows) < 2:
        raise ValueError(
            f"Pass A needs two extracted filings for CIK {cik}, found {len(rows)}. "
            "A one-sided diff would invite findings about changes never compared "
            "against anything. Run `dossier extract` first."
        )

    current = _section(conn, rows[0]["accession_no"], item)
    prior = _section(conn, rows[1]["accession_no"], item)

    for label, section in (("current", current), ("prior", prior)):
        confidence = section["extraction_confidence"]
        if confidence is not None and confidence < min_confidence:
            raise ValueError(
                f"the {label} Item {item} for CIK {cik} extracted at confidence "
                f"{confidence:.2f}, below {min_confidence:.2f}. Analysing a doubtful "
                "parse produces confident findings about text the filing may not "
                "contain. Check the extraction before running the pass."
            )

    return AnalysisInput(
        cik=cik,
        item=item,
        prompt_version=prompt_version,
        instructions=prompt_text(prompt_version),
        current=current,
        prior=prior,
        screen=_screen_reason(conn, cik),
    )


@dataclass
class LoadResult:
    run_key: str
    kept: int = 0
    dropped: int = 0
    drop_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def fabrication_rate(self) -> float | None:
        return fabrication_rate(self.kept, self.dropped)


def load_findings(
    conn,
    cik: int,
    payload: dict,
    item: str = "1A",
    pass_name: str = "a",
) -> LoadResult:
    """Validate findings against their source filings and store what survives.

    A finding that cannot be parsed into the schema, or whose quote is not in the filing
    it cites, is counted and discarded. The run is recorded either way: a pass that
    produced nothing usable is a fact worth keeping, not an absence to be inferred later
    from missing rows.
    """
    raw = payload.get("findings", [])
    model = payload.get("model")
    prompt_version = prompt_version_for(item)
    run_key = idempotency_key(
        f"pass_{pass_name}", {"cik": cik, "item": item}, prompt_version=prompt_version
    )

    parsed: list[Finding] = []
    reasons: Counter[str] = Counter()
    for entry in raw:
        try:
            parsed.append(Finding.from_dict(entry))
        except (TypeError, ValueError):
            # A malformed entry must not cost the good ones beside it.
            reasons["malformed"] += 1

    sources = {}
    for finding in parsed:
        for accession in (finding.accession_no, finding.prior_accession_no):
            if accession and accession not in sources:
                section = _section(conn, accession, item)
                if section:
                    sources[accession] = section["text"]

    kept, dropped = validate_findings(parsed, sources)
    for entry in dropped:
        reasons[entry.reason] += 1

    now = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        # Replace the run rather than accumulating: re-running a pass supersedes it.
        conn.execute("DELETE FROM finding WHERE run_key = ?", (run_key,))
        for finding in kept:
            conn.execute(
                """
                INSERT INTO finding
                    (run_key, cik, accession_no, item, change_type, quote, implication,
                     severity, prior_accession_no, prior_quote, pass, prompt_version,
                     model, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_key,
                    cik,
                    finding.accession_no,
                    finding.item,
                    finding.change_type,
                    finding.quote,
                    finding.implication,
                    finding.severity,
                    finding.prior_accession_no,
                    finding.prior_quote,
                    pass_name,
                    prompt_version,
                    model,
                    now,
                ),
            )
        conn.execute(
            """
            INSERT INTO analysis_run
                (run_key, cik, pass, prompt_version, model, findings_kept,
                 findings_dropped, drop_reasons, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_key) DO UPDATE SET
                model = excluded.model,
                findings_kept = excluded.findings_kept,
                findings_dropped = excluded.findings_dropped,
                drop_reasons = excluded.drop_reasons,
                created_at = excluded.created_at
            """,
            (
                run_key,
                cik,
                pass_name,
                prompt_version,
                model,
                len(kept),
                sum(reasons.values()),
                json.dumps(dict(reasons)),
                now,
            ),
        )

    return LoadResult(
        run_key=run_key,
        kept=len(kept),
        dropped=sum(reasons.values()),
        drop_reasons=dict(reasons),
    )
