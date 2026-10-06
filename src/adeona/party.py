"""Who is travelling: you, named companions from the profile, and ad hoc guests."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import PartyCounts
from .profile import LoyaltyAccount, Profile


class PartyError(ValueError):
    pass


@dataclass(frozen=True)
class TravelerSpec:
    name: str
    type: str  # adult | child | infant_in_seat | infant_on_lap
    hourly_value: float
    bags_checked: int
    loyalty: dict[str, LoyaltyAccount] = field(default_factory=dict)

    @property
    def seated(self) -> bool:
        return self.type != "infant_on_lap"


@dataclass(frozen=True)
class Party:
    travelers: tuple[TravelerSpec, ...]

    @property
    def counts(self) -> PartyCounts:
        n = {t: sum(1 for x in self.travelers if x.type == t)
             for t in ("adult", "child", "infant_in_seat", "infant_on_lap")}
        return PartyCounts(adults=n["adult"], children=n["child"],
                           infants_in_seat=n["infant_in_seat"], infants_on_lap=n["infant_on_lap"])

    @property
    def size(self) -> int:
        return len(self.travelers)

    def describe(self) -> dict:
        return {"travelers": [t.name for t in self.travelers], "counts": self.counts.model_dump()}


def solo(profile: Profile) -> Party:
    return build_party(profile)


def build_party(profile: Profile, names: list[str] | None = None, guests: int = 0,
                children: int = 0, lap_infants: int = 0) -> Party:
    """`names` from profile.travelers ("me" = you); guests have no loyalty accounts.

    Defaults: adults use your hourly value and bag count; children and infants use
    profile.child_hourly_value; infants check no bags; children check your bag count.
    """
    names = names or ["me"]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise PartyError(f"traveler listed twice: {sorted(dupes)}")
    out: list[TravelerSpec] = []
    for name in names:
        if name == "me":
            out.append(TravelerSpec("me", "adult", profile.hourly_value, profile.bags_checked, dict(profile.loyalty)))
            continue
        t = profile.travelers.get(name)
        if t is None:
            known = ", ".join(["me", *profile.travelers]) or "me"
            raise PartyError(f"unknown traveler '{name}' (profile has: {known})")
        out.append(_spec(profile, name, t.type, t.hourly_value, t.bags_checked, t.loyalty))
    out += [_spec(profile, f"guest{i + 1}", "adult") for i in range(guests)]
    out += [_spec(profile, f"child{i + 1}", "child") for i in range(children)]
    out += [_spec(profile, f"lap_infant{i + 1}", "infant_on_lap") for i in range(lap_infants)]
    if not any(t.type == "adult" for t in out):
        raise PartyError("a party needs at least one adult")
    party = Party(tuple(out))
    c = party.counts
    if c.infants_on_lap > c.adults:
        raise PartyError("each lap infant needs an adult")
    if party.size > 9:
        raise PartyError(f"party of {party.size} exceeds the 9-passenger search limit")
    return party


def _spec(profile: Profile, name: str, kind: str, hourly=None, bags=None, loyalty=None) -> TravelerSpec:
    if hourly is None:
        hourly = profile.hourly_value if kind == "adult" else profile.child_hourly_value
    if bags is None:
        bags = 0 if kind.startswith("infant") else profile.bags_checked
    return TravelerSpec(name, kind, hourly, bags, dict(loyalty or {}))
