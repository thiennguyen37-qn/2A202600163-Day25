from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        budget_limit: float = float("inf"),
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.budget_limit = budget_limit
        self.cumulative_cost: float = 0.0

    def complete(self, prompt: str, metadata: dict[str, str] | None = None) -> GatewayResponse:
        """Return a reliable response or a static fallback."""
        t_start = time.perf_counter()

        if self.cache is not None:
            cached, score = self.cache.get(prompt)
            if cached is not None:
                latency_ms = (time.perf_counter() - t_start) * 1000
                return GatewayResponse(
                    text=cached,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=latency_ms,
                    estimated_cost=0.0,
                )

        # Sort cheapest-first when budget is 80% exhausted
        over_budget = self.cumulative_cost >= self.budget_limit * 0.8
        providers = (
            sorted(self.providers, key=lambda p: p.cost_per_1k_tokens)
            if over_budget
            else self.providers
        )

        last_error: str | None = None
        for i, provider in enumerate(providers):
            breaker = self.breakers[provider.name]
            role = "primary" if i == 0 and not over_budget else "fallback"
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})
                self.cumulative_cost += response.estimated_cost
                latency_ms = (time.perf_counter() - t_start) * 1000
                return GatewayResponse(
                    text=response.text,
                    route=f"{role}:{provider.name}",
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=latency_ms,
                    estimated_cost=response.estimated_cost,
                )
            except CircuitOpenError as exc:
                last_error = f"circuit_open:{provider.name} — {exc}"
                continue
            except ProviderError as exc:
                last_error = f"provider_error:{provider.name} — {exc}"
                continue

        latency_ms = (time.perf_counter() - t_start) * 1000
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=latency_ms,
            estimated_cost=0.0,
            error=last_error,
        )
