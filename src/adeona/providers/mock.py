"""Offline provider backed by a JSON fixture. No key, no network, no cost."""

from __future__ import annotations

import json
from pathlib import Path

from ..models import FareOption, Flight, SearchQuery
from ..normalize import flight_from_dict

DEFAULT_FIXTURE = Path(__file__).resolve().parents[3] / "samples" / "sample.json"


def load_flights(path: str | Path) -> list[Flight]:
    data = json.loads(Path(path).read_text())
    rows = data["flights"] if isinstance(data, dict) else data
    return [flight_from_dict(r) for r in rows]


class MockProvider:
    name = "mock"
    cost_per_call_usd = 0.0

    def __init__(self, fixture: str | Path = DEFAULT_FIXTURE):
        self.fixture = Path(fixture)
        self.calls = 0

    def search(self, query: SearchQuery) -> list[Flight]:
        self.calls += 1
        out = []
        for f in load_flights(self.fixture):
            if f.segments[0].origin not in query.origins:
                continue
            if f.segments[-1].destination not in query.destinations:
                continue
            if f.departs_at.date().isoformat() != query.depart_date:
                continue
            if query.max_stops is not None and f.stops > query.max_stops:
                continue
            if query.exclude_basic and f.is_basic_economy:
                continue
            if query.layover_minutes and not all(
                query.layover_minutes[0] <= lo.duration_min <= query.layover_minutes[1] for lo in f.layovers
            ):
                continue
            # Fixture prices are for 1 adult. MOCK party pricing: every seat pays the adult
            # fare, lap infants are free. Real providers quote the party total themselves.
            party = query.party
            out.append(f.model_copy(update={"price": f.price * party.seated, "party": party}))
        return out

    def fare_options(self, query: SearchQuery, flight: Flight) -> list[FareOption]:
        from .base import ProviderError

        raise ProviderError("fare options are not available from the mock provider")
