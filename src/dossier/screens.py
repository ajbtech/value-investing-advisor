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

import calendar
import json
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime

from dossier.asof import TERMINAL_STATUSES, AsOfView
from dossier.securities import is_common_stock

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
_LO, _HI = FISCAL_YEAR_DAYS

#: (tag, unit) pairs pivoted into `annual_raw`.
ANNUAL_TAGS = [
    ("Revenues", "USD"),
    ("RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
    ("RevenueFromContractWithCustomerIncludingAssessedTax", "USD"),
    ("SalesRevenueNet", "USD"),
    ("CostOfRevenue", "USD"),
    ("CostOfGoodsAndServicesSold", "USD"),
    ("CostOfGoodsSold", "USD"),
    ("CostOfServices", "USD"),
    ("GrossProfit", "USD"),
    ("OperatingIncomeLoss", "USD"),
    ("NetIncomeLoss", "USD"),
    ("NetCashProvidedByUsedInOperatingActivities", "USD"),
    ("PaymentsToAcquirePropertyPlantAndEquipment", "USD"),
    ("PaymentsToAcquireProductiveAssets", "USD"),
    ("PaymentsForCapitalImprovements", "USD"),
    ("PaymentsToAcquireOtherPropertyPlantAndEquipment", "USD"),
    ("PaymentsToAcquireMachineryAndEquipment", "USD"),
    ("PaymentsToDevelopSoftware", "USD"),
    ("PaymentsForSoftware", "USD"),
    ("CostOfGoodsAndServicesSoldExcludingDepreciationDepletionAndAmortization", "USD"),
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
    (
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfter"
        "AccumulatedDepreciationAndAmortization",
        "USD",
    ),
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
CREATE TEMP TABLE annual_raw AS
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
#: The ex-depreciation element is last and is a different measure from cost of revenue.
#: That is acceptable only because gross profit feeds Piotroski's margin test alone,
#: which compares a filer with its own prior year, never with another filer.
_COST = (
    'COALESCE("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", '
    '"CostOfServices", '
    '"CostOfGoodsAndServicesSoldExcludingDepreciationDepletionAndAmortization")'
)
#: Since ASC 842 many filers report property including finance-lease right-of-use assets
#: under one element rather than `PropertyPlantAndEquipmentNet`. Tangible capital reads
#: whichever the filer used.
_PPE = (
    'COALESCE("PropertyPlantAndEquipmentNet", '
    '"PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAsset'
    'AfterAccumulatedDepreciationAndAmortization")'
)
#: In priority order: the standard element first, so a filer reporting both stays
#: comparable with every other filer, then the alternates real filers migrate to.
_CAPEX = (
    'COALESCE("PaymentsToAcquirePropertyPlantAndEquipment", '
    '"PaymentsToAcquireProductiveAssets", "PaymentsForCapitalImprovements", '
    '"PaymentsToAcquireOtherPropertyPlantAndEquipment", '
    '"PaymentsToAcquireMachineryAndEquipment")'
)
#: Capitalized software is capital spending that the property elements leave out:
#: Teladoc capitalized $118.6M of it in 2025 against $8.9M the screen counted. It is
#: added to property capex, never used alone — a filer with no property capex is still
#: "no capex" — and never on top of `PaymentsToAcquireProductiveAssets`, which already
#: covers intangibles and would count the same dollars twice.
_SOFTWARE = 'COALESCE("PaymentsToDevelopSoftware", "PaymentsForSoftware", 0)'
_CAPEX_WITH_SOFTWARE = (
    f'{_CAPEX} + CASE WHEN "PaymentsToAcquirePropertyPlantAndEquipment" IS NULL '
    f'AND "PaymentsToAcquireProductiveAssets" IS NOT NULL THEN 0 ELSE {_SOFTWARE} END'
)
_EQUITY = (
    'COALESCE("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", '
    '"StockholdersEquity")'
)

#: `annual_raw` and `annual` are TEMP *tables*, not views: five screens read `annual`,
#: and as a view its pivot over every fact was recomputed for each of them.
ANNUAL_SQL = f"""
CREATE TEMP TABLE annual AS
SELECT cik, fy_end,
  {_REVENUE} AS revenue,
  {_COST} AS cost_of_revenue,
  COALESCE("GrossProfit", {_REVENUE} - {_COST}) AS gross_profit,
  "OperatingIncomeLoss" AS ebit,
  "NetIncomeLoss" AS net_income,
  "NetCashProvidedByUsedInOperatingActivities" AS cfo,
  {_CAPEX_WITH_SOFTWARE} AS capex,
  {_SOFTWARE} AS capitalized_software,
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
  {_PPE} AS ppe,
  COALESCE("WeightedAverageNumberOfSharesOutstandingBasic",
           "WeightedAverageNumberOfDilutedSharesOutstanding") AS shares_weighted
FROM annual_raw
"""

#: A cover-page or balance-sheet share count older than this is not a current count.
SHARE_COUNT_MAX_AGE_MONTHS = 15
#: A point-in-time count below this fraction of the year's weighted-average basic shares
#: is taken to be one class of several, not the whole company.
WHOLE_COMPANY_RATIO = 0.8

#: A chosen share count this many times smaller than the largest the filer has reported
#: in four years is denominated differently from its own other figures — Dillard's tagged
#: an annual weighted average of 15,655 against quarterly figures near 15,618,000. A
#: reverse split is a factor of ten or so, never a hundred, so this separates a filer's
#: units error from a real corporate action.
SHARE_COUNT_IMPLAUSIBLE_RATIO = 100

# The share count behind market cap, from two candidates:
#
# - the latest point-in-time count (the cover page's EntityCommonStockSharesOutstanding,
#   or the balance sheet's CommonStockSharesOutstanding), if under 15 months old. It is
#   the most current figure, so it is preferred.
# - the latest fiscal year's weighted-average basic shares, the EPS denominator, which
#   counts every class of common stock.
#
# Company facts leave out figures reported per share class. A multi-class filer's
# point-in-time count can therefore be one class only (HEICO: 55M of 139M), or years
# out of date (A. O. Smith: 2015). A point-in-time count under 80% of the weighted
# average is treated as partial and the weighted average used instead. A real buyback
# of more than a fifth within a year is sized a little high as a result: that errs
# toward a larger market cap and lower yields, the safer mistake for a screen.
SHARES_SQL = f"""
CREATE TEMP TABLE shares_latest AS
WITH p AS (SELECT as_of FROM asof_param),
point_in_time AS (
  SELECT * FROM (
    SELECT f.*, ROW_NUMBER() OVER (
      PARTITION BY f.cik
      ORDER BY f.period_end DESC,
               CASE f.tag WHEN 'EntityCommonStockSharesOutstanding' THEN 0 ELSE 1 END
    ) AS recency
    FROM fact_asof f CROSS JOIN p
    WHERE f.unit = 'shares' AND f.period_start = ''
      AND f.tag IN ('EntityCommonStockSharesOutstanding', 'CommonStockSharesOutstanding')
      AND f.period_end >= date(p.as_of, '-{SHARE_COUNT_MAX_AGE_MONTHS} months')
  ) WHERE recency = 1
),
weighted AS (
  SELECT * FROM (
    SELECT f.*, ROW_NUMBER() OVER (
      PARTITION BY f.cik
      ORDER BY f.period_end DESC,
               CASE f.tag WHEN 'WeightedAverageNumberOfSharesOutstandingBasic' THEN 0 ELSE 1 END
    ) AS recency
    FROM fact_asof f
    WHERE f.unit = 'shares' AND f.period_start <> ''
      AND julianday(f.period_end) - julianday(f.period_start) BETWEEN {_LO} AND {_HI}
      AND f.tag IN ('WeightedAverageNumberOfSharesOutstandingBasic',
                    'WeightedAverageNumberOfDilutedSharesOutstanding')
  ) WHERE recency = 1
),
-- The largest share figure the filer has reported recently, whatever the tag. It is the
-- yardstick for whether the chosen count is denominated the way the price is: Dillard's
-- tagged its annual weighted average as 15,655 while every quarterly figure that year was
-- about 15,618,000, a thousands error in its own XBRL. Market cap came out at $0.01B and
-- a company of roughly $5B was excluded for being under the $300M floor — silently, and
-- with every figure in the row internally consistent.
recent_max AS (
  SELECT f.cik, MAX(f.value) AS max_shares
  FROM fact_asof f CROSS JOIN p
  WHERE f.unit = 'shares' AND f.value > 0
    AND f.period_end >= date(p.as_of, '-4 years')
  GROUP BY f.cik
),
filers AS (SELECT cik FROM point_in_time UNION SELECT cik FROM weighted),
chosen AS (
  SELECT f.cik, i.value AS i_value, w.value AS w_value, m.max_shares,
    i.value IS NOT NULL AND (w.value IS NULL OR i.value >= {WHOLE_COMPANY_RATIO} * w.value)
      AS use_point_in_time,
    i.period_end AS i_date, i.tag AS i_tag, i.accession_no AS i_acc, i.filed_date AS i_filed,
    w.period_end AS w_date, w.tag AS w_tag, w.accession_no AS w_acc, w.filed_date AS w_filed
  FROM filers f
  LEFT JOIN point_in_time i ON i.cik = f.cik
  LEFT JOIN weighted w ON w.cik = f.cik
  LEFT JOIN recent_max m ON m.cik = f.cik
),
picked AS (
  SELECT chosen.*,
    CASE WHEN use_point_in_time THEN i_value ELSE w_value END AS picked_value
  FROM chosen
)
SELECT cik,
  -- A zero is not a share count. CHS reports EntityCommonStockSharesOutstanding as 0
  -- every quarter, which is true of its common stock and useless as a denominator.
  -- Below a hundredth of the filer's own recent maximum, the figure is denominated
  -- differently from the price; a reverse split is a factor of ten or so, never a
  -- hundred, so this catches the units errors without catching real corporate actions.
  CASE
    WHEN picked_value IS NULL OR picked_value <= 0 THEN NULL
    WHEN max_shares IS NOT NULL AND picked_value * {SHARE_COUNT_IMPLAUSIBLE_RATIO} < max_shares
      THEN NULL
    ELSE picked_value
  END AS shares,
  CASE WHEN use_point_in_time THEN i_date ELSE w_date END AS shares_date,
  CASE WHEN use_point_in_time THEN i_tag ELSE w_tag END AS shares_tag,
  CASE WHEN use_point_in_time THEN i_acc ELSE w_acc END AS shares_accession,
  CASE WHEN use_point_in_time THEN i_filed ELSE w_filed END AS shares_filed,
  picked_value AS shares_reported,
  max_shares AS shares_recent_max
FROM picked
"""

_TERMINAL_SQL = ", ".join(f"'{status}'" for status in TERMINAL_STATUSES)

UNIVERSE_SQL = f"""
CREATE TEMP TABLE universe_screened AS
WITH p AS (SELECT as_of FROM asof_param),
years AS (
  SELECT cik, COUNT(*) AS fiscal_years, MAX(fy_end) AS latest_fy_end FROM annual GROUP BY cik
),
tenk AS (
  SELECT cik, MAX(filed_date) AS last_10k FROM filing_asof
  WHERE form_type IN ('10-K', '10-K/A', '10-KT') GROUP BY cik
),
base AS (
  SELECT u.cik, u.name, u.ticker, u.sic, u.status,
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
    -- Only a filer that died *after* the as-of date is in `universe_asof` with a
    -- terminal status. It was investable that day, and no free source prices a ticker
    -- that has stopped trading, so this count is the survivorship bias that remains.
    WHEN price IS NULL AND status IN ({_TERMINAL_SQL})
      THEN 'no price for a filer that later stopped filing: the survivorship gap'
    WHEN ticker IS NULL THEN 'no ticker on file: not listed today, so it cannot be priced'
    WHEN NOT is_common_stock(ticker)
      THEN 'listed ticker is not common stock: ' || COALESCE(ticker, 'none')
    WHEN price IS NULL THEN 'no price within {MAX_PRICE_AGE_DAYS} days of the as-of date'
    WHEN shares IS NULL THEN 'no share count on file'
    WHEN price * shares < {MARKET_CAP_FLOOR} THEN 'market cap below $300M'
  END AS excluded_because
FROM base
"""

#: A Piotroski score at or above this is flagged. Piotroski's own "high" portfolio was 8-9.
PIOTROSKI_FLAG = 8
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

#: Ranked screens flag their top this-many. Threshold screens flag whatever passes.
TOP_N = 10
#: US federal statutory rate, used for every filer alike so ROIC is comparable across
#: filers rather than reflecting one year's tax items.
STATUTORY_TAX_RATE = 0.21
QUALITY_MIN_ROIC = 0.12
QUALITY_YEARS = 7
QUALITY_MIN_FCF_YIELD = 0.05

# The latest fiscal year of every eligible filer, with the enterprise value the priced
# screens share. Missing debt counts as none and missing cash as none, which errs toward
# a higher EV: a screen that overstates cheapness is the worse mistake.
SCREEN_BASE_SQL = """
CREATE TEMP TABLE screen_base AS
SELECT a.*, u.name, u.ticker, u.market_cap, u.price, u.price_date, u.shares, u.shares_date,
  COALESCE(a.long_term_debt, 0) + COALESCE(a.current_debt, 0) AS total_debt,
  u.market_cap + COALESCE(a.long_term_debt, 0) + COALESCE(a.current_debt, 0)
    - COALESCE(a.cash, 0) AS enterprise_value
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY cik ORDER BY fy_end DESC) AS recency
  FROM annual
) a
JOIN universe_screened u ON u.cik = a.cik AND u.excluded_because IS NULL
WHERE a.recency = 1
"""

# Greenblatt: earnings yield (EBIT / EV) and return on tangible capital
# (EBIT / (net working capital + net PP&E)), ranked separately and the ranks summed.
MAGIC_FORMULA_SQL = f"""
CREATE TEMP VIEW magic_formula AS
WITH m AS (
  SELECT b.*,
    ebit / enterprise_value AS earnings_yield,
    (current_assets - COALESCE(cash, 0)) - (current_liabilities - COALESCE(current_debt, 0))
      + ppe AS tangible_capital
  FROM screen_base b
  WHERE ebit IS NOT NULL AND enterprise_value > 0
    AND current_assets IS NOT NULL AND current_liabilities IS NOT NULL AND ppe IS NOT NULL
),
r AS (
  SELECT m.*, ebit / tangible_capital AS return_on_capital FROM m WHERE tangible_capital > 0
),
ranked AS (
  SELECT r.*,
    RANK() OVER (ORDER BY earnings_yield DESC) AS ey_rank,
    RANK() OVER (ORDER BY return_on_capital DESC) AS roc_rank
  FROM r
),
final AS (
  SELECT ranked.*,
    RANK() OVER (ORDER BY ey_rank + roc_rank, earnings_yield DESC) AS rank
  FROM ranked
)
SELECT *, (rank <= {TOP_N} AND ebit > 0) AS flagged FROM final
"""

# Graham: market cap below current assets less every liability.
NET_NET_SQL = """
CREATE TEMP VIEW net_net AS
WITH n AS (
  SELECT b.*, current_assets - liabilities AS net_current_assets
  FROM screen_base b
  WHERE current_assets IS NOT NULL AND liabilities IS NOT NULL
    AND current_assets - liabilities > 0
)
SELECT n.*, market_cap / net_current_assets AS price_to_ncav,
  RANK() OVER (ORDER BY market_cap / net_current_assets) AS rank,
  (market_cap < net_current_assets) AS flagged
FROM n
"""

# (Operating cash flow - maintenance capex) / EV. Maintenance capex is estimated as the
# smaller of capex and depreciation, or all of capex when depreciation is not reported.
# A filer with no capex figure is left out rather than assumed to spend nothing.
OWNER_EARNINGS_SQL = f"""
CREATE TEMP VIEW owner_earnings AS
WITH o AS (
  SELECT b.*,
    CASE WHEN depreciation IS NULL THEN capex ELSE MIN(capex, depreciation) END
      AS maintenance_capex
  FROM screen_base b
  WHERE cfo IS NOT NULL AND capex IS NOT NULL AND enterprise_value > 0
),
y AS (
  SELECT o.*, cfo - maintenance_capex AS owner_earnings,
    (cfo - maintenance_capex) / enterprise_value AS owner_earnings_yield
  FROM o
),
final AS (
  SELECT y.*, RANK() OVER (ORDER BY owner_earnings_yield DESC) AS rank FROM y
)
SELECT *, (rank <= {TOP_N} AND owner_earnings > 0) AS flagged FROM final
"""

# After-tax ROIC above 12% in each of the last seven consecutive fiscal years, and a
# free-cash-flow yield on market cap above 5%. Survivorship-flavoured by construction:
# only a company that lasted seven years can pass it.
QUALITY_SQL = f"""
CREATE TEMP VIEW quality_at_price AS
WITH yearly AS (
  SELECT cik, fy_end,
    ebit * (1 - {STATUTORY_TAX_RATE})
      / NULLIF(COALESCE(equity, 0) + COALESCE(long_term_debt, 0) + COALESCE(current_debt, 0)
               - COALESCE(cash, 0), 0) AS roic,
    ROW_NUMBER() OVER (PARTITION BY cik ORDER BY fy_end DESC) AS recency
  FROM annual
),
streak AS (
  SELECT cik, COUNT(*) AS years_counted, COUNT(roic) AS years_with_roic,
    MIN(roic) AS min_roic, MIN(fy_end) AS first_fy_end, MAX(fy_end) AS last_fy_end
  FROM yearly WHERE recency <= {QUALITY_YEARS} GROUP BY cik
),
q AS (
  SELECT b.*, s.years_counted, s.years_with_roic, s.min_roic, s.first_fy_end,
    cfo - capex AS free_cash_flow,
    (cfo - capex) / market_cap AS fcf_yield,
    (s.years_counted = {QUALITY_YEARS}
      AND s.years_with_roic = {QUALITY_YEARS}
      AND julianday(s.last_fy_end) - julianday(s.first_fy_end)
          BETWEEN {QUALITY_YEARS - 1} * {_LO} AND {QUALITY_YEARS - 1} * {_HI}
      AND s.min_roic > {QUALITY_MIN_ROIC}
      AND (cfo - capex) / market_cap > {QUALITY_MIN_FCF_YIELD}) AS flagged
  FROM screen_base b JOIN streak s ON s.cik = b.cik
  WHERE b.cfo IS NOT NULL AND b.capex IS NOT NULL
)
SELECT *,
  CASE WHEN flagged THEN RANK() OVER (PARTITION BY flagged ORDER BY fcf_yield DESC) END
    AS rank
FROM q
"""

#: The five screens, in the build plan's order. Never blended into one score: which
#: screen surfaced a company is the first thing the analysis layer needs to know.
SCREENS = {
    "magic_formula": "high EBIT/EV and high return on capital: cheap quality compounders",
    "piotroski": "nine accounting-health tests: improving balance sheets",
    "net_net": "market cap below net current assets: deep value, liquidation floor",
    "owner_earnings": "(operating cash flow - maintenance capex) / EV: true cash generation",
    "quality_at_price": "ROIC above 12% for 7 years with FCF yield above 5%: durable, on sale",
}

_VIEWS = [
    "quality_at_price",
    "owner_earnings",
    "net_net",
    "magic_formula",
    "piotroski",
]

#: Materialised once per run, and dropped after the views that read them. Every one of
#: these is read by several screens, and as views their work was repeated each time.
_TABLES = ["screen_base", "universe_screened", "shares_latest", "annual", "annual_raw"]

# SQLite refuses DROP VIEW on a table, so a name in both lists breaks the second
# `prepare` on a connection — which is exactly what running the screens at several
# dates does.
assert not set(_VIEWS) & set(_TABLES), "a screen object is either a view or a table"


def prepare(conn: sqlite3.Connection, as_of: date | str) -> AsOfView:
    """Materialise the as-of state and (re)build the screen views over it."""
    view = AsOfView(conn, as_of)
    view.materialise()
    # A preferred issue's price is not the company's share price, and a market cap built
    # from one is wrong by a multiple nobody can see. Registered here so the universe SQL
    # can ask, rather than approximating the rule in SQL and having two of them.
    conn.create_function("is_common_stock", 1, is_common_stock, deterministic=True)
    with conn:
        for name in _VIEWS:
            conn.execute(f"DROP VIEW IF EXISTS temp.{name}")
        for name in _TABLES:
            conn.execute(f"DROP TABLE IF EXISTS temp.{name}")
        for sql in (
            _annual_raw_sql(),
            ANNUAL_SQL,
            SHARES_SQL,
            UNIVERSE_SQL,
            PIOTROSKI_SQL,
            SCREEN_BASE_SQL,
            MAGIC_FORMULA_SQL,
            NET_NET_SQL,
            OWNER_EARNINGS_SQL,
            QUALITY_SQL,
        ):
            conn.execute(sql)
        conn.execute("CREATE INDEX temp.annual_by_filer ON annual (cik, fy_end)")
        conn.execute("CREATE INDEX temp.universe_by_filer ON universe_screened (cik)")
        conn.execute("CREATE INDEX temp.screen_base_by_filer ON screen_base (cik)")
    return view


def annual_rows(conn: sqlite3.Connection, cik: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM annual WHERE cik = ? ORDER BY fy_end", (cik,)).fetchall()


def universe_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM universe_screened ORDER BY cik").fetchall()


def screen_rows(conn: sqlite3.Connection, screen: str) -> list[sqlite3.Row]:
    """One screen's output: ranked filers first, then any it could not rank."""
    if screen not in SCREENS:
        raise ValueError(f"no screen named {screen!r}; the screens are {', '.join(SCREENS)}")
    # `screen` is checked against a fixed set above, so it is safe to interpolate.
    return conn.execute(f"SELECT * FROM {screen} ORDER BY rank IS NULL, rank, cik").fetchall()


def piotroski_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return screen_rows(conn, "piotroski")


# -- the output contract ------------------------------------------------------------

#: Bumped when any screen's arithmetic changes. Part of the screen job's key, so a fixed
#: screen re-runs rather than serving the old answer from cache.
#: 3: `first_seen` repaired (migration 007). The job fingerprint counts rows and a
#: migration edits them, so a data repair has to be announced here or a cached run keeps
#: serving the answer from before it.
#: 4: capital expenditure reads four alternate elements as well as the standard one, and
#: every screen reports what it could not rank. Both change what a run says, so a cached
#: answer from before them is the wrong answer.
#: 5: a filer whose listed ticker is a preferred issue, warrant or unit leaves the
#: universe rather than being priced off a security that is not its shares.
#: 6: a filer with no ticker, or one that later stopped filing, is excluded under its own
#: reason instead of as "not common stock: none", so the survivorship gap is counted.
#: 7: capex includes capitalized software, and gross profit falls back to the
#: ex-depreciation cost element.
SCREENER_VERSION = "7"

#: The plan asks for roughly thirty: enough to be worth analysing, few enough to afford.
CANDIDATE_LIMIT = 30

LABELS = {
    "magic_formula": "Magic Formula",
    "piotroski": "Piotroski",
    "net_net": "Net-net",
    "owner_earnings": "Owner earnings yield",
    "quality_at_price": "Quality at price",
}

#: The figures each screen reports about a filer it flagged.
_METRICS = {
    "magic_formula": ["ebit", "enterprise_value", "earnings_yield", "return_on_capital"],
    "piotroski": ["score", "tests_scored", "roa", "roa_1", *_PIOTROSKI_TESTS],
    "net_net": ["net_current_assets", "price_to_ncav"],
    "owner_earnings": [
        "cfo",
        "maintenance_capex",
        "owner_earnings",
        "enterprise_value",
        "owner_earnings_yield",
    ],
    "quality_at_price": ["years_counted", "min_roic", "free_cash_flow", "fcf_yield"],
}


def _reason(screen: str, row: sqlite3.Row, ranked: int) -> str:
    label = LABELS[screen]
    if screen == "magic_formula":
        return (
            f"{label} rank {row['rank']} of {ranked}: earnings yield "
            f"{row['earnings_yield']:.1%}, return on capital {row['return_on_capital']:.0%}"
        )
    if screen == "piotroski":
        return f"{label} {row['score']}/9, rank {row['rank']} of {ranked}"
    if screen == "net_net":
        return f"{label}: market cap {row['price_to_ncav']:.2f}x net current assets"
    if screen == "owner_earnings":
        return f"{label} {row['owner_earnings_yield']:.1%}, rank {row['rank']} of {ranked}"
    return (
        f"{label}: ROIC at least {row['min_roic']:.0%} in each of {QUALITY_YEARS} years, "
        f"FCF yield {row['fcf_yield']:.1%}"
    )


def _inputs(conn: sqlite3.Connection, cik: int, years: int) -> list[dict]:
    """The reported facts behind a candidate's ratios, each with its source filing."""
    fy_ends = [
        row["fy_end"]
        for row in conn.execute(
            "SELECT fy_end FROM annual WHERE cik = ? ORDER BY fy_end DESC LIMIT ?", (cik, years)
        )
    ]
    if not fy_ends:
        return []
    tags = [tag for tag, _ in ANNUAL_TAGS]
    rows = conn.execute(
        f"""
        SELECT tag, unit, period_start, period_end, value, filed_date, accession_no
        FROM fact_asof
        WHERE cik = ?
          AND period_end IN ({", ".join("?" for _ in fy_ends)})
          AND tag IN ({", ".join("?" for _ in tags)})
          AND (period_start = ''
               OR julianday(period_end) - julianday(period_start) BETWEEN {_LO} AND {_HI})
        ORDER BY period_end DESC, tag
        """,
        (cik, *fy_ends, *tags),
    )
    return [dict(row) for row in rows]


def _complete(conn: sqlite3.Connection, candidate: dict, universe: sqlite3.Row) -> dict:
    cik = candidate["cik"]
    price = conn.execute(
        "SELECT close, price_date, source FROM price_asof WHERE cik = ?", (cik,)
    ).fetchone()
    screens = {flag["screen"] for flag in candidate["flagged_by"]}
    years = QUALITY_YEARS if "quality_at_price" in screens else 3
    return {
        "cik": cik,
        "ticker": universe["ticker"],
        "name": universe["name"],
        "market_cap": universe["market_cap"],
        "fiscal_year_end": universe["latest_fy_end"],
        "price": dict(price) if price else None,
        "shares": {
            "value": universe["shares"],
            "tag": universe["shares_tag"],
            "period_end": universe["shares_date"],
            "filed_date": universe["shares_filed"],
            "accession_no": universe["shares_accession"],
        },
        "flagged_by": candidate["flagged_by"],
        "flag_reason": "; ".join(flag["reason"] for flag in candidate["flagged_by"]),
        "inputs": _inputs(conn, cik, years),
    }


#: What each screen needs from the latest fiscal year, and what to call it missing. A
#: filer is eligible for the universe but unrankable by a given screen when one of these
#: was not reported; that is a coverage gap, not an exclusion, and the two are different
#: facts about a run.
_SCREEN_REQUIRES: dict[str, list[tuple[str, str]]] = {
    "magic_formula": [
        ("ebit", "no operating income"),
        ("current_assets", "no current assets"),
        ("current_liabilities", "no current liabilities"),
        ("ppe", "no property, plant and equipment"),
    ],
    "net_net": [
        ("current_assets", "no current assets"),
        ("liabilities", "no total liabilities"),
    ],
    "owner_earnings": [
        ("cfo", "no operating cash flow"),
        ("capex", "no annual capital expenditure"),
    ],
    "quality_at_price": [
        ("ebit", "no operating income"),
        ("cfo", "no operating cash flow"),
        ("capex", "no annual capital expenditure"),
    ],
    # All nine tests must score, so any one missing figure costs the whole filer. Gross
    # profit is the usual culprit and was missing from this list when it was written,
    # which made the report understate the very gap it exists to surface.
    "piotroski": [
        ("cfo", "no operating cash flow"),
        ("net_income", "no net income"),
        ("assets", "no total assets"),
        ("gross_profit", "no gross profit"),
        ("revenue", "no revenue"),
        ("current_assets", "no current assets"),
        ("current_liabilities", "no current liabilities"),
        ("shares_weighted", "no share count"),
    ],
}


def _coverage(conn: sqlite3.Connection, screen: str) -> dict[str, int]:
    """How many eligible filers this screen could not rank, counted by what was missing.

    Counted against the first missing figure only, so the totals add up to filers rather
    than to absences: a filer with neither cash flow nor capex is one filer this screen
    could not see.
    """
    counts: Counter[str] = Counter()
    columns = _SCREEN_REQUIRES.get(screen, [])
    if not columns:
        return {}
    rows = conn.execute(
        f"SELECT {', '.join(column for column, _ in columns)} FROM screen_base"
    ).fetchall()
    for row in rows:
        for column, reason in columns:
            if row[column] is None:
                counts[reason] += 1
                break
    return dict(counts)


def _months_before(as_of: date, months: int) -> str:
    """The same day-of-month this many months earlier, clamped to a real date."""
    year, month = as_of.year, as_of.month - months
    while month <= 0:
        year, month = year - 1, month + 12
    day = min(as_of.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


def _snapshot(conn: sqlite3.Connection, as_of: str) -> dict[int, dict]:
    """What the screens said on one earlier date, per filer."""
    prepare(conn, as_of)
    state: dict[int, dict] = {}
    for row in universe_rows(conn):
        state[row["cik"]] = {
            "as_of": as_of,
            "eligible": row["excluded_because"] is None,
            "excluded_because": row["excluded_because"],
            "market_cap": row["market_cap"],
            "fiscal_year_end": row["latest_fy_end"],
            "flagged_by": [],
        }
    for screen in SCREENS:
        rows = screen_rows(conn, screen)
        ranked = sum(1 for row in rows if row["rank"] is not None)
        for row in rows:
            if row["flagged"] and row["cik"] in state:
                state[row["cik"]]["flagged_by"].append(
                    {"screen": screen, "rank": row["rank"], "ranked": ranked}
                )
    return state


def _trend(history: list[dict]) -> str | None:
    """How long this company has been screening well.

    A company that has been cheap and getting cheaper for two years is a different
    animal from one that fell into the screen this quarter, and that distinction routes
    to different questions downstream.
    """
    if not history:
        return None
    flagged = [bool(point["flagged_by"]) for point in history]
    if all(flagged):
        return "persistent"
    if not any(flagged):
        return "new"
    return "returning" if flagged[-1] else "recent"


def build_candidates(
    conn: sqlite3.Connection,
    as_of: date | str,
    limit: int = CANDIDATE_LIMIT,
    compare_months: tuple[int, ...] = (),
) -> dict:
    """Run all five screens as of a date and assemble the candidate list.

    A filer flagged by several screens is one candidate listing each. Candidates are
    ordered by how many screens flagged them, then by their best rank, and the screens
    are never blended into one score: which screen surfaced a company is the first
    thing the analysis layer needs to know.
    """
    # Earlier snapshots come first: every `prepare` rebuilds the TEMP tables, so the
    # primary date has to be the last one prepared.
    as_of_date = AsOfView(conn, as_of).as_of
    earlier = [
        _snapshot(conn, _months_before(as_of_date, months)) for months in sorted(compare_months)
    ]

    view = prepare(conn, as_of)
    universe = universe_rows(conn)
    excluded = Counter(row["excluded_because"] for row in universe if row["excluded_because"])

    found: dict[int, dict] = {}
    summary = {}
    for screen, description in SCREENS.items():
        rows = screen_rows(conn, screen)
        ranked = sum(1 for row in rows if row["rank"] is not None)
        flagged = [row for row in rows if row["flagged"]]
        summary[screen] = {
            "description": description,
            "eligible": len(universe) - sum(excluded.values()),
            "ranked": ranked,
            "flagged": len(flagged),
            # "Rank 3 of 198" reads like the whole universe when 289 filers were
            # eligible. A screen that cannot rank a third of them for want of one
            # reported figure should say so rather than leave the gap to be inferred.
            #
            # Data gaps only. The rest of the difference between `eligible` and `ranked`
            # is the screen's own definition — net-net skips a filer whose net current
            # assets are negative, quality-at-price one without seven years of returns —
            # and that is the screen working, not a hole in the data.
            "missing_data": _coverage(conn, screen),
        }
        for row in flagged:
            entry = found.setdefault(row["cik"], {"cik": row["cik"], "flagged_by": []})
            entry["flagged_by"].append(
                {
                    "screen": screen,
                    "label": LABELS[screen],
                    "rank": row["rank"],
                    "ranked": ranked,
                    "fiscal_year_end": row["fy_end"],
                    "metrics": {name: row[name] for name in _METRICS[screen]},
                    "reason": _reason(screen, row, ranked),
                }
            )

    ordered = sorted(
        found.values(),
        key=lambda c: (-len(c["flagged_by"]), min(f["rank"] for f in c["flagged_by"]), c["cik"]),
    )[:limit]
    by_cik = {row["cik"]: row for row in universe}
    candidates = [_complete(conn, c, by_cik[c["cik"]]) for c in ordered]
    for candidate in candidates:
        history = [point[candidate["cik"]] for point in earlier if candidate["cik"] in point]
        candidate["history"] = history
        candidate["trend"] = _trend(history)

    return {
        "as_of": view.as_of.isoformat(),
        "compared_with": [point[next(iter(point))]["as_of"] for point in earlier if point],
        "screener_version": SCREENER_VERSION,
        "universe": {
            "filers": len(universe),
            "eligible": len(universe) - sum(excluded.values()),
            "excluded": dict(excluded),
        },
        "screens": summary,
        "candidates": candidates,
    }


def store_candidates(conn: sqlite3.Connection, run: dict) -> int:
    """Replace the candidate rows for this run's date with this run's list."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM candidate WHERE as_of = ?", (run["as_of"],))
        conn.executemany(
            "INSERT INTO candidate (as_of, cik, screener_version, screens, flag_reason, "
            "payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run["as_of"],
                    c["cik"],
                    run["screener_version"],
                    ",".join(f["screen"] for f in c["flagged_by"]),
                    c["flag_reason"],
                    json.dumps(c),
                    now,
                )
                for c in run["candidates"]
            ],
        )
    return len(run["candidates"])
