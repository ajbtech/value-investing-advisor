"""The deterministic screener.

The screener is SQL. No model runs here, because the job is arithmetic and the cost of a
hallucinated ratio is a wasted analysis pass. The screens rank filers against each other
over the per-filer figures `dossier.figures` builds, which read only the TEMP tables
`AsOfView.materialise` writes, so the screens cannot see anything filed after the as-of
date: the gateway is still the only code that reads `fact` or `price`.

Ranking needs the whole universe, so `prepare` always builds figures for every filer.
A caller that needs one filer's figures uses `dossier.figures` directly.
"""

from __future__ import annotations

import calendar
import json
import sqlite3
from collections import Counter
from datetime import UTC, date, datetime

from dossier.asof import AsOfView
from dossier.figures import (
    ANNUAL_TAGS,
    FIGURE_TABLES,
    FISCAL_YEAR_DAYS,
    prepare_figures,
    universe_rows,
)

_LO, _HI = FISCAL_YEAR_DAYS

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
# is needed too. Four deliberate choices, all documented here because a reader will
# trip over them:
#
# - A filer with no long-term debt in either year passes the leverage test. Strictly,
#   leverage has to *fall*; a debt-free company cannot, and failing it for that would
#   penalise the least-levered balance sheets in the universe.
# - A filer is ranked only if all nine tests could be computed. Six passes out of six
#   computable tests is not comparable to six out of nine, so an incomplete score is
#   reported, never ranked.
# - Return on assets, and its change, read *operating* income, not net income. A Pass A
#   run over 18 flagged filers found 8 whose improvement the filing attributes to a
#   one-off, and most of those sit below the operating line: a legal settlement, a lapped
#   loss on a business sale, discontinued operations. Piotroski's accrual test still
#   compares cash flow with net income, because a non-cash gain in net income is exactly
#   what it exists to catch. There is no fallback to net income for a filer that tags no
#   operating income: that would readmit the one-offs, so the filer goes unranked.
# - Items the filer tags as unusual (impairments, restructuring, gains on disposals,
#   debt extinguishment, litigation, discontinued operations) are reported with the
#   score, not adjusted out of it: where they sit relative to the operating line varies
#   by filer, and adjusting blind would double-count as often as it corrected.
PIOTROSKI_SQL = f"""
CREATE TEMP VIEW piotroski AS
WITH seq AS (
  SELECT a.*,
    LAG(fy_end) OVER w AS fy_end_1,
    LAG(fy_end, 2) OVER w AS fy_end_2,
    LAG(ebit) OVER w AS ebit_1,
    LAG(unusual_effect) OVER w AS unusual_effect_1,
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
  SELECT *,
    ebit / assets_1 AS operating_roa, ebit_1 / assets_2 AS operating_roa_1,
    net_income / assets_1 AS roa, net_income_1 / assets_2 AS roa_1,
    CASE WHEN unusual_effect IS NULL AND unusual_effect_1 IS NULL THEN NULL
         ELSE COALESCE(unusual_effect, 0) - COALESCE(unusual_effect_1, 0)
    END AS unusual_change
  FROM seq
  WHERE recency = 1
    AND julianday(fy_end) - julianday(fy_end_1) BETWEEN {_LO} AND {_HI}
    AND julianday(fy_end_1) - julianday(fy_end_2) BETWEEN {_LO} AND {_HI}
),
tested AS (
  SELECT l.*,
    CASE WHEN operating_roa IS NULL THEN NULL WHEN operating_roa > 0 THEN 1 ELSE 0 END
      AS f_roa,
    CASE WHEN cfo IS NULL THEN NULL WHEN cfo > 0 THEN 1 ELSE 0 END AS f_cfo,
    CASE WHEN operating_roa IS NULL OR operating_roa_1 IS NULL THEN NULL
         WHEN operating_roa > operating_roa_1 THEN 1 ELSE 0 END AS f_delta_roa,
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
       THEN RANK() OVER (PARTITION BY tests_scored = 9 ORDER BY score DESC, operating_roa DESC)
  END
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

# SQLite refuses DROP VIEW on a table, so a name in both lists breaks the second
# `prepare` on a connection — which is exactly what running the screens at several
# dates does.
assert not set(_VIEWS) & set(FIGURE_TABLES), "a screen object is either a view or a table"


def prepare(conn: sqlite3.Connection, as_of: date | str) -> AsOfView:
    """Build every filer's figures as of a date, then the screen views that rank them."""
    with conn:
        for name in _VIEWS:
            conn.execute(f"DROP VIEW IF EXISTS temp.{name}")
    view = prepare_figures(conn, as_of)
    with conn:
        for sql in (PIOTROSKI_SQL, MAGIC_FORMULA_SQL, NET_NET_SQL, OWNER_EARNINGS_SQL, QUALITY_SQL):
            conn.execute(sql)
    return view


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
#: 8: Piotroski's return-on-assets tests read operating income, and each Piotroski row
#: carries the filer's tagged unusual items and their change.
SCREENER_VERSION = "8"

SCREEN_JOB = "screen"

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
    "piotroski": [
        "score",
        "tests_scored",
        "operating_roa",
        "operating_roa_1",
        "roa",
        "roa_1",
        "unusual_effect",
        "unusual_effect_1",
        "unusual_change",
        *_PIOTROSKI_TESTS,
    ],
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
        reason = f"{label} {row['score']}/9, rank {row['rank']} of {ranked}"
        change = row["unusual_change"]
        if change:
            # A reader should not have to find this in the metrics: it is the first
            # question the analysis pass asks of a Piotroski flag.
            direction = "added" if change > 0 else "took"
            reason += f"; tagged unusual items {direction} ${abs(change) / 1e6:,.1f}M year on year"
        return reason
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
        ("ebit", "no operating income"),
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
