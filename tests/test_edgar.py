"""The EDGAR client.

Three of the SEC's access rules will get a user blocked if ignored, and one of them —
the declared User-Agent — is the most likely first-run failure in the whole tool. It
should fail here, loudly and with an explanation, rather than as an SEC block the user
has to decode.

Nothing in this file touches the network. Live tests are marked `network` and are
deselected by default.
"""

import httpx
import pytest

from dossier.edgar import (
    EDGAR_MAX_REQUESTS_PER_SECOND,
    EdgarClient,
    InvalidUserAgent,
    RateLimiter,
    SecBlocked,
)

VALID_UA = "Jane Doe jane@example.com"


def fake_transport(handler):
    return httpx.MockTransport(handler)


def always(status=200, json_body=None, text="", headers=None):
    def handler(request):
        if json_body is not None:
            return httpx.Response(status, json=json_body, headers=headers)
        return httpx.Response(status, text=text, headers=headers)

    return fake_transport(handler)


class TestUserAgent:
    """A declared User-Agent of the form `Name your@email.com` is mandatory. Omit it
    and the SEC returns an "Undeclared Automated Tool" error."""

    @pytest.mark.parametrize(
        "user_agent",
        ["", "   ", None, "dossier/0.1", "Jane Doe", "jane@example.com", "Jane Doe jane@"],
    )
    def test_rejects_anything_the_sec_would_reject(self, user_agent):
        with pytest.raises(InvalidUserAgent):
            EdgarClient(user_agent=user_agent, transport=always())

    @pytest.mark.parametrize(
        "user_agent",
        [
            "Jane Doe jane@example.com",
            "Acme Research contact@research.example.com",
            "A B a.b+edgar@sub.example.org",
        ],
    )
    def test_accepts_a_name_and_an_email(self, user_agent):
        assert EdgarClient(user_agent=user_agent, transport=always()).user_agent == user_agent

    def test_the_error_says_what_to_do(self):
        with pytest.raises(InvalidUserAgent) as excinfo:
            EdgarClient(user_agent="dossier/0.1", transport=always())
        message = str(excinfo.value)
        assert "Name your@email.com" in message
        assert "EDGAR_USER_AGENT" in message

    def test_from_env_reads_the_variable(self, monkeypatch):
        monkeypatch.setenv("EDGAR_USER_AGENT", VALID_UA)
        assert EdgarClient.from_env(transport=always()).user_agent == VALID_UA

    def test_from_env_explains_itself_when_unset(self, monkeypatch):
        monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
        with pytest.raises(InvalidUserAgent) as excinfo:
            EdgarClient.from_env(transport=always())
        assert "EDGAR_USER_AGENT" in str(excinfo.value)

    def test_says_it_is_unset_rather_than_printing_none(self, monkeypatch):
        """`Invalid EDGAR User-Agent None.` tells a first-time user nothing."""
        monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
        with pytest.raises(InvalidUserAgent) as excinfo:
            EdgarClient.from_env(transport=always())
        assert "None" not in str(excinfo.value)
        assert "not set" in str(excinfo.value).lower()


class TestHeaders:
    def test_declares_the_user_agent_on_every_request(self):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return httpx.Response(200, text="ok")

        EdgarClient(VALID_UA, transport=fake_transport(handler)).get("/files/x.json")
        assert seen["user-agent"] == VALID_UA

    def test_asks_for_compression(self):
        """Send Accept-Encoding: gzip, deflate. The bulk files are enormous."""
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return httpx.Response(200, text="ok")

        EdgarClient(VALID_UA, transport=fake_transport(handler)).get("/files/x.json")
        assert "gzip" in seen["accept-encoding"]
        assert "deflate" in seen["accept-encoding"]

    def test_resolves_a_bare_path_against_www_sec_gov(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, text="ok")

        EdgarClient(VALID_UA, transport=fake_transport(handler)).get("/files/company_tickers.json")
        assert seen["url"] == "https://www.sec.gov/files/company_tickers.json"

    def test_leaves_an_absolute_url_alone(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json={})

        client = EdgarClient(VALID_UA, transport=fake_transport(handler))
        client.get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json")
        assert seen["url"].startswith("https://data.sec.gov/")


class TestRateLimiter:
    """Maximum 10 requests per second to sec.gov, and they monitor it."""

    def test_the_default_matches_the_published_limit(self):
        assert EDGAR_MAX_REQUESTS_PER_SECOND == 10

    def test_does_not_sleep_below_the_limit(self):
        clock = FakeClock()
        limiter = RateLimiter(max_per_second=10, clock=clock.now, sleep=clock.sleep)
        for _ in range(10):
            clock.advance(0.2)  # well spaced out already
            limiter.acquire()
        assert clock.slept == []

    def test_spaces_out_a_burst(self):
        clock = FakeClock()
        limiter = RateLimiter(max_per_second=10, clock=clock.now, sleep=clock.sleep)
        for _ in range(11):
            limiter.acquire()
        # Ten go through immediately; the eleventh waits for the window to roll.
        assert clock.slept
        assert sum(clock.slept) == pytest.approx(1.0, abs=1e-6)

    def test_a_long_burst_never_exceeds_the_rate(self):
        clock = FakeClock()
        limiter = RateLimiter(max_per_second=10, clock=clock.now, sleep=clock.sleep)
        for _ in range(50):
            limiter.acquire()
        assert clock.now() == pytest.approx(4.0, abs=1e-6)

    def test_the_client_uses_it(self):
        clock = FakeClock()
        client = EdgarClient(
            VALID_UA,
            transport=always(text="ok"),
            rate_limiter=RateLimiter(max_per_second=10, clock=clock.now, sleep=clock.sleep),
        )
        for _ in range(11):
            client.get("/files/x.json")
        assert clock.slept


class TestErrors:
    def test_a_403_is_reported_as_a_block_not_a_generic_http_error(self):
        client = EdgarClient(
            VALID_UA,
            transport=always(403, text="Your Request Originates from an Undeclared Automated Tool"),
        )
        with pytest.raises(SecBlocked) as excinfo:
            client.get("/files/x.json")
        assert "User-Agent" in str(excinfo.value)

    def test_retries_a_429_then_succeeds(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, text="ok")

        client = EdgarClient(VALID_UA, transport=fake_transport(handler), sleep=lambda _: None)
        assert client.get("/files/x.json").text == "ok"
        assert calls["n"] == 2

    def test_gives_up_after_the_retry_limit(self):
        client = EdgarClient(VALID_UA, transport=always(503), sleep=lambda _: None, max_retries=2)
        with pytest.raises(httpx.HTTPStatusError):
            client.get("/files/x.json")

    def test_does_not_retry_a_404(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(404)

        client = EdgarClient(VALID_UA, transport=fake_transport(handler), sleep=lambda _: None)
        with pytest.raises(httpx.HTTPStatusError):
            client.get("/files/missing.json")
        assert calls["n"] == 1


class TestFetching:
    def test_get_json_parses(self):
        client = EdgarClient(VALID_UA, transport=always(json_body={"cik": 320193}))
        assert client.get_json("/files/x.json") == {"cik": 320193}

    def test_download_writes_the_file(self, tmp_path):
        client = EdgarClient(VALID_UA, transport=always(text="payload"))
        dest = tmp_path / "nested" / "companyfacts.zip"
        client.download("/Archives/edgar/daily-index/bulkdata/companyfacts.zip", dest)
        assert dest.read_text() == "payload"

    def test_download_leaves_no_partial_file_on_failure(self, tmp_path):
        def handler(request):
            raise httpx.ConnectError("reset")

        client = EdgarClient(VALID_UA, transport=fake_transport(handler), sleep=lambda _: None)
        dest = tmp_path / "companyfacts.zip"
        with pytest.raises(httpx.ConnectError):
            client.download("/x.zip", dest)
        assert not dest.exists()
        assert list(tmp_path.iterdir()) == []

    def test_company_facts_url_is_zero_padded(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"cik": 320193})

        client = EdgarClient(VALID_UA, transport=fake_transport(handler))
        client.company_facts(320193)
        assert seen["url"] == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"

    def test_a_filer_with_no_xbrl_facts_is_none_not_an_error(self):
        """Plenty of registrants file no XBRL financials at all, and EDGAR answers 404.
        That is a fact about the filer, not a failed request."""
        client = EdgarClient(VALID_UA, transport=always(status=404), sleep=lambda _: None)
        assert client.company_facts(18748) is None

    def test_other_errors_from_company_facts_still_raise(self):
        client = EdgarClient(
            VALID_UA, transport=always(status=500), sleep=lambda _: None, max_retries=1
        )
        with pytest.raises(httpx.HTTPStatusError):
            client.company_facts(320193)

    def test_submissions_url_is_zero_padded(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json={})

        client = EdgarClient(VALID_UA, transport=fake_transport(handler))
        client.submissions(1318605)
        assert seen["url"] == "https://data.sec.gov/submissions/CIK0001318605.json"

    def test_ticker_map(self):
        body = {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla, Inc."},
        }
        client = EdgarClient(VALID_UA, transport=always(json_body=body))
        assert client.ticker_map() == {320193: "AAPL", 1318605: "TSLA"}


class FakeClock:
    """A clock that only moves when something sleeps, so rate limiting is testable."""

    def __init__(self):
        self._t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._t += seconds


@pytest.mark.network
def test_live_sec_is_reachable_with_a_declared_user_agent():
    """Deselected by default. Run with `-m network` on a machine that can reach the SEC."""
    client = EdgarClient.from_env()
    assert len(client.ticker_map()) > 1000
