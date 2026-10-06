from __future__ import annotations

from typing import Protocol

from ..models import FareOption, Flight, SearchQuery


class ProviderError(Exception):
    """A provider call failed (network, auth, quota, malformed response)."""


class Provider(Protocol):
    name: str
    cost_per_call_usd: float  # estimate, for the call log

    def search(self, query: SearchQuery) -> list[Flight]: ...

    def fare_options(self, query: SearchQuery, flight: Flight) -> list[FareOption]: ...
