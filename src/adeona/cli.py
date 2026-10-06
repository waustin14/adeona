"""Command-line entry point: `python -m adeona {score,search}`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .budget import CallBudget
from .env import load_dotenv
from .models import Flight, SearchQuery
from .normalize import flight_from_dict
from .profile import ConfigError, load_assumptions, load_profile, profile_warnings
from .providers import ProviderError, get_provider
from .providers.mock import load_flights
from .party import Party, PartyError, build_party
from .scoring import Ranking, ScoreResult, rank, ticketing_seller
from .search import FlightSearch

DEFAULT_ASSUMPTIONS = "config/assumptions.yaml"
DEFAULT_PROFILE = "config/profile.yaml"


def _money(x: float) -> str:
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _award_flag(r: ScoreResult) -> str:
    a = r.award
    if a is None:
        return ""
    if a.beats_baseline is None:
        return "award: ?"
    return f"AWARD {a.implied_cpp:g}c" if a.beats_baseline else f"award {a.implied_cpp:g}c"


def _seller_tag(r: ScoreResult, profile) -> str:
    if profile is None:
        return ""
    seller, basis = ticketing_seller(r.flight, profile)
    return {"verified": f"sold by {seller}", "assumed": f"sold by {seller}?"}.get(basis or "", "")


def render(ranking: Ranking, details: str = "all", top: int | None = None, out=None, profile=None) -> None:
    out = out or sys.stdout
    rows = ranking.ranked[:top] if top else ranking.ranked
    party = rows[0].party if rows else None
    multi = party is not None and party.size > 1
    if multi:
        print(f"Party: {', '.join(t.name for t in party.travelers)} ({party.counts.label()}); "
              f"Cash and Effective are party totals", file=out)
    header = (f"{'#':>2}  {'Flight':<26} {'Airline':<10} {'Route':<18} {'Departs':<11} {'Stops':>5} {'Time':>6} "
              f"{'Cash':>9} {'Effective':>10}" + (f" {'Per trav.':>10}" if multi else "") + "  Notes")
    print(header, file=out)
    print("-" * len(header), file=out)
    for n, r in enumerate(rows, 1):
        f = r.flight
        print(f"{n:>2}  {f.id[:26]:<26} {f.airline[:10]:<10} {f.route[:18]:<18} {f.departs_at:%m-%d %H:%M} "
              f"{f.stops:>5} {_hm(f.total_duration_min):>6} {_money(f.price):>9} {_money(r.effective_cost):>10}"
              + (f" {_money(r.per_traveler):>10}" if multi else "") + "  "
              f"{'  '.join(t for t in (_award_flag(r), _seller_tag(r, profile)) if t)}", file=out)

    if details != "none":
        shown = rows if details == "all" else rows[:1]
        for n, r in enumerate(shown, 1):
            print(f"\n#{n} {r.flight.id}  ({r.flight.airline}, {r.flight.route})", file=out)
            for i in r.items:
                if i.amount == 0 and i.key in ("seat", "perks", "preferred"):
                    continue
                print(f"    {i.label:<20} {_money(i.amount):>10}   {i.note}", file=out)
            print(f"    {'= Effective cost':<20} {_money(r.effective_cost):>10}", file=out)
            if r.award:
                a = r.award
                cost = _money(a.award_cost) if a.award_cost is not None else "n/a"
                seats = f" x {a.travelers} seats" if a.travelers > 1 else ""
                print(f"    Award: {a.points:,} {a.program} + {_money(a.taxes)} taxes{seats} = {cost} at your cpp -> {a.note}"
                      if a.award_cost is not None else f"    Award: {a.points:,} {a.program}: {a.note}", file=out)
            for w in r.warnings:
                print(f"    ! {w}", file=out)

    if ranking.excluded:
        print("\nExcluded by hard constraints:", file=out)
        for e in ranking.excluded:
            print(f"    {e.flight.id:<28} {_money(e.flight.price):>9}  {'; '.join(e.reasons)}", file=out)


def _load_input(path: str) -> tuple[list[Flight], dict | None]:
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict) and "ok" in data and "flights" in data:  # output of `search`
        return [flight_from_dict(f) for f in data["flights"]], data.get("party")
    return load_flights(path), None


def _party_spec(args: argparse.Namespace) -> dict | None:
    """Party flags as a plain dict, or None when none were given."""
    if not (args.travelers or args.guests or args.children or args.lap_infants):
        return None
    return {"names": args.travelers.split(",") if args.travelers else ["me"],
            "guests": args.guests, "children": args.children, "lap_infants": args.lap_infants}


def _party(profile, spec: dict | None) -> Party:
    return build_party(profile, **spec) if spec else build_party(profile)


def cmd_score(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    assumptions = load_assumptions(args.assumptions)
    for w in profile_warnings(profile, assumptions):
        print(f"warning: {w}", file=sys.stderr)
    flights, searched = _load_input(args.input)
    asked = _party_spec(args)
    if asked and searched and asked != searched:
        raise PartyError(f"input was searched for {searched}, but the flags ask for {asked}; "
                         f"drop the flags or search again")
    party = _party(profile, asked or searched)
    ranking = rank(flights, profile, assumptions, party=party)
    if args.json:
        print(json.dumps({
            "ranked": [r.to_dict() for r in ranking.ranked],
            "excluded": [{"flight_id": e.flight.id, "reasons": e.reasons} for e in ranking.excluded],
        }, indent=2))
    else:
        render(ranking, details=args.details, top=args.top, profile=profile)
        if any(_seller_tag(r, profile).endswith("?") for r in ranking.ranked):
            print("\n'sold by X?' = seller assumed from codeshare data; run search --fare-details to confirm.")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    kwargs = {"fixture": args.fixture} if args.provider == "mock" and args.fixture else {}
    provider = get_provider(args.provider, **kwargs)
    tool = FlightSearch(provider, CallBudget(args.budget))
    spec = _party_spec(args)
    counts = _party(load_profile(args.profile), spec).counts if spec else None
    query = SearchQuery(
        origins=args.origins.upper().split(","), destinations=args.destinations.upper().split(","),
        depart_date=args.date, max_stops=args.max_stops, exclude_basic=args.exclude_basic,
        layover_minutes=tuple(int(x) for x in args.layover.split(",")) if args.layover else None,
        show_hidden=args.show_hidden,
        **(counts.model_dump() if counts else {}),
    )
    result = tool(query)
    if spec:
        result["party"] = spec
    if result["ok"] and args.fare_details:
        # 1 extra call per itinerary; stops cleanly if the budget runs out.
        result["fare_details"] = []
        for i, f in enumerate(result["flights"][: args.fare_details]):
            d = tool.fare_details(query, f)
            result["flights"][i] = d["flight"]
            chosen = d["chosen"]
            result["fare_details"].append({
                "flight_id": d["flight_id"], "ok": d["ok"], "error": d["error"],
                "chosen": f"{chosen['brand']} ${chosen['price']:g}" if chosen else None,
                "options": [f"{o['brand'] or '?'} ${o['price']:g} ({o['book_with']})" for o in d["options"]
                            if o["sold_by_airline"]],
            })
        result["budget_remaining"] = tool.budget.remaining
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text)
        status = "ok" if result["ok"] else f"FAILED: {result['error']}"
        print(f"{status}; {len(result['flights'])} flights -> {args.out} (cache_hit={result['cache_hit']})")
        for d in result.get("fare_details", []):
            print(f"  {d['flight_id']}: {d['chosen'] or d['error'] or 'no airline-direct fare'}  [{', '.join(d['options'])}]")
    else:
        print(text)
    return 0 if result["ok"] else 2


def _add_party_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("party (default: just you)")
    g.add_argument("--travelers", default=None, help="names from profile.travelers, 'me' = you, e.g. me,alex")
    g.add_argument("--guests", type=int, default=0, help="extra adults with no loyalty accounts")
    g.add_argument("--children", type=int, default=0, help="extra children with no loyalty accounts")
    g.add_argument("--lap-infants", type=int, default=0, help="extra lap infants")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="adeona", description="Read-only flight research: rank by effective cost.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("score", help="rank normalized flights from a JSON file")
    s.add_argument("--input", required=True, help="normalized flights JSON (sample or `search` output)")
    s.add_argument("--profile", required=True)
    s.add_argument("--assumptions", default=DEFAULT_ASSUMPTIONS)
    s.add_argument("--details", choices=["all", "top", "none"], default="all")
    s.add_argument("--top", type=int, default=None)
    s.add_argument("--json", action="store_true", help="machine-readable output")
    _add_party_args(s)
    s.set_defaults(func=cmd_score)

    q = sub.add_parser("search", help="search flights (one-way) via a provider")
    q.add_argument("--provider", choices=["mock", "serpapi"], default="mock")
    q.add_argument("--from", dest="origins", required=True, help="comma-separated IATA, e.g. BWI,IAD,DCA")
    q.add_argument("--to", dest="destinations", required=True)
    q.add_argument("--date", required=True, help="YYYY-MM-DD")
    q.add_argument("--max-stops", type=int, default=None)
    q.add_argument("--exclude-basic", action="store_true")
    q.add_argument("--layover", default=None, metavar="MIN,MAX", help="layover minutes range, e.g. 480,1440")
    q.add_argument("--show-hidden", action="store_true", help="include Google's 'View more flights' results")
    q.add_argument("--fare-details", type=int, default=0, metavar="N",
                   help="look up fare brands + bag fees for the first N results (1 call each)")
    q.add_argument("--budget", type=int, default=5, help="max uncached API calls for this task")
    q.add_argument("--fixture", default=None, help="mock provider fixture JSON")
    q.add_argument("--out", default=None, help="write JSON here instead of stdout")
    q.add_argument("--profile", default=DEFAULT_PROFILE, help="used to resolve --travelers names")
    _add_party_args(q)
    q.set_defaults(func=cmd_search)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (ProviderError, PartyError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
