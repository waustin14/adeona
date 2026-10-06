"""Parties: per-traveler bags/time/miles/perks, once-per-trip bonus, party-total prices."""

import json
from pathlib import Path

import pytest

from adeona.cli import main
from adeona.models import PartyCounts, SearchQuery
from adeona.party import PartyError, build_party
from adeona.profile import ConfigError, Profile
from adeona.providers.serpapi import normalize_response
from adeona.scoring import rank, score_flight
from tests.conftest import PROFILE, make_flight, seg

FIX = Path(__file__).parent / "fixtures" / "serpapi"


@pytest.fixture
def fam_profile():
    return Profile.model_validate({**PROFILE, "child_hourly_value": 5, "travelers": {
        "alex": {"type": "adult", "hourly_value": 40, "bags_checked": 2,
                 "loyalty": {"United": {"status": "General", "program": "MileagePlus", "cpp": 1.35}}},
        "sam": {"type": "child"},
        "kai": {"type": "infant_on_lap"},
    }})


def items(r):
    return {i.key: i.amount for i in r.items}


def ua(party, price):
    return make_flight([seg("United", "IAD", "ORD", "2026-11-01T08:00", 120, num="UA 100")], price=price,
                       party=party.counts.model_dump())


# ------------------------------------------------------------------ party building

def test_defaults_and_overrides(fam_profile):
    p = build_party(fam_profile, ["me", "alex", "sam", "kai"], guests=1)
    by = {t.name: t for t in p.travelers}
    assert (by["me"].hourly_value, by["alex"].hourly_value, by["sam"].hourly_value, by["kai"].hourly_value,
            by["guest1"].hourly_value) == (25, 40, 5, 5, 25)
    assert (by["alex"].bags_checked, by["sam"].bags_checked, by["kai"].bags_checked) == (2, 1, 0)
    assert p.counts == PartyCounts(adults=3, children=1, infants_on_lap=1)
    assert by["guest1"].loyalty == {}


@pytest.mark.parametrize("kw,msg", [
    ({"names": ["bob"]}, "unknown traveler 'bob'"),
    ({"names": ["me", "me"]}, "listed twice"),
    ({"names": ["sam"]}, "at least one adult"),
    ({"names": ["me"], "lap_infants": 2}, "each lap infant needs an adult"),
    ({"names": ["me"], "guests": 9}, "9-passenger"),
])
def test_party_errors(fam_profile, kw, msg):
    with pytest.raises(PartyError, match=msg):
        build_party(fam_profile, **kw)


def test_me_is_reserved():
    with pytest.raises(ValueError, match="reserved"):
        Profile.model_validate({**PROFILE, "travelers": {"me": {"type": "adult"}}})


def test_query_party_limits():
    with pytest.raises(ValueError, match="lap infant"):
        SearchQuery(origins=["IAD"], destinations=["FRA"], depart_date="2026-11-14", infants_on_lap=2)


# ------------------------------------------------------------------ scoring

def test_per_traveler_time_and_once_per_trip_bonus(fam_profile, assumptions):
    party = build_party(fam_profile, ["me", "alex", "sam"])
    r = score_flight(ua(party, 600), fam_profile, assumptions, party=party)
    it = items(r)
    assert it["time"] == 2 * (25 + 40 + 5)
    assert it["preferred"] == -30
    assert r.per_traveler == round(r.effective_cost / 3, 2)


def test_companion_bag_waiver(fam_profile, assumptions):
    party = build_party(fam_profile, ["me", "alex", "sam"])
    # me 1 bag waived; alex 2 bags: 1st via my Silver companion benefit, 2nd $50; sam 1 bag via companion
    r = score_flight(ua(party, 600), fam_profile, assumptions, party=party)
    assert items(r)["bags"] == 50
    note = next(i.note for i in r.items if i.key == "bags")
    assert "your MileagePlus Silver covers 1" in note


def test_no_companion_waiver_on_southwest(fam_profile, assumptions):
    party = build_party(fam_profile, ["me"], guests=1)
    f = make_flight([seg("Southwest", "BWI", "MCO", "2026-11-01T08:00", 130, num="WN 1")], price=300,
                    fare_brand="Choice", party=party.counts.model_dump())
    assert items(score_flight(f, fam_profile, assumptions, party=party))["bags"] == 35   # guest pays


def test_miles_per_traveler_with_split_fare(fam_profile, assumptions):
    party = build_party(fam_profile, ["me", "alex", "kai"])     # 2 seats + lap infant
    it = items(score_flight(ua(party, 600), fam_profile, assumptions, party=party))
    # fare share = 600 / 2 seats = 300 (earnable 1.0 in test config)
    assert it["miles"] == round(-300 * 7 * 1.35 / 100, 2)       # Silver 7/$
    assert it["miles:alex"] == round(-300 * 5 * 1.35 / 100, 2)  # General 5/$
    assert "miles:kai" not in it


def test_perks_only_for_status_holders(fam_profile, assumptions):
    party = build_party(fam_profile, ["me", "alex"])
    assert items(score_flight(ua(party, 600), fam_profile, assumptions, party=party))["perks"] == -15


def test_penalties_once_or_per_traveler(fam_profile, assumptions):
    party = build_party(fam_profile, ["me", "alex"])
    f = make_flight([seg("United", "IAD", "DEN", "2026-11-01T21:00", 240, num="UA 1"),
                     seg("United", "DEN", "SFO", "2026-11-02T07:00", 150, num="UA 2")],
                    party=party.counts.model_dump())
    assert items(score_flight(f, fam_profile, assumptions, party=party))["layover_overnight"] == 150
    assumptions.penalties_per_traveler = True
    assert items(score_flight(f, fam_profile, assumptions, party=party))["layover_overnight"] == 300


def test_award_scales_with_seats(fam_profile, assumptions):
    party = build_party(fam_profile, ["me", "alex", "kai"])
    f = make_flight([seg("United", "IAD", "SFO", "2026-11-01T08:00", 330, num="UA 1")], price=1000,
                    award={"program": "MileagePlus", "points": 20000, "taxes": 5.6},
                    party=party.counts.model_dump())
    a = score_flight(f, fam_profile, assumptions, party=party).award
    assert a.travelers == 2 and a.award_cost == pytest.approx(2 * (270 + 5.6))
    assert "2 award seats" in a.note


def test_party_mismatch_refused(fam_profile, assumptions):
    solo_priced = make_flight([seg("United", "IAD", "ORD", "2026-11-01T08:00", 120)])
    with pytest.raises(PartyError, match="priced for 1 adult"):
        rank([solo_priced], fam_profile, assumptions, party=build_party(fam_profile, ["me", "alex"]))


def test_solo_unchanged(profile, assumptions):
    f = make_flight([seg("United", "IAD", "ORD", "2026-11-01T08:00", 120)])
    assert score_flight(f, profile, assumptions).per_traveler == score_flight(f, profile, assumptions).effective_cost


# ------------------------------------------------------------------ live price semantics

def test_live_two_adult_prices_are_party_totals():
    one = {f.id: f.price for f in normalize_response(json.loads((FIX / "iad_fra_long.json").read_text()), True)}
    two = normalize_response(json.loads((FIX / "iad_fra_long_2adults.json").read_text()), True,
                             party=PartyCounts(adults=2))
    common = [f for f in two if f.id in one]
    assert len(common) >= 20
    assert all(abs(f.price - 2 * one[f.id]) <= 1.5 for f in common)   # whole-dollar rounding
    assert all(f.party.adults == 2 for f in two)


# ------------------------------------------------------------------ CLI

def test_cli_party_round_trip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ADEONA_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("ADEONA_LOG_FILE", str(tmp_path / "l.jsonl"))
    out = tmp_path / "r.json"
    assert main(["search", "--from", "BWI,IAD,DCA", "--to", "MCO", "--date", "2026-11-14",
                 "--guests", "1", "--children", "1", "--out", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["party"] == {"names": ["me"], "guests": 1, "children": 1, "lap_infants": 0}
    assert data["query"]["adults"] == 2 and data["query"]["children"] == 1
    assert main(["score", "--input", str(out), "--profile", "config/profile.yaml", "--details", "none"]) == 0
    text = capsys.readouterr().out
    assert "Party: me, guest1, child1 (2 adults, 1 child)" in text and "Per trav." in text


def test_cli_rejects_conflicting_party(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ADEONA_CACHE_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("ADEONA_LOG_FILE", str(tmp_path / "l.jsonl"))
    out = tmp_path / "r.json"
    main(["search", "--from", "BWI", "--to", "MCO", "--date", "2026-11-14", "--guests", "1", "--out", str(out)])
    assert main(["score", "--input", str(out), "--profile", "config/profile.yaml", "--guests", "2"]) == 1
    assert "search again" in capsys.readouterr().err


def test_cli_solo_sample_with_party_flags_refused(capsys):
    assert main(["score", "--input", "samples/sample.json", "--profile", "config/profile.yaml",
                 "--guests", "1"]) == 1
    assert "priced for 1 adult" in capsys.readouterr().err
