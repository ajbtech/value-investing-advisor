"""Daily closing prices, as traded on the day.

A price is a valuation input, not a signal. It exists to turn shares outstanding into a
market cap on the as-of date, and nothing in this package trades on its movement.

Yahoo's chart endpoint needs no key, but its `close` series is adjusted for every split
up to today. A close from before a split therefore already reflects a split that had not
happened yet, which is lookahead bias hiding in a price. `parse_chart` undoes it using
the split events in the same response, so the stored close is what the stock actually
traded at. Ask for a range that ends today and every later split is in that response.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Literal

import httpx

from dossier.ratelimit import RateLimiter

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SOURCE = "yahoo"

#: A generic browser-style agent. Deliberately not EDGAR_USER_AGENT, which carries a real
#: name and email meant for the SEC alone.
USER_AGENT = "Mozilla/5.0 (compatible; dossier research tool)"

#: Unofficial endpoint, so stay well under anything that looks like load.
MAX_REQUESTS_PER_SECOND = 2


PRICES_JOB = "fetch_prices"


class NotIngested(Enum):
    """`ticker_of` for a CIK the store has no filer row for, as distinct from a filer
    that has no ticker. The first needs `dossier ingest`; the second has nothing to price.
    An enum rather than a bare object() so a type checker can narrow it away."""

    NOT_INGESTED = "not_ingested"


NOT_INGESTED = NotIngested.NOT_INGESTED


def ticker_of(conn: sqlite3.Connection, cik: int) -> str | None | Literal[NotIngested.NOT_INGESTED]:
    """The filer's listed ticker, None if it has none, or NOT_INGESTED."""
    row = conn.execute("SELECT ticker FROM filer WHERE cik = ?", (cik,)).fetchone()
    return NOT_INGESTED if row is None else row["ticker"]


def priceable_ciks(conn: sqlite3.Connection) -> list[int]:
    """Every filer with a ticker to price, in CIK order."""
    return [
        row[0]
        for row in conn.execute("SELECT cik FROM filer WHERE ticker IS NOT NULL ORDER BY cik")
    ]


class PriceSourceError(RuntimeError):
    """The price source refused the request or returned no data."""


class NoPriceData(PriceSourceError):
    """The source answered, and its answer is that it has nothing for this ticker.

    Usually a ticker that no longer trades. Retrying today cannot change it, so it is an
    answer to record rather than a failure to retry.
    """


def _source_error(text: str) -> PriceSourceError:
    if "no data found" in text.lower():
        return NoPriceData(text)
    return PriceSourceError(text)


@dataclass(frozen=True)
class PricePoint:
    price_date: str
    close: float


def yahoo_symbol(ticker: str) -> str:
    """EDGAR writes share classes with a dot (BRK.B); Yahoo uses a dash (BRK-B)."""
    return ticker.strip().upper().replace(".", "-")


def _error_text(payload: dict) -> str | None:
    error = (payload.get("chart") or {}).get("error")
    if not error:
        return None
    return error.get("description") or error.get("code") or str(error)


def parse_chart(payload: dict) -> list[PricePoint]:
    """Turn a chart response into closes as traded, split adjustment removed."""
    error = _error_text(payload)
    if error:
        raise _source_error(error)
    results = (payload.get("chart") or {}).get("result") or []
    if not results:
        raise PriceSourceError("price source returned no result")
    result = results[0]

    offset = timedelta(seconds=(result.get("meta") or {}).get("gmtoffset", 0))
    timestamps = result.get("timestamp") or []
    closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    splits = [
        (int(event["date"]), float(event["numerator"]) / float(event["denominator"]))
        for event in ((result.get("events") or {}).get("splits") or {}).values()
    ]

    points = []
    for stamp, close in zip(timestamps, closes, strict=False):
        if close is None:
            continue
        # Every split on a later trading day was already divided out of this close.
        factor = 1.0
        for split_at, ratio in splits:
            if split_at > stamp:
                factor *= ratio
        day = (datetime.fromtimestamp(stamp, UTC) + offset).date()
        points.append(PricePoint(price_date=day.isoformat(), close=round(close * factor, 4)))
    return points


def _epoch(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


class YahooPrices:
    """Fetches daily closes within a polite rate limit."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        rate_limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
    ) -> None:
        self.limiter = rate_limiter or RateLimiter(MAX_REQUESTS_PER_SECOND, sleep=sleep)
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> YahooPrices:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def daily(self, ticker: str, start: date, end: date) -> list[PricePoint]:
        self.limiter.acquire()
        response = self._client.get(
            YAHOO_CHART.format(symbol=yahoo_symbol(ticker)),
            params={
                "period1": _epoch(start),
                "period2": _epoch(end + timedelta(days=1)),
                "interval": "1d",
                "events": "split",
            },
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code != 200:
            raise _source_error(
                _error_text(payload) or f"price source returned HTTP {response.status_code}"
            )
        return parse_chart(payload)


def store_prices(
    conn: sqlite3.Connection, cik: int, ticker: str, points: list[PricePoint], source: str = SOURCE
) -> int:
    """Write closes, keeping the first version of any day already stored."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO price (cik, ticker, price_date, close, source, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(cik, ticker, p.price_date, p.close, source, now) for p in points],
        )
        return conn.total_changes - before
