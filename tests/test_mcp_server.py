import json

import anyio
import pytest

from adeona.budget import CallBudget
from adeona.mcp_server import AdeonaService, build_server
from adeona.party import PartyError
from adeona.providers import get_provider
from adeona.search import FlightSearch


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ADEONA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("ADEONA_LOG_FILE", str(tmp_path / "log.jsonl"))


def service(budget=5) -> AdeonaService:
    return AdeonaService(FlightSearch(get_provider("mock"), CallBudget(budget)),
                         "config/profile.yaml", "config/assumptions.yaml")


def test_search_then_rank_by_search_id():
    svc = service()
    res = svc.search_flights(["bwi", "iad"], ["MCO"], "2026-11-14")
    assert res["ok"] and res["search_id"]
    ranked = svc.rank_flights(search_id=res["search_id"])
    assert ranked["ranked"][0]["flight_id"] == "WN-BWI-MCO-choice"
    for r in ranked["ranked"]:
        assert round(sum(i["amount"] for i in r["items"]), 2) == r["effective_cost"]
    assert svc.budget_status()["used"] == 1


def test_rank_with_raw_flights_matches_search_id():
    svc = service()
    res = svc.search_flights(["BWI", "IAD"], ["MCO"], "2026-11-14")
    by_id = svc.rank_flights(search_id=res["search_id"], top=3)
    raw = svc.rank_flights(flights=res["flights"], top=3)
    assert [r["flight_id"] for r in by_id["ranked"]] == [r["flight_id"] for r in raw["ranked"]]
    assert len(by_id["ranked"]) == 3


def test_rank_needs_exactly_one_source():
    svc = service()
    with pytest.raises(ValueError, match="exactly one"):
        svc.rank_flights()
    with pytest.raises(ValueError, match="unknown search_id"):
        svc.rank_flights(search_id="nope")


def test_budget_exhaustion_is_not_an_exception():
    svc = service(budget=0)
    res = svc.search_flights(["BWI"], ["MCO"], "2026-11-14")
    assert not res["ok"] and res["error"].startswith("budget_exhausted")
    assert "search_id" not in res


def test_party_mismatch_is_refused():
    svc = service()
    res = svc.search_flights(["BWI"], ["MCO"], "2026-11-14", guests=1)
    assert res["party"] == {"names": ["me"], "guests": 1, "children": 0, "lap_infants": 0}
    assert svc.rank_flights(search_id=res["search_id"])["party"]["counts"]["adults"] == 2
    with pytest.raises(PartyError):
        svc.rank_flights(search_id=res["search_id"], children=1)


def test_end_to_end_over_mcp_client():
    from mcp import Client

    server = build_server(provider="mock", budget=3)

    async def run():
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            assert set(tools) == {"search_flights", "fare_details", "rank_flights", "budget_status"}
            assert all(t.annotations.read_only_hint for t in tools.values())
            res = await c.call_tool("search_flights", {"origins": ["BWI"], "destinations": ["MCO"],
                                                       "depart_date": "2026-11-14"})
            assert not res.is_error
            sid = json.loads(res.content[0].text)["search_id"]
            ranked = await c.call_tool("rank_flights", {"search_id": sid, "top": 1})
            return json.loads(ranked.content[0].text)

    out = anyio.run(run)
    assert len(out["ranked"]) == 1 and out["ranked"][0]["effective_cost"] > 0
