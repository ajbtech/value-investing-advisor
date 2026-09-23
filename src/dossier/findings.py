"""Findings, and the validator that keeps unsourced ones out of dossiers.

The first non-negotiable: no uncited claims. Every finding carries verbatim text from
the filing plus the accession number that reported it, and a finding whose quote cannot
be found in its source is dropped before it reaches a dossier.

The validator sits on a knife edge. Too strict and honest quotes fail on a curly
apostrophe or a line break, the fabrication rate reads as noise, and nobody trusts the
signal. Too loose and it waves through text the filing never contained — which is worse,
because it lends false assurance to exactly the failure it exists to catch.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass

#: Below this a quote proves nothing: "revenue" appears in every filing ever written,
#: and counting such a match would inflate the pass rate into meaninglessness.
MIN_QUOTE_CHARS = 24

#: Each side of an ellipsis must still carry weight on its own.
MIN_FRAGMENT_CHARS = 12

#: Pass A reads prose and reports how it changed.
CHANGE_TYPES = frozenset({"added", "removed", "reordered", "softened", "strengthened"})

#: Pass B reads the footnotes, where the finding is an accounting choice rather than a
#: change of wording. A depreciation life that was extended is not "softened": it is a
#: change of estimate, and a list of flags is only worth reading if each one is called
#: by its name. The plan names these seven; a flag outside the list is still refused,
#: because "looks bad" is not a category.
FOOTNOTE_FLAGS = frozenset(
    {
        "policy_change",
        "estimate_change",
        "capitalisation_change",
        "related_party",
        "off_balance_sheet",
        "pension_assumption",
        "segment_change",
    }
)

#: Either vocabulary is accepted on the way in; which one belongs to which pass is the
#: prompt's business, and the pass is recorded on the row.
ALL_FLAGS = CHANGE_TYPES | FOOTNOTE_FLAGS
SEVERITIES = frozenset({"low", "medium", "high"})

_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*")
_WHITESPACE = re.compile(r"\s+")

#: Typographic variants a model will not reproduce byte for byte.
_TYPOGRAPHY = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        "\xa0": " ",
    }
)

#: Phrases that turn an observation into a recommendation. Matched as phrases, not bare
#: words: "the company plans to sell its European division" is an ordinary observation,
#: and banning the word "sell" would reject it.
_RECOMMENDATION = re.compile(
    r"\b(?:we|you|investors?|one)\s+(?:should|ought\s+to|must)\s+"
    r"(?:buy|sell|short|avoid|hold|own|purchase)\b"
    r"|\brecommend(?:s|ed|ing)?\s+(?:a\s+|to\s+)?(?:buy|sell|short|purchase|avoid)"
    r"|\bprice\s+target\b"
    r"|\b(?:buy|sell|hold)\s+rating\b"
    r"|\bworth\s+(?:buying|selling|shorting)\b",
    re.IGNORECASE,
)


#: A thesis is written in the first person about what to do, so it can recommend in the
#: imperative where a finding cannot: "Buy more below $20" has no "we should" in it.
#: Kept separate so the findings validator's behaviour is unchanged.
_IMPERATIVE_RECOMMENDATION = re.compile(
    r"^\W*(?:buy|sell|short|accumulate|add\s+to|trim|exit)\b",
    re.IGNORECASE,
)


def reads_as_recommendation(text: str) -> bool:
    """Does this text tell someone what to do with the stock?

    The passes and the thesis both report and argue rather than advise; judgment about
    what to do with the conclusion is the reader's, and this is a research tool that
    makes no individualised recommendations.
    """
    return bool(_RECOMMENDATION.search(text) or _IMPERATIVE_RECOMMENDATION.match(text))


def normalise_for_match(text: str) -> str:
    """Flatten the differences that are noise, and none of the ones that are signal."""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_TYPOGRAPHY)
    return _WHITESPACE.sub(" ", text).strip().casefold()


def quote_appears_in(quote: str, source: str) -> bool:
    """Is this quote genuinely present in the source filing?

    An ellipsis is accepted, because models legitimately quote that way, but every
    fragment must appear *in order*. Without the ordering rule two unrelated fragments
    could be stitched into a claim the filing never made.
    """
    if not quote or not quote.strip():
        return False

    haystack = normalise_for_match(source)
    fragments = [f for f in _ELLIPSIS.split(quote) if f.strip()]
    if not fragments:
        return False

    if len(fragments) == 1:
        needle = normalise_for_match(fragments[0])
        return len(needle) >= MIN_QUOTE_CHARS and needle in haystack

    normalised = [normalise_for_match(f) for f in fragments]
    if any(len(f) < MIN_FRAGMENT_CHARS for f in normalised):
        return False
    if sum(len(f) for f in normalised) < MIN_QUOTE_CHARS:
        return False

    cursor = 0
    for fragment in normalised:
        found = haystack.find(fragment, cursor)
        if found == -1:
            return False
        cursor = found + len(fragment)
    return True


@dataclass(frozen=True)
class Finding:
    """One observation from an analysis pass, tied to the text that supports it."""

    accession_no: str
    item: str
    change_type: str
    quote: str
    implication: str
    severity: str
    prior_accession_no: str | None = None
    prior_quote: str | None = None

    def __post_init__(self) -> None:
        if self.change_type not in ALL_FLAGS:
            raise ValueError(
                f"unknown change_type {self.change_type!r}; expected one of {sorted(ALL_FLAGS)}"
            )
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"unknown severity {self.severity!r}; expected one of {sorted(SEVERITIES)}"
            )
        if _RECOMMENDATION.search(self.implication):
            # These passes report observations, not views. Judgment happens later, once
            # all four passes are in view; a pass that recommends has skipped that.
            raise ValueError(
                "an analysis pass reports observations, not buy or sell views: "
                f"{self.implication!r}"
            )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> Finding:
        return cls(**payload)


@dataclass(frozen=True)
class DroppedFinding:
    finding: Finding
    reason: str


def validate_findings(
    findings: list[Finding], sources: dict[str, str]
) -> tuple[list[Finding], list[DroppedFinding]]:
    """Split findings into those whose quotes verify and those that do not.

    `sources` maps accession number to the section text the finding was drawn from. A
    finding citing a filing we do not hold is dropped rather than passed: an
    unverifiable citation is indistinguishable from a fabricated one.
    """
    kept: list[Finding] = []
    dropped: list[DroppedFinding] = []

    for item in findings:
        if not item.quote or not item.quote.strip():
            dropped.append(DroppedFinding(item, "quote_missing"))
            continue
        source = sources.get(item.accession_no)
        if source is None:
            dropped.append(DroppedFinding(item, "source_unavailable"))
            continue
        if not quote_appears_in(item.quote, source):
            dropped.append(DroppedFinding(item, "quote_not_found"))
            continue

        if item.prior_quote:
            prior_source = sources.get(item.prior_accession_no or "")
            if prior_source is None:
                dropped.append(DroppedFinding(item, "prior_source_unavailable"))
                continue
            if not quote_appears_in(item.prior_quote, prior_source):
                dropped.append(DroppedFinding(item, "prior_quote_not_found"))
                continue

        kept.append(item)

    return kept, dropped


def fabrication_rate(kept: int, dropped: int) -> float | None:
    """The proportion of findings that failed validation.

    None when there is nothing to measure. Reporting 0.0 for a pass that produced no
    findings at all would read as a clean bill of health for a pass that did nothing.
    """
    total = kept + dropped
    if total == 0:
        return None
    return dropped / total
