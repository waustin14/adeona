"""The `search_flights` tool: provider + 1h cache + per-task call budget + call log.

Always returns normalized JSON (a dict); never raises for budget exhaustion or
provider failures, so an agent loop can reason about the error instead of crashing.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from .budget import CallBudget
from .cache import FileCache
from .calllog import CallLog
from .models import FareOption, Flight, SearchQuery
from .normalize import apply_fare, choose_fare
from .providers.base import Provider, ProviderError


class SearchResult(BaseModel):
    ok: bool
    query: SearchQuery
    provider: str
    cache_hit: bool = False
    error: Optional[str] = None
    budget_remaining: int
    flights: list[Flight] = Field(default_factory=list)


class FareDetails(BaseModel):
    ok: bool
    flight_id: str
    cache_hit: bool = False
    error: Optional[str] = None
    budget_remaining: int
    options: list[FareOption] = Field(default_factory=list)
    chosen: Optional[FareOption] = None
    flight: Flight


# Tool definition for the Phase 4 agent loop (Anthropic tool-use format).
SEARCH_FLIGHTS_TOOL = {
    "name": "search_flights",
    "description": (
        "Search one-way flight offers. Returns normalized JSON with price, airline, stops, "
        "duration, layovers, fare info and bag rules. Read-only; cannot book. Each uncached "
        "call consumes the task's call budget; when exhausted, returns ok=false."
    ),
    "input_schema": SearchQuery.model_json_schema(),
}


class FlightSearch:
    def __init__(self, provider: Provider, budget: CallBudget,
                 cache: Optional[FileCache] = None, log: Optional[CallLog] = None):
        self.provider = provider
        self.budget = budget
        self.cache = cache or FileCache(os.environ.get("ADEONA_CACHE_DIR", ".cache"))
        self.log = log or CallLog(os.environ.get("ADEONA_LOG_FILE", "logs/api_calls.jsonl"))

    def __call__(self, query: SearchQuery | dict[str, Any]) -> dict[str, Any]:
        return self.search_flights(query)

    def _call(self, op: str, params: dict[str, Any], fetch: Callable[[], list[Any]],
              revive: Callable[[dict[str, Any]], Any]) -> tuple[bool, list[Any], Optional[str], bool]:
        """Cache -> budget -> provider -> log. Returns (ok, items, error, cache_hit)."""
        key = FileCache.key(f"{self.provider.name}:{op}", params)
        base = {"provider": self.provider.name, "op": op, "params": params}

        cached = self.cache.get(key)
        if cached is not None:
            items = [revive(x) for x in cached]
            self.log.write(**base, cache_hit=True, ok=True, result_count=len(items),
                           cost_estimate_usd=0.0, budget_remaining=self.budget.remaining)
            return True, items, None, True

        if not self.budget.try_consume():
            err = f"budget_exhausted: all {self.budget.max_calls} calls for this task used"
            self.log.write(**base, cache_hit=False, ok=False, error=err, result_count=0,
                           cost_estimate_usd=0.0, budget_remaining=0)
            return False, [], err, False

        cost = self.provider.cost_per_call_usd
        try:
            items = fetch()
        except ProviderError as e:
            self.log.write(**base, cache_hit=False, ok=False, error=str(e), result_count=0,
                           cost_estimate_usd=cost, budget_remaining=self.budget.remaining)
            return False, [], str(e), False

        self.cache.set(key, [x.model_dump(mode="json", exclude={"stops", "airline"} if isinstance(x, Flight) else None)
                             for x in items])
        self.log.write(**base, cache_hit=False, ok=True, result_count=len(items),
                       cost_estimate_usd=cost, budget_remaining=self.budget.remaining)
        return True, items, None, False

    def search_flights(self, query: SearchQuery | dict[str, Any]) -> dict[str, Any]:
        q = query if isinstance(query, SearchQuery) else SearchQuery.model_validate(query)
        ok, flights, err, hit = self._call("search", q.model_dump(), lambda: self.provider.search(q),
                                           Flight.model_validate)
        return SearchResult(ok=ok, query=q, provider=self.provider.name, cache_hit=hit, error=err,
                            budget_remaining=self.budget.remaining, flights=flights).model_dump(mode="json")

    def fare_details(self, query: SearchQuery | dict[str, Any], flight: Flight | dict[str, Any],
                     allow_basic: bool = False) -> dict[str, Any]:
        """Look up every fare brand for one itinerary (1 call) and re-price it with the best fit.

        `flight` in the result is the itinerary re-priced with the chosen fare, or unchanged
        if no airline-direct fare qualifies.
        """
        q = query if isinstance(query, SearchQuery) else SearchQuery.model_validate(query)
        f = flight if isinstance(flight, Flight) else Flight.model_validate(flight)
        params = {**q.model_dump(), "flight_id": f.id, "provider_ref": f.provider_ref}
        ok, options, err, hit = self._call("fare_options", params, lambda: self.provider.fare_options(q, f),
                                           FareOption.model_validate)
        chosen = choose_fare(options, allow_basic) if ok else None
        return FareDetails(ok=ok, flight_id=f.id, cache_hit=hit, error=err, budget_remaining=self.budget.remaining,
                           options=options, chosen=chosen,
                           flight=apply_fare(f, chosen) if chosen else f).model_dump(mode="json")
