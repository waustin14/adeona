import json

from adeona.cli import main


def test_score_runs_on_sample_without_api_key(capsys, monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    assert main(["score", "--input", "samples/sample.json", "--profile", "config/profile.yaml"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[2].split()[1] == "WN-BWI-MCO-choice"
    assert "Excluded by hard constraints" in out and "= Effective cost" in out


def test_score_json_output(capsys):
    main(["score", "--input", "samples/sample.json", "--profile", "config/profile.yaml", "--json"])
    data = json.loads(capsys.readouterr().out)
    for r in data["ranked"]:
        assert round(sum(i["amount"] for i in r["items"]), 2) == r["effective_cost"]
    assert {e["flight_id"] for e in data["excluded"]} == {"WN-BWI-MCO-basic", "UA-IAD-MCO-basic", "DL-BWI-ATL-JAX-MCO"}


def test_bad_profile_exits_1_with_message(tmp_path, capsys):
    bad = tmp_path / "p.yaml"
    bad.write_text("home_airports: [BWI]\n")
    assert main(["score", "--input", "samples/sample.json", "--profile", str(bad)]) == 1
    assert "hourly_value: required key is missing" in capsys.readouterr().err


def test_search_then_score_pipeline(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ADEONA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("ADEONA_LOG_FILE", str(tmp_path / "log.jsonl"))
    out = tmp_path / "results.json"
    assert main(["search", "--from", "bwi,iad", "--to", "MCO", "--date", "2026-11-14", "--out", str(out)]) == 0
    assert main(["score", "--input", str(out), "--profile", "config/profile.yaml", "--details", "none"]) == 0
    assert "WN-BWI-MCO-choice" in capsys.readouterr().out
    assert (tmp_path / "log.jsonl").exists()


def test_search_budget_zero_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ADEONA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("ADEONA_LOG_FILE", str(tmp_path / "log.jsonl"))
    assert main(["search", "--from", "BWI", "--to", "MCO", "--date", "2026-11-14", "--budget", "0"]) == 2
    assert '"budget_exhausted' in capsys.readouterr().out


def test_serpapi_without_key_is_clean_error(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here, so the real key isn't picked up
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    assert main(["search", "--provider", "serpapi", "--from", "BWI", "--to", "MCO", "--date", "2026-11-14"]) == 1
    assert "SERPAPI_API_KEY is not set" in capsys.readouterr().err


def test_dotenv_loads_without_overriding(tmp_path, monkeypatch):
    from adeona.env import load_dotenv
    f = tmp_path / ".env"
    f.write_text("# c\nA_NEW=1\nexport B_Q=\"two\"\nA_SET=fromfile\nEMPTY=\n")
    monkeypatch.setenv("A_SET", "fromenv")
    for k in ("A_NEW", "B_Q", "EMPTY"):
        monkeypatch.delenv(k, raising=False)
    load_dotenv(f)
    import os
    assert os.environ["A_NEW"] == "1" and os.environ["B_Q"] == "two"
    assert os.environ["A_SET"] == "fromenv" and "EMPTY" not in os.environ
