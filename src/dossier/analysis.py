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
import re
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

#: Pass B is footnote forensics, and the footnotes are Item 8. It is a different job
#: from Pass A: not how the prose changed, but which accounting choices were made and
#: which of them moved.
#: v2 says to match notes by heading rather than by number. The first live run found
#: Kodak's notes renumbered between years — this year's Note 13 is Guarantees, last
#: year's was Financial Instruments — and comparing by number would produce a confident
#: finding about two unrelated disclosures. The four findings from that run stay pinned
#: to v1, whose words are still in the repository exactly as they were.
PASS_B_PROMPTS = {"8": "pass_b_v2"}

#: Pass D reads the proxy's compensation sections. Every one takes the same prompt: the
#: question — what is management paid on — is the same wherever in the document the
#: answer happens to sit.
PASS_D_PROMPTS = dict.fromkeys(
    ("CDA", "SUMMARY_COMP", "PAY_VS_PERFORMANCE", "DIRECTOR_COMP", "RELATED_PERSON"),
    "pass_d_v1",
)

PASS_PROMPTS = {"a": PASS_A_PROMPTS, "b": PASS_B_PROMPTS, "d": PASS_D_PROMPTS}

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


def prompt_version_for(item: str, pass_name: str = "a") -> str:
    """The prompt written for this pass and this section."""
    prompts = PASS_PROMPTS.get(pass_name)
    if prompts is None:
        raise ValueError(f"no pass named {pass_name!r}; the passes are {sorted(PASS_PROMPTS)}")
    try:
        return prompts[item]
    except KeyError:
        known = ", ".join(sorted(prompts))
        raise ValueError(
            f"no Pass {pass_name.upper()} prompt for Item {item}. Pass {pass_name.upper()} "
            f"reads {known}. Running a pass against instructions written for another "
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


#: "NOTE 17:", "Note 3 — Inventories", "NOTE 4. Debt". Numbered notes only: an unnumbered
#: heading inside a note is a subheading, and splitting on it would cut a note in half.
_NOTE_HEADING = re.compile(
    r"^[ \t]*NOTE[ \t]+(\d{1,2})[ \t]*(?:[:.—–-][ \t]*)?(.{0,90})$",
    re.IGNORECASE | re.MULTILINE,
)


def split_notes(text: str) -> list[dict]:
    """Break Item 8 into its numbered notes.

    Item 8 runs to 200,000 characters, so handing it over whole is not an option: the
    pass needs an index it can navigate and the text of the notes it decides to read.
    A section with no numbered headings — an incorporation-by-reference stub, or a parse
    that went wrong — comes back as one unnumbered note rather than as nothing, because
    nothing would look like a filing without footnotes.
    """
    matches = list(_NOTE_HEADING.finditer(text))
    if not matches:
        body = text.strip()
        return [{"number": None, "heading": None, "text": body, "char_count": len(body)}]

    notes = []
    for n, match in enumerate(matches):
        end = matches[n + 1].start() if n + 1 < len(matches) else len(text)
        body = text[match.start() : end].strip()
        notes.append(
            {
                "number": match.group(1),
                "heading": " ".join(match.group(0).split()),
                "text": body,
                "char_count": len(body),
            }
        )
    return notes


def prepare_pass_b(conn, cik: int, item: str = "8") -> dict:
    """Pass B's input: this year's notes, split and indexed, with last year's beside them.

    Unlike Pass A this is not a diff, so one filing is enough — but a change of estimate
    is only visible against the prior year, so the prior notes travel too when they exist.
    """
    prompt_version = prompt_version_for(item, pass_name="b")
    rows = conn.execute(
        "SELECT d.accession_no FROM document_section d "
        "JOIN filing f ON f.accession_no = d.accession_no "
        "WHERE f.cik = ? AND d.item = ? AND f.form_type = '10-K' "
        "ORDER BY f.filed_date DESC",
        (cik, item),
    ).fetchall()
    if not rows:
        raise ValueError(
            f"no extracted Item {item} for CIK {cik}. Run `dossier extract` first; the "
            "footnotes are what this pass reads."
        )

    current = _section(conn, rows[0]["accession_no"], item)
    prior = _section(conn, rows[1]["accession_no"], item) if len(rows) > 1 else None

    index = split_notes(current["text"])
    return {
        "cik": cik,
        "item": item,
        "pass": "b",
        "prompt_version": prompt_version,
        "instructions": prompt_text(prompt_version),
        "current": current,
        "prior": prior,
        "notes": [{k: v for k, v in note.items() if k != "text"} for note in index],
        "prior_notes": (
            [{k: v for k, v in note.items() if k != "text"} for note in split_notes(prior["text"])]
            if prior
            else []
        ),
        "screen": _screen_reason(conn, cik),
    }


def prepare_pass_d(conn, cik: int, item: str = "CDA") -> dict:
    """Pass D's input: this year's proxy compensation sections, and last year's.

    The whole pass is a comparison — a weighting means nothing until you can see what it
    was — so the prior proxy travels whenever the store has one. Every compensation
    section of the current proxy is handed over, because the answer to "what is
    management paid on" sits in a different place in every filer's document.
    """
    prompt_version = prompt_version_for(item, pass_name="d")
    proxies = conn.execute(
        "SELECT DISTINCT f.accession_no, f.filed_date FROM document_section d "
        "JOIN filing f ON f.accession_no = d.accession_no "
        "WHERE f.cik = ? AND f.form_type = 'DEF 14A' ORDER BY f.filed_date DESC",
        (cik,),
    ).fetchall()
    if not proxies:
        raise ValueError(
            f"no extracted proxy for CIK {cik}. Run `dossier extract --cik {cik} "
            "--form 'DEF 14A'` first: this pass reads the compensation sections, and "
            "they are in the proxy rather than the 10-K."
        )

    def sections_of(accession: str) -> dict[str, dict]:
        rows = conn.execute(
            "SELECT accession_no, item, text, extraction_confidence, char_count "
            "FROM document_section WHERE accession_no = ? AND item IN "
            "('CDA', 'SUMMARY_COMP', 'PAY_VS_PERFORMANCE', 'DIRECTOR_COMP', 'RELATED_PERSON')",
            (accession,),
        ).fetchall()
        return {row["item"]: dict(row) for row in rows}

    current_accession = proxies[0]["accession_no"]
    prior_accession = proxies[1]["accession_no"] if len(proxies) > 1 else None
    return {
        "cik": cik,
        "item": item,
        "pass": "d",
        "prompt_version": prompt_version,
        "instructions": prompt_text(prompt_version),
        "proxy": {
            "accession_no": current_accession,
            "filed_date": proxies[0]["filed_date"],
            "sections": sections_of(current_accession),
        },
        "prior_proxy": (
            {
                "accession_no": prior_accession,
                "filed_date": proxies[1]["filed_date"],
                "sections": sections_of(prior_accession),
            }
            if prior_accession
            else None
        ),
        "screen": _screen_reason(conn, cik),
    }


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
    prompt_version = prompt_version_for(item, pass_name=pass_name)
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
