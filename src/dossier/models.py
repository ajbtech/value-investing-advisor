"""The model layer, and the cost controls in front of it.

Users bring their own API key, so the spend is theirs. That makes showing a projected
cost before every batch run an obligation rather than a nicety, and it is why the
estimate and the ceiling live here rather than being bolted on at the call site.

Nothing in this module calls a model. `ModelClient` is the interface; providers
implement it, and tests drive a fake through it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: Published API rates, in US dollars per million tokens, cached 2026-06-24. Refresh
#: from https://claude.com/pricing — a stale table produces a confident estimate that
#: is wrong, which is worse than no estimate, because the user acts on the number.
PRICES: dict[str, Price] = {}

#: A cache read costs roughly a tenth of a fresh input token. Pass A sends the same two
#: filings to several prompts, so this is the difference between a sane bill and a silly
#: one — and it is why the estimate models cached tokens separately.
CACHE_READ_MULTIPLIER = 0.1

#: Deliberately low. A stranger's first run must not be able to spend a fortune by
#: accident; anyone who wants more raises it knowingly.
DEFAULT_CEILING_USD = 5.0

CEILING_ENV = "DOSSIER_COST_CEILING"


class UnknownModel(KeyError):
    """No published price for this model, so no honest estimate can be made."""


class BudgetExceeded(RuntimeError):
    """The projected cost of a run exceeds the configured ceiling."""


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float


PRICES.update(
    {
        "claude-opus-5": Price(5.00, 25.00),
        "claude-sonnet-5": Price(2.00, 10.00),
        "claude-haiku-4-5": Price(1.00, 5.00),
    }
)


def price_of(model: str) -> Price:
    try:
        return PRICES[model]
    except KeyError as exc:
        raise UnknownModel(
            f"no published price for {model!r}. Known models: {', '.join(sorted(PRICES))}. "
            "Add it to dossier.models.PRICES rather than guessing — an estimate the user "
            "acts on must not be invented."
        ) from exc


def ceiling_from_env() -> float:
    raw = os.environ.get(CEILING_ENV)
    if not raw:
        return DEFAULT_CEILING_USD
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{CEILING_ENV} must be a number in US dollars, got {raw!r}") from exc


@dataclass(frozen=True)
class CostEstimate:
    model: str
    usd: float
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    calls: int = 1

    def human(self) -> str:
        """Dollars and cents, except when that would read as free.

        Showing $0.00 before a run that costs real money trains people to skip the
        confirmation, which defeats the point of showing it at all.
        """
        if 0 < self.usd < 0.01:
            return f"${self.usd:.4f}"
        return f"${self.usd:,.2f}"

    def check_ceiling(self, ceiling_usd: float | None = None) -> None:
        limit = ceiling_from_env() if ceiling_usd is None else ceiling_usd
        if self.usd > limit:
            raise BudgetExceeded(
                f"this run is projected to cost {self.human()}, over the ceiling of "
                f"${limit:,.2f}. Raise it with {CEILING_ENV} if that is what you intend."
            )

    @classmethod
    def total(cls, estimates: list[CostEstimate]) -> CostEstimate:
        if not estimates:
            return cls(model="none", usd=0.0, input_tokens=0, output_tokens=0, calls=0)
        models = {estimate.model for estimate in estimates}
        return cls(
            # A total across models must not claim to be one of them: the per-token
            # rates differ, so naming one would invite dividing by the wrong price.
            model=models.pop() if len(models) == 1 else "mixed",
            usd=sum(e.usd for e in estimates),
            input_tokens=sum(e.input_tokens for e in estimates),
            output_tokens=sum(e.output_tokens for e in estimates),
            cached_input_tokens=sum(e.cached_input_tokens for e in estimates),
            calls=sum(e.calls for e in estimates),
        )


def estimate_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
    calls: int = 1,
) -> CostEstimate:
    """Project what a call — or `calls` identical calls — will cost."""
    price = price_of(model)
    per_call = (
        input_tokens * price.input_per_mtok
        + cached_input_tokens * price.input_per_mtok * CACHE_READ_MULTIPLIER
        + output_tokens * price.output_per_mtok
    ) / 1_000_000
    return CostEstimate(
        model=model,
        usd=per_call * calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        calls=calls,
    )


@dataclass(frozen=True)
class ModelResponse:
    """What a model actually returned, and what it actually cost."""

    model: str
    text: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0

    @property
    def cache_hit(self) -> bool:
        """If this reads False across repeated runs, caching is silently broken and the
        bill is several times what it should be."""
        return self.cached_input_tokens > 0

    def cost(self) -> CostEstimate:
        """The bill, not the projection. Tracking only the estimate means never
        learning that the estimate was wrong."""
        return estimate_cost(
            self.model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_input_tokens=self.cached_input_tokens,
        )


@runtime_checkable
class ModelClient(Protocol):
    """What the analysis passes need from a model, and nothing more.

    Kept deliberately narrow so a second provider — or a local model — is a small class
    rather than a rewrite. The plan commits to Anthropic, OpenAI and local Ollama.
    """

    model: str

    def count_tokens(self, system: str, prompt: str) -> int:
        """Tokens this request would consume, for the estimate shown before a run."""
        ...

    def complete(self, system: str, prompt: str, *, max_tokens: int = 16000) -> ModelResponse:
        """Run one request and return its text and usage."""
        ...
