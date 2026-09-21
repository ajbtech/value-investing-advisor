"""A polite EDGAR client.

The SEC publishes three access rules and enforces them: ten requests a second, a
declared `User-Agent` naming a human and an email, and compression on. Getting the
User-Agent wrong is the most likely first-run failure in the whole tool, so it fails
here with an explanation rather than as an opaque SEC block.

EDGAR needs no key and no account. Prefer the nightly bulk ZIPs over per-company calls
wherever possible — they take rate limiting out of the inner loop entirely.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import httpx

EDGAR_MAX_REQUESTS_PER_SECOND = 10

WWW = "https://www.sec.gov"
DATA = "https://data.sec.gov"

#: A name followed by an email address, e.g. "Jane Doe jane@example.com".
USER_AGENT_PATTERN = re.compile(r"^\s*\S+(?:\s+\S+)*\s+[^@\s]+@[^@\s]+\.[^@\s]+\s*$")

USER_AGENT_HELP = (
    "The SEC requires a declared User-Agent of the form `Name your@email.com`, and "
    "returns an 'Undeclared Automated Tool' error without one. Set the EDGAR_USER_AGENT "
    "environment variable, for example:\n\n"
    '    EDGAR_USER_AGENT="Jane Doe jane@example.com"'
)

#: Statuses worth trying again: the SEC throttling us, or a transient gateway problem.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class InvalidUserAgent(ValueError):
    """The declared User-Agent is missing or not in the form the SEC requires."""


class SecBlocked(RuntimeError):
    """The SEC refused the request outright, almost always over the User-Agent."""


class RateLimiter:
    """A sliding-window limiter: at most `max_per_second` requests in any one second.

    The clock and sleep are injected so the pacing can be tested without spending real
    seconds on it.
    """

    def __init__(
        self,
        max_per_second: int = EDGAR_MAX_REQUESTS_PER_SECOND,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_per_second < 1:
            raise ValueError("max_per_second must be at least 1")
        self.max_per_second = max_per_second
        self._clock = clock
        self._sleep = sleep
        self._recent: deque[float] = deque()

    def acquire(self) -> None:
        while True:
            now = self._clock()
            while self._recent and now - self._recent[0] >= 1.0:
                self._recent.popleft()
            if len(self._recent) < self.max_per_second:
                self._recent.append(now)
                return
            self._sleep(self._recent[0] + 1.0 - now)


class EdgarClient:
    """Fetches from EDGAR within the SEC's published limits."""

    def __init__(
        self,
        user_agent: str | None,
        *,
        transport: httpx.BaseTransport | None = None,
        rate_limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        timeout: float = 60.0,
    ) -> None:
        self.user_agent = self._validated(user_agent)
        self.limiter = rate_limiter or RateLimiter()
        self._sleep = sleep
        self.max_retries = max_retries
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": self.user_agent,
                # The bulk files are enormous; the SEC asks for this explicitly.
                "Accept-Encoding": "gzip, deflate",
            },
        )

    @staticmethod
    def _validated(user_agent: str | None) -> str:
        if user_agent is None or not user_agent.strip():
            raise InvalidUserAgent(f"The EDGAR User-Agent is not set.\n\n{USER_AGENT_HELP}")
        if not USER_AGENT_PATTERN.match(user_agent):
            raise InvalidUserAgent(
                f"The EDGAR User-Agent {user_agent!r} is not in the form the SEC "
                f"requires.\n\n{USER_AGENT_HELP}"
            )
        return user_agent

    @classmethod
    def from_env(cls, **kwargs) -> EdgarClient:
        """Build a client from EDGAR_USER_AGENT, explaining itself when it is unset."""
        return cls(os.environ.get("EDGAR_USER_AGENT"), **kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EdgarClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- requests ------------------------------------------------------------

    @staticmethod
    def _resolve(url: str) -> str:
        return url if url.startswith("http") else f"{WWW}{url}"

    def _check(self, response: httpx.Response) -> None:
        if response.status_code == 403:
            raise SecBlocked(
                "The SEC refused this request (403). This almost always means the "
                f"User-Agent was rejected.\n\n{USER_AGENT_HELP}"
            )
        response.raise_for_status()

    def get(self, url: str, **kwargs) -> httpx.Response:
        """GET within the rate limit, retrying only what is worth retrying."""
        target = self._resolve(url)
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self.limiter.acquire()
            try:
                response = self._client.get(target, **kwargs)
            except httpx.TransportError as exc:
                last_error = exc
                self._backoff(attempt, None)
                continue
            if response.status_code in RETRY_STATUSES and attempt < self.max_retries - 1:
                self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            self._check(response)
            return response
        assert last_error is not None
        raise last_error

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after is not None:
            try:
                self._sleep(float(retry_after))
                return
            except ValueError:
                pass  # A date-formatted Retry-After; fall through to exponential.
        self._sleep(2.0**attempt)

    def get_json(self, url: str) -> dict:
        return self.get(url).json()

    def download(self, url: str, dest: Path | str) -> Path:
        """Stream a (possibly very large) file to disk, atomically.

        Nothing appears at `dest` until the whole body has arrived, so an interrupted
        download can never be mistaken for a complete one.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, suffix=".part")
        tmp = Path(tmp_name)
        try:
            self.limiter.acquire()
            with os.fdopen(fd, "wb") as handle:
                with self._client.stream("GET", self._resolve(url)) as response:
                    self._check(response)
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return dest

    # -- endpoints -----------------------------------------------------------

    @staticmethod
    def cik_str(cik: int) -> str:
        """EDGAR wants CIKs zero-padded to ten digits in its JSON endpoints."""
        return f"CIK{int(cik):010d}"

    def company_facts(self, cik: int) -> dict:
        """Every XBRL fact for one filer. Prefer the bulk ZIP for more than a handful."""
        return self.get_json(f"{DATA}/api/xbrl/companyfacts/{self.cik_str(cik)}.json")

    def submissions(self, cik: int) -> dict:
        """One filer's filing index: dates, accession numbers, forms."""
        return self.get_json(f"{DATA}/submissions/{self.cik_str(cik)}.json")

    def ticker_map(self) -> dict[int, str]:
        """CIK to ticker, from the weekly `company_tickers.json`."""
        body = self.get_json(f"{WWW}/files/company_tickers.json")
        return {int(row["cik_str"]): row["ticker"] for row in body.values()}
