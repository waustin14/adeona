"""SerpApi Google Flights adapter (engine=google_flights).

Verified against live responses on 2026-10-06 (fixtures in tests/fixtures/serpapi/).

What the two call types give us:
  * search: price, segments, layovers. No fare brand or bag fees. With exclude_basic=true
    the price is the cheapest non-basic fare (confirmed for Southwest "Choice" and
    Frontier "Economy Bundle"), so results are marked is_basic_economy=False.
  * booking options (1 extra call per itinerary, via booking_token): every fare brand
    with its price, features, and the airline's own bag prices ("1st checked bag: 45").
  * one-way only here; round trips need a second call per departure_token (Phase 4).
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from typing import Any, Optional

import httpx

from ..models import BagRules, FareOption, Flight, Layover, PartyCounts, SearchQuery, Segment
from ..normalize import derive_layovers
from .base import ProviderError

ENDPOINT = "https://serpapi.com/search.json"
TRAVEL_CLASS = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}


def stops_param(max_stops: Optional[int]) -> int:
    # SerpApi: 0 any, 1 nonstop only, 2 one stop or fewer, 3 two stops or fewer
    return 0 if max_stops is None else min(max_stops, 2) + 1


class SerpApiProvider:
    name = "serpapi"
    # [ASSUMPTION] Starter plan $25 / 1,000 searches (serpapi.com/pricing, 2026-10-06).
    # Cached SerpApi results don't count against quota, but we estimate conservatively.
    cost_per_call_usd = 0.025

    def __init__(self, api_key: Optional[str] = None, timeout: float = 60.0,
                 client: Optional[httpx.Client] = None):
        self.api_key = api_key or os.environ.get("SERPAPI_API_KEY")
        if not self.api_key:
            raise ProviderError("SERPAPI_API_KEY is not set (see .env.example)")
        self.client = client or httpx.Client(timeout=timeout)

    def build_params(self, q: SearchQuery) -> dict[str, Any]:
        if q.return_date:
            raise ProviderError("round trips are not supported yet; search each direction one-way")
        params: dict[str, Any] = {
            "engine": "google_flights",
            "departure_id": ",".join(q.origins),
            "arrival_id": ",".join(q.destinations),
            "outbound_date": q.depart_date,
            "type": 2,
            "adults": q.adults,
            "children": q.children,
            "infants_in_seat": q.infants_in_seat,
            "infants_on_lap": q.infants_on_lap,
            "travel_class": TRAVEL_CLASS.get(q.cabin, 1),
            "stops": stops_param(q.max_stops),
            "bags": q.carry_on_bags,
            "currency": "USD",
            "hl": "en",
            "gl": "us",
        }
        if q.exclude_basic:
            params["exclude_basic"] = "true"
        if q.layover_minutes:
            params["layover_duration"] = f"{q.layover_minutes[0]},{q.layover_minutes[1]}"
        if q.show_hidden:
            params["show_hidden"] = "true"
        return params

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self.client.get(ENDPOINT, params={**params, "api_key": self.api_key})
        except httpx.HTTPError as e:
            raise ProviderError(f"network error: {type(e).__name__}") from None
        if resp.status_code != 200:
            # Body may echo the request URL; keep the message short and key-free.
            raise ProviderError(f"HTTP {resp.status_code} from SerpApi")
        return resp.json()

    def search(self, query: SearchQuery) -> list[Flight]:
        data = self._get(self.build_params(query))
        if data.get("error"):
            if "hasn't returned any results" in data["error"]:
                return []
            raise ProviderError(f"SerpApi error: {data['error']}")
        return normalize_response(data, exclude_basic=query.exclude_basic, party=query.party)

    def fare_options(self, query: SearchQuery, flight: Flight) -> list[FareOption]:
        if not flight.provider_ref:
            raise ProviderError(f"{flight.id} has no booking_token; was it found via serpapi?")
        # booking_token must be sent with the search it came from.
        data = self._get({**self.build_params(query), "booking_token": flight.provider_ref})
        if data.get("error"):
            raise ProviderError(f"SerpApi error: {data['error']}")
        options = parse_booking_options(data)
        if query.exclude_basic:
            # Price anchor: the exclude_basic search already returned the cheapest fare Google
            # counts as non-basic, so anything cheaper is basic by Google's own classification,
            # and an unlabeled fare at exactly that price is that non-basic fare.
            for o in options:
                if o.price < flight.price - 0.5:
                    o.is_basic_economy = True
                elif o.is_basic_economy is None and abs(o.price - flight.price) <= 0.5:
                    o.is_basic_economy = False
        return options


def _ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def _operating(seg: dict[str, Any]) -> Optional[str]:
    """Operating carrier. Live data (2026-10-06) uses `plane_and_crew_by`, e.g. "HOP!" or
    "Air Nostrum for Iberia"; it's undocumented, so also accept an "Operated by X" extension."""
    if seg.get("plane_and_crew_by"):
        return str(seg["plane_and_crew_by"]).strip()
    for e in seg.get("extensions") or []:
        if e.lower().startswith("operated by "):
            return e[len("operated by "):].strip()
    return None


def normalize_item(item: dict[str, Any], exclude_basic: bool,
                   party: Optional[PartyCounts] = None) -> Optional[Flight]:
    if item.get("price") is None:
        return None  # Google sometimes lists flights without a price
    segs = []
    for s in item["flights"]:
        dep, arr = s["departure_airport"], s["arrival_airport"]
        segs.append(Segment(
            airline=s.get("airline", "Unknown"),
            flight_number=s.get("flight_number", ""),
            origin=dep["id"], destination=arr["id"],
            departs_at=_ts(dep["time"]), arrives_at=_ts(arr["time"]),
            duration_min=int(s.get("duration", 0)),
            operating_airline=_operating(s),
            cabin=s.get("travel_class", "Economy"),
            # Segment-level `overnight` (undocumented) marks a red-eye flight, NOT a layover.
            red_eye=bool(s.get("overnight")),
            often_delayed=bool(s.get("often_delayed_by_over_30_min")),
            also_sold_by=list(s.get("ticket_also_sold_by") or []),
        ))
    derived = derive_layovers(segs)
    raw = item.get("layovers") or []
    layovers = []
    for i, d in enumerate(derived):
        r = raw[i] if i < len(raw) else {}
        layovers.append(Layover(
            airport=d.airport,
            duration_min=int(r.get("duration", d.duration_min)),
            overnight=bool(r.get("overnight")) or d.overnight,
            airport_change=d.airport_change,
            departs_from=d.departs_from,
        ))
    fid = hashlib.sha1("|".join(f"{s.flight_number}@{s.departs_at:%Y%m%d%H%M}" for s in segs).encode()).hexdigest()[:10]
    return Flight(
        id=f"gf-{fid}",
        source="serpapi",
        price=float(item["price"]),
        segments=segs,
        layovers=layovers,
        total_duration_min=int(item.get("total_duration") or sum(s.duration_min for s in segs)),
        is_basic_economy=False if exclude_basic else None,
        notes=list(item.get("extensions") or []),
        provider_ref=item.get("booking_token"),
        party=party or PartyCounts(),
    )


def normalize_response(data: dict[str, Any], exclude_basic: bool = False,
                       party: Optional[PartyCounts] = None) -> list[Flight]:
    """`party` must be the passenger mix the search was run for: `price` is its total."""
    out = []
    for item in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        f = normalize_item(item, exclude_basic, party)
        if f is not None:
            out.append(f)
    return out


# ------------------------------------------------------------ booking options

_NTH_BAG = re.compile(r"^(\d)(?:st|nd|rd|th) checked bag:\s*\$?(\d+(?:\.\d+)?)", re.I)
_FREE_CHECKED = re.compile(r"^(\d+) free checked bags?", re.I)
_NTH_FREE = re.compile(r"^(\d)(?:st|nd|rd|th) checked bag free", re.I)  # live: "1st checked bag free"


def parse_bag_prices(lines: list[str]) -> BagRules:
    """Parse Google's bag strings, e.g. ["1 free carry-on", "1st checked bag: 45"]."""
    rules = BagRules()
    for line in lines or []:
        low = line.lower()
        if "free carry-on" in low:
            rules.carry_on_included = True
        elif low.startswith("no carry-on"):
            rules.carry_on_included = False
        elif m := _FREE_CHECKED.match(line):
            rules.checked_bags_included = int(m.group(1))
        elif m := _NTH_FREE.match(line):
            rules.checked_bags_included = max(rules.checked_bags_included, int(m.group(1)))
        elif m := _NTH_BAG.match(line):
            n, fee = int(m.group(1)), float(m.group(2))
            if n == 1:
                rules.first_checked_fee = fee
            elif n == 2:
                rules.second_checked_fee = fee
    return rules


def is_basic_fare(brand: Optional[str], features: list[str], bags: BagRules) -> Optional[bool]:
    """Classify a fare as basic economy.

    The name alone is not enough: JetBlue "Main Base" is basic by Google's definition
    (exclude_basic search priced it out) although its name lacks "basic". SerpApi's docs
    define non-basic as "free seat selection and carry on bags", so we test those features.
    """
    feats = {f.lower() for f in features}
    if brand and "basic" in brand.lower():
        return True
    if "seat selection for a fee" in feats or bags.carry_on_included is False:
        return True
    if "free seat selection" in feats or brand:
        return False
    return None


def parse_booking_options(data: dict[str, Any]) -> list[FareOption]:
    # Itinerary-level bag prices apply when an option doesn't list its own.
    default_bags = (data.get("baggage_prices") or {}).get("together") or []
    out = []
    for bo in data.get("booking_options") or []:
        opt = bo.get("together")  # split-ticket options ("departing"/"returning") are round-trip only
        if not opt or opt.get("price") is None:
            continue
        brand = opt.get("option_title")
        bags = parse_bag_prices(opt.get("baggage_prices") or default_bags)
        out.append(FareOption(
            brand=brand,
            price=float(opt["price"]),
            book_with=opt.get("book_with", "?"),
            sold_by_airline=bool(opt.get("airline")),
            marketed_as=list(opt.get("marketed_as") or []),
            is_basic_economy=is_basic_fare(brand, opt.get("extensions") or [], bags),
            bag_rules=bags,
            features=list(opt.get("extensions") or []),
        ))
    return out
