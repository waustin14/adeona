"""Deterministic effective-cost scoring. Pure functions; no I/O, no LLM judgment.

effective_cost = cash + bag/seat fees + time cost + layover penalties
                 - value of miles earned - status perk value - preferred airline bonus

Lower is better. Every score carries line items that sum exactly to the total.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from .models import Flight
from .party import Party, PartyError, TravelerSpec, solo
from .profile import Assumptions, LoyaltyAccount, Profile
from .reference import AirlineReference, load_reference


@dataclass(frozen=True)
class LineItem:
    key: str
    label: str
    amount: float  # signed $; negative = credit
    note: str = ""


@dataclass(frozen=True)
class AwardAnalysis:
    program: str
    points: int
    taxes: float
    cpp_baseline: Optional[float]
    award_cost: Optional[float]  # points valued at your cpp + taxes
    implied_cpp: float  # cents per point this redemption yields vs. paying cash
    beats_baseline: Optional[bool]
    note: str = ""
    travelers: int = 1  # award seats needed; points/taxes above are per seat, award_cost is the total


@dataclass(frozen=True)
class ScoreResult:
    flight: Flight
    effective_cost: float
    items: list[LineItem]
    warnings: list[str] = field(default_factory=list)
    award: Optional[AwardAnalysis] = None
    party: Optional[Party] = None

    @property
    def per_traveler(self) -> float:
        return round(self.effective_cost / (self.party.size if self.party else 1), 2)

    def to_dict(self) -> dict:
        return {
            "flight_id": self.flight.id,
            "airline": self.flight.airline,
            "route": self.flight.route,
            "effective_cost": self.effective_cost,
            "per_traveler": self.per_traveler,
            "party": self.party.describe() if self.party else None,
            "items": [asdict(i) for i in self.items],
            "warnings": self.warnings,
            "award": asdict(self.award) if self.award else None,
        }


@dataclass(frozen=True)
class Exclusion:
    flight: Flight
    reasons: list[str]


@dataclass(frozen=True)
class Ranking:
    ranked: list[ScoreResult]
    excluded: list[Exclusion]


# ------------------------------------------------------------ hard constraints


def check_constraints(flight: Flight, profile: Profile) -> tuple[list[str], list[str]]:
    """Return (violations, warnings). Any violation removes the flight before scoring."""
    hc = profile.hard_constraints
    violations: list[str] = []
    warnings: list[str] = []

    if flight.stops > hc.max_stops:
        violations.append(f"{flight.stops} stops > max {hc.max_stops}")
    for lo in flight.layovers:
        if lo.duration_min < hc.min_layover_min:
            violations.append(f"{lo.duration_min}m layover at {lo.airport} < min {hc.min_layover_min}m")
    if hc.no_basic_economy:
        if flight.is_basic_economy:
            violations.append(f"basic economy ({flight.fare_brand or 'flagged by source'})")
        elif flight.is_basic_economy is None:
            warnings.append("fare brand unknown: could be basic economy")
    avoided = sorted(flight.airlines & set(profile.avoid_airlines))
    if avoided:
        violations.append(f"avoided airline: {', '.join(avoided)}")
    if len(flight.layovers) != flight.stops:
        warnings.append(f"layover data incomplete ({len(flight.layovers)} layovers for {flight.stops} stops)")
    return violations, warnings


# ------------------------------------------------------------- line items
#
# Party rules (decisions 2026-10-06): cash is the party total; bags, seats, time, miles and
# perks are per traveler; the preferred-airline bonus is once per trip; layover/red-eye
# penalties are once per trip unless assumptions.penalties_per_traveler.


def _fee_for_bag(n: int, flight: Flight, fees) -> float:
    if n == 1:
        fee = flight.bag_rules.first_checked_fee
        return fees.first_checked_bag if fee is None else fee
    fee = flight.bag_rules.second_checked_fee
    return fees.second_checked_bag if fee is None else fee


def bag_fees(flight: Flight, party: Party, a: Assumptions) -> LineItem:
    if all(t.bags_checked == 0 for t in party.travelers):
        return LineItem("bags", "Checked bag fees", 0.0, "no checked bags")
    airline = flight.airline
    fees = a.fees_for(airline)
    included = flight.bag_rules.checked_bags_included
    # Status waivers apply only when the primary carrier is the one the status is with.
    own = {t.name: fees.status_free_bags.get(t.loyalty[airline].status, 0) if airline in t.loyalty else 0
           for t in party.travelers}
    # A status holder's waiver may extend to companions on the same reservation (assumed: one booking).
    holders = [(fees.companion_free_bags.get(t.loyalty[airline].status, 0), t) for t in party.travelers
               if airline in t.loyalty]
    companion, holder = max(holders, key=lambda h: h[0], default=(0, None))

    total, parts = 0.0, []
    for t in party.travelers:
        if t.bags_checked == 0:
            continue
        via_companion = companion if holder is not None and holder is not t else 0
        free = max(included, own[t.name], via_companion)
        cost = sum(_fee_for_bag(n, flight, fees) for n in range(free + 1, t.bags_checked + 1))
        total += cost
        why = []
        if included:
            why.append(f"{included} included in fare")
        if own[t.name]:
            why.append(f"{t.loyalty[airline].status} covers up to {own[t.name]}")
        elif via_companion:
            whose = "your" if holder.name == "me" else f"{holder.name}'s"  # type: ignore[union-attr]
            why.append(f"{whose} {holder.loyalty[airline].status} covers {via_companion}")  # type: ignore[union-attr]
        if party.size == 1:
            parts.append("; ".join([f"{t.bags_checked} bag(s)", *why]))
        else:
            parts.append(f"{t.name} {t.bags_checked} bag(s) ${cost:g}" + (f" ({', '.join(why)})" if why else ""))
    return LineItem("bags", "Checked bag fees", round(total, 2), "; ".join(parts))


def seat_fees(flight: Flight, party: Party, a: Assumptions) -> LineItem:
    seats = party.counts.seated
    suffix = f" x {seats} seats" if seats > 1 else ""
    if flight.seat_selection_fee is not None:
        return LineItem("seat", "Seat selection", flight.seat_selection_fee * seats, "quoted by source" + suffix)
    if flight.is_basic_economy:
        fee = a.fees_for(flight.airline).seat_selection_basic
        return LineItem("seat", "Seat selection", fee * seats, "basic economy, assumed fee" + suffix)
    return LineItem("seat", "Seat selection", 0.0, "included")


def time_cost(flight: Flight, party: Party) -> LineItem:
    hours = flight.total_duration_min / 60
    rate = sum(t.hourly_value for t in party.travelers)
    if party.size == 1:
        note = f"{hours:.2f}h x ${rate:g}/h"
    else:
        note = f"{hours:.2f}h x (" + " + ".join(f"${t.hourly_value:g} {t.name}" for t in party.travelers) + ")"
    return LineItem("time", "Travel time", round(hours * rate, 2), note)


def layover_penalties(flight: Flight, profile: Profile, a: Assumptions, mult: int = 1) -> list[LineItem]:
    p = a.layover_penalties
    tag = f" (x{mult} travelers)" if mult > 1 else ""
    items = []
    for lo in flight.layovers:
        if profile.hard_constraints.min_layover_min <= lo.duration_min < p.short_connection_under_min:
            items.append(LineItem("layover_short", "Short connection", p.short_connection * mult,
                                  f"{lo.duration_min}m at {lo.airport}{tag}"))
        if lo.overnight:
            items.append(LineItem("layover_overnight", "Overnight layover", p.overnight * mult,
                                  f"at {lo.airport}{tag}"))
        if lo.airport_change:
            items.append(LineItem("layover_airport_change", "Airport change", p.airport_change * mult,
                                  f"{lo.airport} -> {lo.departs_from or '?'}{tag}"))
    return items


def red_eye(flight: Flight, a: Assumptions, mult: int = 1) -> list[LineItem]:
    eyes = [s for s in flight.segments if s.red_eye]
    if not eyes or a.red_eye_penalty == 0:
        return []
    return [LineItem("red_eye", "Red-eye flight", a.red_eye_penalty * len(eyes) * mult,
                     ", ".join(s.flight_number for s in eyes) + (f" (x{mult} travelers)" if mult > 1 else ""))]


def primary_code(flight: Flight, ref: AirlineReference) -> Optional[str]:
    seg = next(s for s in flight.segments if s.airline == flight.airline)
    return ref.code_for_segment(seg.airline, seg.flight_number)


def _crediting(flight: Flight, loyalty: dict[str, LoyaltyAccount], ref: AirlineReference) -> Optional[str]:
    if flight.airline in loyalty:
        return flight.airline
    alliance = ref.alliance_by_code.get(primary_code(flight, ref) or "")
    if alliance is None:
        return None
    for mine in loyalty:
        code = ref.resolve(mine)
        if code and ref.alliance_by_code.get(code) == alliance:
            return mine
    return None


def crediting_airline(flight: Flight, profile: Profile, ref: AirlineReference) -> Optional[str]:
    """Which of my loyalty airlines this flight credits to.

    Own airline first; otherwise one of my airlines in the same alliance, per the
    service's reference data (never the profile).
    """
    return _crediting(flight, profile.loyalty, ref)


def _same_airline(x: str, y: str, ref: AirlineReference) -> bool:
    return x == y or (ref.resolve(x) is not None and ref.resolve(x) == ref.resolve(y))


def _party_loyalty(party: Party) -> dict[str, LoyaltyAccount]:
    merged: dict[str, LoyaltyAccount] = {}
    for t in party.travelers:
        for airline, acct in t.loyalty.items():
            merged.setdefault(airline, acct)
    return merged


def _seller(flight: Flight, loyalty: dict[str, LoyaltyAccount],
            ref: AirlineReference) -> tuple[Optional[str], Optional[str]]:
    if flight.seller:
        mine = next((m for m in loyalty if _same_airline(m, flight.seller, ref)), None)
        return (mine, "verified") if mine and not _same_airline(mine, flight.airline, ref) else (None, None)
    if flight.airline in loyalty:
        return None, None
    for mine in loyalty:
        if all(_same_airline(mine, s.airline, ref) or any(_same_airline(mine, x, ref) for x in s.also_sold_by)
               for s in flight.segments):
            return mine, "assumed"
    return None, None


def ticketing_seller(flight: Flight, profile: Profile, ref: Optional[AirlineReference] = None,
                     party: Optional[Party] = None) -> tuple[Optional[str], Optional[str]]:
    """Which loyalty airline (of anyone in the party) sells this itinerary, and how we know.

    Returns (airline name, "verified" | "assumed") or (None, None).
      verified: fare details showed the chosen fare is sold by that airline.
      assumed:  scan stage; every segment is flown or codeshare-sold by that airline
                (Google `ticket_also_sold_by`), so we assume you'd buy from it at the
                search price. Confirm with --fare-details.
    Only matters when the seller differs from the operating airline; own-metal is unchanged.
    """
    return _seller(flight, _party_loyalty(party or solo(profile)), ref or load_reference())


def _miles_for(flight: Flight, t: TravelerSpec, fare: float, seller: Optional[str], basis: Optional[str],
               a: Assumptions, ref: AirlineReference, key: str, label: str) -> LineItem:
    primary = flight.airline
    # Sold by one of their airlines (e.g. a United-issued ticket on a Lufthansa flight): earn
    # at that airline's own revenue rate. [SRC: United 016 tickets on partners earn per $]
    sold = seller if seller in t.loyalty else None
    crediting = sold or _crediting(flight, t.loyalty, ref)
    if crediting is None:
        return LineItem(key, label, 0.0, f"no program credits {primary}")
    acct = t.loyalty[crediting]
    earnable = fare * a.earnable_fraction_of_price
    if crediting == primary or sold:
        rule = a.earn.get(crediting)
        if rule is None:
            return LineItem(key, label, 0.0, f"no earn rule for {crediting} in assumptions")
        brand = flight.fare_brand or "default"
        base = rule.per_dollar_by_brand.get(brand, rule.per_dollar_by_brand.get("default", 0))
        bonus = rule.status_bonus_pct.get(acct.status, 0)
        rate = base * (1 + bonus / 100)
        how = f"{rate:g}/$ ({brand}, +{bonus:g}% {acct.status})"
        if sold:
            how += f"; sold by {sold} ({basis}), flown by {primary}"
    else:
        rate = a.partner_earn_per_dollar.get(crediting, 0)
        how = f"partner {primary} ({ref.alliance_of(crediting)}) -> {acct.program} @ {rate:g}/$"
    miles = round(earnable * rate)
    return LineItem(key, label, -round(miles * acct.cpp / 100, 2),
                    f"{miles:,} {acct.program} pts @ {acct.cpp:g}c; {how}")


def miles_value(flight: Flight, party: Party, a: Assumptions,
                ref: Optional[AirlineReference] = None) -> list[LineItem]:
    """One line per traveler with loyalty accounts ("miles" for you, "miles:<name>" for others).

    [ASSUMPTION] Each seated traveler's ticket = party price / seats; lap infants earn nothing.
    """
    ref = ref or load_reference()
    seller, basis = _seller(flight, _party_loyalty(party), ref)
    fare = flight.price / max(party.counts.seated, 1)
    out = []
    for t in party.travelers:
        is_me = t.name == "me"
        if not is_me and (not t.loyalty or not t.seated):
            continue
        key, label = ("miles", "Miles earned") if is_me else (f"miles:{t.name}", f"Miles earned ({t.name})")
        out.append(_miles_for(flight, t, fare, seller, basis, a, ref, key, label))
    return out


def status_perks(flight: Flight, party: Party, a: Assumptions) -> LineItem:
    total, who = 0.0, []
    for t in party.travelers:
        acct = t.loyalty.get(flight.airline)
        perks = a.status_perks.get(flight.airline, {}).get(acct.status) if acct else None
        if perks and perks.total:
            total += perks.total
            who.append(acct.status if party.size == 1 else f"{t.name}: {acct.status}")  # type: ignore[union-attr]
    if not who:
        return LineItem("perks", "Status perks", 0.0, "none")
    return LineItem("perks", "Status perks", -total, "; ".join(who))


def preferred_bonus(flight: Flight, profile: Profile) -> LineItem:
    if flight.airline in profile.preferred_airlines:
        return LineItem("preferred", "Preferred airline", -profile.preferred_airline_bonus, flight.airline)
    return LineItem("preferred", "Preferred airline", 0.0, "")


def analyze_award(flight: Flight, profile: Profile, party: Optional[Party] = None) -> Optional[AwardAnalysis]:
    aw = flight.award
    if aw is None:
        return None
    n = (party or solo(profile)).counts.seated  # [ASSUMPTION] lap infants not priced
    implied = round((flight.price - aw.taxes * n) / (aw.points * n) * 100, 2)
    acct = profile.account_for_program(aw.program)
    if acct is None:
        return AwardAnalysis(aw.program, aw.points, aw.taxes, None, None, implied, None,
                             "no cpp for this program", n)
    cost = round(n * (aw.points / 100 * acct.cpp + aw.taxes), 2)
    beats = cost < flight.price
    note = (f"redeem: {implied:g}c/pt beats your {acct.cpp:g}c baseline" if beats
            else f"pay cash: {implied:g}c/pt is below your {acct.cpp:g}c baseline")
    if n > 1:
        note += f"; needs {n} award seats (availability unverified)"
    return AwardAnalysis(aw.program, aw.points, aw.taxes, acct.cpp, cost, implied, beats, note, n)


# ---------------------------------------------------------------- entry points


def score_flight(flight: Flight, profile: Profile, a: Assumptions, warnings: Optional[list[str]] = None,
                 ref: Optional[AirlineReference] = None, party: Optional[Party] = None) -> ScoreResult:
    party = party or solo(profile)
    if flight.party != party.counts:
        raise PartyError(f"{flight.id} was priced for {flight.party.label()}, but the party is "
                         f"{party.counts.label()}; search again with this party")
    mult = party.size if a.penalties_per_traveler else 1
    items = [
        LineItem("cash", "Cash price", flight.price,
                 " ".join(x for x in (flight.fare_brand, f"(total, {party.counts.label()})" if party.size > 1 else "") if x)),
        bag_fees(flight, party, a),
        seat_fees(flight, party, a),
        time_cost(flight, party),
        *layover_penalties(flight, profile, a, mult),
        *red_eye(flight, a, mult),
        *miles_value(flight, party, a, ref),
        status_perks(flight, party, a),
        preferred_bonus(flight, profile),
    ]
    total = round(sum(i.amount for i in items), 2)
    warns = list(warnings or [])
    seller, basis = ticketing_seller(flight, profile, ref, party)
    if basis == "assumed":
        warns.append(f"seller assumed: {seller} also sells this flight; price and miles unverified "
                     f"until --fare-details confirms it")
    return ScoreResult(flight, total, items, warns, analyze_award(flight, profile, party), party)


def rank(flights: list[Flight], profile: Profile, a: Assumptions,
         ref: Optional[AirlineReference] = None, party: Optional[Party] = None) -> Ranking:
    ranked, excluded = [], []
    for f in flights:
        violations, warnings = check_constraints(f, profile)
        if violations:
            excluded.append(Exclusion(f, violations))
        else:
            ranked.append(score_flight(f, profile, a, warnings, ref, party))
    ranked.sort(key=lambda r: (r.effective_cost, r.flight.total_duration_min, r.flight.id))
    return Ranking(ranked, excluded)
