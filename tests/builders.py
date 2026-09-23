"""Build small synthetic stores for screen tests.

Each helper writes rows the way ingest would: facts carry the accession and filed date
of the 10-K that reported them, so everything the screens compute is subject to the
same as-of rules as real data.
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta

#: Balance-sheet (point-in-time) tags. Everything else is a flow over the fiscal year.
INSTANT_TAGS = frozenset(
    {
        "Assets",
        "AssetsCurrent",
        "Liabilities",
        "LiabilitiesCurrent",
        "LiabilitiesAndStockholdersEquity",
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "CashAndCashEquivalentsAtCarryingValue",
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "AccountsReceivableNetCurrent",
        "InventoryNet",
        "PropertyPlantAndEquipmentNet",
        "Goodwill",
        "IntangibleAssetsNetExcludingGoodwill",
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "LongTermDebtCurrent",
        "DebtCurrent",
        "OperatingLeaseLiabilityNoncurrent",
        "RetainedEarningsAccumulatedDeficit",
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
    }
)

SHARE_TAGS = frozenset(
    {
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
    }
)


#: Shared by every builder, so two builders on one store never reuse an accession.
_ACCESSIONS = itertools.count(1)


class StoreBuilder:
    def __init__(self, conn):
        self.conn = conn

    def _accession(self, cik: int) -> str:
        return f"{cik:010d}-00-{next(_ACCESSIONS):06d}"

    def filer(
        self,
        cik: int,
        name: str = "Example Co",
        ticker: str | None = "EXMP",
        sic: str | None = "3571",
        first_seen: str = "2000-01-01",
        status: str = "active",
        status_date: str | None = None,
    ) -> StoreBuilder:
        self.conn.execute(
            "INSERT INTO filer (cik, name, ticker, sic, first_seen, status, status_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cik, name, ticker, sic, first_seen, status, status_date),
        )
        return self

    def annual(self, cik: int, fy_end: str, filed: str, form: str = "10-K", **values) -> str:
        """One annual report: a filing row plus a fact per tag, filed on `filed`."""
        accession = self._accession(cik)
        self.conn.execute(
            "INSERT INTO filing (accession_no, cik, form_type, filed_date, period_end) "
            "VALUES (?, ?, ?, ?, ?)",
            (accession, cik, form, filed, fy_end),
        )
        start = (date.fromisoformat(fy_end) - timedelta(days=364)).isoformat()
        for tag, value in values.items():
            if value is None:
                # `Tag=None` means this filer does not report it, which is a different
                # thing from reporting zero and is what several real filers do.
                continue
            self.fact(
                cik,
                accession,
                tag,
                value,
                fy_end,
                filed,
                start=None if tag in INSTANT_TAGS else start,
                form=form,
            )
        return accession

    def fact(
        self,
        cik: int,
        accession: str,
        tag: str,
        value: float,
        period_end: str,
        filed: str,
        start: str | None = None,
        form: str = "10-K",
    ) -> None:
        unit = "shares" if tag in SHARE_TAGS else "USD"
        self.conn.execute(
            "INSERT INTO fact (accession_no, cik, tag, unit, period_start, period_end, value, "
            "form_type, filed_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (accession, cik, tag, unit, start or "", period_end, value, form, filed),
        )

    def shares(self, cik: int, count: float, as_of_date: str, filed: str) -> None:
        """A cover-page share count, the way dei reports it."""
        accession = self._accession(cik)
        self.conn.execute(
            "INSERT INTO filing (accession_no, cik, form_type, filed_date) "
            "VALUES (?, ?, '10-Q', ?)",
            (accession, cik, filed),
        )
        self.fact(
            cik,
            accession,
            "EntityCommonStockSharesOutstanding",
            count,
            as_of_date,
            filed,
            form="10-Q",
        )

    def price(self, cik: int, day: str, close: float, ticker: str = "EXMP") -> None:
        self.conn.execute(
            "INSERT INTO price (cik, ticker, price_date, close, source, fetched_at) "
            "VALUES (?, ?, ?, ?, 'test', '2026-01-01T00:00:00+00:00')",
            (cik, ticker, day, close),
        )

    def history(self, cik: int, years: list[int], filed_month_day: str = "02-15", **values) -> None:
        """Several calendar fiscal years with identical figures, each filed the next spring."""
        for year in years:
            self.annual(cik, f"{year}-12-31", f"{year + 1}-{filed_month_day}", **values)

    def done(self):
        self.conn.commit()
        return self.conn
