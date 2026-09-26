"""The per-filer figures: what each filer reported, as it could be known on a date.

Every table here is computed from one filer's own facts, price and filings, so it can
be built for just the filers a caller needs. The screens rank filers against each
other and need everyone; a valuation or a re-check of one company does not, and should
not pay for the whole store.

`prepare_figures` builds, in order:

- `annual_raw`: one row per filer per fiscal year, one column per tag.
- `annual`: the figures the screens use, with the fallbacks filers force on us
  (revenue under four different tags, gross profit reported or derived, and so on).
- `shares_latest`: the most recent share count known by the as-of date.
- `universe_screened`: every filer in the as-of universe with its market cap and, if
  it cannot be screened, the first filter it failed.
- `screen_base`: the latest fiscal year of every eligible filer, with enterprise value.

Every one reads only the TEMP tables `AsOfView.materialise` writes, so the gateway is
still the only code that reads `fact` or `price`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date
from statistics import mean

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
    ("CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization", "USD"),
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
    # Unusual items, reported beside a Piotroski flag rather than adjusted out of it.
    ("AssetImpairmentCharges", "USD"),
    ("GoodwillAndIntangibleAssetImpairment", "USD"),
    ("GoodwillImpairmentLoss", "USD"),
    ("ImpairmentOfIntangibleAssetsExcludingGoodwill", "USD"),
    ("ImpairmentOfIntangibleAssetsIndefinitelivedExcludingGoodwill", "USD"),
    ("ImpairmentOfLongLivedAssetsHeldForUse", "USD"),
    ("ImpairmentOfLongLivedAssetsToBeDisposedOf", "USD"),
    ("OperatingLeaseImpairmentLoss", "USD"),
    ("RestructuringCharges", "USD"),
    ("RestructuringCosts", "USD"),
    ("GainLossOnDispositionOfAssets1", "USD"),
    ("GainLossOnDispositionOfAssets", "USD"),
    ("GainLossOnSaleOfPropertyPlantEquipment", "USD"),
    ("GainLossOnSaleOfProperties", "USD"),
    ("GainLossOnSaleOfBusiness", "USD"),
    ("DisposalGroupNotDiscontinuedOperationGainLossOnDisposal", "USD"),
    ("GainsLossesOnExtinguishmentOfDebt", "USD"),
    ("GainLossRelatedToLitigationSettlement", "USD"),
    ("IncomeLossFromDiscontinuedOperationsNetOfTaxAttributableToReportingEntity", "USD"),
    ("IncomeLossFromDiscontinuedOperationsNetOfTax", "USD"),
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
    '"CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization")'
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


def _sum_or_null(*expressions: str) -> str:
    """The sum of whichever expressions are reported, or NULL when none is."""
    absent = " AND ".join(f"({e}) IS NULL" for e in expressions)
    total = " + ".join(f"COALESCE({e}, 0)" for e in expressions)
    return f"CASE WHEN {absent} THEN NULL ELSE {total} END"


# Impairment elements nest: `AssetImpairmentCharges` can be the total of everything
# below it, `GoodwillAndIntangibleAssetImpairment` the total of goodwill and intangibles.
# At each level the aggregate wins over its parts, so no dollar counts twice.
_INTANGIBLE_IMPAIRMENT = (
    'COALESCE("ImpairmentOfIntangibleAssetsExcludingGoodwill", '
    '"ImpairmentOfIntangibleAssetsIndefinitelivedExcludingGoodwill")'
)
_GOODWILL_AND_INTANGIBLE_IMPAIRMENT = (
    'COALESCE("GoodwillAndIntangibleAssetImpairment", '
    + _sum_or_null('"GoodwillImpairmentLoss"', _INTANGIBLE_IMPAIRMENT)
    + ")"
)
_IMPAIRMENT = (
    'COALESCE("AssetImpairmentCharges", '
    + _sum_or_null(
        _GOODWILL_AND_INTANGIBLE_IMPAIRMENT,
        '"ImpairmentOfLongLivedAssetsHeldForUse"',
        '"ImpairmentOfLongLivedAssetsToBeDisposedOf"',
        '"OperatingLeaseImpairmentLoss"',
    )
    + ")"
)


#: Items the filer itself tags as unusual, each an expression and its sign on income.
#: Charges are reported positive and reduce income; `GainLoss` elements are positive for
#: a gain. Within a kind, the first reported element wins, so an aggregate and its parts
#: never count the same dollars twice.
_UNUSUAL_ITEMS = [
    (_IMPAIRMENT, -1),
    ('COALESCE("RestructuringCharges", "RestructuringCosts")', -1),
    (
        'COALESCE("GainLossOnDispositionOfAssets1", "GainLossOnDispositionOfAssets", '
        '"GainLossOnSaleOfPropertyPlantEquipment", "GainLossOnSaleOfProperties")',
        1,
    ),
    (
        'COALESCE("GainLossOnSaleOfBusiness", '
        '"DisposalGroupNotDiscontinuedOperationGainLossOnDisposal")',
        1,
    ),
    ('"GainsLossesOnExtinguishmentOfDebt"', 1),
    ('"GainLossRelatedToLitigationSettlement"', 1),
    (
        'COALESCE("IncomeLossFromDiscontinuedOperationsNetOfTaxAttributableToReportingEntity", '
        '"IncomeLossFromDiscontinuedOperationsNetOfTax")',
        1,
    ),
]
#: Their net effect on income, NULL when the filer tagged none: an absent tag is not
#: evidence that nothing unusual happened, only that nothing was tagged.
_UNUSUAL_EFFECT = (
    "CASE WHEN "
    + " AND ".join(f"({expr}) IS NULL" for expr, _ in _UNUSUAL_ITEMS)
    + " THEN NULL ELSE "
    + " + ".join(f"{'-' if sign < 0 else ''}COALESCE({expr}, 0)" for expr, sign in _UNUSUAL_ITEMS)
    + " END"
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
  {_UNUSUAL_EFFECT} AS unusual_effect,
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

#: Materialised by `prepare_figures`, in dependency order.
FIGURE_TABLES = ["screen_base", "universe_screened", "shares_latest", "annual", "annual_raw"]


def prepare_figures(
    conn: sqlite3.Connection, as_of: date | str, ciks: Iterable[int] | None = None
) -> AsOfView:
    """Materialise the as-of state and the per-filer figures, for `ciks` or everyone.

    Scoping narrows *who* is read, never *when*: the as-of filter applies either way.
    """
    view = AsOfView(conn, as_of)
    view.materialise(ciks=ciks)
    # A preferred issue's price is not the company's share price, and a market cap built
    # from one is wrong by a multiple nobody can see. Registered here so the universe SQL
    # can ask, rather than approximating the rule in SQL and having two of them.
    conn.create_function("is_common_stock", 1, is_common_stock, deterministic=True)
    with conn:
        for name in FIGURE_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS temp.{name}")
        for sql in (_annual_raw_sql(), ANNUAL_SQL, SHARES_SQL, UNIVERSE_SQL, SCREEN_BASE_SQL):
            conn.execute(sql)
        conn.execute("CREATE INDEX temp.annual_by_filer ON annual (cik, fy_end)")
        conn.execute("CREATE INDEX temp.universe_by_filer ON universe_screened (cik)")
        conn.execute("CREATE INDEX temp.screen_base_by_filer ON screen_base (cik)")
    return view


def annual_rows(conn: sqlite3.Connection, cik: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM annual WHERE cik = ? ORDER BY fy_end", (cik,)).fetchall()


def universe_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM universe_screened ORDER BY cik").fetchall()


#: Years of capex history behind the revenue-scaled maintenance estimate.
MAINTENANCE_CAPEX_YEARS = 7


def maintenance_capex(rows: list[dict]) -> dict | None:
    """Two estimates of what a company must spend to stand still, and their spread.

    Filers do not report maintenance capex. Total capex is an upper bound that counts
    growth spending as maintenance; the revenue-scaled historical average is what this
    company has typically spent per dollar of revenue. The lesser is used, and both are
    kept: a wide spread means the estimate is doing a lot of work and the reader should
    see that rather than a single confident number.
    """
    usable = [r for r in rows if r.get("capex") is not None][:MAINTENANCE_CAPEX_YEARS]
    if not usable:
        return None

    total_capex = float(usable[0]["capex"])
    rates = [float(r["capex"]) / float(r["revenue"]) for r in usable if r.get("revenue")]
    latest_revenue = usable[0].get("revenue")
    revenue_scaled = mean(rates) * float(latest_revenue) if rates and latest_revenue else None

    used = total_capex if revenue_scaled is None else min(total_capex, revenue_scaled)
    spread = None if revenue_scaled is None else abs(total_capex - revenue_scaled)
    return {
        "total_capex": total_capex,
        "revenue_scaled": revenue_scaled,
        "used": used,
        "spread": spread,
        "years": len(usable),
    }
