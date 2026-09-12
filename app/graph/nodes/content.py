"""Content layer nodes (F1): build the day-by-day plan, then render its prose.

Two nodes on purpose, and the split is the whole design:

* :func:`plan_content` calls capabilities and deterministic policy only. It
  decides days, cities, transfer days, attraction assignment, lodging and price.
* :func:`content_render` calls the LLM and writes **prose only**. Every fact it
  is shown came from the first node, and everything it returns is checked in code
  against that plan.

Keeping them apart is what makes the promise "the itinerary survives the model
being unavailable" true: when ``content_render`` fails, the plan is already
complete and only the wording falls back to templates.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Sequence

from app.domain.geo import Coord, Crs
from app.domain.ids import new_id
from app.domain.timebase import parse_ymd, to_ymd
from app.domain.trip import (
    DayAttraction,
    DayPlan,
    TripSegment,
)
from app.graph.deps import GraphDeps
from app.graph.leg_planner import PlaceMatch, resolve_destinations
from app.graph.nodes.streaming import close_stream, stream_callback
from app.graph.state import AgentState, emit
from app.graph.stub_fallback import llm_for
from app.graph.trip_planner import build_trip
from app.llm.context import assemble, summarise_state
from app.llm.schemas import ItineraryRenderOutput
from app.policy import day_plan, segments as segment_policy
from app.policy import slots as slot_policy

_RENDER_TASK = (
    "任务：把已经排好的多日行程渲染成中文主题与注意事项。\n"
    "硬性要求：\n"
    "1. 只能使用下方已给出的城市、日期、景点与天气事实，"
    "不得新增任何地点、价格、营业时间、评分数字。\n"
    "2. day_themes 的 day_index 必须与输入的天数一一对应，一天一条，不得增删。\n"
    "3. 换城日的主题必须体现“移动”，不得把两个城市的点混在同一句里。\n"
    "4. theme 不超过 12 字；summary 不超过 60 字，只描述这一天在做什么，"
    "不承诺任何具体时刻、票价或酒店名。\n"
    "5. content_notes 只写可执行、可验证的小贴士，不得出现具体金额。\n"
    "6. citations 只能填输入中出现过的 attraction_id 或 day_index，没有依据就留空。"
)

#: A digit anywhere in generated prose means a number nobody verified.
_NUMBER_RE = re.compile(r"\d")


def _degradation(capability_id: str, level: int, reason: str) -> dict[str, Any]:
    return {"capability_id": capability_id, "provider": None, "level": level, "reason": reason}


def _slot_value(state: AgentState, key: str) -> Any:
    raw = (state.get("slots") or {}).get(key)
    if isinstance(raw, dict):
        return raw.get("value")
    return raw


def _resolved_slots(state: AgentState) -> dict[str, slot_policy.SlotValue]:
    return {
        key: slot_policy.SlotValue.model_validate(value)
        for key, value in (state.get("slots") or {}).items()
    }


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


def _segment_text(state: AgentState) -> str:
    """The user's own words about where they are going, for the parser.

    Two sources, in order of fidelity: the ``destinations`` slot when the
    extractor understood the multi-city shape, otherwise the plain destination
    plus whatever the schema-level ``destination_segments`` carried. The parser
    then re-derives the structure deterministically, so a partially-parsed
    extraction still lands on a sensible itinerary.
    """

    raw = _slot_value(state, "destinations")
    # The gate's normaliser already renders this slot into the parser's own
    # vocabulary ("北京3天，天津2天"), so a plain string is the expected shape by the
    # time planning runs. A raw list is still accepted: a future extractor may
    # hand one over before normalisation.
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, list) and raw:
        pieces: list[str] = []
        for item in raw:
            if isinstance(item, dict) and item.get("city"):
                days = item.get("days")
                pieces.append(f"{item['city']}{days}天" if days else str(item["city"]))
            elif isinstance(item, str) and item:
                pieces.append(item)
        if pieces:
            return "，".join(pieces)

    plain = _slot_value(state, "destination")
    pieces = [str(plain)] if plain else []
    for extra in (state.get("destination_segments") or []):
        if isinstance(extra, dict) and extra.get("city"):
            pieces.append(str(extra["city"]))
    return "，".join(pieces)


def _coord_of(match: PlaceMatch | None) -> Coord | None:
    """A ``PlaceMatch`` carries bare lat/lon; the day layer needs a CRS."""

    if match is None or not match.found:
        return None
    return Coord(lat=float(match.lat), lon=float(match.lon), crs=Crs.WGS84)


def _build_segments(
    parsed: segment_policy.SegmentParse,
    trip_days: int,
    matches: dict[str, PlaceMatch],
) -> tuple[list[TripSegment], int, list[str]]:
    """Turn parsed specs into domain segments with coordinates attached."""

    notes: list[str] = []
    planned, transfer_days = segment_policy.allocate_days(parsed.segments, trip_days, notes=notes)
    result: list[TripSegment] = []
    for spec in planned:
        result.append(
            TripSegment(
                segment_id=spec.segment_id,
                city=spec.city,
                coord=_coord_of(matches.get(spec.city)),
                day_count=spec.days,
                lodging_area=spec.lodging_area,
            )
        )
    return result, transfer_days, notes


async def _resolve_cities(deps: GraphDeps, cities: Sequence[str]) -> tuple[dict[str, PlaceMatch], list[dict[str, Any]]]:
    matches: dict[str, PlaceMatch] = {}
    degradation: list[dict[str, Any]] = []
    for city in cities:
        places, notes = await resolve_destinations(deps, city)
        degradation.extend(notes)
        if places:
            matches[city] = places[0]
    return matches, degradation


def _leaving_day(windows: Sequence[Any], segment_index: int) -> Any | None:
    """The day you actually leave ``segment_index``.

    Two shapes have to work here. When a transfer day was reserved, it is its own
    window. When the user's day counts already fill the trip ("北京3天+天津2天"
    over five days), there is no spare day, so the move shares the last day of the
    stay -- and that is still the day the leg belongs on.
    """

    reserved = [
        window for window in windows if window.is_transfer_day and window.segment_index == segment_index
    ]
    if reserved:
        return reserved[0]
    own = [window for window in windows if window.segment_index == segment_index]
    return own[-1] if own else None


def _as_datetime(value: Any) -> datetime | None:
    """Parse a leg timestamp, which may be an ISO string or already a datetime."""

    if value is None or isinstance(value, datetime):
        return value if isinstance(value, datetime) else None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _retime_leg(leg: dict[str, Any], day: Any, *, depart_at: Any = None) -> dict[str, Any]:
    """Move a leg onto the day it actually happens and give it a clock.

    The chain builder works from a departure date and returns times on that date;
    a transfer leg belongs on the transfer day, so the date part is replaced and
    the clock is kept. An arrival before departure means the service runs past
    midnight, which is why the arrival gets the next day.

    A road chain carries no timetable at all -- there is nothing to look up -- so
    it gets ``depart_at`` (the day's own start) plus its duration, and is marked
    as an estimate. Leaving it timeless would put a leg with no clock on a
    timeline that is supposed to start from its arrival.
    """

    if day is None:
        return leg

    def _move(value: Any) -> datetime | None:
        moment = _as_datetime(value)
        return datetime.combine(day, moment.time()) if moment is not None else None

    depart = _move(leg.get("depart"))
    arrive = _move(leg.get("arrive"))
    had_clock = depart is not None
    # ``depart_at`` is a bare ``time`` (the day's rhythm), not a timestamp.
    start_of_day = (
        depart_at
        if isinstance(depart_at, datetime)
        else datetime.combine(day, depart_at)
        if isinstance(depart_at, time)
        else _as_datetime(depart_at)
    )

    if depart is None and start_of_day is not None:
        depart = datetime.combine(day, start_of_day.time())
    if arrive is None and depart is not None and leg.get("duration_min"):
        arrive = depart + timedelta(minutes=int(leg["duration_min"]))
    if depart is not None and arrive is not None and arrive < depart:
        arrive += timedelta(days=1)

    return {
        **leg,
        "day": to_ymd(day),
        "depart": depart.isoformat() if depart else None,
        "arrive": arrive.isoformat() if arrive else None,
        # A road estimate is not a timetable, and the itinerary must say so.
        "approx": bool(leg.get("approx")) or not had_clock,
    }


async def _build_transfers(
    deps: GraphDeps,
    segments: Sequence[TripSegment],
    windows: Sequence[Any],
    degradation: list[dict[str, Any]],
    notes: list[str],
) -> list[dict[str, Any]]:
    """Attach a real cross-city leg to every hop between two segments.

    This is the F1.5 gap: the day layer could already reserve time for a move, but
    nothing filled it. Each hop now gets an actual chain (rail first, road as the
    fallback) anchored on the transfer day, so the itinerary shows a train and a
    timetable instead of "未获取到具体车次".
    """

    transfers: list[dict[str, Any]] = []
    for index in range(len(segments) - 1):
        window = _leaving_day(windows, index)
        if window is None:
            continue
        origin, destination = segments[index], segments[index + 1]
        if origin.coord is None or destination.coord is None:
            notes.append(f"{origin.city} → {destination.city}：两端坐标未解析，未查询城际交通")
            degradation.append(_degradation("place.resolve", 2, "cross-city endpoints unresolved"))
            continue

        # A railway timetable is what a city change needs, even when the two
        # cities are close enough that driving would normally win.
        plan = await build_trip(
            deps,
            origin_text=origin.city,
            destination_text=destination.city,
            day=to_ymd(window.date),
            force_intercity=True,
        )
        degradation.extend(plan.degradation)
        for note in plan.notes:
            notes.append(f"{origin.city} → {destination.city}：{note}")
        if not plan.legs:
            notes.append(f"{origin.city} → {destination.city}：未查到可用方案，当天仅留出移动时间")
            continue

        retimed = [
            _retime_leg(dict(leg), window.date, depart_at=window.start) for leg in plan.legs
        ]
        # A road chain has no duration of its own when the route lookup failed;
        # without one the leg has a departure and no arrival, which cannot be
        # drawn. The deterministic estimate is labelled, never presented as real.
        for leg in retimed:
            if leg.get("arrive") or leg.get("duration_min"):
                continue
            minutes = day_plan.estimate_transfer_minutes(origin.coord, destination.coord)
            if minutes:
                leg["duration_min"] = minutes
                leg["approx"] = True
                notes.append(
                    f"{origin.city} → {destination.city}：未取到实时路线，行程时间 {minutes} 分钟为估算"
                )
                retimed = [_retime_leg(item, window.date, depart_at=window.start) for item in retimed]
                break
        destination.transfer_in = retimed[0]
        transfers.extend(retimed)

        # The window keeps the departing city (that is where the day starts and
        # what the attractions belong to) but the arrival city is now known, so
        # the "今晚住哪" line can follow it. The flag matters too: a moving day
        # gets fewer stops and its timeline starts at the arrival.
        arriving_city = retimed[-1].get("destination_text") or destination.city
        window.is_transfer_day = True
        window.transfer = retimed[0]
        window.overnight_city = arriving_city
    return transfers


# ---------------------------------------------------------------------------
# Attractions (F1: from the user's own destination words, no new data source)
# ---------------------------------------------------------------------------


async def _attraction_pool(
    deps: GraphDeps,
    segments: Sequence[TripSegment],
    degradation: list[dict[str, Any]],
) -> dict[int, list[DayAttraction]]:
    """Candidate attractions per segment index.

    F1 deliberately does not query a POI directory: the source that would answer
    it (``poi.discover``) returns unnormalised payloads today, so relying on it
    would make the day layer depend on a capability that may be empty. Instead the
    user's own destination plus its disambiguated alternatives become the pool,
    which is already enough to lay out a multi-day skeleton -- and it needs no key.
    """

    pools: dict[int, list[DayAttraction]] = {}
    for index, segment in enumerate(segments):
        if segment.coord is None:
            pools[index] = []
            continue
        pools[index] = [
            DayAttraction(
                attraction_id=new_id("attr"),
                name=segment.city,
                category="other",
                coord=segment.coord,
                crs=segment.coord.crs,
                provider="place.resolve",
                description=None,
            )
        ]
    if segments and all(not pool for pool in pools.values()):
        # Coordinates are what the day layer needs; without them there is nothing
        # to place on a day, and this has to be visible rather than an empty plan.
        degradation.append(_degradation("place.resolve", 2, "no attraction candidates resolved"))
        for segment in segments:
            segment.notes = [*segment.notes, "该城市未解析到坐标，未排入具体景点"]
    return pools


# ---------------------------------------------------------------------------
# The planning node
# ---------------------------------------------------------------------------


async def plan_content(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    """Build the day-by-day content plan (no LLM)."""

    degradation = list(state.get("degradation_log") or [])
    derived = dict(state.get("derived_slots") or {})
    trip_days = int(derived.get("trip_days") or 0) or 1

    depart_raw = _slot_value(state, "depart_date")
    try:
        depart = parse_ymd(depart_raw)
    except (TypeError, ValueError):
        depart = date.today() + timedelta(days=1)
        degradation.append(_degradation("clock.today", 2, "departure date unreadable, used tomorrow"))

    parsed = segment_policy.parse_segments(_segment_text(state))
    notes: list[str] = list(parsed.notes)

    matches, more = await _resolve_cities(deps, [spec.city for spec in parsed.segments])
    degradation.extend(more)
    planned, transfer_days, alloc_notes = _build_segments(parsed, trip_days, matches)
    notes.extend(alloc_notes)
    if not planned:
        return {
            "planned_content": {},
            "degradation_log": degradation,
            "assumptions": [*(state.get("assumptions") or []), *notes],
            "outbox": emit(state, "state_update", {"phase": "PREVIEW", "content": "empty"}),
        }

    windows = day_plan.make_day_windows(
        depart, trip_days, rhythm=_slot_value(state, "day_rhythm") or None
    )
    windows = day_plan.assign_days_to_segments(planned, windows, transfer_days=transfer_days)

    # Cross-city travel (F1.5): each hop gets a real chain, retimed onto its
    # transfer day. Run before packing so a transfer day's timeline starts from
    # the actual arrival rather than from the default morning.
    transfers = await _build_transfers(deps, planned, windows, degradation, notes)

    pools = await _attraction_pool(deps, planned, degradation)
    anchors = day_plan.anchors_from_segments(planned, windows)

    # Assign per segment so days never mix cities.
    assignment: dict[int, list[DayAttraction]] = {}
    unassigned: list[DayAttraction] = []
    for index in range(len(planned)):
        segment_windows = [window for window in windows if window.segment_index == index]
        if not segment_windows:
            continue
        by_day, left, assign_notes = day_plan.assign_attractions(
            pools.get(index, []),
            segment_windows,
            anchors=anchors,
            per_day_max=deps.settings.per_day_max_attractions,
        )
        assignment.update(by_day)
        unassigned.extend(left)
        notes.extend(assign_notes)

    lodging = day_plan.build_lodging_options(planned, windows)
    days: list[DayPlan] = []
    for window in windows:
        stops = assignment.get(window.day_index, [])
        day = day_plan.pack_day(
            window,
            stops,
            lodging=lodging.get(window.day_index),
            meals=day_plan.default_meals(window.city) if stops else [],
        )
        days.append(day)

    total, cost_note = day_plan.price_estimate(days, planned, transfers=transfers)

    content = {
        "segments": [segment.model_dump(mode="json") for segment in planned],
        "days": [day.model_dump(mode="json") for day in days],
        "transfers": transfers,
        "content_notes": [],
        "degraded_content": list(degradation),
        "cost_estimate": total,
        "cost_note": cost_note,
        "unassigned": [item.attraction_id for item in unassigned],
        "transfer_days": transfer_days,
    }
    return {
        "planned_content": content,
        # The cross-city legs join the plan's leg list so they are frozen by the
        # same hash and shown by the same renderer as the door-to-door legs.
        "planned_legs": [*(state.get("planned_legs") or []), *transfers],
        "degradation_log": degradation,
        "assumptions": [*(state.get("assumptions") or []), *notes],
        "outbox": emit(
            state,
            "state_update",
            {
                "phase": "PREVIEW",
                "days": len(days),
                "segments": len(planned),
                "transfer_days": transfer_days,
                "transfers": len(transfers),
                "cost_estimate": total,
            },
        ),
    }


# ---------------------------------------------------------------------------
# The rendering node (the only LLM call in the content layer)
# ---------------------------------------------------------------------------


def template_themes(days: Sequence[DayPlan]) -> dict[int, tuple[str, str]]:
    """Deterministic themes, used whenever the model is unavailable.

    This is the whole reason the content layer can promise an itinerary with no
    key and no network: the wording is produced here, not borrowed.
    """

    themes: dict[int, tuple[str, str]] = {}
    for day in days:
        if day.is_transfer_day:
            theme = f"转场 {day.city} → {day.overnight_city or '下一站'}"
        elif day.weather_veto:
            theme = "天气改室内"
        else:
            categories = {item.category for item in day.attractions}
            if "museum" in categories:
                theme = "博物馆日"
            elif categories & {"park", "viewpoint"}:
                theme = "户外日"
            elif "historic" in categories:
                theme = "历史城区"
            elif day.attractions:
                theme = "城区漫步"
            else:
                theme = "机动安排"
        if day.attractions:
            summary = "、".join(item.name for item in day.attractions[:3])
        else:
            # Empty days are honest, but they must read as "nothing was supplied
            # to fill it" rather than "the planner ran out of ideas".
            summary = f"{day.city} 机动，视体力与天气决定"
        themes[day.day_index] = (theme, summary[:60])
    return themes


def template_notes(days: Sequence[DayPlan]) -> list[str]:
    notes = ["跨城日请预留取票与安检时间，末班车后到达需确认当晚入住时间"]
    if any(day.weather_veto for day in days):
        notes.append("有日期触发天气硬性否决，出发前一天请再确认一次预报")
    empty = [day.day_index for day in days if not day.attractions and not day.is_transfer_day]
    if empty:
        notes.append(
            "第 " + "、".join(str(index) for index in empty) + " 天未排入具体景点："
            "候选点不足，可告诉我你想去哪几个地方，我按位置重新排"
        )
    return notes[:5]


def _strip_numbers(text: str) -> str:
    """Remove any sentence carrying a number: the model cannot know one."""

    parts = [chunk.strip() for chunk in re.split(r"[。；;\n]", text) if chunk.strip()]
    kept = [chunk for chunk in parts if not _NUMBER_RE.search(chunk)]
    return "。".join(kept)


async def content_render(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    """Write the day themes and notes. Never changes the plan."""

    content = dict(state.get("planned_content") or {})
    days = [DayPlan.model_validate(item) for item in content.get("days") or []]
    if not days:
        return {"planned_content": content, "outbox": emit(state, "state_update", {"phase": "PREVIEW"})}

    degradation = list(state.get("degradation_log") or [])
    themes = template_themes(days)
    notes = template_notes(days)

    llm = llm_for(deps)
    prompt = assemble(
        task=_RENDER_TASK,
        schema_hint=(
            '{"scope":"<in_scope|ambiguous|out_of_scope|abuse>",'
            '"day_themes":[{"day_index":1,"theme":"","summary":""}],'
            '"content_notes":[""],"citations":[""]}'
        ),
        state_summary=summarise_state(
            slots=state.get("slots") or {},
            phase="PREVIEW",
            assumptions=state.get("assumptions") or [],
        ),
        user_input=json.dumps(
            [
                {
                    "day_index": day.day_index,
                    "date": to_ymd(day.date) if day.date else None,
                    "city": day.city,
                    "overnight_city": day.overnight_city,
                    "transfer": day.is_transfer_day,
                    "attractions": [
                        {"attraction_id": item.attraction_id, "name": item.name, "category": item.category}
                        for item in day.attractions
                    ],
                }
                for day in days
            ],
            ensure_ascii=False,
        ),
    )

    response = await llm.structured(
        node="itinerary_render",
        schema=ItineraryRenderOutput,
        prompt=prompt,
        on_delta=stream_callback(deps, "itinerary_render"),
    )
    parsed = response.parsed if isinstance(response.parsed, ItineraryRenderOutput) else None
    # Same contract as the preview: the stream was raw JSON, so the client is
    # told to drop it and use the day themes that actually made it into the plan.
    await close_stream(deps, "itinerary_render", ok=parsed is not None)
    if parsed is None:
        degradation.append(_degradation("llm.itinerary_render", 2, response.error or "invalid response"))
    else:
        allowed = {item.attraction_id for day in days for item in day.attractions}
        for item in parsed.day_themes:
            if not (1 <= item.day_index <= len(days)):
                degradation.append(
                    _degradation("llm.itinerary_render", 1, f"out-of-range day_index {item.day_index}")
                )
                continue
            theme = _strip_numbers(item.theme)[:12]
            summary = _strip_numbers(item.summary)[:60]
            fallback_theme, fallback_summary = themes[item.day_index]
            themes[item.day_index] = (theme or fallback_theme, summary or fallback_summary)
        cleaned = [_strip_numbers(note) for note in parsed.content_notes]
        notes = [note for note in cleaned if note][:5] or notes
        for citation in parsed.citations:
            if citation and citation not in allowed and not citation.isdigit():
                # A citation nobody can resolve is recorded, not raised: the plan
                # is still correct, only the explanation is thinner than claimed.
                degradation.append(
                    _degradation("llm.itinerary_render", 1, f"unresolved citation {citation}")
                )

    for day in days:
        theme, summary = themes[day.day_index]
        day.theme, day.summary = theme, summary

    content["days"] = [day.model_dump(mode="json") for day in days]
    content["content_notes"] = notes
    content["degraded_content"] = list(degradation)
    return {
        "planned_content": content,
        "degradation_log": degradation,
        "outbox": emit(state, "state_update", {"phase": "PREVIEW", "rendered": len(days)}),
    }
