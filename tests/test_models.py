"""The model layer: provider-agnostic, with cost controls in front of it.

Users bring their own key, so the spend is theirs — which makes showing a projected
cost before every batch run an obligation rather than a nicety. Nothing here calls a
real model; the client is an interface, and the tests drive a fake through it.
"""

import pytest

from dossier.models import (
    DEFAULT_CEILING_USD,
    BudgetExceeded,
    CostEstimate,
    ModelResponse,
    UnknownModel,
    estimate_cost,
    price_of,
)


class TestPricing:
    def test_knows_the_current_models(self):
        for model in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
            price = price_of(model)
            assert price.input_per_mtok > 0
            assert price.output_per_mtok > price.input_per_mtok

    def test_refuses_a_model_it_has_no_price_for(self):
        """Silently assuming a price would produce a confident estimate that is wrong,
        which is worse than refusing — the user acts on the number."""
        with pytest.raises(UnknownModel):
            price_of("some-model-that-does-not-exist")

    def test_the_error_names_the_models_it_does_know(self):
        with pytest.raises(UnknownModel) as excinfo:
            price_of("gpt-9")
        assert "claude-opus-5" in str(excinfo.value)


class TestCostEstimate:
    def test_multiplies_tokens_by_the_per_million_rate(self):
        estimate = estimate_cost("claude-opus-5", input_tokens=1_000_000, output_tokens=0)
        assert estimate.usd == pytest.approx(price_of("claude-opus-5").input_per_mtok)

    def test_counts_input_and_output_separately(self):
        price = price_of("claude-opus-5")
        estimate = estimate_cost("claude-opus-5", input_tokens=1_000_000, output_tokens=1_000_000)
        assert estimate.usd == pytest.approx(price.input_per_mtok + price.output_per_mtok)

    def test_cached_input_is_cheaper_than_fresh_input(self):
        """Pass A sends the same two filings to several prompts. If caching did not
        change the estimate there would be no reason to bother with it."""
        fresh = estimate_cost("claude-opus-5", input_tokens=500_000, output_tokens=0)
        cached = estimate_cost(
            "claude-opus-5", input_tokens=0, cached_input_tokens=500_000, output_tokens=0
        )
        assert cached.usd < fresh.usd

    def test_scales_over_a_batch(self):
        one = estimate_cost("claude-opus-5", input_tokens=10_000, output_tokens=1_000)
        thirty = estimate_cost("claude-opus-5", input_tokens=10_000, output_tokens=1_000, calls=30)
        assert thirty.usd == pytest.approx(one.usd * 30)
        assert thirty.calls == 30

    def test_reports_the_model_and_tokens_it_priced(self):
        """So a surprising number can be checked rather than merely disbelieved."""
        estimate = estimate_cost("claude-opus-5", input_tokens=10_000, output_tokens=500)
        assert estimate.model == "claude-opus-5"
        assert estimate.input_tokens == 10_000
        assert estimate.output_tokens == 500

    def test_reads_as_dollars_and_cents(self):
        estimate = estimate_cost("claude-opus-5", input_tokens=200_000, output_tokens=4_000)
        assert estimate.human().startswith("$")

    def test_a_tiny_estimate_does_not_round_to_free(self):
        """Showing $0.00 before a run that costs real money trains people to skip the
        confirmation, which defeats the point of showing it."""
        estimate = estimate_cost("claude-haiku-4-5", input_tokens=100, output_tokens=10)
        assert estimate.usd > 0
        assert estimate.human() != "$0.00"


class TestBudgetCeiling:
    def test_a_run_within_the_ceiling_is_allowed(self):
        estimate = estimate_cost("claude-haiku-4-5", input_tokens=1_000, output_tokens=100)
        estimate.check_ceiling(DEFAULT_CEILING_USD)  # must not raise

    def test_a_run_over_the_ceiling_is_refused(self):
        estimate = estimate_cost(
            "claude-opus-5", input_tokens=1_000_000, output_tokens=100_000, calls=100
        )
        with pytest.raises(BudgetExceeded):
            estimate.check_ceiling(DEFAULT_CEILING_USD)

    def test_the_refusal_says_the_cost_and_the_ceiling(self):
        estimate = estimate_cost("claude-opus-5", input_tokens=10_000_000, output_tokens=0)
        with pytest.raises(BudgetExceeded) as excinfo:
            estimate.check_ceiling(5.0)
        message = str(excinfo.value)
        assert "5.00" in message
        assert "DOSSIER_COST_CEILING" in message

    def test_the_default_ceiling_is_conservative(self):
        """A stranger's first run must not be able to spend a fortune by accident."""
        assert 0 < DEFAULT_CEILING_USD <= 25


class TestModelResponse:
    def test_reports_what_a_call_actually_cost(self):
        """The estimate is a projection; this is the bill. Both are needed — tracking
        only the estimate means never learning that it was wrong."""
        response = ModelResponse(
            model="claude-opus-5",
            text='{"findings": []}',
            input_tokens=12_000,
            output_tokens=800,
            cached_input_tokens=40_000,
        )
        assert response.cost().usd > 0
        assert response.cost().model == "claude-opus-5"

    def test_exposes_whether_the_cache_was_used(self):
        """If this reads zero across repeated runs, caching is silently broken and the
        bill is several times what it should be."""
        assert (
            ModelResponse(
                model="claude-opus-5",
                text="{}",
                input_tokens=1,
                output_tokens=1,
                cached_input_tokens=5_000,
            ).cache_hit
            is True
        )
        assert (
            ModelResponse(
                model="claude-opus-5", text="{}", input_tokens=1, output_tokens=1
            ).cache_hit
            is False
        )


class TestCostEstimateAggregation:
    def test_estimates_add_up(self):
        a = estimate_cost("claude-opus-5", input_tokens=1000, output_tokens=100)
        b = estimate_cost("claude-haiku-4-5", input_tokens=1000, output_tokens=100)
        total = CostEstimate.total([a, b])
        assert total.usd == pytest.approx(a.usd + b.usd)
        assert total.calls == 2

    def test_a_mixed_model_total_says_so_rather_than_naming_one(self):
        a = estimate_cost("claude-opus-5", input_tokens=1000, output_tokens=100)
        b = estimate_cost("claude-haiku-4-5", input_tokens=1000, output_tokens=100)
        assert CostEstimate.total([a, b]).model == "mixed"

    def test_totalling_nothing_is_free_rather_than_an_error(self):
        assert CostEstimate.total([]).usd == 0.0
