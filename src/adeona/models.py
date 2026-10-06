"""Normalized flight schema shared by every provider, the scorer, and the CLI."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, computed_field, model_validator


class PartyCounts(BaseModel):
    """Passenger mix a price was quoted for. Google Flights quotes ONE total for the party."""

    adults: int = Field(default=1, ge=1)
    children: int = Field(default=0, ge=0)
    infants_in_seat: int = Field(default=0, ge=0)
    infants_on_lap: int = Field(default=0, ge=0)

    @property
    def size(self) -> int:
        return self.adults + self.children + self.infants_in_seat + self.infants_on_lap

    @property
    def seated(self) -> int:
        return self.adults + self.children + self.infants_in_seat

    def label(self) -> str:
        parts = [(self.adults, "adult"), (self.children, "child"),
                 (self.infants_in_seat, "infant in seat"), (self.infants_on_lap, "lap infant")]
        return ", ".join(f"{n} {w}{'' if n == 1 else ('ren' if w == 'child' else 's')}" for n, w in parts if n)


class Segment(BaseModel):
    airline: str  # marketing carrier name, e.g. "United"
    flight_number: str
    origin: str  # IATA
    destination: str  # IATA
    departs_at: datetime  # local time at origin
    arrives_at: datetime  # local time at destination
    duration_min: int = Field(ge=0)
    operating_airline: Optional[str] = None  # set when codeshare differs from marketing carrier
    cabin: str = "Economy"
    booking_class: Optional[str] = None  # fare bucket letter if the source exposes it
    red_eye: bool = False  # the flight itself runs overnight (not a layover)
    also_sold_by: list[str] = Field(default_factory=list)  # codeshare sellers (Google ticket_also_sold_by)
    often_delayed: bool = False  # source says often delayed by 30+ min


class Layover(BaseModel):
    airport: str  # IATA where you land
    duration_min: int = Field(ge=0)
    overnight: bool = False
    # True when the next segment departs from a different airport (e.g. LGA -> JFK)
    airport_change: bool = False
    departs_from: Optional[str] = None  # IATA of next departure when airport_change


class BagRules(BaseModel):
    carry_on_included: Optional[bool] = None
    checked_bags_included: int = 0
    # Fee for the 1st / 2nd checked bag as quoted by the source; None = unknown,
    # in which case the scorer falls back to config/assumptions.yaml.
    first_checked_fee: Optional[float] = None
    second_checked_fee: Optional[float] = None


class AwardOption(BaseModel):
    program: str  # loyalty program name, must match a profile loyalty `program`
    points: int = Field(gt=0)
    taxes: float = Field(ge=0)


class Flight(BaseModel):
    id: str
    source: str = "unknown"
    # Total cash price for the whole `party` (verified live 2026-10-06: Google quotes party totals).
    price: float = Field(ge=0)
    currency: str = "USD"
    segments: list[Segment] = Field(min_length=1)
    layovers: list[Layover] = Field(default_factory=list)
    total_duration_min: int = Field(ge=0)  # first departure -> last arrival, incl. layovers
    fare_brand: Optional[str] = None  # e.g. "Basic Economy", "Main", "Choice"
    # None = source did not say; the hard-constraint filter treats this as a warning.
    is_basic_economy: Optional[bool] = None
    bag_rules: BagRules = Field(default_factory=BagRules)
    seat_selection_fee: Optional[float] = None
    award: Optional[AwardOption] = None
    notes: list[str] = Field(default_factory=list)
    # Opaque provider handle for follow-up lookups (SerpApi booking_token). Not a secret.
    provider_ref: Optional[str] = None
    party: PartyCounts = Field(default_factory=PartyCounts)
    # Airline that sells (tickets) the chosen fare, set only once fare details confirm it.
    # None = unverified; scoring may then *assume* a seller from segment also_sold_by.
    seller: Optional[str] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stops(self) -> int:
        return len(self.segments) - 1

    @computed_field  # type: ignore[prop-decorator]
    @property
    def airline(self) -> str:
        """Primary carrier: the marketing airline flying the most minutes."""
        minutes: dict[str, int] = {}
        for s in self.segments:
            minutes[s.airline] = minutes.get(s.airline, 0) + s.duration_min
        return max(minutes, key=lambda a: minutes[a])

    @property
    def airlines(self) -> set[str]:
        names = {s.airline for s in self.segments}
        names |= {s.operating_airline for s in self.segments if s.operating_airline}
        return names

    @property
    def departs_at(self) -> datetime:
        return self.segments[0].departs_at

    @property
    def arrives_at(self) -> datetime:
        return self.segments[-1].arrives_at

    @property
    def route(self) -> str:
        parts = [self.segments[0].origin]
        for prev, nxt in zip(self.segments, self.segments[1:]):
            parts.append(prev.destination if prev.destination == nxt.origin else f"{prev.destination}/{nxt.origin}")
        parts.append(self.segments[-1].destination)
        return "-".join(parts)


class FareOption(BaseModel):
    """One bookable fare for an itinerary (e.g. Southwest Basic / Choice / Choice Preferred)."""

    brand: Optional[str] = None
    price: float = Field(ge=0)
    book_with: str
    sold_by_airline: bool = False
    marketed_as: list[str] = Field(default_factory=list)  # flight numbers on this ticket, e.g. ["UA 8826"]
    is_basic_economy: Optional[bool] = None
    bag_rules: BagRules = Field(default_factory=BagRules)
    features: list[str] = Field(default_factory=list)


class SearchQuery(BaseModel):
    origins: list[str] = Field(min_length=1)
    destinations: list[str] = Field(min_length=1)
    depart_date: str  # YYYY-MM-DD
    return_date: Optional[str] = None
    adults: int = Field(default=1, ge=1, le=9)
    children: int = Field(default=0, ge=0, le=8)
    infants_in_seat: int = Field(default=0, ge=0, le=8)
    infants_on_lap: int = Field(default=0, ge=0, le=8)
    cabin: str = "economy"
    max_stops: Optional[int] = None
    exclude_basic: bool = False
    carry_on_bags: int = 0
    # [min, max] minutes per layover, e.g. [480, 1440] to look for overnight connections
    layover_minutes: Optional[tuple[int, int]] = None
    show_hidden: bool = False  # include results behind Google's "View more flights"

    @model_validator(mode="after")
    def _party_limits(self) -> "SearchQuery":
        # [ASSUMPTION] Google Flights caps a search at 9 passengers, and airlines allow one
        # lap infant per adult.
        if self.party.size > 9:
            raise ValueError(f"party of {self.party.size} exceeds the 9-passenger search limit")
        if self.infants_on_lap > self.adults:
            raise ValueError("each lap infant needs an adult (infants_on_lap <= adults)")
        return self

    @property
    def party(self) -> PartyCounts:
        return PartyCounts(adults=self.adults, children=self.children,
                           infants_in_seat=self.infants_in_seat, infants_on_lap=self.infants_on_lap)
