"""MCP server: exposes search, fare lookup and effective-cost ranking as read-only tools.

Run with `python -m adeona mcp` (stdio) or `python -m adeona mcp --transport http`.
One server process = one call budget, shared by every client connected to it. Search
results are kept in memory under a `search_id` so `rank_flights` and `fare_details`
can refer to them without the client re-sending every itinerary.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .budget import CallBudget
from .models import Flight, SearchQuery
from .normalize import flight_from_dict
from .party import Party, PartyError, build_party
from .profile import load_assumptions, load_profile, profile_warnings
from .providers import get_provider
from .scoring import rank, ticketing_seller
from .search import FlightSearch

INSTRUCTIONS = (
    "Adeona is a read-only flight research assistant. It finds one-way flight options and "
    "ranks them by effective cost (cash price adjusted for bag/seat fees, travel time, "
    "connection quality and loyalty value). It cannot book, hold or pay for anything; tell "
    "the user to book themselves. Typical flow: search_flights -> optionally fare_details on "
    "the top few -> rank_flights with the returned search_id. Uncached searches spend a "
    "shared call budget; check budget_remaining and stop when it is 0."
)

MAX_STORED_SEARCHES = 50

_READ_ONLY = {"read_only_hint": True, "destructive_hint": False, "open_world_hint": True}


@dataclass
class _Stored:
    query: SearchQuery
    flights: list[Flight]
    party: Optional[dict] = None


def _party_spec(travelers: Optional[list[str]], guests: int, children: int, lap_infants: int) -> Optional[dict]:
    """Same shape the CLI writes into search output; None means 'just you'."""
    if not (travelers or guests or children or lap_infants):
        return None
    return {"names": travelers or ["me"], "guests": guests, "children": children, "lap_infants": lap_infants}


@dataclass
class AdeonaService:
    """Tool implementations, independent of the MCP transport so they are easy to test."""

    tool: FlightSearch
    profile_path: str
    assumptions_path: str
    _searches: dict[str, _Stored] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        # Fail at startup, not on the first tool call, if the config is broken.
        self.profile = load_profile(self.profile_path)
        self.assumptions = load_assumptions(self.assumptions_path)
        self.warnings = profile_warnings(self.profile, self.assumptions)

    def _party(self, spec: Optional[dict]) -> Party:
        return build_party(self.profile, **spec) if spec else build_party(self.profile)

    def _get(self, search_id: str) -> _Stored:
        s = self._searches.get(search_id)
        if s is None:
            raise ValueError(f"unknown search_id '{search_id}'; run search_flights first")
        return s

    def _store(self, stored: _Stored) -> str:
        sid = uuid.uuid4().hex[:12]
        self._searches[sid] = stored
        while len(self._searches) > MAX_STORED_SEARCHES:
            self._searches.pop(next(iter(self._searches)))
        return sid

    # ------------------------------------------------------------------ tools

    def search_flights(self, origins: list[str], destinations: list[str], depart_date: str,
                       max_stops: Optional[int] = None, exclude_basic: bool = False,
                       layover_minutes: Optional[tuple[int, int]] = None, show_hidden: bool = False,
                       travelers: Optional[list[str]] = None, guests: int = 0, children: int = 0,
                       lap_infants: int = 0, fare_details: int = 0) -> dict[str, Any]:
        spec = _party_spec(travelers, guests, children, lap_infants)
        counts = self._party(spec).counts if spec else None
        query = SearchQuery(
            origins=[o.upper() for o in origins], destinations=[d.upper() for d in destinations],
            depart_date=depart_date, max_stops=max_stops, exclude_basic=exclude_basic,
            layover_minutes=layover_minutes, show_hidden=show_hidden,
            **(counts.model_dump() if counts else {}),
        )
        with self._lock:
            result = self.tool(query)
            if result["ok"] and fare_details:
                result["fare_details"] = []
                for i, f in enumerate(result["flights"][:fare_details]):
                    d = self.tool.fare_details(query, f)
                    result["flights"][i] = d["flight"]
                    result["fare_details"].append(_fare_summary(d))
                result["budget_remaining"] = self.tool.budget.remaining
            if result["ok"]:
                flights = [flight_from_dict(f) for f in result["flights"]]
                result["search_id"] = self._store(_Stored(query, flights, spec))
        if spec:
            result["party"] = spec
        return result

    def fare_details(self, search_id: str, flight_id: str) -> dict[str, Any]:
        with self._lock:
            s = self._get(search_id)
            idx = next((i for i, f in enumerate(s.flights) if f.id == flight_id), None)
            if idx is None:
                raise ValueError(f"flight '{flight_id}' is not in search {search_id}")
            d = self.tool.fare_details(s.query, s.flights[idx])
            if d["ok"]:
                s.flights[idx] = Flight.model_validate(d["flight"])
        return {**_fare_summary(d), "budget_remaining": d["budget_remaining"], "flight": d["flight"]}

    def rank_flights(self, search_id: Optional[str] = None, flights: Optional[list[dict]] = None,
                     travelers: Optional[list[str]] = None, guests: int = 0, children: int = 0,
                     lap_infants: int = 0, top: Optional[int] = None) -> dict[str, Any]:
        if (search_id is None) == (flights is None):
            raise ValueError("pass exactly one of search_id or flights")
        searched = None
        if search_id is not None:
            s = self._get(search_id)
            items, searched = list(s.flights), s.party
        else:
            items = [flight_from_dict(f) for f in flights]
        asked = _party_spec(travelers, guests, children, lap_infants)
        if asked and searched and asked != searched:
            raise PartyError(f"search was priced for {searched}, but this asks for {asked}; "
                             f"omit the party arguments or search again")
        party = self._party(asked or searched)
        ranking = rank(items, self.profile, self.assumptions, party=party)
        ranked = ranking.ranked[:top] if top else ranking.ranked
        out = []
        for r in ranked:
            seller, basis = ticketing_seller(r.flight, self.profile)
            out.append({**r.to_dict(), "price": r.flight.price, "stops": r.flight.stops,
                        "departs_at": r.flight.departs_at.isoformat(),
                        "duration_min": r.flight.total_duration_min,
                        "seller": seller, "seller_basis": basis})
        return {
            "party": party.describe(),
            "ranked": out,
            "excluded": [{"flight_id": e.flight.id, "price": e.flight.price, "reasons": e.reasons}
                         for e in ranking.excluded],
            "profile_warnings": self.warnings,
            "note": "Read-only research. Prices are party totals; the user books directly.",
        }

    def budget_status(self) -> dict[str, Any]:
        b = self.tool.budget
        return {"provider": self.tool.provider.name, "max_calls": b.max_calls,
                "used": b.used, "remaining": b.remaining}


def _fare_summary(d: dict[str, Any]) -> dict[str, Any]:
    chosen = d["chosen"]
    return {
        "flight_id": d["flight_id"], "ok": d["ok"], "error": d["error"], "cache_hit": d["cache_hit"],
        "chosen": {"brand": chosen["brand"], "price": chosen["price"], "book_with": chosen["book_with"]}
        if chosen else None,
        "airline_direct_options": [{"brand": o["brand"], "price": o["price"], "book_with": o["book_with"]}
                                   for o in d["options"] if o["sold_by_airline"]],
    }


def build_server(provider: str = "mock", budget: int = 10, profile: str = "config/profile.yaml",
                 assumptions: str = "config/assumptions.yaml", fixture: Optional[str] = None):
    """Create the MCPServer with Adeona's tools registered. Requires the `mcp` extra."""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    kwargs = {"fixture": fixture} if provider == "mock" and fixture else {}
    svc = AdeonaService(FlightSearch(get_provider(provider, **kwargs), CallBudget(budget)), profile, assumptions)
    server = MCPServer("adeona", instructions=INSTRUCTIONS)
    ro = ToolAnnotations(**_READ_ONLY)

    @server.tool(annotations=ro)
    def search_flights(origins: list[str], destinations: list[str], depart_date: str,
                       max_stops: Optional[int] = None, exclude_basic: bool = False,
                       layover_minutes: Optional[tuple[int, int]] = None, show_hidden: bool = False,
                       travelers: Optional[list[str]] = None, guests: int = 0, children: int = 0,
                       lap_infants: int = 0, fare_details: int = 0) -> dict[str, Any]:
        """Search one-way flights from any of `origins` to any of `destinations` (IATA codes) on
        `depart_date` (YYYY-MM-DD). Returns normalized itineraries plus a `search_id` for
        rank_flights / fare_details. Prices are totals for the whole party.

        Party: `travelers` are names from the user's profile ('me' = the user); `guests`,
        `children`, `lap_infants` add travelers without loyalty accounts. Omit all for a solo trip.
        `layover_minutes` = [min, max] per connection, e.g. [480, 1440] for long layovers.
        `show_hidden` includes Google's 'View more flights' results.
        `fare_details` = N also looks up fare brands and bag fees for the first N results and
        re-prices each at its cheapest airline-direct, non-basic fare (1 budget call each).
        Each uncached call spends the shared budget; ok=false with budget_exhausted when empty.
        """
        return svc.search_flights(origins, destinations, depart_date, max_stops, exclude_basic,
                                  layover_minutes, show_hidden, travelers, guests, children,
                                  lap_infants, fare_details)

    @server.tool(annotations=ro)
    def fare_details(search_id: str, flight_id: str) -> dict[str, Any]:
        """Look up every fare brand and the airline's bag fees for one itinerary from a previous
        search (1 budget call unless cached). The stored itinerary is re-priced at the cheapest
        airline-direct, non-basic fare, so a later rank_flights uses the confirmed price and seller.
        """
        return svc.fare_details(search_id, flight_id)

    @server.tool(annotations=ToolAnnotations(**{**_READ_ONLY, "open_world_hint": False, "idempotent_hint": True}))
    def rank_flights(search_id: Optional[str] = None, flights: Optional[list[dict]] = None,
                     travelers: Optional[list[str]] = None, guests: int = 0, children: int = 0,
                     lap_infants: int = 0, top: Optional[int] = None) -> dict[str, Any]:
        """Rank itineraries by effective cost using the user's profile and fee assumptions.
        Pass `search_id` from search_flights (preferred) or a raw `flights` list in Adeona's
        normalized schema. Party defaults to the one the search was priced for. Each ranked
        entry has an itemized cost breakdown (`items` sum to `effective_cost`), warnings, award
        analysis, and the ticketing seller (`seller_basis`: verified or assumed). `excluded`
        lists flights removed by the user's hard constraints, with reasons. Costs no budget.
        """
        return svc.rank_flights(search_id, flights, travelers, guests, children, lap_infants, top)

    @server.tool(annotations=ToolAnnotations(**{**_READ_ONLY, "open_world_hint": False}))
    def budget_status() -> dict[str, Any]:
        """Remaining uncached API calls for this server session, and which provider is live."""
        return svc.budget_status()

    return server
