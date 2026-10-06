"""Codeshare seller handling (decision 2026-10-06: approach D, bonus follows operating airline)."""

import json
from pathlib import Path

import pytest

from adeona.normalize import apply_fare, choose_fare
from adeona.providers.serpapi import normalize_response, parse_booking_options
from adeona.scoring import score_flight, ticketing_seller
from tests.conftest import make_flight, seg

FIX = Path(__file__).parent / "fixtures" / "serpapi"


def fra(fid):
    raw = json.loads((FIX / "iad_fra_long.json").read_text())
    return next(f for f in normalize_response(raw, exclude_basic=True) if f.id == fid)


def items(r):
    return {i.key: i.amount for i in r.items}


@pytest.fixture
def lh419():
    return fra("gf-b774080543")  # LH 419, ticket_also_sold_by: United, $799


def test_scan_stage_assumes_united_seller(lh419, profile, assumptions):
    assert ticketing_seller(lh419, profile) == ("United", "assumed")
    r = score_flight(lh419, profile, assumptions)
    it = items(r)
    assert it["miles"] == round(-799 * 7 * 1.35 / 100, 2)   # United's own 7/$, not partner 5/$
    assert it["preferred"] == 0                            # bonus follows the operating airline
    assert it["perks"] == 0                                # perks follow the operating airline
    assert it["bags"] == 40                                # no Silver waiver assumed
    assert any(w.startswith("seller assumed: United") for w in r.warnings)


def test_verify_stage_confirms_united(lh419, profile, assumptions):
    opts = parse_booking_options(json.loads((FIX / "booking_lh.json").read_text()))
    for o in opts:  # provider-side price anchor
        if o.is_basic_economy is None and abs(o.price - lh419.price) <= 0.5:
            o.is_basic_economy = False
    verified = apply_fare(lh419, choose_fare(opts))
    assert verified.seller == "United" and "as UA 8826" in verified.notes[-1]
    r = score_flight(verified, profile, assumptions)
    assert ticketing_seller(verified, profile) == ("United", "verified")
    assert not any("assumed" in w for w in r.warnings)
    assert items(r)["bags"] == 90                          # quoted for the United-sold fare
    # test config: earnable fraction 1.0 -> 799*7 = 5,593 mi @1.35c = $75.51
    assert r.effective_cost == pytest.approx(799 + 90 + 191.67 - 75.51)


def test_verify_stage_can_overturn_assumption(lh419, profile, assumptions):
    """If fare details show only Lufthansa sells it, fall back to partner earning."""
    opts = [o for o in parse_booking_options(json.loads((FIX / "booking_lh.json").read_text()))
            if o.book_with == "Lufthansa"]
    opts[0].is_basic_economy = False
    verified = apply_fare(lh419, opts[0])
    assert ticketing_seller(verified, profile) == (None, None)
    miles = next(i for i in score_flight(verified, profile, assumptions).items if i.key == "miles")
    assert "partner Lufthansa" in miles.note and miles.amount == round(-2919 * 5 * 1.35 / 100, 2)


def test_non_loyalty_seller_ignored(profile):
    af = next(f for f in normalize_response(json.loads((FIX / "iad_fra_long.json").read_text()), True)
              if any(s.flight_number == "AF 53" for s in f.segments))
    assert ticketing_seller(af, profile) == (None, None)    # sold by Delta: not my program


def test_seller_must_cover_every_segment(profile):
    f = make_flight([seg("Lufthansa", "IAD", "FRA", "2026-11-14T18:00", 460, num="LH 419", also_sold_by=["United"]),
                     seg("Lufthansa", "FRA", "BER", "2026-11-15T10:00", 70, num="LH 190")])
    assert ticketing_seller(f, profile) == (None, None)


def test_own_metal_unchanged(profile, assumptions):
    f = make_flight([seg("United", "IAD", "FRA", "2026-11-14T17:30", 475, num="UA 989", also_sold_by=["Lufthansa"])],
                    price=799)
    assert ticketing_seller(f, profile) == (None, None)
    assert items(score_flight(f, profile, assumptions))["preferred"] == -30
