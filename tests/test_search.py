import json

import httpx
import pytest

from adeona.budget import CallBudget
from adeona.cache import FileCache
from adeona.calllog import CallLog
from adeona.models import SearchQuery
from adeona.providers import MockProvider, ProviderError
from adeona.providers.serpapi import SerpApiProvider, normalize_response, stops_param
from adeona.search import SEARCH_FLIGHTS_TOOL, FlightSearch

Q = {"origins": ["BWI", "IAD", "DCA"], "destinations": ["MCO"], "depart_date": "2026-11-14"}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    provider = MockProvider()
    log_path = tmp_path / "calls.jsonl"
    def make(budget=3):
        return FlightSearch(provider, CallBudget(budget), FileCache(tmp_path / "cache", clock=clock),
                            CallLog(log_path))
    return make, provider, clock, log_path


def log_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_returns_normalized_json(env):
    make, provider, *_ = env
    out = make()(Q)
    assert out["ok"] and out["provider"] == "mock"
    assert len(out["flights"]) == 10
    f = out["flights"][0]
    for k in ("price", "airline", "stops", "total_duration_min", "layovers", "fare_brand", "bag_rules", "segments"):
        assert k in f
    json.dumps(out)  # fully serializable


def test_cache_hit_skips_provider_and_budget(env):
    make, provider, _, log_path = env
    s = make(budget=1)
    s(Q)
    second = s(Q)
    assert second["ok"] and second["cache_hit"] and provider.calls == 1
    assert second["budget_remaining"] == 0
    assert [r["cache_hit"] for r in log_lines(log_path)] == [False, True]


def test_cache_expires_after_one_hour(env):
    make, provider, clock, _ = env
    s = make()
    s(Q)
    clock.t += 3599
    s(Q)
    assert provider.calls == 1
    clock.t += 2
    s(Q)
    assert provider.calls == 2


def test_budget_exhaustion_fails_safely(env):
    make, provider, _, log_path = env
    s = make(budget=1)
    s(Q)
    out = s({**Q, "destinations": ["TPA"]})
    assert out["ok"] is False and out["error"].startswith("budget_exhausted")
    assert out["flights"] == [] and provider.calls == 1
    assert log_lines(log_path)[-1]["error"].startswith("budget_exhausted")


def test_zero_budget_never_calls(env):
    make, provider, *_ = env
    assert make(budget=0)(Q)["ok"] is False and provider.calls == 0


def test_provider_error_is_returned_not_raised(env, tmp_path):
    class Boom:
        name, cost_per_call_usd = "boom", 0.01
        def search(self, q):
            raise ProviderError("HTTP 503 from upstream")
    s = FlightSearch(Boom(), CallBudget(2), FileCache(tmp_path / "c2"), CallLog(tmp_path / "l2.jsonl"))
    out = s(Q)
    assert out == {**out, "ok": False, "error": "HTTP 503 from upstream", "budget_remaining": 1}
    rec = log_lines(tmp_path / "l2.jsonl")[0]
    assert rec["cost_estimate_usd"] == 0.01 and rec["result_count"] == 0


def test_log_records_params_count_cost(env):
    make, _, _, log_path = env
    make()(Q)
    rec = log_lines(log_path)[0]
    assert rec["params"]["origins"] == ["BWI", "IAD", "DCA"]
    assert rec["result_count"] == 10 and rec["cost_estimate_usd"] == 0.0 and "ts" in rec


def test_log_redacts_secrets(tmp_path):
    log = CallLog(tmp_path / "x.jsonl")
    log.write(provider="p", params={"api_key": "sk-secret", "q": 1})
    text = (tmp_path / "x.jsonl").read_text()
    assert "sk-secret" not in text and '"***"' in text


def test_mock_filters(env):
    make, *_ = env
    out = make()({**Q, "origins": ["BWI"], "max_stops": 0, "exclude_basic": True})
    ids = {f["id"] for f in out["flights"]}
    assert ids == {"WN-BWI-MCO-choice", "NK-BWI-MCO"}


def test_tool_schema_is_valid_json_schema():
    assert SEARCH_FLIGHTS_TOOL["input_schema"]["required"] == ["origins", "destinations", "depart_date"]


# ---------------------------------------------------------------- SerpApi adapter

SERP_PAYLOAD = {
    "best_flights": [{
        "flights": [
            {"departure_airport": {"name": "Dulles", "id": "IAD", "time": "2026-11-14 21:10"},
             "arrival_airport": {"name": "Newark", "id": "EWR", "time": "2026-11-14 22:25"},
             "duration": 75, "airline": "United", "flight_number": "UA 3391", "travel_class": "Economy",
             "extensions": ["Operated by Mesa Airlines"]},
            {"departure_airport": {"name": "Newark", "id": "EWR", "time": "2026-11-15 06:00"},
             "arrival_airport": {"name": "Orlando", "id": "MCO", "time": "2026-11-15 08:45"},
             "duration": 165, "airline": "United", "flight_number": "UA 1610", "travel_class": "Economy"},
        ],
        "layovers": [{"duration": 455, "name": "Newark", "id": "EWR", "overnight": True}],
        "total_duration": 695, "price": 142, "type": "One way", "extensions": ["Wi-Fi for a fee"],
    }],
    "other_flights": [{"flights": [
        {"departure_airport": {"id": "BWI", "time": "2026-11-14 07:05"},
         "arrival_airport": {"id": "MCO", "time": "2026-11-14 09:20"},
         "duration": 135, "airline": "Southwest", "flight_number": "WN 1423"}],
        "total_duration": 135}],  # no price -> dropped
}


def test_serpapi_normalization():
    flights = normalize_response(SERP_PAYLOAD, exclude_basic=True)
    assert len(flights) == 1
    f = flights[0]
    assert f.price == 142 and f.stops == 1 and f.total_duration_min == 695
    assert f.layovers[0].overnight and f.layovers[0].duration_min == 455
    assert f.segments[0].operating_airline == "Mesa Airlines"
    assert f.is_basic_economy is False


def test_serpapi_unknown_basic_without_exclude():
    assert normalize_response(SERP_PAYLOAD)[0].is_basic_economy is None


def test_serpapi_stops_mapping():
    assert [stops_param(x) for x in (None, 0, 1, 2, 5)] == [0, 1, 2, 3, 3]


def test_serpapi_requires_key(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="SERPAPI_API_KEY"):
        SerpApiProvider()


def test_serpapi_request_and_key_not_in_error(monkeypatch):
    seen = {}
    def handler(req: httpx.Request):
        seen.update(dict(req.url.params))
        return httpx.Response(401, json={"error": "Invalid API key"})
    p = SerpApiProvider(api_key="sk-secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError) as e:
        p.search(SearchQuery(**Q, max_stops=1, exclude_basic=True))
    assert "sk-secret" not in str(e.value)
    assert seen["departure_id"] == "BWI,IAD,DCA" and seen["type"] == "2"
    assert seen["stops"] == "2" and seen["exclude_basic"] == "true"


def test_serpapi_rejects_round_trip_for_now():
    p = SerpApiProvider(api_key="x", client=httpx.Client())
    with pytest.raises(ProviderError, match="round trips"):
        p.build_params(SearchQuery(**Q, return_date="2026-11-20"))
