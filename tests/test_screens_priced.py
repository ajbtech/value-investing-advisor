"""The four screens that need a market cap.

Every figure here is worked by hand from HEALTHY, the latest fiscal year of a filer
whose market cap is 20.00 x 100M shares = $2.0B:

- enterprise value = 2,000 + 150 debt - 200 cash = $1,950M
- tangible capital = (700 - 200) - (300 - 0) + 400 PP&E = $600M
- net current assets = 700 current assets - 600 total liabilities = $100M
- invested capital = 900 equity + 150 debt - 200 cash = $850M
"""

import pytest

from dossier.screens import prepare, screen_rows
from dossier.store import open_store
from tests.builders import StoreBuilder
from tests.test_screens import AS_OF, HEALTHY, by_cik, eligible_filer


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "test.sqlite") as conn:
        yield conn


def run(store, screen):
    store.commit()
    prepare(store, AS_OF)
    return by_cik(screen_rows(store, screen))


class TestMagicFormula:
    def test_earnings_yield_and_return_on_capital(self, store):
        eligible_filer(StoreBuilder(store), 1)
        row = run(store, "magic_formula")[1]
        assert row["enterprise_value"] == pytest.approx(1_950_000_000)
        assert row["earnings_yield"] == pytest.approx(300 / 1950)
        assert row["return_on_capital"] == pytest.approx(300 / 600)
        assert row["rank"] == 1
        assert row["flagged"] == 1

    def test_ranks_on_both_measures_together(self, store):
        b = StoreBuilder(store)
        eligible_filer(b, 1)
        eligible_filer(b, 2, OperatingIncomeLoss=600_000_000)
        rows = run(store, "magic_formula")
        assert rows[2]["rank"] == 1
        assert rows[1]["rank"] == 2

    def test_a_negative_enterprise_value_is_not_ranked(self, store):
        """Cash above market cap plus debt makes EV negative, and EBIT over a negative
        number is not a yield."""
        eligible_filer(StoreBuilder(store), 1, CashAndCashEquivalentsAtCarryingValue=5e9)
        assert 1 not in run(store, "magic_formula")

    def test_an_operating_loss_is_never_flagged(self, store):
        eligible_filer(StoreBuilder(store), 1, OperatingIncomeLoss=-50_000_000)
        assert run(store, "magic_formula")[1]["flagged"] == 0


class TestNetNet:
    def test_flags_a_market_cap_below_net_current_assets(self, store):
        eligible_filer(StoreBuilder(store), 1, AssetsCurrent=5e9, Liabilities=1e9)
        row = run(store, "net_net")[1]
        assert row["net_current_assets"] == pytest.approx(4e9)
        assert row["price_to_ncav"] == pytest.approx(0.5)
        assert row["flagged"] == 1

    def test_an_ordinary_company_is_not_a_net_net(self, store):
        eligible_filer(StoreBuilder(store), 1)
        row = run(store, "net_net")[1]
        assert row["price_to_ncav"] == pytest.approx(20.0)
        assert row["flagged"] == 0

    def test_negative_net_current_assets_are_not_ranked(self, store):
        eligible_filer(StoreBuilder(store), 1, Liabilities=900_000_000)
        assert 1 not in run(store, "net_net")


class TestOwnerEarnings:
    def test_maintenance_capex_is_the_smaller_of_capex_and_depreciation(self, store):
        eligible_filer(StoreBuilder(store), 1)
        row = run(store, "owner_earnings")[1]
        assert row["maintenance_capex"] == pytest.approx(50_000_000)
        assert row["owner_earnings_yield"] == pytest.approx(230 / 1950)

    def test_all_capex_counts_when_depreciation_is_missing(self, store):
        values = {k: v for k, v in HEALTHY.items() if k != "DepreciationDepletionAndAmortization"}
        b = StoreBuilder(store)
        b.filer(1)
        b.history(1, [2019, 2020, 2021, 2022, 2023, 2024], **values)
        b.shares(1, 100_000_000, "2025-04-30", "2025-05-05")
        b.price(1, "2025-06-27", 20.0)
        row = run(store, "owner_earnings")[1]
        assert row["maintenance_capex"] == pytest.approx(60_000_000)

    def test_missing_capex_is_not_assumed_to_be_zero(self, store):
        values = {
            k: v for k, v in HEALTHY.items() if k != "PaymentsToAcquirePropertyPlantAndEquipment"
        }
        b = StoreBuilder(store)
        b.filer(1)
        b.history(1, [2019, 2020, 2021, 2022, 2023, 2024], **values)
        b.shares(1, 100_000_000, "2025-04-30", "2025-05-05")
        b.price(1, "2025-06-27", 20.0)
        assert 1 not in run(store, "owner_earnings")


def quality_filer(b, cik, years=range(2018, 2025), **overrides):
    b.filer(cik, name=f"Filer {cik}")
    b.history(cik, list(years), **{**HEALTHY, **overrides})
    b.shares(cik, 100_000_000, "2025-04-30", "2025-05-05")
    b.price(cik, "2025-06-27", 20.0)


class TestQualityAtPrice:
    def test_seven_strong_years_and_a_cash_yield_is_flagged(self, store):
        quality_filer(StoreBuilder(store), 1)
        row = run(store, "quality_at_price")[1]
        assert row["years_counted"] == 7
        assert row["min_roic"] == pytest.approx(300 * 0.79 / 850)
        assert row["fcf_yield"] == pytest.approx(220 / 2000)
        assert row["flagged"] == 1

    def test_one_weak_year_breaks_the_streak(self, store):
        b = StoreBuilder(store)
        quality_filer(b, 1, years=range(2018, 2024))
        b.annual(1, "2024-12-31", "2025-02-15", **{**HEALTHY, "OperatingIncomeLoss": 10_000_000})
        assert run(store, "quality_at_price")[1]["flagged"] == 0

    def test_six_years_is_not_seven(self, store):
        eligible_filer(StoreBuilder(store), 1)
        row = run(store, "quality_at_price")[1]
        assert row["years_counted"] == 6
        assert row["flagged"] == 0

    def test_an_expensive_price_fails_the_cash_yield(self, store):
        b = StoreBuilder(store)
        quality_filer(b, 1)
        store.execute("UPDATE price SET close = 200.0")
        row = run(store, "quality_at_price")[1]
        assert row["fcf_yield"] == pytest.approx(220 / 20000)
        assert row["flagged"] == 0


class TestScreenNames:
    def test_an_unknown_screen_is_refused(self, store):
        prepare(store, AS_OF)
        with pytest.raises(ValueError, match="no screen"):
            screen_rows(store, "momentum")
