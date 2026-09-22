"""The deterministic screener.

No model runs here: the job is arithmetic, and a hallucinated ratio costs a wasted
analysis pass. What these tests guard is that the arithmetic only ever sees what was
knowable on the as-of date, and that every filer that drops out of the universe says why.
"""

import pytest

from dossier.asof import AsOfView
from dossier.screens import annual_rows, prepare, universe_rows
from dossier.store import open_store
from tests.builders import StoreBuilder

AS_OF = "2025-06-30"

HEALTHY = {
    "Revenues": 2_000_000_000,
    "CostOfRevenue": 1_200_000_000,
    "OperatingIncomeLoss": 300_000_000,
    "NetIncomeLoss": 220_000_000,
    "NetCashProvidedByUsedInOperatingActivities": 280_000_000,
    "PaymentsToAcquirePropertyPlantAndEquipment": 60_000_000,
    "DepreciationDepletionAndAmortization": 50_000_000,
    "Assets": 1_500_000_000,
    "AssetsCurrent": 700_000_000,
    "LiabilitiesCurrent": 300_000_000,
    "Liabilities": 600_000_000,
    "StockholdersEquity": 900_000_000,
    "CashAndCashEquivalentsAtCarryingValue": 200_000_000,
    "LongTermDebtNoncurrent": 150_000_000,
    "PropertyPlantAndEquipmentNet": 400_000_000,
    "WeightedAverageNumberOfSharesOutstandingBasic": 100_000_000,
}


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        yield conn


def eligible_filer(b: StoreBuilder, cik: int, **overrides):
    """A filer that should pass every universe filter as of AS_OF."""
    b.filer(cik, name=overrides.pop("name", f"Filer {cik}"), sic=overrides.pop("sic", "3571"))
    b.history(cik, [2019, 2020, 2021, 2022, 2023, 2024], **{**HEALTHY, **overrides})
    b.shares(cik, 100_000_000, "2025-04-30", "2025-05-05")
    b.price(cik, "2025-06-27", 20.0)


def by_cik(rows):
    return {row["cik"]: row for row in rows}


class TestMaterialise:
    def test_a_fact_filed_after_the_as_of_date_is_not_there(self, store):
        b = StoreBuilder(store)
        b.filer(1)
        b.annual(1, "2024-12-31", "2025-07-15", NetIncomeLoss=5.0)
        b.done()
        AsOfView(store, AS_OF).materialise()
        assert store.execute("SELECT COUNT(*) FROM temp.fact_asof").fetchone()[0] == 0

    def test_a_restatement_is_seen_only_once_it_was_filed(self, store):
        b = StoreBuilder(store)
        b.filer(1)
        b.annual(1, "2023-12-31", "2024-02-15", NetIncomeLoss=100.0)
        later = b._accession(1)
        store.execute(
            "INSERT INTO filing (accession_no, cik, form_type, filed_date) "
            "VALUES (?, 1, '10-K', '2025-02-15')",
            (later,),
        )
        b.fact(1, later, "NetIncomeLoss", 90.0, "2023-12-31", "2025-02-15", start="2023-01-01")
        b.done()

        AsOfView(store, "2024-12-31").materialise()
        before = store.execute("SELECT value FROM temp.fact_asof").fetchall()
        AsOfView(store, AS_OF).materialise()
        after = store.execute("SELECT value FROM temp.fact_asof").fetchall()
        assert [r[0] for r in before] == [100.0]
        assert [r[0] for r in after] == [90.0]

    def test_a_price_after_the_as_of_date_is_not_there(self, store):
        b = StoreBuilder(store)
        b.filer(1)
        b.price(1, "2025-06-27", 10.0)
        b.price(1, "2025-07-01", 99.0)
        b.done()
        AsOfView(store, AS_OF).materialise()
        rows = store.execute("SELECT close, price_date FROM temp.price_asof").fetchall()
        assert [tuple(r) for r in rows] == [(10.0, "2025-06-27")]


class TestAnnualFigures:
    def test_one_row_per_fiscal_year_known_by_then(self, store):
        b = StoreBuilder(store)
        b.filer(1)
        b.history(1, [2022, 2023, 2024], **HEALTHY)
        b.done()
        prepare(store, "2025-01-31")
        years = [row["fy_end"] for row in annual_rows(store, 1)]
        assert years == ["2022-12-31", "2023-12-31"]

    def test_revenue_falls_back_to_the_asc_606_tag(self, store):
        b = StoreBuilder(store)
        b.filer(1)
        b.annual(
            1, "2024-12-31", "2025-02-15", RevenueFromContractWithCustomerExcludingAssessedTax=500.0
        )
        b.done()
        prepare(store, AS_OF)
        assert annual_rows(store, 1)[0]["revenue"] == 500.0

    def test_gross_profit_is_derived_when_not_reported(self, store):
        b = StoreBuilder(store)
        b.filer(1)
        b.annual(1, "2024-12-31", "2025-02-15", Revenues=500.0, CostOfGoodsAndServicesSold=300.0)
        b.done()
        prepare(store, AS_OF)
        assert annual_rows(store, 1)[0]["gross_profit"] == 200.0

    def test_liabilities_are_derived_when_only_the_total_is_reported(self, store):
        b = StoreBuilder(store)
        b.filer(1)
        b.annual(
            1,
            "2024-12-31",
            "2025-02-15",
            NetIncomeLoss=50.0,
            LiabilitiesAndStockholdersEquity=1000.0,
            StockholdersEquity=700.0,
        )
        b.done()
        prepare(store, AS_OF)
        assert annual_rows(store, 1)[0]["liabilities"] == 300.0

    def test_a_quarter_is_not_a_fiscal_year(self, store):
        b = StoreBuilder(store)
        b.filer(1)
        acc = b.annual(1, "2024-12-31", "2025-02-15", NetIncomeLoss=100.0)
        b.fact(1, acc, "NetIncomeLoss", 30.0, "2024-12-31", "2025-02-15", start="2024-10-01")
        b.done()
        prepare(store, AS_OF)
        rows = annual_rows(store, 1)
        assert len(rows) == 1
        assert rows[0]["net_income"] == 100.0


class TestUniverse:
    def test_a_healthy_filer_is_eligible(self, store):
        b = StoreBuilder(store)
        eligible_filer(b, 1)
        b.done()
        prepare(store, AS_OF)
        row = by_cik(universe_rows(store))[1]
        assert row["excluded_because"] is None
        assert row["market_cap"] == pytest.approx(2_000_000_000)

    def test_financials_go_to_their_own_track(self, store):
        b = StoreBuilder(store)
        eligible_filer(b, 1, sic="6021")
        b.done()
        prepare(store, AS_OF)
        row = by_cik(universe_rows(store))[1]
        assert row["track"] == "financial"
        assert "financial" in row["excluded_because"]

    def test_no_recent_10k_is_excluded(self, store):
        b = StoreBuilder(store)
        b.filer(1)
        b.history(1, [2017, 2018, 2019, 2020, 2021, 2022], **HEALTHY)
        b.shares(1, 100_000_000, "2025-04-30", "2025-05-05")
        b.price(1, "2025-06-27", 20.0)
        b.done()
        prepare(store, AS_OF)
        assert "15 months" in by_cik(universe_rows(store))[1]["excluded_because"]

    def test_short_history_is_excluded(self, store):
        b = StoreBuilder(store)
        b.filer(1)
        b.history(1, [2022, 2023, 2024], **HEALTHY)
        b.shares(1, 100_000_000, "2025-04-30", "2025-05-05")
        b.price(1, "2025-06-27", 20.0)
        b.done()
        prepare(store, AS_OF)
        assert "5 years" in by_cik(universe_rows(store))[1]["excluded_because"]

    def test_a_stale_price_is_not_a_price(self, store):
        """A filer that stopped trading still has an old close on file. Using it as
        today's market cap would value a dead company at its last good day."""
        b = StoreBuilder(store)
        b.filer(1)
        b.history(1, [2019, 2020, 2021, 2022, 2023, 2024], **HEALTHY)
        b.shares(1, 100_000_000, "2025-04-30", "2025-05-05")
        b.price(1, "2025-03-03", 20.0)
        b.done()
        prepare(store, AS_OF)
        assert "price" in by_cik(universe_rows(store))[1]["excluded_because"]

    def test_below_the_market_cap_floor_is_excluded(self, store):
        b = StoreBuilder(store)
        eligible_filer(b, 1)
        store.execute("UPDATE price SET close = 1.0")
        b.done()
        prepare(store, AS_OF)
        assert "$300M" in by_cik(universe_rows(store))[1]["excluded_because"]

    def test_a_filer_that_failed_later_is_still_in_the_universe(self, store):
        """Survivorship: as of mid-2025 this company had not failed yet."""
        b = StoreBuilder(store)
        eligible_filer(b, 1)
        store.execute("UPDATE filer SET status = 'delisted_for_cause', status_date = '2025-11-01'")
        b.done()
        prepare(store, AS_OF)
        assert 1 in by_cik(universe_rows(store))

    def test_a_filer_that_had_already_failed_is_not(self, store):
        b = StoreBuilder(store)
        eligible_filer(b, 1)
        store.execute("UPDATE filer SET status = 'deregistered', status_date = '2025-01-15'")
        b.done()
        prepare(store, AS_OF)
        assert 1 not in by_cik(universe_rows(store))
