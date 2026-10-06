import pytest

from adeona.reference import AirlineReference, load_reference
from adeona.scoring import crediting_airline, score_flight
from tests.conftest import make_flight, seg


@pytest.fixture
def ref():
    return load_reference()


def test_reference_loads_and_is_dated(ref):
    assert ref.as_of == "2026-10-06"
    assert {ref.alliance_by_code[c] for c in ("UA", "AA", "DL")} == {"Star Alliance", "oneworld", "SkyTeam"}


@pytest.mark.parametrize("name,code,alliance", [
    ("United", "UA", "Star Alliance"),
    ("Tap Air Portugal", "TP", "Star Alliance"),      # Google's capitalization
    ("SWISS", "LX", "Star Alliance"),
    ("Turkish Airlines", "TK", "Star Alliance"),
    ("ITA Airways", "AZ", "Star Alliance"),          # joined 2026-04-01
    ("Scandinavian Airlines", "SK", "SkyTeam"),      # moved from Star 2024-09-01
    ("Hawaiian", "HA", "oneworld"),
    ("Aeroflot", "SU", None),                        # suspended
    ("Southwest", "WN", None),                       # unaligned
    ("Chase", None, None),                           # bank, not an airline
])
def test_resolution(ref, name, code, alliance):
    assert ref.resolve(name) == code
    assert ref.alliance_of(name) == alliance


@pytest.mark.parametrize("fn,code", [("LH 419", "LH"), ("UA8826", "UA"), ("F9 4355", "F9"), ("100", None)])
def test_code_from_flight_number(fn, code):
    assert AirlineReference.code_from_flight_number(fn) == code


def test_duplicate_membership_rejected():
    with pytest.raises(ValueError, match="both"):
        AirlineReference.from_dict({"as_of": "x", "alliances": {"A": {"XX": ["X"]}, "B": {"XX": ["X"]}}})


def test_ambiguous_name_rejected():
    with pytest.raises(ValueError, match="maps to both"):
        AirlineReference.from_dict({"as_of": "x", "alliances": {"A": {"XX": ["Same"], "YY": ["Same"]}}})


@pytest.mark.parametrize("airline,num,credits", [
    ("Turkish Airlines", "TK 188", "United"),
    ("SWISS", "LX 73", "United"),
    ("Tap Air Portugal", "TP 232", "United"),
    ("Scandinavian Airlines", "SK 926", None),   # SkyTeam now
    ("Delta", "DL 1440", None),
    ("United", "UA 2045", "United"),             # own metal
    ("Southwest", "WN 1423", "Southwest"),
])
def test_crediting_uses_service_data_not_profile(profile, ref, airline, num, credits):
    f = make_flight([seg(airline, "IAD", "FRA", "2026-11-14T18:00", 460, num=num)])
    assert crediting_airline(f, profile, ref) == credits


def test_partner_line_item_names_alliance(profile, assumptions):
    f = make_flight([seg("Turkish Airlines", "IAD", "IST", "2026-11-14T21:20", 600, num="TK 8")], price=585)
    miles = next(i for i in score_flight(f, profile, assumptions).items if i.key == "miles")
    assert miles.amount == -39.49 and "(Star Alliance)" in miles.note   # 585*5=2925 MP @1.35c


def test_swapped_reference_changes_crediting(profile, assumptions):
    alt = AirlineReference.from_dict({"as_of": "test", "alliances": {"X": {"UA": ["United"], "SK": ["SAS"]}}})
    f = make_flight([seg("SAS", "IAD", "CPH", "2026-11-14T18:00", 480, num="SK 926")])
    assert crediting_airline(f, profile, alt) == "United"
