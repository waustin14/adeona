# Adeona

Read-only flight research assistant. It finds flight options and ranks them by
**effective cost**: cash price adjusted for fees, time, connection quality and
loyalty value. **It recommends; you book.** Nothing in this codebase can purchase,
hold, or store payment details.

## Quick start (no API key needed)

```bash
uv venv .venv && source .venv/bin/activate      # or: python -m venv .venv
uv pip install -e ".[dev]"                       # or: pip install -e ".[dev]"

python -m adeona score --input samples/sample.json --profile config/profile.yaml
python -m adeona score --input samples/sample.json --profile config/profile.yaml --details top
python -m adeona score --input samples/sample.json --profile config/profile.yaml --json

# search tool (mock provider, offline) -> score
python -m adeona search --from BWI,IAD,DCA --to MCO --date 2026-11-14 --out results.json
python -m adeona score --input results.json --profile config/profile.yaml

pytest -q
```

Live search: put `SERPAPI_API_KEY=...` in `.env` (loaded automatically; never committed), then:

```bash
python -m adeona search --provider serpapi --from BWI,IAD,DCA --to MCO --date 2026-11-14 \
    --max-stops 1 --exclude-basic --fare-details 3 --budget 5 --out results.json
# long connections (8-24h layovers) + Google's hidden "View more flights" results:
python -m adeona search --provider serpapi --from IAD --to FRA --date 2026-11-14 \
    --max-stops 1 --exclude-basic --layover 480,1440 --show-hidden --out fra.json
python -m adeona score --input results.json --profile config/profile.yaml --details top
```

`--fare-details N` spends 1 extra call per itinerary (first N results) to fetch every fare
brand with its features and the airline's bag prices, then re-prices the itinerary at the
cheapest **airline-direct, non-basic** fare. Searches with identical parameters within an
hour are served from `.cache/` without spending budget.

## Traveling with others

```bash
# you + a named companion from profile.travelers + one child guest
python -m adeona search --provider serpapi --from IAD --to FRA --date 2026-11-14 \
    --exclude-basic --travelers me,alex --children 1 --out fam.json
python -m adeona score --input fam.json --profile config/profile.yaml   # party read from fam.json
```

* **Who:** `--travelers` picks named companions from `profile.travelers` (`me` = you).
  `--guests`, `--children` and `--lap-infants` add one-off travelers with no loyalty accounts.
* **Prices are party totals.** Verified live on 2026-10-06: an `adults=2` search returned
  2x the 1-adult price on all 20 matching itineraries. `score` refuses to score results
  priced for a different party than the one you ask for.
* **Per traveler:** bags, seat fees, travel time (each traveler's `hourly_value`; children
  default to `child_hourly_value`), miles (each traveler's own accounts; the fare is split
  evenly across seats) and status perks.
* **Once per trip:** the preferred-airline bonus, plus layover and red-eye penalties unless
  `penalties_per_traveler: true`.
* **Companion bags:** a status holder's free bag covers companions only where
  `companion_free_bags` says so (United Silver: yes, verify; Southwest: not assumed).
* **Awards:** points x seats; the note flags that award availability for N seats is unverified.

## Layout

```
config/profile.yaml       your preferences and loyalty accounts (validated on load)
config/assumptions.yaml   fees, earn rates, perk values, penalties. Every value tagged SRC/ASSUMPTION/PREFERENCE
samples/sample.json       MOCK itineraries covering each edge case (invented prices)
src/adeona/
  data/airlines.yaml      SERVICE reference data: alliance membership by IATA code, dated with sources
  reference.py            loads it; resolves airline names/flight numbers to codes and alliances
  party.py                builds the party from profile travelers + guest flags
  models.py               normalized schema: Flight, Segment, Layover, BagRules, AwardOption, SearchQuery
  normalize.py            derives layovers, overnight and airport-change flags from segments
  profile.py              profile + assumptions loaders with readable errors
  scoring.py              pure scoring functions + hard-constraint filter
  search.py               search_flights tool: provider + 1h cache + call budget + call log
  cache.py budget.py calllog.py
  providers/mock.py       offline fixture provider
  providers/serpapi.py    SerpApi Google Flights adapter (search + booking options), verified live 2026-10-06
  cli.py                  `python -m adeona score|search`
logs/api_calls.jsonl      one line per search attempt: params, cache hit, result count, cost estimate
```

## Scoring

```
effective_cost = cash_price
               + checked-bag fees   (fare allowance and status waiver applied)
               + seat fee           (only quoted fees, or basic economy)
               + total_hours * hourly_value
               + layover penalties  (short connection / overnight / airport change)
               - miles_earned * cpp / 100
               - status perk value  (only on the airline you hold status with)
               - preferred_airline_bonus
```

* **Hard constraints first.** Flights with too many stops, any layover under the minimum,
  basic economy, or any segment marketed or operated by an avoided airline are excluded,
  and the reason is printed.
* **Breakdown always.** Every result lists line items that sum exactly to the total
  (enforced by tests).
* **Miles earned:** own airline: `price x earnable_fraction x brand rate x (1 + status bonus)`.
  Partner: credits to your program at `partner_earn_per_dollar` when it shares an alliance
  with one of your loyalty airlines. Alliance membership comes from the service's
  `src/adeona/data/airlines.yaml`, matched by the IATA code in the flight number
  (`TK 188` is TK, Star Alliance), not from your profile. No program: 0.

## Profile vs. service data

The profile holds only facts about **you**: home airports, time value, preferred and avoided
airlines, loyalty accounts (status, cpp), bags, and hard constraints. Facts about the
**industry** ship with the service. Alliance membership lives in `src/adeona/data/airlines.yaml`,
and fees and earn rates live in `config/assumptions.yaml`. A profile that still has an
`alliances:` key is rejected, and the error message says where that data lives now.
* **Awards:** `points/100 x cpp + taxes` compared to the cash price. Shows the implied
  cents per point and flags `AWARD` when it beats your baseline cpp.
* Ties are broken by shorter total duration, then by id.

## Decisions

**2026-10-06: codeshare sellers (approach D).** Some flights are sold by one of your airlines
but flown by a partner (e.g. LH 419, also sold by United as UA 8826).
* **Scan:** if every segment is flown or codeshare-sold by one of your loyalty airlines
  (Google's `ticket_also_sold_by`), assume you'd buy from it at the search price. The table
  shows `sold by United?` and the breakdown carries a warning.
* **Verify:** `--fare-details` records the actual seller of the chosen airline-direct fare.
  That confirms the assumption, or overturns it and falls back to partner earning.
* **Only the miles line uses the seller.** Bags, status perks and the **preferred-airline
  bonus follow the airline that flies the plane** (your choice: the bonus is about the airline
  you fly; miles are counted separately).

**2026-10-06: parties.** Named travelers in the profile **and** head-count flags for one-off
guests; travel time is valued **per traveler**.

## Assumptions (all live in `config/assumptions.yaml` or are noted here)

| # | Assumption | Why / status |
|---|---|---|
| 1 | Profile `Chase` line fixed: `Ultimate Rewards. cpp` became `Ultimate Rewards, cpp` | typo in the original spec; the loader reports it with line and column |
| 2 | Chase UR earns nothing on flights; it is used only for award comparisons | it's a bank currency, not an airline |
| 2b | Alliance membership as of 2026-10-06: SAS is SkyTeam (since 2024-09-01), ITA is Star (since 2026-04-01), Aeroflot and S7 are suspended (no partner credit) | oneworld.com (official) + Wikipedia; staralliance.com and skyteam.com block automated reads, so re-check by hand |
| 2c | A flight sold by one of your airlines but flown by a partner earns at **the seller's own per-$ rate** (e.g. United 7/$ for Silver on a United-issued ticket for LH 419) | United rule for 016 tickets on partners, from 2015-era sources: **verify on united.com**. See "Decisions" |
| 2d | Silver free bag is **not** assumed on United-sold, partner-flown tickets; the quoted fee is used | unverified United rule; quoted $90 on the live LH 419 example |
| 3 | `earnable_fraction_of_price: 0.85` | sources return total price only; taxes don't earn |
| 4 | United 5 mi/$ base, Premier Silver +40% (7/$) | SRC 2026-10-06 (NerdWallet). Gold/Plat/1K values are unverified |
| 5 | Southwest Basic 2, Choice 6, Choice Preferred 10, Choice Extra 14 pts/$; A-List Preferred +100% | from secondary sources 2026-10-06: **verify on southwest.com** |
| 6 | United Premier Silver: 1 free checked bag; Southwest A-List Preferred: 2 free | SRC 2026-10-06; waiver applies only when the primary carrier is that airline |
| 7 | Bag fees: UA $40/$50, WN **$45** (live-verified)/$55, others $40/$50 | the rest are **unverified placeholders**; a fee from `--fare-details` overrides them |
| 8 | Partner earn (e.g. Lufthansa credited to United) = flat 5/$ | placeholder: real United partner earning is distance x fare class |
| 9 | "Overnight" = layover overlaps 00:00 to 05:00; a source flag overrides | your call |
| 10 | Primary airline = marketing carrier with the most flight minutes; drives fees, perks, earn and bonus | simple and deterministic |
| 11 | Avoided airline is a hard filter, not a penalty | spec lists it with constraints |
| 12 | Unknown fare brand (`is_basic_economy: null`) is kept but shown with a warning | Google Flights doesn't expose fare brands unless you pass `exclude_basic` |
| 13 | Perk $ values and layover penalties | your preferences: tune freely |
| 14 | SerpApi cost estimate $0.025/call | Starter plan $25 / 1,000 searches (2026-10-06) |
| 15 | Multiple segments on one itinerary are treated as one ticket | the LGA/JFK sample is a self-transfer and only demonstrates the penalty |
| 16 | `--fare-details` picks only fares sold directly by the airline | OTA tickets can complicate status benefits and changes; OTA prices are listed but ignored |
| 17 | A fare is basic if its name says "basic", **or** it charges for seat selection, **or** it has no carry-on, **or** it's cheaper than the exclude-basic search price | live finding: JetBlue "Main Base" is basic but its name doesn't say so |
| 18 | An unnamed airline-direct fare at exactly the exclude-basic search price counts as non-basic | live finding: the IAD-FRA fare details had no fare names at all |
| 19 | Overnight layover comes from our 00:00-05:00 rule, combined with Google's flag | live finding: Google left the flag off for real overnight layovers (IST 15:20 to 08:10) |
| 20 | Google's segment-level `overnight` means a **red-eye flight**, not a layover; `red_eye_penalty` defaults to $0 | your preference |
| 22 | Each seated traveler's fare = party price / seats (for miles); lap infants earn nothing and need no award seat | Google gives one party total, with no per-passenger split |
| 23 | The whole party is on one reservation (so companion bag benefits apply) | true if booked together |
| 24 | United Silver's free bag extends to companions; Southwest A-List Preferred's does not (not confirmed) | secondary sources for United (cap of 8 or 9 companions); nothing found for Southwest |
| 25 | Status perks (boarding etc.) count only for the traveler holding the status | companion boarding rules vary |
| 26 | Mock provider prices a party as adult fare x seats, lap infants free | mock only; SerpApi quotes real party totals |
| 21 | Default bag fees ($40/$50) are domestic-shaped and may be wrong on long-haul flights; use `--fare-details` there | live: the $799 IAD-FRA fare sold by United showed a $90 first bag |

## Flight data API: research (2026-10-06)

| Option | Status | Cost | Southwest? | Fit |
|---|---|---|---|---|
| **Amadeus Self-Service** | **Shut down 2026-07-17**; no new signups | n/a | no | not an option |
| **SerpApi, Google Flights engine** | live, self-serve | free 250/mo; $25/1k; cached and failed calls are free | **yes** (Google Flights has shown WN fares since 2024) | **recommended** |
| Duffel | live, self-serve | built for booking: $3/order; searches beyond a 1500:1 search-to-book ratio cost $0.005 each | not listed | richer fare and bag data, but made for sellers |
| Ignav | live, self-serve | 1k free, then $2/1k | unknown | no fare rules or brands; newer and less proven |

**Recommendation: SerpApi Google Flights** (adopted; verified live). It is the only self-serve option found that
returns Southwest, your top airline. It is also free at your volume, and returns layovers
with an `overnight` flag. Trade-off: SerpApi gets its data by scraping Google Flights. That
is a third-party search engine, not an airline site, so it meets the "no airline scraping"
rule as written, but you should know about it. It has no checked-bag fees or fare brand
letters. The adapter covers this with `exclude_basic=true` and the fees in the assumptions file.

## What's mocked

* All prices and award costs in `samples/sample.json` are made up.
* SerpApi adapter: **verified live on 2026-10-06** on BWI/IAD/DCA to MCO and on IAD to FRA,
  including overnight layovers (CPH, IST), red-eye flags, codeshare operators, and fare
  details for a partner flight. Real responses are in `tests/fixtures/serpapi/`.
* Round trips in the SerpApi adapter are not supported yet (each direction needs its own one-way search).
