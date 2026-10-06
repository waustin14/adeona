"""Load and validate the traveler profile and the assumptions config.

Both loaders raise ConfigError with one human-readable line per problem.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

IATA = re.compile(r"^[A-Z]{3}$")


class ConfigError(Exception):
    def __init__(self, path: str | Path, problems: list[str]):
        self.path = str(path)
        self.problems = problems
        super().__init__(f"Invalid config {self.path}:\n" + "\n".join(f"  - {p}" for p in problems))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- profile


class LoyaltyAccount(_Strict):
    status: str
    program: str
    cpp: float = Field(gt=0, le=10, description="cents per point you value this currency at")


class HardConstraints(_Strict):
    max_stops: int = Field(default=1, ge=0)
    min_layover_min: int = Field(default=45, ge=0)
    no_basic_economy: bool = True


TravelerType = Literal["adult", "child", "infant_in_seat", "infant_on_lap"]


class Traveler(_Strict):
    """A regular companion. Omitted values fall back as noted."""

    type: TravelerType = "adult"
    hourly_value: Optional[float] = Field(default=None, ge=0)  # adult: yours; child/infant: child_hourly_value
    bags_checked: Optional[int] = Field(default=None, ge=0, le=4)  # adult/child: yours; infant: 0
    loyalty: dict[str, LoyaltyAccount] = Field(default_factory=dict)


class Profile(_Strict):
    home_airports: list[str] = Field(min_length=1)
    hourly_value: float = Field(ge=0)
    preferred_airlines: list[str] = Field(default_factory=list)
    avoid_airlines: list[str] = Field(default_factory=list)
    preferred_airline_bonus: float = Field(default=0, ge=0)
    loyalty: dict[str, LoyaltyAccount] = Field(default_factory=dict)
    bags_checked: int = Field(default=0, ge=0, le=4)
    hard_constraints: HardConstraints = Field(default_factory=HardConstraints)
    # Regular companions, picked per trip with --travelers. "me" is you (the top-level fields).
    travelers: dict[str, Traveler] = Field(default_factory=dict)
    child_hourly_value: float = Field(default=0, ge=0)  # children/infants without their own value

    @field_validator("home_airports")
    @classmethod
    def _iata(cls, v: list[str]) -> list[str]:
        out = [a.strip().upper() for a in v]
        bad = [orig for orig, a in zip(v, out) if not IATA.match(a)]
        if bad:
            raise ValueError(f"not 3-letter IATA codes: {bad}")
        return out

    @model_validator(mode="before")
    @classmethod
    def _moved_keys(cls, data: Any) -> Any:
        if isinstance(data, dict) and "alliances" in data:
            raise ValueError(
                "'alliances' is no longer a profile setting: alliance membership is service "
                "reference data (src/adeona/data/airlines.yaml). Delete this key."
            )
        return data

    @model_validator(mode="after")
    def _cross_checks(self) -> "Profile":
        if "me" in self.travelers:
            raise ValueError("travelers: 'me' is reserved for you (the top-level profile fields)")
        overlap = set(self.preferred_airlines) & set(self.avoid_airlines)
        if overlap:
            raise ValueError(f"airlines listed as both preferred and avoided: {sorted(overlap)}")
        return self

    # -- helpers used by the scorer
    def account_for_airline(self, airline: str) -> Optional[LoyaltyAccount]:
        return self.loyalty.get(airline)

    def account_for_program(self, program: str) -> Optional[LoyaltyAccount]:
        for acct in self.loyalty.values():
            if acct.program.lower() == program.lower():
                return acct
        return None


# ------------------------------------------------------------ assumptions


class EarnRule(_Strict):
    # points/miles per USD of earnable fare, keyed by fare brand; "default" is the fallback.
    per_dollar_by_brand: dict[str, float]
    status_bonus_pct: dict[str, float] = Field(default_factory=dict)


class PerkValues(_Strict):
    priority_boarding: float = 0
    upgrade_odds: float = 0
    lounge: float = 0

    @property
    def total(self) -> float:
        return self.priority_boarding + self.upgrade_odds + self.lounge


class AirlineFees(_Strict):
    first_checked_bag: float = Field(ge=0)
    second_checked_bag: float = Field(ge=0)
    seat_selection_basic: float = Field(default=0, ge=0)
    # free checked bags granted by status, keyed by the status string in the profile
    status_free_bags: dict[str, int] = Field(default_factory=dict)
    # free bags that status extends to companions on the same reservation (absent = none)
    companion_free_bags: dict[str, int] = Field(default_factory=dict)


class LayoverPenalties(_Strict):
    short_connection_under_min: int = Field(ge=0)
    short_connection: float = Field(ge=0)
    overnight: float = Field(ge=0)
    airport_change: float = Field(ge=0)


class Assumptions(_Strict):
    earnable_fraction_of_price: float = Field(gt=0, le=1)
    earn: dict[str, EarnRule]  # keyed by airline
    partner_earn_per_dollar: dict[str, float] = Field(default_factory=dict)  # keyed by crediting airline
    status_perks: dict[str, dict[str, PerkValues]] = Field(default_factory=dict)  # airline -> status -> perks
    fees: dict[str, AirlineFees]
    default_fees: AirlineFees
    layover_penalties: LayoverPenalties
    red_eye_penalty: float = Field(default=0, ge=0)  # $ per overnight flight segment
    # False: layover/red-eye penalties are charged once per trip; True: once per traveler.
    penalties_per_traveler: bool = False

    def fees_for(self, airline: str) -> AirlineFees:
        return self.fees.get(airline, self.default_fees)


# ---------------------------------------------------------------- loading


def _format_errors(err: ValidationError) -> list[str]:
    out = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "(root)"
        msg = e["msg"].removeprefix("Value error, ")
        if e["type"] == "extra_forbidden":
            msg = "unknown key (typo?)"
        elif e["type"] == "missing":
            msg = "required key is missing"
        out.append(f"{loc}: {msg}")
    return out


def _read_yaml(path: str | Path) -> Any:
    p = Path(path)
    if not p.exists():
        raise ConfigError(p, [f"file not found: {p.resolve()}"])
    try:
        data = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise ConfigError(p, [f"YAML syntax error{where}: {getattr(e, 'problem', e)}"]) from None
    if not isinstance(data, dict):
        raise ConfigError(p, ["top level must be a mapping of keys to values"])
    return data


def load_profile(path: str | Path) -> Profile:
    data = _read_yaml(path)
    try:
        return Profile.model_validate(data)
    except ValidationError as e:
        raise ConfigError(path, _format_errors(e)) from None


def load_assumptions(path: str | Path) -> Assumptions:
    data = _read_yaml(path)
    try:
        return Assumptions.model_validate(data)
    except ValidationError as e:
        raise ConfigError(path, _format_errors(e)) from None


def profile_warnings(profile: Profile, assumptions: Assumptions) -> list[str]:
    """Non-fatal mismatches between profile and assumptions (e.g. unknown status names)."""
    warns = []
    for airline, acct in profile.loyalty.items():
        if airline in assumptions.earn:
            bonus = assumptions.earn[airline].status_bonus_pct
            if bonus and acct.status not in bonus:
                warns.append(f"{airline} status '{acct.status}' has no earn bonus in assumptions; using 0%")
        fees = assumptions.fees.get(airline)
        if fees and fees.status_free_bags and acct.status not in fees.status_free_bags:
            warns.append(f"{airline} status '{acct.status}' has no bag waiver in assumptions; assuming none")
    return warns
