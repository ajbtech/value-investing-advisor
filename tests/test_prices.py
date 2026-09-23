"""Point-in-time prices.

A price is a valuation input, not a signal: it turns shares outstanding into a market
cap on the as-of date and nothing else. The trap is that free price sources adjust
history for splits that happened later. Yahoo reports Apple's close on 2020-08-28 as
124.81, which is $499.23 divided by the 4-for-1 split three days afterwards. Multiply
that by the share count Apple had actually reported by then and the market cap comes
out four times too small, because the price already "knows" about a future split.
Stored prices are the price as traded that day, with the split adjustment undone.
"""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from dossier.asof import AsOfView
from dossier.prices import (
    PricePoint,
    PriceSourceError,
    YahooPrices,
    parse_chart,
    store_prices,
    yahoo_symbol,
)
from dossier.store import open_store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def chart():
    return json.loads((FIXTURES / "yahoo_chart_AAPL_2020_split.json").read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        conn.execute("INSERT INTO filer (cik, name, ticker) VALUES (320193, 'Apple Inc.', 'AAPL')")
        conn.commit()
        yield conn


def by_date(points):
    return {p.price_date: p.close for p in points}


class TestUndoingTheSplitAdjustment:
    def test_a_close_before_a_split_is_the_price_as_traded(self, chart):
        closes = by_date(parse_chart(chart))
        assert closes["2020-08-28"] == pytest.approx(499.23, abs=0.01)

    def test_a_close_on_or_after_the_split_is_left_alone(self, chart):
        closes = by_date(parse_chart(chart))
        assert closes["2020-08-31"] == pytest.approx(129.04, abs=0.01)
        assert closes["2020-09-04"] == pytest.approx(120.96, abs=0.01)

    def test_dates_are_exchange_trading_days(self, chart):
        dates = sorted(by_date(parse_chart(chart)))
        assert dates[0] == "2020-08-24"
        assert dates[-1] == "2020-09-04"
        assert len(dates) == 10

    def test_a_missing_close_is_skipped_not_zeroed(self, chart):
        """Yahoo returns null for a day it has no print for. A zero would read as a
        stock that went to nothing, which is the worst possible thing to invent."""
        chart["chart"]["result"][0]["indicators"]["quote"][0]["close"][2] = None
        closes = by_date(parse_chart(chart))
        assert "2020-08-26" not in closes
        assert len(closes) == 9

    def test_a_reported_error_raises(self):
        payload = {
            "chart": {
                "result": None,
                "error": {"code": "Not Found", "description": "No data found"},
            }
        }
        with pytest.raises(PriceSourceError, match="No data found"):
            parse_chart(payload)


class TestSymbols:
    def test_share_classes_use_a_dash(self):
        assert yahoo_symbol("BRK.B") == "BRK-B"

    def test_plain_tickers_pass_through_uppercased(self):
        assert yahoo_symbol("aapl") == "AAPL"


class TestAsOfReads:
    @pytest.fixture
    def priced(self, store, chart):
        store_prices(store, 320193, "AAPL", parse_chart(chart))
        return store

    def test_reads_the_close_on_the_as_of_date(self, priced):
        price = AsOfView(priced, "2020-08-28").price(320193)
        assert price.price_date == "2020-08-28"
        assert price.close == pytest.approx(499.23, abs=0.01)

    def test_a_weekend_reads_the_last_trading_day_before_it(self, priced):
        price = AsOfView(priced, "2020-08-30").price(320193)
        assert price.price_date == "2020-08-28"

    def test_a_later_price_is_never_visible(self, priced):
        """The same rule as for facts: nothing dated after the as-of date exists."""
        price = AsOfView(priced, "2020-08-30").price(320193)
        assert price.price_date <= "2020-08-30"

    def test_before_any_price_there_is_none(self, priced):
        assert AsOfView(priced, "2020-01-01").price(320193) is None

    def test_storing_twice_changes_nothing(self, priced, chart):
        inserted = store_prices(priced, 320193, "AAPL", parse_chart(chart))
        assert inserted == 0


class TestClient:
    def _client(self, handler):
        return YahooPrices(transport=httpx.MockTransport(handler), sleep=lambda _: None)

    def test_asks_for_the_range_and_the_split_events(self, chart):
        seen = {}

        def handler(request):
            seen["url"] = request.url
            seen["ua"] = request.headers.get("user-agent", "")
            return httpx.Response(200, json=chart)

        points = self._client(handler).daily("AAPL", date(2020, 8, 24), date(2020, 9, 5))
        assert seen["url"].path.endswith("/AAPL")
        assert seen["url"].params["events"] == "split"
        assert seen["url"].params["interval"] == "1d"
        assert all(isinstance(p, PricePoint) for p in points)

    def test_never_sends_the_edgar_user_agent(self, chart, monkeypatch):
        """The EDGAR User-Agent carries a real name and email for the SEC. It has no
        business being sent to anyone else."""
        monkeypatch.setenv("EDGAR_USER_AGENT", "Jane Doe jane@example.com")
        seen = {}

        def handler(request):
            seen["ua"] = request.headers.get("user-agent", "")
            return httpx.Response(200, json=chart)

        self._client(handler).daily("AAPL", date(2020, 8, 24), date(2020, 9, 5))
        assert "jane@example.com" not in seen["ua"]

    def test_an_http_error_raises(self):
        client = self._client(
            lambda request: httpx.Response(
                404,
                json={
                    "chart": {
                        "result": None,
                        "error": {
                            "code": "Not Found",
                            "description": "No data found, symbol may be delisted",
                        },
                    }
                },
            )
        )
        with pytest.raises(PriceSourceError, match="delisted"):
            client.daily("GONE", date(2020, 1, 1), date(2020, 2, 1))
