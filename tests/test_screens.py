"""The deterministic screener.

No model runs here: the job is arithmetic, and a hallucinated ratio costs a wasted
analysis pass. What these tests guard is that the arithmetic only ever sees what was
knowable on the as-of date, and that every filer that drops out of the universe says why.
"""

import pytest

from dossier.asof import AsOfView
from dossier.screens import annual_rows, piotroski_rows, prepare, universe_rows
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


class TestCapexIsNotOneTag:
    """Filers migrate XBRL elements and never migrate back. A live run found 83 of 289
    eligible filers with no annual capital expenditure: American Electric Power last
    reported `PaymentsToAcquirePropertyPlantAndEquipment` in 2020, MasTec in 2011. They
    did not stop spending — they started tagging it differently. One hard-coded element
    silently shrank the owner earnings screen by a third of its universe."""

    @pytest.mark.parametrize(
        "tag",
        [
            "PaymentsToAcquireProductiveAssets",
            "PaymentsForCapitalImprovements",
            "PaymentsToAcquireOtherPropertyPlantAndEquipment",
        ],
    )
    def test_an_alternate_capex_element_is_read(self, store, tag):
        b = StoreBuilder(store)
        eligible_filer(
            b, 1, PaymentsToAcquirePropertyPlantAndEquipment=None, **{tag: 60_000_000}
        )
        b.done()
        prepare(store, AS_OF)
        assert by_cik(annual_rows(store, 1))[1]["capex"] == 60_000_000

    def test_the_standard_element_still_wins_when_both_are_present(self, store):
        """A filer reporting both is reporting the same spending twice, under a general
        element and a specific one. Taking the standard one keeps it comparable."""
        b = StoreBuilder(store)
        eligible_filer(b, 1, PaymentsToAcquireProductiveAssets=99_000_000)
        b.done()
        prepare(store, AS_OF)
        assert by_cik(annual_rows(store, 1))[1]["capex"] == 60_000_000


class TestTheScreensSayWhatTheyCouldNotRank:
    """A screen that ranks 198 of 289 eligible filers is not wrong, but the missing 91
    are invisible unless it says so. "Rank 3 of 198" reads like the whole universe."""

    def test_a_filer_with_no_capex_is_counted_as_uncovered(self, store):
        from dossier.screens import build_candidates

        b = StoreBuilder(store)
        eligible_filer(b, 1)
        eligible_filer(
            b, 2, name="No Capex Co", PaymentsToAcquirePropertyPlantAndEquipment=None
        )
        b.done()
        run = build_candidates(store, AS_OF)
        owner_earnings = run["screens"]["owner_earnings"]
        assert owner_earnings["missing_data"]["no annual capital expenditure"] == 1
        assert owner_earnings["eligible"] == 2
        assert owner_earnings["ranked"] == 1

    def test_a_screen_that_skips_a_filer_by_definition_is_not_a_data_gap(self, store):
        """Net-net ranks only filers whose net current assets are positive. That is the
        screen working, and counting it as missing data would cry wolf."""
        from dossier.screens import build_candidates

        b = StoreBuilder(store)
        eligible_filer(b, 1)
        eligible_filer(b, 2, name="Indebted Co", Liabilities=3_000_000_000)
        b.done()
        run = build_candidates(store, AS_OF)
        assert run["screens"]["net_net"]["missing_data"] == {}
        assert run["screens"]["net_net"]["ranked"] == 1


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

    def test_a_stale_cover_page_count_gives_way_to_the_weighted_average(self, store):
        """Found live: A. O. Smith's last single cover-page count is from 2015. Since then
        it has reported per share class, which company facts leave out."""
        b = StoreBuilder(store)
        b.filer(1)
        b.history(1, [2019, 2020, 2021, 2022, 2023, 2024], **HEALTHY)
        b.shares(1, 40_000_000, "2015-08-05", "2015-08-10")
        b.price(1, "2025-06-27", 20.0)
        b.done()
        prepare(store, AS_OF)
        row = by_cik(universe_rows(store))[1]
        assert row["shares_tag"] == "WeightedAverageNumberOfSharesOutstandingBasic"
        assert row["market_cap"] == pytest.approx(20.0 * 100_000_000)

    def test_a_count_far_below_the_weighted_average_is_one_class_of_several(self, store):
        """Found live: HEICO's balance-sheet count covers one of its two classes, 55M of
        139M. Priced as the whole company it looked five times cheaper than it is."""
        b = StoreBuilder(store)
        b.filer(1)
        b.history(1, [2019, 2020, 2021, 2022, 2023, 2024], **HEALTHY)
        b.shares(1, 40_000_000, "2025-04-30", "2025-05-05")
        b.price(1, "2025-06-27", 20.0)
        b.done()
        prepare(store, AS_OF)
        row = by_cik(universe_rows(store))[1]
        assert row["shares"] == 100_000_000
        assert row["shares_tag"] == "WeightedAverageNumberOfSharesOutstandingBasic"

    def test_a_fresh_whole_company_count_is_preferred(self, store):
        """The cover-page count is the most current, so it wins when it plausibly counts
        every share: here a 2% buyback since fiscal year end."""
        b = StoreBuilder(store)
        b.filer(1)
        b.history(1, [2019, 2020, 2021, 2022, 2023, 2024], **HEALTHY)
        b.shares(1, 98_000_000, "2025-04-30", "2025-05-05")
        b.price(1, "2025-06-27", 20.0)
        b.done()
        prepare(store, AS_OF)
        row = by_cik(universe_rows(store))[1]
        assert row["shares"] == 98_000_000
        assert row["shares_tag"] == "EntityCommonStockSharesOutstanding"

    def test_no_current_count_of_any_kind_is_excluded(self, store):
        values = {
            k: v for k, v in HEALTHY.items() if k != "WeightedAverageNumberOfSharesOutstandingBasic"
        }
        b = StoreBuilder(store)
        b.filer(1)
        b.history(1, [2019, 2020, 2021, 2022, 2023, 2024], **values)
        b.shares(1, 100_000_000, "2011-07-29", "2011-08-05")
        b.price(1, "2025-06-27", 20.0)
        b.done()
        prepare(store, AS_OF)
        assert "share count" in by_cik(universe_rows(store))[1]["excluded_because"]

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


# -- Piotroski ------------------------------------------------------------------------

#: Three consecutive fiscal years in which every one of the nine tests passes in 2024.
IMPROVING = {
    2022: {"Assets": 1000.0, "NetIncomeLoss": 40.0},
    2023: {
        "NetIncomeLoss": 50.0,
        "Assets": 1000.0,
        "NetCashProvidedByUsedInOperatingActivities": 60.0,
        "LongTermDebtNoncurrent": 300.0,
        "AssetsCurrent": 400.0,
        "LiabilitiesCurrent": 300.0,
        "WeightedAverageNumberOfSharesOutstandingBasic": 100.0,
        "Revenues": 1000.0,
        "GrossProfit": 300.0,
    },
    2024: {
        "NetIncomeLoss": 80.0,
        "Assets": 1000.0,
        "NetCashProvidedByUsedInOperatingActivities": 120.0,
        "LongTermDebtNoncurrent": 250.0,
        "AssetsCurrent": 450.0,
        "LiabilitiesCurrent": 300.0,
        "WeightedAverageNumberOfSharesOutstandingBasic": 100.0,
        "Revenues": 1100.0,
        "GrossProfit": 360.0,
    },
}

#: The mirror image: in 2024 only the accrual test passes, and only because operating
#: cash flow, while negative, is less negative than net income.
DETERIORATING = {
    2022: {"Assets": 1000.0, "NetIncomeLoss": 40.0},
    2023: IMPROVING[2024],
    2024: {
        "NetIncomeLoss": -10.0,
        "Assets": 1000.0,
        "NetCashProvidedByUsedInOperatingActivities": -5.0,
        "LongTermDebtNoncurrent": 400.0,
        "AssetsCurrent": 300.0,
        "LiabilitiesCurrent": 300.0,
        "WeightedAverageNumberOfSharesOutstandingBasic": 120.0,
        "Revenues": 900.0,
        "GrossProfit": 200.0,
    },
}


def screened_filer(b: StoreBuilder, cik: int, years: dict, early=(2019, 2020, 2021)):
    """An eligible filer whose last three fiscal years are given explicitly."""
    b.filer(cik, name=f"Filer {cik}")
    b.history(cik, list(early), **HEALTHY)
    for year, values in years.items():
        b.annual(cik, f"{year}-12-31", f"{year + 1}-02-15", **values)
    b.shares(cik, 100_000_000, "2025-04-30", "2025-05-05")
    b.price(cik, "2025-06-27", 20.0)


class TestPiotroski:
    def test_an_improving_company_scores_nine(self, store):
        b = StoreBuilder(store)
        screened_filer(b, 1, IMPROVING)
        b.done()
        prepare(store, AS_OF)
        row = by_cik(piotroski_rows(store))[1]
        assert row["tests_scored"] == 9
        assert row["score"] == 9
        assert row["flagged"] == 1

    def test_a_deteriorating_company_scores_one(self, store):
        b = StoreBuilder(store)
        screened_filer(b, 1, DETERIORATING)
        b.done()
        prepare(store, AS_OF)
        row = by_cik(piotroski_rows(store))[1]
        assert row["score"] == 1
        assert row["f_accrual"] == 1
        assert row["flagged"] == 0

    def test_ranks_higher_scores_first(self, store):
        b = StoreBuilder(store)
        screened_filer(b, 1, DETERIORATING)
        screened_filer(b, 2, IMPROVING)
        b.done()
        prepare(store, AS_OF)
        ranked = [row["cik"] for row in piotroski_rows(store) if row["rank"] is not None]
        assert ranked == [2, 1]

    def test_uses_the_latest_year_known_by_the_as_of_date(self, store):
        """As of January 2025 the 2024 10-K had not been filed, so the score is for 2023."""
        b = StoreBuilder(store)
        screened_filer(b, 1, IMPROVING)
        b.shares(1, 100_000_000, "2024-10-31", "2024-11-05")
        b.price(1, "2025-01-30", 20.0)
        b.done()
        prepare(store, "2025-01-31")
        assert by_cik(piotroski_rows(store))[1]["fy_end"] == "2023-12-31"

    def test_missing_inputs_leave_a_company_unranked(self, store):
        """Six passes out of six computable tests is not comparable to six out of nine."""
        b = StoreBuilder(store)
        incomplete = {
            **IMPROVING,
            2024: {
                k: v for k, v in IMPROVING[2024].items() if k not in ("Revenues", "GrossProfit")
            },
        }
        screened_filer(b, 1, incomplete)
        b.done()
        prepare(store, AS_OF)
        row = by_cik(piotroski_rows(store))[1]
        assert row["tests_scored"] < 9
        assert row["rank"] is None
        assert row["flagged"] == 0

    def test_a_gap_between_fiscal_years_is_not_a_comparison(self, store):
        """Comparing 2024 with 2022 because 2023 is missing would score two years of
        change as one."""
        b = StoreBuilder(store)
        gapped = {2021: IMPROVING[2022], 2022: IMPROVING[2023], 2024: IMPROVING[2024]}
        screened_filer(b, 1, gapped, early=(2017, 2018, 2019, 2020))
        b.done()
        prepare(store, AS_OF)
        assert 1 not in by_cik(piotroski_rows(store))

    def test_a_debt_free_company_passes_the_leverage_test(self, store):
        b = StoreBuilder(store)
        debt_free = {
            year: {k: v for k, v in values.items() if k != "LongTermDebtNoncurrent"}
            for year, values in IMPROVING.items()
        }
        screened_filer(b, 1, debt_free)
        b.done()
        prepare(store, AS_OF)
        assert by_cik(piotroski_rows(store))[1]["f_leverage"] == 1

    def test_only_the_eligible_universe_is_screened(self, store):
        b = StoreBuilder(store)
        screened_filer(b, 1, IMPROVING)
        store.execute("UPDATE price SET close = 0.01")
        b.done()
        prepare(store, AS_OF)
        assert 1 not in by_cik(piotroski_rows(store))
