"""Which of a filer's listed securities is its common stock.

EDGAR lists every security a filer has registered, and `ingest` keeps the first. A stored
ticker can therefore be a preferred issue (`SCE-PG`), an exchange-listed bond, a warrant
or a unit rather than the common stock. Market capitalisation computed from a preferred
share's price is not that company's market capitalisation, and every ratio built on it —
earnings yield, price to net current assets, free cash flow yield — is wrong by a
multiple nobody can see.

At 500 filers this never bit: other filters caught those tickers first. At 8,000 it
would, which is why the check exists now.

The distinction that matters is between a *share class* and a *different security*.
`BF-B`, `MOG-A` and `CRD-A` are common stock with two classes; `SCE-PG` and `CMS-PB` are
preferred. Both use a hyphen, so the suffix has to be read rather than the punctuation.
"""

from __future__ import annotations

import re

#: What the suffix after the separator means, in the convention EDGAR and the exchanges
#: use. A single letter that is not one of these is a share class — Brown-Forman's `B`,
#: Moog's `A` — and share classes are common stock.
_SUFFIXES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^P[A-Z]?$"), "preferred"),
    (re.compile(r"^PR[A-Z]?$"), "preferred"),
    (re.compile(r"^W(?:S|T)?$"), "warrant"),
    (re.compile(r"^WI$"), "when_issued"),
    (re.compile(r"^U$"), "unit"),
    (re.compile(r"^R(?:T)?$"), "right"),
    (re.compile(r"^[A-Z]$"), "common"),
]

_SEPARATOR = re.compile(r"[-.]")


def classify_ticker(ticker: str | None) -> str:
    """What kind of security this ticker denotes.

    Returns `common`, `preferred`, `warrant`, `unit`, `right`, `when_issued`, or
    `unknown`. An unrecognised suffix is `unknown` rather than assumed common: the whole
    point is to avoid pricing a company off something that is not its shares, and a guess
    would put that back.
    """
    if not ticker or not ticker.strip():
        return "unknown"

    cleaned = ticker.strip().upper()
    parts = _SEPARATOR.split(cleaned)
    if len(parts) == 1:
        return "common"
    if len(parts) > 2 or not parts[0]:
        return "unknown"

    suffix = parts[1]
    for pattern, kind in _SUFFIXES:
        if pattern.match(suffix):
            return kind
    return "unknown"


def is_common_stock(ticker: str | None) -> bool:
    """Can a market capitalisation honestly be built from this ticker's price?"""
    return classify_ticker(ticker) == "common"
