"""The deterministic screener.

The screener is SQL. No model runs here, because the job is arithmetic and the cost of a
hallucinated ratio is a wasted analysis pass. Every view reads only the TEMP tables that
`AsOfView.materialise` writes, so the screens cannot see anything filed after the as-of
date: the gateway is still the only code that reads `fact` or `price`.

`prepare` builds, in order:

- `annual_raw`: one row per filer per fiscal year, one column per tag.
- `annual`: the figures the screens use, with the fallbacks filers force on us
  (revenue under four different tags, gross profit reported or derived, and so on).
- `shares_latest`: the most recent share count known by the as-of date.
- `universe_screened`: every filer in the as-of universe with its market cap and, if
  it cannot be screened, the first filter it failed.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from dossier.asof import AsOfView

#: The build plan's universe filters.
MARKET_CAP_FLOOR = 300_000_000
MIN_FISCAL_YEARS = 5
RECENT_10K_MONTHS = 15
MAX_PRICE_AGE_DAYS = 10
#: SEC division H: finance, insurance and real estate. Their balance sheets break every
#: standard ratio, so they go to a separate track rather than into these screens.
FINANCIAL_SIC = (6000, 6799)

#: A duration this long is a fiscal year; 52/53-week years land at 364 or 371 days.
FISCAL_YEAR_DAYS = (350, 380)

#: (tag, unit) pairs pivoted into `annual_raw`.
ANNUAL_TAGS = [
    ("Revenues", "USD"),
    ("RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
    ("RevenueFromContractWithCustomerIncludingAssessedTax", "USD"),
    ("SalesRevenueNet", "USD"),
    ("CostOfRevenue", "USD"),
    ("CostOfGoodsAndServicesSold", "USD"),
    ("GrossProfit", "USD"),
    ("OperatingIncomeLoss", "USD"),
    ("NetIncomeLoss", "USD"),
    ("NetCashProvidedByUsedInOperatingActivities", "USD"),
    ("PaymentsToAcquirePropertyPlantAndEquipment", "USD"),
    ("DepreciationDepletionAndAmortization", "USD"),
    ("DepreciationAmortizationAndAccretionNet", "USD"),
    ("Assets", "USD"),
    ("AssetsCurrent", "USD"),
    ("Liabilities", "USD"),
    ("LiabilitiesCurrent", "USD"),
    ("LiabilitiesAndStockholdersEquity", "USD"),
    ("StockholdersEquity", "USD"),
    ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "USD"),
    ("CashAndCashEquivalentsAtCarryingValue", "USD"),
    ("LongTermDebtNoncurrent", "USD"),
    ("LongTermDebt", "USD"),
    ("LongTermDebtCurrent", "USD"),
    ("DebtCurrent", "USD"),
    ("PropertyPlantAndEquipmentNet", "USD"),
    ("WeightedAverageNumberOfSharesOutstandingBasic", "shares"),
    ("WeightedAverageNumberOfDilutedSharesOutstanding", "shares"),
]


def _annual_raw_sql() -> str:
    lo, hi = FISCAL_YEAR_DAYS
    columns = ",\n  ".join(
        f"MAX(CASE WHEN tag = '{tag}' AND unit = '{unit}' THEN value END) AS \"{tag}\""
        for tag, unit in ANNUAL_TAGS
    )
    return f"""
CREATE TEMP VIEW annual_raw AS
WITH fiscal_year AS (
  SELECT DISTINCT cik, period_end AS fy_end FROM fact_asof
  WHERE period_start <> ''
    AND julianday(period_end) - julianday(period_start) BETWEEN {lo} AND {hi}
),
reported AS (
  SELECT f.cik, y.fy_end, f.tag, f.unit, f.value
  FROM fact_asof f JOIN fiscal_year y ON f.cik = y.cik AND f.period_end = y.fy_end
  WHERE f.period_start = ''
     OR julianday(f.period_end) - julianday(f.period_start) BETWEEN {lo} AND {hi}
)
SELECT cik, fy_end,
  {columns}
FROM reported GROUP BY cik, fy_end
"""


_REVENUE = (
    'COALESCE("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", '
    '"RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet")'
)
_COST = 'COALESCE("CostOfRevenue", "CostOfGoodsAndServicesSold")'
_EQUITY = (
    'COALESCE("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", '
    '"StockholdersEquity")'
)

ANNUAL_SQL = f"""
CREATE TEMP VIEW annual AS
SELECT cik, fy_end,
  {_REVENUE} AS revenue,
  {_COST} AS cost_of_revenue,
  COALESCE("GrossProfit", {_REVENUE} - {_COST}) AS gross_profit,
  "OperatingIncomeLoss" AS ebit,
  "NetIncomeLoss" AS net_income,
  "NetCashProvidedByUsedInOperatingActivities" AS cfo,
  "PaymentsToAcquirePropertyPlantAndEquipment" AS capex,
  COALESCE("DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet")
    AS depreciation,
  "Assets" AS assets,
  "AssetsCurrent" AS current_assets,
  "LiabilitiesCurrent" AS current_liabilities,
  COALESCE("Liabilities", "LiabilitiesAndStockholdersEquity" - {_EQUITY}) AS liabilities,
  {_EQUITY} AS equity,
  "CashAndCashEquivalentsAtCarryingValue" AS cash,
  COALESCE("LongTermDebtNoncurrent", "LongTermDebt" - COALESCE("LongTermDebtCurrent", 0))
    AS long_term_debt,
  COALESCE("DebtCurrent", "LongTermDebtCurrent") AS current_debt,
  "PropertyPlantAndEquipmentNet" AS ppe,
  COALESCE("WeightedAverageNumberOfSharesOutstandingBasic",
           "WeightedAverageNumberOfDilutedSharesOutstanding") AS shares_weighted
FROM annual_raw
"""

SHARES_SQL = """
CREATE TEMP VIEW shares_latest AS
SELECT cik, value AS shares, period_end AS shares_date, tag AS shares_tag,
       accession_no AS shares_accession, filed_date AS shares_filed
FROM (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY cik
    ORDER BY period_end DESC,
             CASE tag WHEN 'EntityCommonStockSharesOutstanding' THEN 0 ELSE 1 END
  ) AS recency
  FROM fact_asof
  WHERE unit = 'shares' AND period_start = ''
    AND tag IN ('EntityCommonStockSharesOutstanding', 'CommonStockSharesOutstanding')
)
WHERE recency = 1
"""

UNIVERSE_SQL = f"""
CREATE TEMP VIEW universe_screened AS
WITH p AS (SELECT as_of FROM asof_param),
years AS (
  SELECT cik, COUNT(*) AS fiscal_years, MAX(fy_end) AS latest_fy_end FROM annual GROUP BY cik
),
tenk AS (
  SELECT cik, MAX(filed_date) AS last_10k FROM filing_asof
  WHERE form_type IN ('10-K', '10-K/A', '10-KT') GROUP BY cik
),
base AS (
  SELECT u.cik, u.name, u.ticker, u.sic,
    CASE WHEN CAST(u.sic AS INTEGER) BETWEEN {FINANCIAL_SIC[0]} AND {FINANCIAL_SIC[1]}
         THEN 'financial' ELSE 'general' END AS track,
    t.last_10k, COALESCE(y.fiscal_years, 0) AS fiscal_years, y.latest_fy_end,
    CASE WHEN pr.price_date >= date(p.as_of, '-{MAX_PRICE_AGE_DAYS} days')
         THEN pr.close END AS price,
    pr.price_date,
    s.shares, s.shares_date, s.shares_tag, s.shares_accession, s.shares_filed,
    t.last_10k >= date(p.as_of, '-{RECENT_10K_MONTHS} months') AS recent_10k
  FROM universe_asof u
  CROSS JOIN p
  LEFT JOIN tenk t ON t.cik = u.cik
  LEFT JOIN years y ON y.cik = u.cik
  LEFT JOIN price_asof pr ON pr.cik = u.cik
  LEFT JOIN shares_latest s ON s.cik = u.cik
)
SELECT cik, name, ticker, sic, track, last_10k, fiscal_years, latest_fy_end,
  price, price_date, shares, shares_date, shares_tag, shares_accession, shares_filed,
  price * shares AS market_cap,
  CASE
    WHEN recent_10k IS NOT 1
      THEN 'no 10-K filed in the {RECENT_10K_MONTHS} months to the as-of date'
    WHEN track = 'financial' THEN 'financial filer: bank, insurer or REIT screens are separate'
    WHEN fiscal_years < {MIN_FISCAL_YEARS}
      THEN 'fewer than {MIN_FISCAL_YEARS} years of XBRL history'
    WHEN price IS NULL THEN 'no price within {MAX_PRICE_AGE_DAYS} days of the as-of date'
    WHEN shares IS NULL THEN 'no share count on file'
    WHEN price * shares < {MARKET_CAP_FLOOR} THEN 'market cap below $300M'
  END AS excluded_because
FROM base
"""

#: A Piotroski score at or above this is flagged. Piotroski's own "high" portfolio was 8-9.
PIOTROSKI_FLAG = 8

_LO, _HI = FISCAL_YEAR_DAYS
_PIOTROSKI_TESTS = [
    "f_roa",
    "f_cfo",
    "f_delta_roa",
    "f_accrual",
    "f_leverage",
    "f_liquidity",
    "f_no_dilution",
    "f_margin",
    "f_turnover",
]

# Piotroski (2000), nine binary tests on the latest fiscal year (t) against the one
# before it (t-1). Return on assets uses beginning-of-year assets, so t-2's balance sheet
# is needed too. Two deliberate choices, both documented here because a reader will
# trip over them:
#
# - A filer with no long-term debt in either year passes the leverage test. Strictly,
#   leverage has to *fall*; a debt-free company cannot, and failing it for that would
#   penalise the least-levered balance sheets in the universe.
# - A filer is ranked only if all nine tests could be computed. Six passes out of six
#   computable tests is not comparable to six out of nine, so an incomplete score is
#   reported, never ranked.
PIOTROSKI_SQL = f"""
CREATE TEMP VIEW piotroski AS
WITH seq AS (
  SELECT a.*,
    LAG(fy_end) OVER w AS fy_end_1,
    LAG(fy_end, 2) OVER w AS fy_end_2,
    LAG(net_income) OVER w AS net_income_1,
    LAG(assets) OVER w AS assets_1,
    LAG(assets, 2) OVER w AS assets_2,
    LAG(long_term_debt) OVER w AS long_term_debt_1,
    LAG(current_assets) OVER w AS current_assets_1,
    LAG(current_liabilities) OVER w AS current_liabilities_1,
    LAG(shares_weighted) OVER w AS shares_weighted_1,
    LAG(gross_profit) OVER w AS gross_profit_1,
    LAG(revenue) OVER w AS revenue_1,
    ROW_NUMBER() OVER (PARTITION BY cik ORDER BY fy_end DESC) AS recency
  FROM annual a
  WINDOW w AS (PARTITION BY cik ORDER BY fy_end)
),
latest AS (
  SELECT *, net_income / assets_1 AS roa, net_income_1 / assets_2 AS roa_1
  FROM seq
  WHERE recency = 1
    AND julianday(fy_end) - julianday(fy_end_1) BETWEEN {_LO} AND {_HI}
    AND julianday(fy_end_1) - julianday(fy_end_2) BETWEEN {_LO} AND {_HI}
),
tested AS (
  SELECT l.*,
    CASE WHEN roa IS NULL THEN NULL WHEN roa > 0 THEN 1 ELSE 0 END AS f_roa,
    CASE WHEN cfo IS NULL THEN NULL WHEN cfo > 0 THEN 1 ELSE 0 END AS f_cfo,
    CASE WHEN roa IS NULL OR roa_1 IS NULL THEN NULL
         WHEN roa > roa_1 THEN 1 ELSE 0 END AS f_delta_roa,
    CASE WHEN cfo IS NULL OR net_income IS NULL THEN NULL
         WHEN cfo > net_income THEN 1 ELSE 0 END AS f_accrual,
    CASE WHEN NULLIF(assets, 0) IS NULL OR NULLIF(assets_1, 0) IS NULL THEN NULL
         WHEN COALESCE(long_term_debt, 0) = 0 AND COALESCE(long_term_debt_1, 0) = 0 THEN 1
         WHEN COALESCE(long_term_debt, 0) / assets
              < COALESCE(long_term_debt_1, 0) / assets_1 THEN 1 ELSE 0 END AS f_leverage,
    CASE WHEN current_assets IS NULL OR current_assets_1 IS NULL
           OR NULLIF(current_liabilities, 0) IS NULL
           OR NULLIF(current_liabilities_1, 0) IS NULL THEN NULL
         WHEN current_assets / current_liabilities
              > current_assets_1 / current_liabilities_1 THEN 1 ELSE 0 END AS f_liquidity,
    CASE WHEN shares_weighted IS NULL OR shares_weighted_1 IS NULL THEN NULL
         WHEN shares_weighted <= shares_weighted_1 THEN 1 ELSE 0 END AS f_no_dilution,
    CASE WHEN gross_profit IS NULL OR gross_profit_1 IS NULL
           OR NULLIF(revenue, 0) IS NULL OR NULLIF(revenue_1, 0) IS NULL THEN NULL
         WHEN gross_profit / revenue > gross_profit_1 / revenue_1 THEN 1 ELSE 0 END AS f_margin,
    CASE WHEN revenue IS NULL OR revenue_1 IS NULL
           OR NULLIF(assets_1, 0) IS NULL OR NULLIF(assets_2, 0) IS NULL THEN NULL
         WHEN revenue / assets_1 > revenue_1 / assets_2 THEN 1 ELSE 0 END AS f_turnover
  FROM latest l
),
scored AS (
  SELECT t.*, u.name, u.ticker, u.market_cap,
    {" + ".join(f"COALESCE({name}, 0)" for name in _PIOTROSKI_TESTS)} AS score,
    {" + ".join(f"({name} IS NOT NULL)" for name in _PIOTROSKI_TESTS)} AS tests_scored
  FROM tested t
  JOIN universe_screened u ON u.cik = t.cik AND u.excluded_because IS NULL
)
SELECT *,
  CASE WHEN tests_scored = 9
       THEN RANK() OVER (PARTITION BY tests_scored = 9 ORDER BY score DESC, roa DESC) END
    AS rank,
  (tests_scored = 9 AND score >= {PIOTROSKI_FLAG}) AS flagged
FROM scored
"""

_VIEWS = ["piotroski", "universe_screened", "shares_latest", "annual", "annual_raw"]


def prepare(conn: sqlite3.Connection, as_of: date | str) -> AsOfView:
    """Materialise the as-of state and (re)build the screen views over it."""
    view = AsOfView(conn, as_of)
    view.materialise()
    with conn:
        for name in _VIEWS:
            conn.execute(f"DROP VIEW IF EXISTS temp.{name}")
        for sql in (_annual_raw_sql(), ANNUAL_SQL, SHARES_SQL, UNIVERSE_SQL, PIOTROSKI_SQL):
            conn.execute(sql)
    return view


def annual_rows(conn: sqlite3.Connection, cik: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM annual WHERE cik = ? ORDER BY fy_end", (cik,)).fetchall()


def universe_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM universe_screened ORDER BY cik").fetchall()


def piotroski_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Ranked filers first, then any whose score could not be completed."""
    return conn.execute("SELECT * FROM piotroski ORDER BY rank IS NULL, rank, cik").fetchall()
