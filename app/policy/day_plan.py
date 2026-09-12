"""Deterministic day planning (content layer F1).

Everything that decides *shape* lives here: how many days each city gets, where
the transfer day falls, which attractions land on which day, what the day's
timeline looks like, where the traveller sleeps, and what the trip costs.

Two rules shape the module:

* **Pure.** No IO, no provider SDK, no LLM, no capability calls -- travel times,
  forecasts and attraction facts all arrive as arguments. That is what makes the
  whole day layer unit-testable, and it is the same discipline
  :mod:`app.policy.constraints` follows.
* **Every shortage is reported.** A day that does not fit, a segment that cannot
  be reached, an attraction that clashes with opening hours: each one appends a
  note instead of being silently dropped, because the user is about to consent
  to this plan.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, Field

from app.domain.geo import Coord, Crs
from app.domain.ids import new_id
from app.domain.trip import (
    DayAttraction,
    DayPlan,
    DayScheduleItem,
    LodgingOption,
    MealSuggestion,
    TripSegment,
)
from app.policy import weather_rules

#: Suggested visit length by category. A deterministic table, not a lookup and
#: not an LLM guess: it is a decision parameter, and decisions belong in code
#: (P1). F2 can replace it with real opening-hours data per attraction.
CATEGORY_DURATION: dict[str, int] = {
    "museum": 120,
    "park": 120,
    "temple": 90,
    "historic": 120,
    "market": 90,
    "viewpoint": 60,
    "theme_park": 240,
    "other": 120,
}

#: Categories that happen outdoors, used to map a day onto the weather rules.
_OUTDOOR = frozenset({"park", "historic", "viewpoint", "market"})
_INDOOR = frozenset({"museum", "theme_park"})

DEFAULT_RHYTHM = {"start": "09:00", "end": "20:00"}
LUNCH_WINDOW = ("11:30", "13:00")
DINNER_WINDOW = ("17:30", "19:00")

#: Anything beyond this between two stops is a different area, not a next stop.
MAX_SAME_DAY_KM = 60.0
#: Transfers on a moving day get a smaller share of the day's slots.
TRANSFER_DAY_SLOTS = 2
ARRIVE_LATE_AFTER = time(15, 0)

_R = 6371.0


def haversine_km(a: Coord | None, b: Coord | None) -> float | None:
    """Great-circle distance in km, or ``None`` when either end is unknown.

    Delegates to :meth:`Coord.distance_m`, which normalises CRS first: mixing a
    GCJ-02 amap point with a WGS-84 OSM one is off by hundreds of metres, and a
    day cluster built on that error is wrong in a way nobody can see.
    """

    if a is None or b is None:
        return None
    return a.distance_m(b) / 1000.0


class DayWindow(BaseModel):
    day_index: int
    date: date
    start: time = time(9, 0)
    end: time = time(20, 0)
    city: str = ""
    segment_index: int = 0
    is_transfer_day: bool = False
    overnight_city: str | None = None
    #: Transport arriving in this city on a transfer day (an existing leg dict).
    transfer: dict[str, Any] | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def capacity_minutes(self) -> int:
        return max(0, (self.end.hour * 60 + self.end.minute) - (self.start.hour * 60 + self.start.minute))


def parse_rhythm(rhythm: Mapping[str, Any] | None) -> tuple[time, time]:
    """Read a ``{"start": "09:00", "end": "20:00"}`` mapping defensively."""

    source = rhythm or DEFAULT_RHYTHM

    def _one(key: str, fallback: str) -> time:
        raw = str(source.get(key) or fallback).strip()
        try:
            hour, _, minute = raw.partition(":")
            return time(int(hour), int(minute or 0))
        except (TypeError, ValueError):
            return time(*[int(part) for part in fallback.split(":")])

    start, end = _one("start", DEFAULT_RHYTHM["start"]), _one("end", DEFAULT_RHYTHM["end"])
    if end <= start:
        # A reversed or zero-length window would make every day empty; the
        # conservative repair is the default window, not an empty plan.
        return time(9, 0), time(20, 0)
    return start, end


def make_day_windows(
    depart: date,
    trip_days: int,
    *,
    rhythm: Mapping[str, Any] | None = None,
) -> list[DayWindow]:
    """One window per calendar day of the trip."""

    start, end = parse_rhythm(rhythm)
    return [
        DayWindow(day_index=index + 1, date=depart + timedelta(days=index), start=start, end=end)
        for index in range(max(0, trip_days))
    ]


def assign_days_to_segments(
    segments: Sequence[TripSegment],
    windows: Sequence[DayWindow],
    *,
    transfer_days: int = 0,
) -> list[DayWindow]:
    """Give each window its city, and mark the days spent moving.

    A transfer is placed on the **last day of the segment being left**, which is
    what people actually do: check out, travel, arrive, sleep in the new city.
    That day is therefore also the first day in the new city, and its
    ``overnight_city`` differs from its ``city`` -- the one place in the itinerary
    where the two legitimately disagree.
    """

    if not windows:
        return []
    if not segments:
        return list(windows)

    total = len(windows)
    reserved = min(max(0, transfer_days), max(0, len(segments) - 1), max(0, total - 1))
    content = max(1, total - reserved)
    desired = [max(1, int(segment.day_count or 1)) for segment in segments]

    # Scale the requested day counts onto the calendar. ``allocate_days`` already
    # made them consistent with the trip length; this makes them consistent with
    # the number of *windows*, which is the number that actually gets rendered.
    layout = _distribute(desired, content)

    # Walk the calendar, inserting a transfer position after every stay that is
    # followed by another one. The transfer position is a day of its own, so the
    # stays keep the length the user asked for.
    spans: list[tuple[int, int]] = []
    transfer_positions: list[int] = []
    cursor = 0
    for index, count in enumerate(layout):
        spans.append((cursor, cursor + max(0, count) - 1))
        cursor += count
        if index < reserved and index < len(layout) - 1:
            transfer_positions.append(cursor)
            cursor += 1

    transfers_used = 0
    result: list[DayWindow] = []
    for position, window in enumerate(windows):
        if position in transfer_positions:
            # A transfer is spent leaving one city and sleeping in the next, so
            # its activities belong to the city being left.
            segment_index = transfer_positions.index(position)
            is_transfer = transfers_used < reserved
            if is_transfer:
                transfers_used += 1
        else:
            segment_index = 0
            is_transfer = False
            for index, (first, last) in enumerate(spans):
                if first <= position <= last:
                    segment_index = index
                    break
            else:
                # More calendar days than the layout accounts for (the caller
                # asked for more windows than the trip needs): keep the last city
                # and say so rather than silently dropping the day.
                segment_index = len(segments) - 1
                window = window.model_copy(
                    update={"notes": [*window.notes, "该日超出分段天数，按最后一段处理"]}
                )

        segment = segments[segment_index]
        next_city = segments[segment_index + 1].city if is_transfer else segment.city
        result.append(
            window.model_copy(
                update={
                    "city": segment.city,
                    "segment_index": segment_index,
                    "is_transfer_day": is_transfer,
                    "overnight_city": next_city,
                    "transfer": (segments[segment_index + 1].transfer_in or None)
                    if is_transfer
                    else None,
                }
            )
        )

    return result


def _distribute(desired: Sequence[int], target: int) -> list[int]:
    """Scale requested day counts onto exactly ``target`` days.

    The proportions come from ``desired``, which is what keeps a 3:2 request
    looking like a 3:2 trip. A flat "one day each, remainder to the front" rule
    turns "北京3天+天津2天" over five windows into (4, 1) -- technically feasible,
    and not what anyone asked for.

    Only applied when the request left room for interpretation; an exact fit is
    left alone so a deliberate 2/2 is never rewritten by a rounding rule.
    """

    count = len(desired)
    if count == 0:
        return []
    if target < count:
        return [1 if index < target else 0 for index in range(count)]
    if target == sum(desired):
        return list(desired)

    total = sum(desired)
    exact = [target * value / total for value in desired]
    scaled = [max(1, int(value)) for value in exact]
    # Hand out the remainder to the largest fractional parts, which is what makes
    # the result preserve the requested proportions instead of the list order.
    order = sorted(range(count), key=lambda i: exact[i] - int(exact[i]), reverse=True)
    index = 0
    while sum(scaled) < target:
        scaled[order[index % count]] += 1
        index += 1
    while sum(scaled) > target:
        position = max(range(count), key=lambda i: scaled[i])
        scaled[position] -= 1
    return [max(1, value) for value in scaled]


# ---------------------------------------------------------------------------
# Assignment: anchor + nearest-neighbour within a segment.
# ---------------------------------------------------------------------------


def assign_attractions(
    pool: Sequence[DayAttraction],
    windows: Sequence[DayWindow],
    *,
    anchors: Mapping[int, Coord | None] | None = None,
    per_day_max: int = 4,
) -> tuple[dict[int, list[DayAttraction]], list[DayAttraction], list[str]]:
    """Distribute a city's attractions across that city's days.

    Returns ``(by_day_index, unassigned, notes)``. Anchors keep the days
    geographically apart; within a day the nearest unassigned point wins, which
    is the same greedy the route tools use and is enough for a handful of stops.
    """

    notes: list[str] = []
    usable = [item for item in pool if item.coord is not None]
    unusable = [item for item in pool if item.coord is None]
    if unusable:
        notes.append(f"{len(unusable)} 个候选点没有坐标，未排入行程")

    if not windows or not usable:
        return {window.day_index: [] for window in windows}, list(pool), notes

    anchor_by_index = anchors or {}
    remaining = list(usable)
    assignment: dict[int, list[DayAttraction]] = {window.day_index: [] for window in windows}
    ordered = sorted(windows, key=lambda window: window.day_index)
    placed: list[DayAttraction] = []

    for window in ordered:
        if not remaining:
            break
        anchor = anchor_by_index.get(window.day_index)
        slots = TRANSFER_DAY_SLOTS if window.is_transfer_day else per_day_max

        if anchor is not None:
            remaining.sort(key=lambda item: (haversine_km(anchor, item.coord) or 0.0))
        elif placed:
            # No stay to anchor on, so spread: open each day with the point
            # furthest from everything already scheduled. Without this a sparse
            # pool fills day one and leaves the rest of the trip empty, which
            # reads as "the planner gave up" rather than "there was little to go on".
            remaining.sort(
                key=lambda item: -min(
                    (haversine_km(item.coord, other.coord) or 0.0) for other in placed
                )
            )

        chosen: list[DayAttraction] = [remaining[0]]
        remaining.pop(0)
        while len(chosen) < slots and remaining:
            last = chosen[-1]
            remaining.sort(key=lambda item: (haversine_km(last.coord, item.coord) or 0.0))
            candidate = remaining[0]
            gap = haversine_km(last.coord, candidate.coord)
            if gap is not None and gap > MAX_SAME_DAY_KM:
                break
            chosen.append(candidate)
            remaining.pop(0)

        assignment[window.day_index] = chosen
        placed.extend(chosen)

    if remaining:
        notes.append(f"{len(remaining)} 个候选点未能排入 {len(windows)} 天，已列为备选")
    return assignment, [*remaining, *unusable], notes


# ---------------------------------------------------------------------------
# Timeline.
# ---------------------------------------------------------------------------


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _clock(base: date, minutes: int) -> datetime:
    return datetime.combine(base, time(0, 0)) + timedelta(minutes=minutes)


def _transfer_item(window: DayWindow) -> DayScheduleItem | None:
    """Turn the arriving leg into one schedule item."""

    transfer = window.transfer or {}
    if not transfer:
        return None
    depart = transfer.get("depart")
    arrive = transfer.get("arrive")

    def _parse(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    return DayScheduleItem(
        kind="city_transfer",
        ref_id=transfer.get("leg_id"),
        start=_parse(depart),
        end=_parse(arrive),
        duration_min=transfer.get("duration_min"),
        cost=transfer.get("price"),
        evidence=[
            f"{transfer.get('origin_text') or '出发城市'} → "
            f"{transfer.get('destination_text') or window.city}"
        ],
    )


def pack_day(
    window: DayWindow,
    attractions: Sequence[DayAttraction],
    *,
    transfers: Mapping[tuple[str, str], int] | None = None,
    lodging: LodgingOption | None = None,
    meals: Sequence[MealSuggestion] | None = None,
) -> DayPlan:
    """Lay one day out on a clock, and say what did not fit."""

    notes: list[str] = []
    items: list[DayScheduleItem] = []
    clock = _minutes(window.start)
    ceiling = _minutes(window.end)
    meal_list = list(meals or [])

    transfer_item = _transfer_item(window)
    if transfer_item is not None:
        items.append(transfer_item)
        if transfer_item.end is not None and transfer_item.end.date() == window.date:
            clock = max(clock, _minutes(transfer_item.end.time()))
        if transfer_item.end is not None and transfer_item.end.time() >= ARRIVE_LATE_AFTER:
            notes.append("当天到达较晚，只安排就近活动")
    elif window.is_transfer_day:
        notes.append("跨城移动日，未获取到具体车次，当天只安排就近活动")

    placed: list[DayAttraction] = []
    overflow: list[DayAttraction] = []
    effective_ceiling = ceiling - (30 if lodging is not None else 0)

    for index, attraction in enumerate(attractions):
        duration = CATEGORY_DURATION.get(attraction.category, CATEGORY_DURATION["other"])
        if attraction.recommend_duration_min:
            duration = attraction.recommend_duration_min
        next_id = (
            attractions[index + 1].attraction_id if index + 1 < len(attractions) else None
        )
        gap = 0
        estimated = False
        if next_id:
            if transfers and (attraction.attraction_id, next_id) in transfers:
                gap = int(transfers[(attraction.attraction_id, next_id)] or 0)
            elif transfers:
                estimated = True

        if clock + duration > effective_ceiling:
            overflow.append(attraction)
            continue

        item = DayScheduleItem(
            kind="attraction",
            ref_id=attraction.attraction_id,
            start=_clock(window.date, clock),
            end=_clock(window.date, clock + duration),
            duration_min=duration,
            cost=attraction.ticket_price,
            transport_to_next=(
                {"duration_min": gap, "estimated": estimated} if next_id else None
            ),
            evidence=[attraction.provider] if attraction.provider else [],
        )
        items.append(item)
        placed.append(attraction)
        clock += duration + gap

    for meal in meal_list:
        items.append(
            DayScheduleItem(
                kind="meal",
                ref_id=None,
                start=_clock(window.date, clock),
                end=_clock(window.date, clock + 60),
                duration_min=60,
                cost=meal.price_hint,
                evidence=[meal.name] if meal.name else [],
            )
        )
        clock += 60

    if lodging is not None and placed:
        items.append(
            DayScheduleItem(
                kind="lodging",
                ref_id=None,
                start=_clock(window.date, clock),
                duration_min=None,
                evidence=[lodging.name or lodging.area or "住宿"],
            )
        )

    if overflow:
        notes.append(
            f"{len(overflow)} 个景点当日时间不足，建议移至其它日期："
            + "、".join(item.name for item in overflow[:3])
        )

    return DayPlan(
        day_index=window.day_index,
        date=window.date,
        city=window.city,
        segment_index=window.segment_index,
        overnight_city=window.overnight_city,
        is_transfer_day=window.is_transfer_day,
        items=items,
        attractions=placed,
        meals=list(meal_list),
        lodging=[lodging] if lodging is not None else [],
        notes=[*window.notes, *notes],
    )


def _candidate_day_modes(day: DayPlan) -> list[str]:
    """Translate a day's activities into the weather vocabulary."""

    modes: list[str] = []
    for attraction in day.attractions:
        if attraction.category in _OUTDOOR:
            modes.append("outdoor")
        elif attraction.category in _INDOOR:
            modes.append("museum")
    for item in day.items:
        if item.kind == "city_transfer" and item.evidence:
            modes.append("train")
    return modes or ["outdoor"]


def adjust_for_weather(
    day: DayPlan,
    weather: Mapping[str, Any],
    *,
    indoor_pool: Sequence[DayAttraction] = (),
) -> DayPlan:
    """Apply the existing two-path weather rules to a day.

    Hard vetoes swap outdoor stops for an indoor alternative when the pool has
    one; when it does not, the day is kept but flagged, because silently
    deleting a stop is worse than showing a warning the user can act on.
    """

    verdict = weather_rules.assess(dict(weather or {}), modes=_candidate_day_modes(day))
    update: dict[str, Any] = {"weather": dict(weather or {})}
    notes = list(day.notes)

    if verdict.vetoes:
        update["weather_veto"] = True
        notes.extend(verdict.notes)
        indoor = [item for item in indoor_pool if item.category in _INDOOR]
        if indoor:
            replaced = list(day.attractions)
            swaps = min(len(indoor), len(replaced))
            for idx in range(swaps):
                replaced[idx] = indoor[idx]
            update["attractions"] = replaced
            notes.append(f"天气硬性否决，已替换 {swaps} 个户外安排为室内备选")
        else:
            notes.append("天气触发硬性否决，但没有可替换的室内备选，请自行决定是否改期")
    elif verdict.penalties or verdict.notes:
        notes.extend(verdict.notes)

    update["notes"] = notes
    updated = day.model_copy(update=update)
    return updated


def build_lodging_options(
    segments: Sequence[TripSegment],
    windows: Sequence[DayWindow],
) -> dict[int, LodgingOption]:
    """One lodging entry per night, following the city the traveller sleeps in.

    F1 keeps this deterministic and free: the name is the area the user asked
    for, or an advisory naming the day's district. F2 replaces the advisory with
    a real ``poi.stay`` quote, and the price stays advisory until then.
    """

    options: dict[int, LodgingOption] = {}
    by_city = {segment.city: segment for segment in segments}
    for window in windows:
        # Where you sleep is not always where you spent the day: on a transfer
        # day the stay belongs to the city you arrive in, so look the segment up
        # by overnight city and only fall back to the activity segment.
        sleeping_city = window.overnight_city or _arrival_city(window) or window.city
        segment = by_city.get(sleeping_city)
        if segment is None and 0 <= window.segment_index < len(segments):
            segment = segments[window.segment_index]
        area = (segment.lodging_area if segment else None) or sleeping_city
        options[window.day_index] = LodgingOption(
            name=f"{area}一带",
            area=area,
            coord=segment.coord if segment else None,
            provider="policy",
            advisory=True,
            nights=1,
        )
    return options


def _arrival_city(window: DayWindow) -> str | None:
    """Read the arrival city out of the day's own transfer item.

    The day layer cannot import the orchestration node that built the leg, so it
    reads the label the item already carries instead of a new field: the move is
    described in ``evidence`` as "出发地 → 目的地".
    """

    for item in window.items:
        if item.kind != "city_transfer":
            continue
        for note in item.evidence:
            if "→" in note:
                arrival = note.split("→")[-1].strip()
                if arrival:
                    return arrival
    return None


def price_estimate(
    days: Sequence[DayPlan],
    segments: Sequence[TripSegment],
    *,
    transfers: Sequence[dict[str, Any]] = (),
) -> tuple[float | None, str]:
    """Total *known* cost, plus an explicit statement of what is missing.

    Returns ``None`` when nothing is priceable -- a fabricated ``0`` would read
    as "free", which is worse than "unknown".
    """

    tickets = sum(
        attraction.ticket_price or 0
        for day in days
        for attraction in day.attractions
        if attraction.ticket_price is not None
    )
    nights = sum(len(day.lodging) for day in days)
    lodging = 0.0
    priced_nights = 0
    for day in days:
        for option in day.lodging:
            if option.nightly_price is not None:
                lodging += option.nightly_price
                priced_nights += 1
    rail = sum(
        float(leg.get("price") or 0)
        for leg in transfers
        if leg.get("price") is not None
    )

    known = tickets + lodging + rail
    parts: list[str] = []
    if tickets:
        parts.append(f"门票 ¥{tickets:,.0f}")
    if priced_nights:
        parts.append(f"住宿 ¥{lodging:,.0f}（{priced_nights} 晚）")
    if rail:
        parts.append(f"城际交通 ¥{rail:,.0f}")
    missing: list[str] = []
    if nights > priced_nights:
        missing.append("住宿参考价")
    if not tickets:
        missing.append("门票")
    note = "含" + "、".join(parts) if parts else "暂无可估算项目"
    if missing:
        note += f"；未含{('、'.join(missing))}与市内交通、餐饮"
    else:
        note += "；未含市内交通与餐饮"
    return (known if parts else None), note


def anchors_from_segments(
    segments: Sequence[TripSegment],
    windows: Sequence[DayWindow],
) -> dict[int, Coord | None]:
    """Anchor each day on the stay location the user chose for that city."""

    anchors: dict[int, Coord | None] = {}
    for window in windows:
        segment = (
            segments[window.segment_index]
            if 0 <= window.segment_index < len(segments)
            else None
        )
        anchors[window.day_index] = segment.coord if segment else None
    return anchors


def default_meals(city: str) -> list[MealSuggestion]:
    """F1 dining: a deterministic "eat nearby" line, no quota spent.

    Real restaurant POIs are F2 (``poi.food``); until then the honest output is a
    suggestion that cannot be wrong, not a fabricated restaurant name.
    """

    return [
        MealSuggestion(slot="lunch", name=f"{city} 就近午餐", kind="local", provider="policy"),
        MealSuggestion(slot="dinner", name=f"{city} 就近晚餐", kind="local", provider="policy"),
    ]


def estimate_transfer_minutes(
    origin: Coord | None,
    destination: Coord | None,
    *,
    average_kmh: float = 30.0,
    overhead_min: int = 15,
) -> int | None:
    """Fallback travel time when no route capability answered.

    The same 30 km/h + 15 min rule the route tools use as an estimate, so an
    unverified number is at least consistent with every other estimate and is
    always labelled as such by the caller.
    """

    distance = haversine_km(origin, destination)
    if distance is None:
        return None
    return int(round(distance / average_kmh * 60 + overhead_min))
