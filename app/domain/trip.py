"""Trip / leg / candidate models (minimal for M0; filled out in M1).

The day layer (``TripSegment`` / ``DayPlan``) arrived with the content layer
(F1). Every new field carries a ``default_factory`` so a checkpoint written by
an older build still deserialises -- plans are versioned and replayed from
SQLite, and a schema change must never invalidate a stored conversation.
"""

from __future__ import annotations

from datetime import date, datetime
from datetime import date as Date  # aliased: a field named ``date`` would shadow the type
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.domain.geo import Coord, Crs


class RouteScope(StrEnum):
    LOCAL = "local"
    REGIONAL = "regional"
    INTERCITY = "intercity"


class TransportMode(StrEnum):
    METRO = "metro"
    BUS = "bus"
    RAIL = "rail"
    FLIGHT = "flight"
    LONG_DISTANCE = "longdistance"
    TAXI = "taxi"
    DRIVE = "drive"
    WALK = "walk"
    BIKE = "bike"


class LegCandidate(BaseModel):
    candidate_id: str
    scope: RouteScope = RouteScope.LOCAL
    modes: list[TransportMode] = Field(default_factory=list)
    depart: datetime | None = None
    arrive: datetime | None = None
    duration_min: int | None = None
    transfer_count: int | None = None
    walk_meters: int | None = None
    price: float | None = None
    features: dict[str, float] = Field(default_factory=dict)
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class Leg(BaseModel):
    leg_id: str
    origin: Coord | None = None
    destination: Coord | None = None
    scope: RouteScope = RouteScope.LOCAL
    day: date | None = None
    selected_candidate_id: str | None = None
    candidates: list[LegCandidate] = Field(default_factory=list)
    reason_trace: dict[str, Any] | None = None
    rejected: list[dict[str, Any]] = Field(default_factory=list)


class Trip(BaseModel):
    trip_id: str
    session_id: str
    origin_label: str | None = None
    destination_label: str | None = None
    date_window: dict[str, Any] = Field(default_factory=dict)
    travelers: list[dict[str, Any]] = Field(default_factory=list)
    preferences: dict[str, Any] = Field(default_factory=dict)
    legs: list[Leg] = Field(default_factory=list)
    stays: list[dict[str, Any]] = Field(default_factory=list)
    tickets: list[dict[str, Any]] = Field(default_factory=list)
    reminders: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    degradation: list[dict[str, Any]] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Content layer (F1): segments, days, attractions, lodging.
#
# These are *presentation and planning* structures, not new sources of truth:
# every fact in them (rating, opening hours, ticket price, travel time) is
# either produced by a capability or by a deterministic policy function. The
# LLM is only ever allowed to fill the prose fields (`theme`, `summary`,
# `description`, `content_notes`), and each of those has a template fallback.
# --------------------------------------------------------------------------


#: Attraction categories. The vocabulary is deliberately small: it is the key
#: for two deterministic tables -- suggested visit duration and the
#: indoor/outdoor split that decides how weather is assessed.
ATTRACTION_CATEGORIES = (
    "museum",
    "park",
    "temple",
    "historic",
    "market",
    "viewpoint",
    "theme_park",
    "other",
)


class DayAttraction(BaseModel):
    """One attraction on one day. Facts only -- no prose beyond ``description``."""

    attraction_id: str
    name: str
    category: str = "other"
    coord: Coord | None = None
    crs: Crs | None = None
    recommend_duration_min: int = 120
    opening_hours: dict[str, Any] = Field(default_factory=dict)
    ticket_price: float | None = None
    rating: float | None = None
    review_count: int | None = None
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    #: Which provider answered, so the card can show a provenance affordance.
    provider: str | None = None
    #: Content confidence; feeds the scoring uncertainty penalty.
    confidence: float = 1.0
    #: True when opening hours clash with the day's rhythm.
    out_of_window: bool = False


class MealSuggestion(BaseModel):
    slot: str = "lunch"  # lunch | dinner | snack
    name: str = ""
    kind: str = "local"  # local | convenience | hotel
    coord: Coord | None = None
    price_hint: float | None = None
    provider: str | None = None
    note: str | None = None


class LodgingOption(BaseModel):
    name: str = ""
    area: str | None = None
    nightly_price: float | None = None
    nights: int = 0
    coord: Coord | None = None
    provider: str | None = None
    #: Deep-link parameters. Payment always happens on the user's side.
    booking_ref: dict[str, Any] = Field(default_factory=dict)
    #: True when this is a district suggestion rather than a named property.
    advisory: bool = False


class DayScheduleItem(BaseModel):
    kind: str = "attraction"  # city_transfer | attraction | meal | lodging | rest
    ref_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    duration_min: int | None = None
    cost: float | None = None
    #: Travel time to the next stop. Copied from a route capability, never
    #: invented -- that is what keeps "推荐理由可指认" true for the day layer.
    transport_to_next: dict[str, Any] | None = None
    weather_note: str | None = None
    evidence: list[str] = Field(default_factory=list)


class DayPlan(BaseModel):
    day_index: int = 1
    date: Date | None = None
    #: City the day's activities happen in.
    city: str = ""
    segment_index: int = 0
    #: Where the traveller sleeps that night. Differs from ``city`` on a
    #: transfer day, which is exactly when the user needs it stated.
    overnight_city: str | None = None
    is_transfer_day: bool = False
    theme: str = ""
    summary: str = ""
    weather: dict[str, Any] = Field(default_factory=dict)
    weather_veto: bool = False
    items: list[DayScheduleItem] = Field(default_factory=list)
    attractions: list[DayAttraction] = Field(default_factory=list)
    meals: list[MealSuggestion] = Field(default_factory=list)
    lodging: list[LodgingOption] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class TripSegment(BaseModel):
    """One city stay inside a multi-city trip."""

    segment_id: str
    city: str
    coord: Coord | None = None
    day_count: int = 1
    start_day_index: int | None = None
    lodging_area: str | None = None
    lodging: list[LodgingOption] = Field(default_factory=list)
    #: Transport from the previous city; reuses the existing leg structure so
    #: the same constraint validation applies.
    transfer_in: dict[str, Any] | None = None
    #: Things the user must be told about this stay (unresolved coordinates, a
    #: trimmed day count). Shortages are reported, never silent.
    notes: list[str] = Field(default_factory=list)


class TripContent(BaseModel):
    """The content-layer half of a trip, kept next to (not inside) ``Trip``."""

    segments: list[TripSegment] = Field(default_factory=list)
    days: list[DayPlan] = Field(default_factory=list)
    content_notes: list[str] = Field(default_factory=list)
    degraded_content: list[dict[str, Any]] = Field(default_factory=list)
    cost_estimate: float | None = None
    cost_note: str = ""
