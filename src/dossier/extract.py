"""Pull Item sections out of a 10-K.

This is the fiddly part of the pipeline. Item boundaries are inconsistent across filers
and years, and the obvious implementation — find "Item 1A", take what follows — returns
the table of contents, because the TOC matches the same pattern and comes first.

The approach here: find every candidate heading, then pick, for each item, the candidate
that yields the longest span. A TOC entry is followed by the next TOC line; a real
section is followed by pages of prose. That one property separates them without any
guessing about page numbers or document structure.

Every section carries a confidence, because a parse that went wrong quietly is worse
than one that failed loudly: the analysis layer can refuse a low-confidence section
rather than reasoning over a page of nothing.
"""

from __future__ import annotations

import html as html_module
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

#: Bumped when the extractor's behaviour changes. It is part of every extraction job's
#: idempotency key, so a fixed parser re-extracts rather than serving the old bad parse
#: from cache — the same reason prompt_version exists for the analysis passes.
EXTRACTOR_VERSION = "1"

#: Sections the analysis passes actually read.
DEFAULT_ITEMS = ("1", "1A", "1B", "2", "7", "7A", "8")

#: How long each section ought to be, roughly, in a real 10-K. Below it, a parse is more
#: likely a stray match than a real section. These differ per item on purpose: "None." is
#: a perfectly ordinary Item 1B and must not be penalised, while a 500-character Item 1A
#: means something went wrong. `DEFAULT_PLAUSIBLE` covers anything not listed.
PLAUSIBLE_LENGTH = {
    "1": 2000,
    "1A": 1500,
    "1B": 0,
    "2": 100,
    "7": 1500,
    "7A": 100,
    "8": 100,
}
DEFAULT_PLAUSIBLE = 500

#: What normally follows each item. Ending anywhere else still works, but is less certain.
EXPECTED_SUCCESSOR = {
    "1": "1A",
    "1A": "1B",
    "1B": "2",
    "2": "3",
    "7": "7A",
    "7A": "8",
}

_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_BLOCK = re.compile(r"</?(p|div|tr|br|h[1-6]|li|table|thead|tbody)\b[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")

#: A heading: "Item 1A.", "ITEM 1A —", "Item 1A:" at the start of a line. The separator
#: is optional because plenty of filers omit it.
_HEADING = re.compile(
    r"^[ \t]*item[ \t]+(\d{1,2}[A-Z]?)[ \t]*[.\-–—:)]?[ \t]*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class Section:
    item: str
    text: str
    confidence: float
    ended_at: str | None
    heading: str

    @property
    def char_count(self) -> int:
        return len(self.text)


def normalise(raw_html: str) -> str:
    """Turn filing HTML into line-oriented plain text.

    Block elements become newlines so headings sit on their own line; every other tag is
    removed without leaving a space, so `Item 1<font>A</font>.` closes up to `Item 1A.`
    rather than becoming `Item 1 A .`.
    """
    text = _SCRIPT_OR_STYLE.sub(" ", raw_html)
    text = _BLOCK.sub("\n", text)
    text = _TAG.sub("", text)
    text = html_module.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


@dataclass(frozen=True)
class _Candidate:
    item: str
    start: int  # where the heading begins
    body_start: int  # where the text after the heading begins
    heading: str


def _candidates(text: str) -> list[_Candidate]:
    found = []
    for match in _HEADING.finditer(text):
        item = match.group(1).upper()
        found.append(
            _Candidate(
                item=item,
                start=match.start(),
                body_start=match.end(),
                heading=match.group(0).strip(),
            )
        )
    return found


def _score(item: str, length: int, ended_at: str | None) -> float:
    """How much to trust this parse.

    Three things lower it: an unexpectedly short section, ending at an item other than
    the one that normally follows, and running to the end of the document with no
    closing boundary at all.
    """
    confidence = 1.0
    plausible = PLAUSIBLE_LENGTH.get(item, DEFAULT_PLAUSIBLE)
    if plausible and length < plausible:
        # Scales down smoothly rather than cliff-edging, so a slightly short section is
        # merely less trusted and a two-line one is barely trusted at all.
        confidence *= max(0.15, length / plausible)
    if ended_at is None:
        confidence *= 0.6  # ran to the end of the filing; we never saw a boundary
    elif ended_at != EXPECTED_SUCCESSOR.get(item):
        confidence *= 0.8
    return round(min(1.0, confidence), 3)


def extract_sections(
    raw_html: str, items: tuple[str, ...] | list[str] = DEFAULT_ITEMS
) -> dict[str, Section]:
    """Extract the requested items. Items genuinely absent are simply not returned."""
    text = normalise(raw_html)
    candidates = _candidates(text)
    wanted = {item.upper() for item in items}

    sections: dict[str, Section] = {}
    for index, candidate in enumerate(candidates):
        if candidate.item not in wanted:
            continue

        # The section runs to the next heading of a *different* item. A repeated heading
        # of the same item (the TOC line, then the real one) must not close the span.
        end = len(text)
        ended_at = None
        for following in candidates[index + 1 :]:
            if following.item != candidate.item:
                end = following.start
                ended_at = following.item
                break

        body = text[candidate.body_start : end].strip()
        existing = sections.get(candidate.item)
        # Longest span wins: a TOC entry is followed by the next TOC line, a real
        # section by pages of prose. That is what tells them apart.
        if existing is None or len(body) > existing.char_count:
            sections[candidate.item] = Section(
                item=candidate.item,
                text=body,
                confidence=_score(candidate.item, len(body), ended_at),
                ended_at=ended_at,
                heading=candidate.heading,
            )

    return {item: section for item, section in sections.items() if section.text}


@dataclass
class ExtractResult:
    accession_no: str
    sections: int = 0
    items: list[str] = field(default_factory=list)
    lowest_confidence: float = 1.0


def store_sections(
    conn: sqlite3.Connection, accession_no: str, sections: dict[str, Section]
) -> ExtractResult:
    """Write sections for one filing, replacing any previous extraction of it.

    Filings never change, but the extractor does, so a better parse has to be able to
    supersede a worse one without leaving both rows behind. Low-confidence sections are
    stored too, flagged: dropping them would leave the analysis layer unable to tell a
    bad parse from a section the filer simply never wrote.
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    result = ExtractResult(accession_no=accession_no)
    with conn:
        for item, section in sorted(sections.items()):
            conn.execute(
                """
                INSERT INTO document_section
                    (accession_no, item, text, extraction_confidence, char_count,
                     extracted_at, heading, ended_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(accession_no, item) DO UPDATE SET
                    text = excluded.text,
                    extraction_confidence = excluded.extraction_confidence,
                    char_count = excluded.char_count,
                    extracted_at = excluded.extracted_at,
                    heading = excluded.heading,
                    ended_at = excluded.ended_at
                """,
                (
                    accession_no,
                    item,
                    section.text,
                    section.confidence,
                    section.char_count,
                    now,
                    section.heading,
                    section.ended_at,
                ),
            )
            result.sections += 1
            result.items.append(item)
            result.lowest_confidence = min(result.lowest_confidence, section.confidence)
    if not result.items:
        result.lowest_confidence = 0.0
    return result


def extract_filing(
    conn: sqlite3.Connection,
    accession_no: str,
    raw_html: str,
    items: tuple[str, ...] | list[str] = DEFAULT_ITEMS,
) -> ExtractResult:
    """Extract one filing's sections and write them to the store."""
    return store_sections(conn, accession_no, extract_sections(raw_html, items))
