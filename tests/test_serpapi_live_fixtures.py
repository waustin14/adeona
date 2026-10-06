"""Adapter tests against real SerpApi responses captured 2026-10-06."""

import json
from pathlib import Path

import httpx
import pytest

from adeona.budget import CallBudget
from adeona.cache import FileCache
from adeona.calllog import CallLog
from adeona.models import SearchQuery
from adeona.normalize import choose_fare
from adeona.providers.serpapi import SerpApiProvider, normalize_response, parse_bag_prices, parse_booking_options
from adeona.search import FlightSearch

FIX = Path(__file__).parent / "fixtures" / "serpapi"
Q = SearchQuery(origins=["BWI", "IAD", "DCA"], destinations=["MCO"], depart_date="2026-11-14",
                max_stops=1, exclude_basic=True)


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def test_real_search_normalizes_every_item():
    raw = load("bwi_mco")
    flights = normalize_response(raw, exclude_basic=True)
    assert len(flights) == len(raw["best_flights"]) + len(raw["other_flights"]) == 34
    assert {f.airline for f in flights} >= {"Southwest", "United", "Frontier"}
    assert all(f.provider_ref and f.is_basic_economy is False for f in flights)
    one_stop = [f for f in flights if f.stops == 1]
    assert len(one_stop) == 2 and all(len(f.layovers) == 1 for f in one_stop)
    # total_duration from Google = segments + layover
    for f in one_stop:
        assert f.total_duration_min == sum(s.duration_min for s in f.segments) + f.layovers[0].duration_min


def test_real_southwest_fare_brands():
    opts = parse_booking_options(load("booking_wn"))
    assert [(o.brand, o.price, o.is_basic_economy) for o in opts] == [
        ("Basic", 66, True), ("Choice", 102, False), ("Choice Preferred", 168, False)]
    assert all(o.bag_rules.first_checked_fee == 45 and o.bag_rules.carry_on_included for o in opts)
    assert choose_fare(opts).brand == "Choice"
    assert choose_fare(opts, allow_basic=True).brand == "Basic"


def test_real_frontier_ignores_otas_and_basic():
    opts = parse_booking_options(load("booking_f9"))
    assert len(opts) > 10 and sum(o.sold_by_airline for o in opts) == 2
    chosen = choose_fare(opts)
    assert (chosen.brand, chosen.price, chosen.book_with) == ("Economy Bundle", 83, "Frontier")
    assert chosen.bag_rules.first_checked_fee is None  # not quoted -> assumptions fallback


@pytest.mark.parametrize("lines,expected", [
    (["1 free carry-on", "1st checked bag: 45"], {"carry_on_included": True, "first_checked_fee": 45}),
    (["No carry-on bags", "No checked bags"], {"carry_on_included": False}),
    (["1st checked bag: $40", "2nd checked bag: $50"], {"first_checked_fee": 40, "second_checked_fee": 50}),
    (["2 free checked bags"], {"checked_bags_included": 2}),
])
def test_bag_price_strings(lines, expected):
    rules = parse_bag_prices(lines).model_dump()
    assert {k: rules[k] for k in expected} == expected


def test_fare_details_end_to_end_reprices_flight(tmp_path):
    search_raw, booking_raw = load("bwi_mco"), load("booking_wn")
    calls = []

    def handler(req: httpx.Request):
        calls.append(dict(req.url.params))
        return httpx.Response(200, json=booking_raw if "booking_token" in req.url.params else search_raw)

    provider = SerpApiProvider(api_key="test", client=httpx.Client(transport=httpx.MockTransport(handler)))
    tool = FlightSearch(provider, CallBudget(3), FileCache(tmp_path / "c"), CallLog(tmp_path / "l.jsonl"))
    res = tool(Q)
    wn = next(f for f in res["flights"] if f["airline"] == "Southwest" and f["price"] == 102)
    d = tool.fare_details(Q, wn)
    assert d["ok"] and d["chosen"]["brand"] == "Choice"
    assert d["flight"]["fare_brand"] == "Choice" and d["flight"]["bag_rules"]["first_checked_fee"] == 45
    assert calls[1]["booking_token"] == wn["provider_ref"] and calls[1]["departure_id"] == "BWI,IAD,DCA"
    # cached second lookup costs nothing
    tool.fare_details(Q, wn)
    assert len(calls) == 2 and d["budget_remaining"] == 1
    ops = [json.loads(line)["op"] for line in (tmp_path / "l.jsonl").read_text().splitlines()]
    assert ops == ["search", "fare_options", "fare_options"]


def test_fare_details_budget_exhausted(tmp_path):
    provider = SerpApiProvider(api_key="test", client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=load("bwi_mco")))))
    tool = FlightSearch(provider, CallBudget(1), FileCache(tmp_path / "c"), CallLog(tmp_path / "l.jsonl"))
    f = tool(Q)["flights"][0]
    d = tool.fare_details(Q, f)
    assert d["ok"] is False and d["error"].startswith("budget_exhausted")
    assert d["flight"]["price"] == f["price"]  # unchanged


def test_jetblue_main_base_is_basic_by_features():
    """Regression: 'Main Base' has no 'basic' in its name but is basic economy."""
    from adeona.models import FareOption
    from adeona.providers.serpapi import is_basic_fare
    opts = [FareOption.model_validate(o) for o in load("booking_b6_options")["options"]]
    by_brand = {o.brand: is_basic_fare(o.brand, o.features, o.bag_rules) for o in opts}
    assert by_brand == {"Main Base": True, "Main": False, "Main Flex": False,
                        "Even More Base": False, "Even More": False}


def test_price_anchor_marks_cheaper_fares_basic(tmp_path):
    """With exclude_basic, any fare cheaper than the search price is basic per Google."""
    from adeona.models import Flight
    search_raw, booking_raw = load("bwi_mco"), load("booking_wn")
    for bo in booking_raw["booking_options"]:  # strip the signals so only the anchor can catch it
        bo["together"]["option_title"] = "Mystery"
        bo["together"]["extensions"] = []
    provider = SerpApiProvider(api_key="t", client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=booking_raw if "booking_token" in r.url.params else search_raw))))
    wn = next(f for f in provider.search(Q) if f.airline == "Southwest" and f.price == 102)
    opts = provider.fare_options(Q, wn)
    assert [(o.price, o.is_basic_economy) for o in opts] == [(66, True), (102, False), (168, False)]
    assert choose_fare(opts).price == 102


# ------------------------------------------------ IAD-FRA (overnight layovers, codeshares)

FRA_Q = SearchQuery(origins=["IAD"], destinations=["FRA"], depart_date="2026-11-14", max_stops=1,
                    exclude_basic=True, layover_minutes=(480, 1440), show_hidden=True)


def fra_flights():
    return {f.id: f for f in normalize_response(load("iad_fra_long"), exclude_basic=True)}


def test_overnight_layovers_detected_although_google_flag_absent():
    raw = load("iad_fra_long")
    assert not any("overnight" in lo for it in raw["best_flights"] + raw["other_flights"]
                   for lo in it.get("layovers", []))
    overnight = {f.layovers[0].airport for f in fra_flights().values() if f.layovers and f.layovers[0].overnight}
    assert overnight == {"CPH", "IST"}


def test_overnight_layover_scored_live(profile, assumptions):
    from adeona.scoring import score_flight
    sas = fra_flights()["gf-e6a5794688"]
    items = {i.key: i for i in score_flight(sas, profile, assumptions).items}
    assert items["layover_overnight"].amount == 150 and "CPH" in items["layover_overnight"].note


def test_red_eye_is_segment_flag_not_layover():
    lh = fra_flights()["gf-b774080543"]  # LH 419 nonstop, departs 18:00
    assert lh.segments[0].red_eye and lh.layovers == []


def test_red_eye_penalty_optional(profile, assumptions):
    from adeona.scoring import score_flight
    lh = fra_flights()["gf-b774080543"]
    assert "red_eye" not in {i.key for i in score_flight(lh, profile, assumptions).items}
    assumptions.red_eye_penalty = 25
    assert {i.key: i.amount for i in score_flight(lh, profile, assumptions).items}["red_eye"] == 25


def test_operating_carrier_from_plane_and_crew_by():
    ops = {s.operating_airline for f in fra_flights().values() for s in f.segments if s.operating_airline}
    assert {"HOP!", "KLM Cityhopper", "Air Nostrum for Iberia", "Cityjet"} <= ops


def test_avoid_regional_operator(profile):
    from adeona.scoring import check_constraints
    profile.avoid_airlines = ["Cityjet"]
    assert "avoided airline: Cityjet" in check_constraints(fra_flights()["gf-e6a5794688"], profile)[0]


def test_layover_and_hidden_params():
    p = SerpApiProvider(api_key="x", client=httpx.Client())
    params = p.build_params(FRA_Q)
    assert params["layover_duration"] == "480,1440" and params["show_hidden"] == "true"


def test_unlabeled_fares_use_price_anchor_and_bag_free_string():
    """Live LH 419: no option_title anywhere; United-sold $799 = the exclude_basic price."""
    lh = fra_flights()["gf-b774080543"]
    provider = SerpApiProvider(api_key="t", client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=load("booking_lh")))))
    opts = [o for o in provider.fare_options(FRA_Q, lh) if o.sold_by_airline]
    assert [(o.book_with, o.price, o.is_basic_economy) for o in opts] == [
        ("United", 799, False), ("Lufthansa", 2919, None)]
    assert opts[1].bag_rules.checked_bags_included == 1   # "1st checked bag free"
    chosen = choose_fare(opts)
    assert (chosen.book_with, chosen.price, chosen.bag_rules.first_checked_fee) == ("United", 799, 90)
