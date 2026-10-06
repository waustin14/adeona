import pytest

from adeona.scoring import analyze_award, check_constraints, rank, score_flight
from tests.conftest import make_flight, seg


def items(result):
    return {i.key: i.amount for i in result.items}


def test_nonstop_preferred_airline_full_breakdown(profile, assumptions):
    # United nonstop, $200, 120 min, Silver: bag waived, 7 mi/$ @1.35c
    f = make_flight([seg("United", "IAD", "ORD", "2026-11-01T08:00", 120)])
    r = score_flight(f, profile, assumptions)
    it = items(r)
    assert it["cash"] == 200
    assert it["bags"] == 0                       # Silver waives 1 bag
    assert it["time"] == 50                      # 2h x $25
    assert it["miles"] == -18.9                  # 200*7=1400 mi * 1.35c
    assert it["perks"] == -15
    assert it["preferred"] == -30
    assert r.effective_cost == pytest.approx(200 + 50 - 18.9 - 15 - 30)


def test_line_items_always_sum_to_total(profile, assumptions):
    f = make_flight([seg("Delta", "BWI", "ATL", "2026-11-01T08:00", 110),
                     seg("Delta", "ATL", "MIA", "2026-11-01T10:40", 100)], price=180)
    r = score_flight(f, profile, assumptions)
    assert round(sum(i.amount for i in r.items), 2) == r.effective_cost


def test_basic_economy_is_filtered(profile, assumptions):
    f = make_flight([seg("United", "IAD", "ORD", "2026-11-01T08:00", 120)],
                    is_basic_economy=True, fare_brand="Basic Economy")
    ranking = rank([f], profile, assumptions)
    assert ranking.ranked == []
    assert "basic economy" in ranking.excluded[0].reasons[0]


def test_basic_economy_allowed_when_constraint_off(profile, assumptions):
    profile.hard_constraints.no_basic_economy = False
    f = make_flight([seg("Delta", "IAD", "ATL", "2026-11-01T08:00", 120)],
                    is_basic_economy=True, fare_brand="Basic Economy")
    r = rank([f], profile, assumptions).ranked[0]
    assert items(r)["seat"] == 25                # default basic seat fee
    assert items(r)["bags"] == 40                # no status on Delta


def test_unknown_fare_brand_kept_with_warning(profile, assumptions):
    f = make_flight([seg("United", "IAD", "ORD", "2026-11-01T08:00", 120)], is_basic_economy=None)
    r = rank([f], profile, assumptions).ranked[0]
    assert any("basic economy" in w for w in r.warnings)


def test_overnight_layover_penalized(profile, assumptions):
    f = make_flight([seg("United", "IAD", "DEN", "2026-11-01T21:00", 240),
                     seg("United", "DEN", "SFO", "2026-11-02T07:00", 150)])
    assert f.layovers[0].overnight
    it = items(score_flight(f, profile, assumptions))
    assert it["layover_overnight"] == 150
    assert "layover_short" not in it


def test_airport_change_penalized(profile, assumptions):
    f = make_flight([seg("United", "IAD", "LGA", "2026-11-01T08:00", 75),
                     seg("United", "JFK", "BOS", "2026-11-01T12:00", 70)])
    lo = f.layovers[0]
    assert lo.airport_change and lo.departs_from == "JFK"
    assert items(score_flight(f, profile, assumptions))["layover_airport_change"] == 100


def test_short_connection_penalized_but_legal(profile, assumptions):
    f = make_flight([seg("United", "IAD", "ORD", "2026-11-01T08:00", 120),
                     seg("United", "ORD", "MSP", "2026-11-01T11:00", 80)])  # 60m layover
    assert items(score_flight(f, profile, assumptions))["layover_short"] == 40


def test_layover_below_minimum_excluded(profile, assumptions):
    f = make_flight([seg("United", "IAD", "ORD", "2026-11-01T08:00", 120),
                     seg("United", "ORD", "MSP", "2026-11-01T10:30", 80)])  # 30m
    v, _ = check_constraints(f, profile)
    assert any("< min 45m" in x for x in v)


def test_too_many_stops_excluded(profile, assumptions):
    f = make_flight([seg("United", "IAD", "ORD", "2026-11-01T06:00", 120),
                     seg("United", "ORD", "DEN", "2026-11-01T09:00", 150),
                     seg("United", "DEN", "SFO", "2026-11-01T13:00", 150)])
    v, _ = check_constraints(f, profile)
    assert v == ["2 stops > max 1"]


def test_status_bag_waiver_vs_no_status(profile, assumptions):
    sw = make_flight([seg("Southwest", "BWI", "MCO", "2026-11-01T08:00", 130)], fare_brand="Choice")
    profile.bags_checked = 2
    assert items(score_flight(sw, profile, assumptions))["bags"] == 0       # A-List Preferred: 2 free
    profile.loyalty.pop("Southwest")
    assert items(score_flight(sw, profile, assumptions))["bags"] == 80      # 35 + 45


def test_fare_included_bag_and_source_quoted_fee(profile, assumptions):
    profile.bags_checked = 2
    f = make_flight([seg("Delta", "BWI", "ATL", "2026-11-01T08:00", 110)],
                    bag_rules={"checked_bags_included": 1, "second_checked_fee": 45})
    assert items(score_flight(f, profile, assumptions))["bags"] == 45


def test_partner_airline_earns_on_alliance_program(profile, assumptions):
    f = make_flight([seg("Lufthansa", "IAD", "FRA", "2026-11-01T17:00", 480)], price=800)
    it = items(score_flight(f, profile, assumptions))
    assert it["miles"] == -54.0                  # 800 * 5 partner rate = 4000 MP @ 1.35c
    assert it["preferred"] == 0
    assert it["perks"] == 0                      # perks only on own metal
    assert it["bags"] == 40                      # Silver waiver does not extend to partner


def test_non_alliance_airline_earns_nothing(profile, assumptions):
    f = make_flight([seg("Delta", "BWI", "ATL", "2026-11-01T08:00", 110)])
    assert items(score_flight(f, profile, assumptions))["miles"] == 0


def test_southwest_fare_brand_earn_rate(profile, assumptions):
    basic = make_flight([seg("Southwest", "BWI", "MCO", "2026-11-01T08:00", 130)], price=100, fare_brand="Basic")
    choice = make_flight([seg("Southwest", "BWI", "MCO", "2026-11-01T08:00", 130)], price=100, fare_brand="Choice")
    # A-List Preferred +100%: Basic 4/$, Choice 12/$ @1.2c
    assert items(score_flight(basic, profile, assumptions))["miles"] == -4.8
    assert items(score_flight(choice, profile, assumptions))["miles"] == -14.4


def test_avoided_airline_excluded_even_on_one_segment(profile, assumptions):
    f = make_flight([seg("United", "IAD", "FLL", "2026-11-01T08:00", 150),
                     seg("Spirit", "FLL", "SJU", "2026-11-01T12:00", 160)])
    ranking = rank([f], profile, assumptions)
    assert ranking.excluded[0].reasons == ["avoided airline: Spirit"]


def test_avoided_operating_carrier_on_codeshare(profile, assumptions):
    f = make_flight([seg("United", "IAD", "FLL", "2026-11-01T08:00", 150, operating_airline="Spirit")])
    assert check_constraints(f, profile)[0] == ["avoided airline: Spirit"]


def test_ranking_order_and_tiebreak(profile, assumptions):
    cheap_slow = make_flight([seg("Delta", "BWI", "ATL", "2026-11-01T08:00", 110),
                              seg("Delta", "ATL", "LAX", "2026-11-01T11:00", 300)], price=250, fid="slow")
    pricier_fast = make_flight([seg("United", "IAD", "LAX", "2026-11-01T08:00", 330)], price=300, fid="fast")
    order = [r.flight.id for r in rank([cheap_slow, pricier_fast], profile, assumptions).ranked]
    assert order == ["fast", "slow"]


def test_award_beats_baseline(profile, assumptions):
    f = make_flight([seg("United", "IAD", "SFO", "2026-11-01T08:00", 330)], price=500,
                    award={"program": "MileagePlus", "points": 20000, "taxes": 5.6})
    a = analyze_award(f, profile)
    assert a.award_cost == pytest.approx(275.6)  # 20000 * 1.35c + 5.60
    assert a.implied_cpp == pytest.approx(2.47)
    assert a.beats_baseline is True


def test_award_below_baseline(profile, assumptions):
    f = make_flight([seg("Southwest", "BWI", "MCO", "2026-11-01T08:00", 130)], price=120,
                    award={"program": "Rapid Rewards", "points": 12000, "taxes": 5.6})
    a = analyze_award(f, profile)
    assert a.beats_baseline is False and a.implied_cpp < 1.2


def test_award_unknown_program(profile, assumptions):
    f = make_flight([seg("Delta", "BWI", "ATL", "2026-11-01T08:00", 110)], price=300,
                    award={"program": "SkyMiles", "points": 15000, "taxes": 5.6})
    a = analyze_award(f, profile)
    assert a.beats_baseline is None and a.award_cost is None


@pytest.mark.parametrize("arr,dep,expected", [
    ("2026-11-01T23:00", "2026-11-02T08:00", True),   # classic overnight
    ("2026-11-02T01:00", "2026-11-02T07:00", True),   # red-eye arrival
    ("2026-11-01T18:00", "2026-11-01T22:00", False),  # long evening layover
    ("2026-11-02T05:30", "2026-11-02T09:00", False),  # early morning, after window
])
def test_overnight_definition(arr, dep, expected):
    from datetime import datetime
    from adeona.normalize import is_overnight
    assert is_overnight(datetime.fromisoformat(arr), datetime.fromisoformat(dep)) is expected
