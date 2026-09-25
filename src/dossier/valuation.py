"""The valuation engine: arithmetic in code, assumptions from the model.

The division of labour is the whole design. Historical figures come from the store,
exact and with the filing they were reported in. Revenue growth and the owner earnings
margin come from a model, as a bear/base/bull triple with a written justification each.
The discount rate, the terminal growth cap and the margin of safety are constants here,
because an input that can move them is an input that can talk a valuation into whatever
answer was wanted.

Owner earnings is the base measure, in the Buffett formulation: cash from operations
less the capital expenditure needed to stand still. Because operating cash flow is
already after interest and tax, discounting owner earnings gives the value of the
equity directly — net debt is not subtracted again.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from dossier.analysis import prompt_text
from dossier.figures import annual_rows, maintenance_capex, prepare_figures

#: One number, set once, applied to every filer. Letting it vary per company is how a
#: DCF becomes a machine for justifying whatever you already wanted to buy.
DISCOUNT_RATE = 0.10

#: Capped in code. Left free, a model routinely produces a terminal assumption that
#: implies the company eventually exceeds world GDP.
TERMINAL_GROWTH_CAP = 0.025

#: The buy threshold is a fixed discount to the *bear* case, applied last.
MARGIN_OF_SAFETY = 0.30

#: Years projected explicitly before the terminal value takes over.
PROJECTION_YEARS = 10

VALUATION_VERSION = "1"

#: Pinned like any other prompt, so a valuation can be traced to the words behind it.
VALUATION_PROMPT = "valuation_v1"

#: What a model may propose. Anything else — above all the discount rate — is fixed.
PROPOSABLE = ("revenue_growth", "owner_earnings_margin", "terminal_growth")

#: A justification that says only that someone was being careful. The plan names this
#: one by name; the others are the same sentence with a different adjective.
_VAGUE = re.compile(
    r"^\W*(a |an |the )?(very |fairly |quite )?"
    r"(conservative|prudent|reasonable|cautious|modest|sensible|realistic)"
    r"\s*(estimate|assumption|figure|number|view)?\W*$",
    re.I,
)

#: A justification earns its place by pointing at something checkable: a figure, a
#: percentage, a year, or the accession number of the filing it came from.
_CITES_SOMETHING = re.compile(r"\d")


@dataclass(frozen=True)
class Assumption:
    """One input a model proposes, as a triple, with its reasoning attached."""

    name: str
    bear: float
    base: float
    bull: float
    justification: str

    def __post_init__(self) -> None:
        if self.name not in PROPOSABLE:
            if self.name == "discount_rate":
                raise ValueError(
                    "the discount rate is not an assumption a model proposes: it is one "
                    f"number, {DISCOUNT_RATE:.0%}, applied to every filer alike. A rate "
                    "that varies per company turns a DCF into a machine for justifying "
                    "a conclusion already reached."
                )
            raise ValueError(f"unknown assumption {self.name!r}; expected one of {PROPOSABLE}")

        if not (self.bear <= self.base <= self.bull):
            raise ValueError(
                f"{self.name}: the bear case must not be better than the base case, nor "
                f"the base than the bull — got bear={self.bear}, base={self.base}, "
                f"bull={self.bull}. A triple ordered the other way is a labelling error, "
                "and it would put the margin of safety on the wrong end of the range."
            )

        text = self.justification.strip()
        if not text or _VAGUE.match(text) or not _CITES_SOMETHING.search(text):
            raise ValueError(
                f"{self.name}: a justification must reference a specific finding or "
                f"filing figure, not describe the estimator — got {self.justification!r}. "
                "The point of the sentence is that a reader can check it."
            )

    def scenario(self, case: str) -> float:
        return getattr(self, case)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "bear": self.bear,
            "base": self.base,
            "bull": self.bull,
            "justification": self.justification,
        }


@dataclass
class Inputs:
    """What the store supplies: exact figures, with the filings behind them."""

    cik: int
    as_of: str
    revenue: float
    owner_earnings: float
    shares: float
    price: float | None = None
    history: list[dict] = field(default_factory=list)

    @property
    def market_cap(self) -> float | None:
        return None if self.price is None else self.price * self.shares

    @property
    def owner_earnings_margin(self) -> float | None:
        if not self.revenue:
            return None
        return self.owner_earnings / self.revenue

    def to_dict(self) -> dict:
        return {
            "cik": self.cik,
            "as_of": self.as_of,
            "revenue": self.revenue,
            "owner_earnings": self.owner_earnings,
            "owner_earnings_margin": self.owner_earnings_margin,
            "shares": self.shares,
            "price": self.price,
            "market_cap": self.market_cap,
            "history": self.history,
        }


def scenario_value(
    revenue: float,
    growth: float,
    margin: float,
    terminal_growth: float = TERMINAL_GROWTH_CAP,
    discount_rate: float = DISCOUNT_RATE,
    years: int = PROJECTION_YEARS,
) -> float:
    """Present value of owner earnings on one set of assumptions.

    Revenue grows at `growth` for `years`; owner earnings are `margin` of it. The
    terminal value takes over after that, at a growth rate capped in code.
    """
    terminal_growth = min(terminal_growth, TERMINAL_GROWTH_CAP)
    if terminal_growth >= discount_rate:
        # A terminal rate at or above the discount rate values a finite company at
        # infinity. The cap makes this unreachable; the guard keeps it that way.
        raise ValueError("terminal growth must stay below the discount rate")

    total = 0.0
    projected_revenue = revenue
    for year in range(1, years + 1):
        projected_revenue *= 1 + growth
        total += (projected_revenue * margin) / (1 + discount_rate) ** year

    final = projected_revenue * margin
    terminal = final * (1 + terminal_growth) / (discount_rate - terminal_growth)
    return total + terminal / (1 + discount_rate) ** years


def implied_growth(
    market_cap: float,
    revenue: float,
    margin: float,
    terminal_growth: float = TERMINAL_GROWTH_CAP,
    discount_rate: float = DISCOUNT_RATE,
    years: int = PROJECTION_YEARS,
) -> float | None:
    """The revenue growth today's price already assumes, on the given margin.

    Often the more useful number: it reframes the question from what the company is
    worth to what the market believes, and whether you disagree.
    """
    if market_cap <= 0 or revenue <= 0 or margin <= 0:
        return None

    lo, hi = -0.50, 0.50

    def gap(growth: float) -> float:
        return (
            scenario_value(revenue, growth, margin, terminal_growth, discount_rate, years)
            - market_cap
        )

    if gap(lo) > 0 or gap(hi) < 0:
        # The price implies something outside a range any company sustains for a decade.
        # Saying so is more honest than extrapolating to an answer nobody should use.
        return None

    for _ in range(200):
        mid = (lo + hi) / 2
        if gap(mid) > 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def value(inputs: Inputs, assumptions: list[Assumption]) -> dict:
    """Bear, base and bull, each with the assumptions that produced it."""
    by_name = {a.name: a for a in assumptions}
    for required in ("revenue_growth", "owner_earnings_margin"):
        if required not in by_name:
            raise ValueError(
                f"missing the {required} assumption. Every scenario needs both, as a "
                "triple: a valuation built on one lever hides which one is carrying it."
            )

    terminal = by_name.get("terminal_growth")
    cases = {}
    for case in ("bear", "base", "bull"):
        growth = by_name["revenue_growth"].scenario(case)
        margin = by_name["owner_earnings_margin"].scenario(case)
        terminal_growth = terminal.scenario(case) if terminal is not None else TERMINAL_GROWTH_CAP
        equity = scenario_value(inputs.revenue, growth, margin, terminal_growth)
        cases[case] = {
            "equity_value": equity,
            "per_share": equity / inputs.shares if inputs.shares else None,
            "revenue_growth": growth,
            "owner_earnings_margin": margin,
            "terminal_growth": min(terminal_growth, TERMINAL_GROWTH_CAP),
        }

    bear_per_share = cases["bear"]["per_share"]
    market_cap = inputs.market_cap
    implied = {
        "price": inputs.price,
        "market_cap": market_cap,
        "margin_used": by_name["owner_earnings_margin"].base,
        "growth": (
            None
            if market_cap is None
            else implied_growth(market_cap, inputs.revenue, by_name["owner_earnings_margin"].base)
        ),
    }

    return {
        "cik": inputs.cik,
        "as_of": inputs.as_of,
        "valuation_version": VALUATION_VERSION,
        "discount_rate": DISCOUNT_RATE,
        "terminal_growth_cap": TERMINAL_GROWTH_CAP,
        "projection_years": PROJECTION_YEARS,
        "margin_of_safety": MARGIN_OF_SAFETY,
        "inputs": inputs.to_dict(),
        "assumptions": [a.to_dict() for a in assumptions],
        "bear": cases["bear"],
        "base": cases["base"],
        "bull": cases["bull"],
        "implied": implied,
        # Applied last and mechanically, to the bear case. A margin of safety off the
        # base case is the base case with a smaller number written next to it.
        "buy_below": None if bear_per_share is None else bear_per_share * (1 - MARGIN_OF_SAFETY),
    }


# -- the two halves: what the store hands over, and what comes back -------------------


def _history(conn: sqlite3.Connection, cik: int) -> list[dict]:
    """The filer's annual figures, newest first, each with the filing it came from.

    The plan's rule for the analysis layer applies here too: a valuation should be
    reproducible from its own input file, so every figure travels with its source.
    """
    sources = {
        row["period_end"]: row
        for row in conn.execute(
            "SELECT period_end, accession_no, filed_date FROM ("
            "  SELECT period_end, accession_no, filed_date, ROW_NUMBER() OVER ("
            "    PARTITION BY period_end ORDER BY filed_date, accession_no"
            "  ) AS first_report"
            "  FROM fact_asof WHERE cik = ? AND period_start <> ''"
            ") WHERE first_report = 1",
            (cik,),
        ).fetchall()
    }
    rows = []
    for row in reversed(annual_rows(conn, cik)):
        entry = dict(row)
        source = sources.get(entry["fy_end"])
        entry["accession_no"] = source["accession_no"] if source else None
        entry["filed_date"] = source["filed_date"] if source else None
        rows.append(entry)
    return rows


def historical_inputs(conn: sqlite3.Connection, cik: int, as_of: date | str) -> Inputs:
    """Everything the store knows about this filer as of a date, exactly."""
    row = conn.execute("SELECT * FROM screen_base WHERE cik = ?", (cik,)).fetchone()
    if row is None:
        raise ValueError(
            f"CIK {cik} is not in the eligible universe as of {as_of}. The screens "
            "exclude filers for reasons that apply just as much to a valuation — no "
            "recent 10-K, a financial-sector balance sheet, too little history, no "
            "price. `dossier screen --json` reports which one."
        )

    history = _history(conn, cik)
    capex = maintenance_capex(history)
    owner_earnings = None
    if row["cfo"] is not None and capex is not None:
        owner_earnings = float(row["cfo"]) - capex["used"]
    if owner_earnings is None:
        raise ValueError(
            f"CIK {cik} has no owner earnings as of {as_of}: operating cash flow or "
            "capital expenditure is missing. Assuming either one would value a company "
            "on figures it never reported."
        )

    return Inputs(
        cik=cik,
        as_of=str(as_of),
        revenue=float(row["revenue"]),
        owner_earnings=owner_earnings,
        shares=float(row["shares"]),
        price=None if row["price"] is None else float(row["price"]),
        history=history,
    )


def prepare_valuation(conn: sqlite3.Connection, cik: int, as_of: date | str) -> dict:
    """Write the valuation's input: the figures, the findings, and the fixed rules."""
    prepare_figures(conn, as_of, ciks=[cik])
    inputs = historical_inputs(conn, cik, as_of)
    findings = [
        dict(row)
        for row in conn.execute(
            "SELECT accession_no, item, change_type, quote, implication, severity, "
            "prompt_version FROM finding WHERE cik = ? ORDER BY "
            "CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, item",
            (cik,),
        ).fetchall()
    ]
    return {
        "cik": cik,
        "as_of": str(as_of),
        "valuation_version": VALUATION_VERSION,
        "prompt_version": VALUATION_PROMPT,
        "instructions": prompt_text(VALUATION_PROMPT),
        "inputs": inputs.to_dict(),
        "maintenance_capex": maintenance_capex(inputs.history),
        "findings": findings,
        # Stated in the input so the model can see what it cannot change, rather than
        # discovering it when the validator refuses an assumption.
        "fixed": {
            "discount_rate": DISCOUNT_RATE,
            "terminal_growth_cap": TERMINAL_GROWTH_CAP,
            "margin_of_safety": MARGIN_OF_SAFETY,
            "projection_years": PROJECTION_YEARS,
        },
    }


def _assumptions_from(payload: dict) -> list[Assumption]:
    proposed = payload.get("valuation") or {}
    if not proposed:
        raise ValueError(
            "no assumptions in the payload: expected a 'valuation' object with a triple "
            "and a justification for each input."
        )
    assumptions = []
    for name, fields in proposed.items():
        if not isinstance(fields, dict):
            raise ValueError(f"{name}: expected an object with bear, base, bull and justification")
        missing = {"bear", "base", "bull", "justification"} - set(fields)
        if missing:
            raise ValueError(f"{name}: missing {', '.join(sorted(missing))}")
        assumptions.append(
            Assumption(
                name=name,
                bear=float(fields["bear"]),
                base=float(fields["base"]),
                bull=float(fields["bull"]),
                justification=str(fields["justification"]),
            )
        )
    return assumptions


def load_valuation(conn: sqlite3.Connection, cik: int, as_of: date | str, payload: dict) -> dict:
    """Validate the proposed assumptions, do the arithmetic, and store the result.

    Nothing is written until every assumption has passed. A valuation stored with one
    unjustified input is worse than no valuation: it looks exactly like a good one.
    """
    assumptions = _assumptions_from(payload)
    prepare_figures(conn, as_of, ciks=[cik])
    inputs = historical_inputs(conn, cik, as_of)
    result = value(inputs, assumptions)
    result["prompt_version"] = VALUATION_PROMPT
    result["model"] = payload.get("model")
    result["commentary"] = payload.get("commentary", "")

    with conn:
        conn.execute(
            "DELETE FROM valuation WHERE cik = ? AND as_of = ? AND valuation_version = ?",
            (cik, str(as_of), VALUATION_VERSION),
        )
        conn.execute(
            "INSERT INTO valuation (cik, as_of, valuation_version, model, payload, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                cik,
                str(as_of),
                VALUATION_VERSION,
                payload.get("model"),
                json.dumps(result),
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
    return result


def stored_valuation(conn: sqlite3.Connection, cik: int, as_of: date | str) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM valuation WHERE cik = ? AND as_of = ? AND valuation_version = ?",
        (cik, str(as_of), VALUATION_VERSION),
    ).fetchone()
    return None if row is None else json.loads(row["payload"])
