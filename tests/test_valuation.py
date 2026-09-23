"""Milestone 8: the valuation engine.

The arithmetic is Python. The model's only job is to propose assumptions and defend
them, so everything a model supplies arrives as a triple with a written justification,
and everything that must not be negotiable — the terminal growth cap, the discount rate,
the margin of safety — is a constant in code that no input can move.
"""

import pytest

from dossier.valuation import (
    DISCOUNT_RATE,
    MARGIN_OF_SAFETY,
    TERMINAL_GROWTH_CAP,
    Assumption,
    Inputs,
    implied_growth,
    maintenance_capex,
    scenario_value,
    value,
)


def history(years=7, capex=100.0, revenue=1000.0, depreciation=120.0):
    """A filer's annual history, newest first, as `annual_rows` hands it over."""
    return [
        {
            "fy_end": f"20{24 - n}-12-31",
            "revenue": revenue,
            "capex": capex,
            "depreciation": depreciation,
            "cfo": 200.0,
            "net_income": 90.0,
            "accession_no": f"0000000000-2{4 - n}-000001",
            "filed_date": f"20{25 - n}-02-14",
        }
        for n in range(years)
    ]


def inputs(**overrides) -> Inputs:
    payload = dict(
        cik=57131,
        as_of="2026-09-22",
        revenue=1000.0,
        owner_earnings=100.0,
        shares=40.0,
        price=25.0,
        history=history(),
    )
    payload.update(overrides)
    return Inputs(**payload)


def assumptions(**overrides) -> list[Assumption]:
    payload = {
        "revenue_growth": Assumption(
            name="revenue_growth",
            bear=-0.02,
            base=0.02,
            bull=0.05,
            justification=(
                "Consolidated sales rose 0.8% in fiscal 2026 to $2,126,635 thousand "
                "(0000057131-26-000019)."
            ),
        ),
        "owner_earnings_margin": Assumption(
            name="owner_earnings_margin",
            bear=0.06,
            base=0.08,
            bull=0.10,
            justification=(
                "Owner earnings were 8% of revenue in fiscal 2026, and $11.5 million of "
                "that sits in one-off gains on sale-leasebacks."
            ),
        ),
    }
    payload.update(overrides)
    return list(payload.values())


class TestMaintenanceCapex:
    """The one genuinely hard input, since filers do not report it. Both estimates are
    exposed and the spread is shown rather than hidden inside a single number."""

    def test_exposes_both_estimates_and_the_spread(self):
        estimate = maintenance_capex(history(capex=100.0, revenue=1000.0))
        assert estimate["total_capex"] == 100.0
        assert estimate["revenue_scaled"] == pytest.approx(100.0)
        assert "spread" in estimate

    def test_uses_the_lesser_of_the_two(self):
        """Capex above the historical revenue-scaled rate is growth spending, not
        maintenance: a company that doubled its capex has not doubled what it must spend
        to stand still."""
        rows = history(capex=100.0)
        rows[0] = dict(rows[0], capex=300.0)
        estimate = maintenance_capex(rows)
        assert estimate["total_capex"] == 300.0
        assert estimate["revenue_scaled"] < 300.0
        assert estimate["used"] == estimate["revenue_scaled"]

    def test_a_filer_with_no_capex_history_gets_no_estimate(self):
        """Assuming a company spends nothing to stand still is the most flattering
        possible error."""
        assert maintenance_capex([]) is None


class TestTheConstraintsLiveInCode:
    def test_terminal_growth_is_capped_however_high_the_assumption(self):
        """Left free, a terminal growth rate implies the company eventually exceeds
        world GDP."""
        capped = scenario_value(revenue=1000.0, growth=0.03, margin=0.10, terminal_growth=0.09)
        at_cap = scenario_value(
            revenue=1000.0, growth=0.03, margin=0.10, terminal_growth=TERMINAL_GROWTH_CAP
        )
        assert capped == at_cap

    def test_an_assumption_cannot_be_a_point_estimate(self):
        with pytest.raises(TypeError):
            Assumption(name="revenue_growth", base=0.02, justification="x")

    def test_the_discount_rate_is_not_something_a_model_proposes(self):
        """Letting it vary per company is how a DCF becomes a machine for justifying
        whatever you already wanted to buy."""
        with pytest.raises(ValueError, match="discount rate"):
            Assumption(
                name="discount_rate",
                bear=0.12,
                base=0.10,
                bull=0.08,
                justification="Ten percent is the rate used for every filer.",
            )

    def test_bear_must_not_be_better_than_bull(self):
        with pytest.raises(ValueError, match="bear"):
            Assumption(
                name="revenue_growth",
                bear=0.10,
                base=0.02,
                bull=-0.02,
                justification="Sales rose 0.8% in fiscal 2026 to $2,126,635 thousand.",
            )


class TestJustifications:
    """Every assumption carries a one-sentence justification referencing a specific
    finding or filing figure. 'Conservative estimate' is rejected by the validator."""

    def test_a_vague_justification_is_rejected(self):
        with pytest.raises(ValueError, match="justification"):
            Assumption(
                name="revenue_growth",
                bear=-0.02,
                base=0.02,
                bull=0.05,
                justification="A conservative estimate.",
            )

    def test_a_justification_citing_a_figure_is_accepted(self):
        assumption = Assumption(
            name="revenue_growth",
            bear=-0.02,
            base=0.02,
            bull=0.05,
            justification="Written same-store sales fell 3% in fiscal 2026.",
        )
        assert assumption.base == 0.02

    def test_a_justification_citing_an_accession_is_accepted(self):
        assumption = Assumption(
            name="owner_earnings_margin",
            bear=0.06,
            base=0.08,
            bull=0.10,
            justification="The warranty reserve release is in 0000057131-26-000019.",
        )
        assert assumption.bull == 0.10

    def test_an_empty_justification_is_rejected(self):
        with pytest.raises(ValueError, match="justification"):
            Assumption(name="revenue_growth", bear=-0.02, base=0.02, bull=0.05, justification="  ")


class TestTheValuation:
    def test_the_result_is_a_range_not_a_point(self):
        result = value(inputs(), assumptions())
        assert result["bear"]["per_share"] < result["base"]["per_share"]
        assert result["base"]["per_share"] < result["bull"]["per_share"]

    def test_the_margin_of_safety_is_a_discount_to_the_bear_case(self):
        """Applied last and mechanically. A margin of safety off the base case is not a
        margin of safety; it is the base case with a smaller number written next to it."""
        result = value(inputs(), assumptions())
        assert result["buy_below"] == pytest.approx(
            result["bear"]["per_share"] * (1 - MARGIN_OF_SAFETY)
        )

    def test_it_records_the_discount_rate_it_used(self):
        assert value(inputs(), assumptions())["discount_rate"] == DISCOUNT_RATE

    def test_it_reports_what_the_current_price_already_assumes(self):
        """Often more informative than your own estimate: it reframes the question from
        what is it worth to what does the market believe, and do I disagree?"""
        result = value(inputs(), assumptions())
        assert result["implied"]["growth"] is not None
        assert result["implied"]["price"] == 25.0

    def test_implied_growth_recovers_the_growth_that_produced_a_price(self):
        market_cap = scenario_value(revenue=1000.0, growth=0.03, margin=0.08)
        recovered = implied_growth(market_cap, revenue=1000.0, margin=0.08)
        assert recovered == pytest.approx(0.03, abs=1e-3)

    def test_every_assumption_reaches_the_output_with_its_justification(self):
        result = value(inputs(), assumptions())
        names = {a["name"] for a in result["assumptions"]}
        assert names == {"revenue_growth", "owner_earnings_margin"}
        assert all(a["justification"] for a in result["assumptions"])

    def test_a_filer_with_no_price_still_values_but_implies_nothing(self):
        result = value(inputs(price=None), assumptions())
        assert result["base"]["per_share"] > 0
        assert result["implied"]["growth"] is None
