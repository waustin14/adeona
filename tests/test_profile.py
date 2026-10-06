import textwrap

import pytest

from adeona.profile import ConfigError, load_assumptions, load_profile, profile_warnings


def write(tmp_path, text):
    p = tmp_path / "profile.yaml"
    p.write_text(textwrap.dedent(text))
    return p


GOOD = """
home_airports: [bwi, IAD]
hourly_value: 25
preferred_airlines: [United]
loyalty:
  United: {status: MileagePlus Silver, program: MileagePlus, cpp: 1.35}
bags_checked: 1
hard_constraints: {max_stops: 1, min_layover_min: 45, no_basic_economy: true}
"""


def test_shipped_configs_load():
    p = load_profile("config/profile.yaml")
    a = load_assumptions("config/assumptions.yaml")
    assert p.home_airports == ["BWI", "IAD", "DCA"]
    assert profile_warnings(p, a) == []


def test_good_profile_normalizes_iata(tmp_path):
    assert load_profile(write(tmp_path, GOOD)).home_airports == ["BWI", "IAD"]


def test_original_spec_typo_gives_clear_error(tmp_path):
    # The spec had a period instead of a comma: "program: Ultimate Rewards. cpp: 1.5"
    bad = GOOD.replace(
        "bags_checked:",
        "  Chase: {status: Sapphire Reserve, program: Ultimate Rewards. cpp: 1.5}\nbags_checked:",
    )
    assert bad != GOOD
    with pytest.raises(ConfigError) as e:
        load_profile(write(tmp_path, bad))
    assert "line" in str(e.value) or "loyalty.Chase" in str(e.value)


def test_missing_required_key(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_profile(write(tmp_path, GOOD.replace("hourly_value: 25\n", "")))
    assert "hourly_value: required key is missing" in e.value.problems


def test_typo_key_rejected(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_profile(write(tmp_path, GOOD.replace("bags_checked", "bag_checked")))
    assert "bag_checked: unknown key (typo?)" in e.value.problems


def test_bad_values_all_reported(tmp_path):
    bad = GOOD.replace("hourly_value: 25", "hourly_value: -5").replace("[bwi, IAD]", "[BWI, Dulles]")
    with pytest.raises(ConfigError) as e:
        load_profile(write(tmp_path, bad))
    msgs = " | ".join(e.value.problems)
    assert "hourly_value" in msgs and "Dulles" in msgs


def test_negative_cpp_rejected(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_profile(write(tmp_path, GOOD.replace("cpp: 1.35", "cpp: -1")))
    assert any(p.startswith("loyalty.United.cpp") for p in e.value.problems)


def test_preferred_and_avoided_conflict(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_profile(write(tmp_path, GOOD + "avoid_airlines: [United]\n"))
    assert "both preferred and avoided" in str(e.value)


def test_missing_file():
    with pytest.raises(ConfigError, match="file not found"):
        load_profile("nope.yaml")


def test_unknown_status_warns(tmp_path):
    p = load_profile(write(tmp_path, GOOD.replace("MileagePlus Silver", "Premier Gold")))
    a = load_assumptions("config/assumptions.yaml")
    assert any("Premier Gold" in w for w in profile_warnings(p, a))


def test_alliances_key_rejected_with_pointer(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_profile(write(tmp_path, GOOD + "alliances: {StarAlliance: [United, Lufthansa]}\n"))
    msg = str(e.value)
    assert "no longer a profile setting" in msg and "data/airlines.yaml" in msg
