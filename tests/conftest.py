from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from adeona.normalize import flight_from_dict
from adeona.profile import Assumptions, Profile

PROFILE = {
    "home_airports": ["BWI", "IAD", "DCA"],
    "hourly_value": 25,
    "preferred_airlines": ["Southwest", "United"],
    "avoid_airlines": ["Spirit"],
    "preferred_airline_bonus": 30,
    "loyalty": {
        "Southwest": {"status": "A-List Preferred", "program": "Rapid Rewards", "cpp": 1.2},
        "United": {"status": "MileagePlus Silver", "program": "MileagePlus", "cpp": 1.35},
        "Chase": {"status": "Sapphire Reserve", "program": "Ultimate Rewards", "cpp": 1.5},
    },
    "bags_checked": 1,
    "hard_constraints": {"max_stops": 1, "min_layover_min": 45, "no_basic_economy": True},
}

ASSUMPTIONS = {
    "earnable_fraction_of_price": 1.0,  # 1.0 keeps test arithmetic obvious
    "earn": {
        "United": {"per_dollar_by_brand": {"default": 5}, "status_bonus_pct": {"MileagePlus Silver": 40}},
        "Southwest": {
            "per_dollar_by_brand": {"Basic": 2, "Choice": 6, "default": 6},
            "status_bonus_pct": {"A-List Preferred": 100},
        },
    },
    "partner_earn_per_dollar": {"United": 5},
    "status_perks": {"United": {"MileagePlus Silver": {"priority_boarding": 5, "upgrade_odds": 10}}},
    "fees": {
        "United": {"first_checked_bag": 40, "second_checked_bag": 50, "seat_selection_basic": 20,
                   "status_free_bags": {"MileagePlus Silver": 1},
                   "companion_free_bags": {"MileagePlus Silver": 1}},
        "Southwest": {"first_checked_bag": 35, "second_checked_bag": 45,
                      "status_free_bags": {"A-List Preferred": 2}},
    },
    "default_fees": {"first_checked_bag": 40, "second_checked_bag": 50, "seat_selection_basic": 25},
    "layover_penalties": {"short_connection_under_min": 75, "short_connection": 40,
                          "overnight": 150, "airport_change": 100},
}


@pytest.fixture
def profile() -> Profile:
    return Profile.model_validate(PROFILE)


@pytest.fixture
def assumptions() -> Assumptions:
    return Assumptions.model_validate(ASSUMPTIONS)


def seg(airline, origin, dest, dep: str, minutes: int, num="100", **kw):
    d = datetime.fromisoformat(dep)
    return {"airline": airline, "flight_number": num, "origin": origin, "destination": dest,
            "departs_at": d.isoformat(), "arrives_at": (d + timedelta(minutes=minutes)).isoformat(),
            "duration_min": minutes, **kw}


def make_flight(segments, price=200.0, fid="F1", **kw):
    return flight_from_dict({"id": fid, "price": price, "segments": segments,
                             "is_basic_economy": False, **kw})
