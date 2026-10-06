"""Helpers that turn loosely-specified flight dicts into validated Flight objects."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Optional

from .models import BagRules, FareOption, Flight, Layover, Segment


OVERNIGHT_WINDOW_END_HOUR = 5  # [ASSUMPTION] a layover touching 00:00-05:00 is "overnight"


def is_overnight(arrive: datetime, depart: datetime) -> bool:
    """True if the layover overlaps the 00:00-05:00 window on any night.

    Catches both "land 23:00, leave 08:00" and "land 01:00, leave 07:00".
    A source-provided flag (e.g. Google Flights `overnight`) takes precedence.
    """
    day = arrive.date()
    while day <= depart.date():
        start = datetime.combine(day, time(0))
        end = datetime.combine(day, time(OVERNIGHT_WINDOW_END_HOUR))
        if arrive < end and depart > start:
            return True
        day += timedelta(days=1)
    return False


def derive_layovers(segments: list[Segment]) -> list[Layover]:
    out = []
    for prev, nxt in zip(segments, segments[1:]):
        minutes = int((nxt.departs_at - prev.arrives_at).total_seconds() // 60)
        change = prev.destination != nxt.origin
        out.append(
            Layover(
                airport=prev.destination,
                duration_min=max(minutes, 0),
                overnight=is_overnight(prev.arrives_at, nxt.departs_at),
                airport_change=change,
                departs_from=nxt.origin if change else None,
            )
        )
    return out


def flight_from_dict(d: dict[str, Any]) -> Flight:
    """Validate a normalized flight dict, deriving layovers and total duration if absent."""
    data = dict(d)
    segments = [Segment.model_validate(s) for s in data.pop("segments")]
    if "layovers" not in data:
        data["layovers"] = [lo.model_dump() for lo in derive_layovers(segments)]
    if "total_duration_min" not in data:
        # Local-time arithmetic is wrong across time zones, so sum segment + layover minutes.
        lay = sum(lo["duration_min"] if isinstance(lo, dict) else lo.duration_min for lo in data["layovers"])
        data["total_duration_min"] = sum(s.duration_min for s in segments) + lay
    data.pop("stops", None)
    data.pop("airline", None)  # computed fields; ignore if round-tripped
    return Flight.model_validate({**data, "segments": [s.model_dump() for s in segments]})


def choose_fare(options: list[FareOption], allow_basic: bool = False) -> Optional[FareOption]:
    """Cheapest fare sold directly by the airline, skipping basic economy unless allowed.

    [ASSUMPTION] Direct-from-airline only: OTA tickets can complicate status benefits and
    changes, and an OTA price can't be checked against the airline's fare rules.
    """
    direct = [o for o in options if o.sold_by_airline]
    if not allow_basic:
        direct = [o for o in direct if o.is_basic_economy is False]
    return min(direct, key=lambda o: o.price, default=None)


def apply_fare(flight: Flight, fare: FareOption) -> Flight:
    """Return a copy of `flight` priced and described by `fare`."""
    old = flight.bag_rules
    new = fare.bag_rules
    bags = BagRules(
        carry_on_included=new.carry_on_included if new.carry_on_included is not None else old.carry_on_included,
        checked_bags_included=max(new.checked_bags_included, old.checked_bags_included),
        first_checked_fee=new.first_checked_fee if new.first_checked_fee is not None else old.first_checked_fee,
        second_checked_fee=new.second_checked_fee if new.second_checked_fee is not None else old.second_checked_fee,
    )
    return flight.model_copy(update={
        "price": fare.price,
        "fare_brand": fare.brand,
        "is_basic_economy": fare.is_basic_economy,
        "bag_rules": bags,
        "seller": fare.book_with if fare.sold_by_airline else flight.seller,
        "notes": [*flight.notes, f"fare verified: {fare.brand or 'unnamed fare'} ${fare.price:g} via {fare.book_with}"
                  + (f" as {', '.join(fare.marketed_as)}" if fare.marketed_as else "")],
    })
