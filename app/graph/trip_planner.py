"""M3: chain legs into a door-to-door itinerary.

A trip is not one leg. "望京到上海" is: get to the station, take the train,
get from the station. Each hop has a different mode, and the binding constraint
is temporal -- leg N must *finish* before leg N+1 starts, with a buffer that
depends on what came before it (framework §6.5).

The legs are therefore constructed *backwards from the train*: find the
departure, subtract the buffer and the access time, and the first leg is
defined. That makes the chain valid by construction, and the validator then
independently confirms it rather than being the thing that discovers it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.domain.ids import new_id
from app.domain.timebase import LOCAL_TZ, combine_ymd_hm, minutes_between
from app.graph.leg_planner import (
    RAIL_MIN_KM,
    LegPlan,
    PlaceMatch,
    build_leg,
    forecast_for,
    haversine_km,
    rail_candidates,
    resolve_destinations,
    resolve_place,
    route_between,
    score_candidates,
)
from app.policy import buffer as buffer_policy
from app.policy import constraints as constraint_policy

#: How many train options to try before giving up on the chain.
MAX_CHAIN_ATTEMPTS = 4


@dataclass
class TripPlan:
    legs: list[dict[str, Any]] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    degradation: list[dict[str, Any]] = field(default_factory=list)


def _clock(day: str, hhmm: str | None) -> datetime | None:
    if not day or not hhmm:
        return None
    try:
        return combine_ymd_hm(day, hhmm)
    except (TypeError, ValueError):
        return None


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _city_candidates(station: str) -> list[str]:
    """``"北京南"`` -> ``"北京"``; ``"上海虹桥"`` -> no clean reduction."""

    base = (station or "").strip()
    if not base:
        return []
    forms = [base]
    if base.endswith("站"):
        forms.append(base[:-1])
    trimmed = forms[-1]
    if trimmed and trimmed[-1] in "东南西北":
        forms.append(trimmed[:-1])
    # Most-reduced first: "北京" is a city, "北京南" is an airport near it.
    return list(dict.fromkeys(f for f in reversed(forms) if f))


#: Feature codes that are never a railway station. GeoNames has no rail
#: stations in China, so an unresolvable name tends to fall through to the
#: nearest airport -- which is how "北京南" became a closed airfield 25 km away.
_NOT_A_STATION = frozenset({"AIRP", "AIRH", "AIRF", "RSTN", "RSTP"})


async def _station_place(deps: Any, name: str | None, *, fallback_city: str | None = None) -> PlaceMatch:
    """Locate a railway station well enough to measure the transfer.

    GeoNames carries no railway stations: ``北京南站`` returns nothing, and the
    bare ``北京南`` matches Beijing's *closed Nanyuan airport* 25 km away. A
    confident wrong coordinate is worse than an approximate right one, so this
    resolves the station's **city centre** and flags the leg as approximate.
    """

    for query in [*_city_candidates(name or ""), fallback_city or ""]:
        if not query:
            continue
        match, _ = await resolve_place(deps, query)
        if match.found and match.feature_code not in _NOT_A_STATION:
            return match.model_copy(update={"approx": True})
    return PlaceMatch(query=name or "")


async def build_trip(
    deps: Any,
    *,
    origin_text: str,
    destination_text: str,
    day: str | None,
    weights: dict[str, float] | None = None,
    force_intercity: bool = False,
) -> TripPlan:
    """Build the best chain we can, and say what we could not build.

    ``force_intercity`` makes the rail chain run even when the two cities are
    close enough that a road trip would normally win. That is what a *multi-city*
    trip needs: 北京 → 天津 is only ~120 km, but a traveller changing city wants a
    train and a timetable, not "drive 1h40m" and no way to book it.
    """

    origin, _ = await resolve_place(deps, origin_text)
    leg_plan: LegPlan | None = None

    # Destinations must be *disambiguated before* anything measures distance.
    # A bare 长城 geocodes to a village of the same name in Shanxi, ~400 km
    # away, which flipped a day-trip to the Great Wall into a cross-province
    # rail query to 晋中. The landmark expansion has to run first, so the
    # scope decision sees 八达岭 (67 km) rather than a stranger's village.
    destinations, _notes = await resolve_destinations(deps, destination_text)
    intercity = force_intercity or bool(
        origin.found
        and destinations
        and min(haversine_km(origin, dest) for dest in destinations) >= RAIL_MIN_KM
    )

    if not intercity:
        leg_plan = await build_leg(
            deps, origin_text=origin_text, destination_text=destination_text, day=day, weights=weights
        )
        return TripPlan(legs=[leg_plan.leg], notes=list(leg_plan.notes), degradation=list(leg_plan.degradation))

    return await _build_rail_chain(
        deps,
        origin=origin,
        destinations=destinations,
        day=day,
        weights=weights,
    )


async def _build_rail_chain(
    deps: Any,
    *,
    origin: PlaceMatch,
    destinations: list[PlaceMatch],
    day: str | None,
    weights: dict[str, float] | None,
) -> TripPlan:
    plan = TripPlan()

    dest = destinations[0]
    weather, notes_weather = await forecast_for(deps, dest, day)
    plan.degradation.extend(notes_weather)

    trains, notes_rail = await rail_candidates(
        deps, origin=origin, dest=dest, day=day, weather=weather, weights=weights
    )
    plan.degradation.extend(notes_rail)
    if not trains:
        plan.notes.append("没有查到铁路方案，已回退到公路方案")
        fallback = await build_leg(
            deps, origin_text=origin.name or "", destination_text=(dest.name or ""), day=day, weights=weights
        )
        plan.legs = [fallback.leg]
        plan.degradation.extend(fallback.degradation)
        return plan

    # `rail_candidates` returns *unscored* options on purpose (a score only
    # means something relative to its set), and it leaves the transient
    # assessment attached. Score them here, or the ranking below sorts by a
    # missing key and the plan carries a non-serialisable object.
    score_candidates(trains, weights=weights, degradation=plan.degradation)

    # Cheapest-and-quickest first; try each until one chains cleanly.
    ranked = sorted(trains, key=lambda c: c.get("score") or 0, reverse=True)[:MAX_CHAIN_ATTEMPTS]

    for train in ranked:
        chain, reason = await _chain_for_train(deps, origin=origin, dest=dest, train=train, day=day, weather=weather)
        plan.degradation.extend(chain.pop("_degradation", []))
        if chain["legs"]:
            plan.legs = chain["legs"]
            plan.violations = chain["violations"]
            if chain["notes"]:
                plan.notes.extend(chain["notes"])
            if reason:
                plan.notes.append(reason)
            return plan
        plan.notes.append(reason or f"{train.get('train_code')} 无法衔接")

    # Nothing chained: show the rail option alone rather than nothing at all.
    best = ranked[0]
    plan.notes.append("所有车次都无法与市内接驳衔接，仅给出铁路段")
    plan.legs = [_rail_leg(best, day)]
    return plan


async def _chain_for_train(
    deps: Any,
    *,
    origin: PlaceMatch,
    dest: PlaceMatch,
    train: dict[str, Any],
    day: str | None,
    weather: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Access leg + rail leg + egress leg, built backwards from the departure."""

    degradation: list[dict[str, Any]] = []
    notes: list[str] = []

    depart = _clock(day or "", train.get("depart_time"))
    arrive = _clock(day or "", train.get("arrive_time"))
    if depart is None or arrive is None:
        return {"legs": [], "violations": [], "notes": [], "_degradation": degradation}, "车次时刻缺失"
    if arrive < depart:  # overnight service
        arrive += timedelta(days=1)

    rail_leg = _rail_leg(train, day, depart=depart, arrive=arrive)

    from_station = await _station_place(deps, train.get("from_station"), fallback_city=origin.name)
    to_station = await _station_place(deps, train.get("to_station"), fallback_city=dest.name)

    legs: list[dict[str, Any]] = []
    access_leg = None
    if origin.found and from_station.found and haversine_km(origin, from_station) >= 1.0:
        access_route, notes_route = await route_between(deps, origin, from_station, "driving")
        degradation.extend(notes_route)
        if access_route:
            board_buffer = buffer_policy.required_buffer_minutes(
                prev_mode="driving", cross_station=False, uncertainty=0
            )
            access_arrive = depart - timedelta(minutes=board_buffer)
            access_depart = access_arrive - timedelta(minutes=access_route["duration_min"])
            access_leg = _road_leg(
                origin=origin, dest=from_station, route=access_route,
                day=day, depart=access_depart, arrive=access_arrive,
                label=f"{origin.name} → {train.get('from_station')}",
                dest_label=train.get("from_station"),
            )
            access_leg["buffer_minutes"] = board_buffer
            access_leg["approx"] = True
            legs.append(access_leg)
    else:
        notes.append("起点到车站的接驳没能建立（车站坐标未解析）")

    legs.append(rail_leg)

    if dest.found and to_station.found and haversine_km(to_station, dest) >= 1.0:
        egress_route, notes_route = await route_between(deps, to_station, dest, "driving")
        degradation.extend(notes_route)
        if egress_route:
            alight_buffer = buffer_policy.required_buffer_minutes(
                prev_mode="train", cross_station=False, uncertainty=0
            )
            egress_depart = arrive + timedelta(minutes=alight_buffer)
            egress_arrive = egress_depart + timedelta(minutes=egress_route["duration_min"])
            egress_leg = _road_leg(
                origin=to_station, dest=dest, route=egress_route,
                day=day, depart=egress_depart, arrive=egress_arrive,
                label=f"{train.get('to_station')} → {dest.name}",
                origin_label=train.get("to_station"),
            )
            egress_leg["buffer_minutes"] = alight_buffer
            egress_leg["approx"] = True
            legs.append(egress_leg)
    else:
        notes.append("车站到目的地的接驳没能建立（车站坐标未解析）")

    violations = constraint_policy.validate_trip(legs, constraints={"uncertainty": 0})
    hard = [v for v in violations if v.recoverable is False]
    if hard:
        return {"legs": [], "violations": [], "notes": [], "_degradation": degradation}, (
            f"{train.get('train_code')} 衔接不成立：" + "；".join(v.detail for v in hard[:1])
        )

    return {
        "legs": legs,
        "violations": [v.model_dump(mode="json") for v in violations],
        "notes": notes,
        "_degradation": degradation,
    }, None


def _rail_leg(
    train: dict[str, Any],
    day: str | None,
    *,
    depart: datetime | None = None,
    arrive: datetime | None = None,
) -> dict[str, Any]:
    depart = depart or _clock(day or "", train.get("depart_time"))
    arrive = arrive or _clock(day or "", train.get("arrive_time"))
    return {
        "leg_id": new_id("leg"),
        "scope": "intercity",
        "kind": "rail",
        "origin_text": train.get("from_station"),
        "destination_text": train.get("to_station"),
        "day": day,
        "depart": _iso(depart),
        "arrive": _iso(arrive),
        "duration_min": train.get("duration_min"),
        "price": train.get("price"),
        "modes": ["train"],
        "train_code": train.get("train_code"),
        "seats": train.get("seats"),
        "selected_candidate_id": train.get("candidate_id"),
        "candidates": [train],
        "reason_trace": train.get("reason_trace"),
    }


def _road_leg(
    *,
    origin: PlaceMatch,
    dest: PlaceMatch,
    route: dict[str, Any],
    day: str | None,
    depart: datetime | None,
    arrive: datetime | None,
    label: str,
    origin_label: str | None = None,
    dest_label: str | None = None,
) -> dict[str, Any]:
    return {
        "leg_id": new_id("leg"),
        "scope": "local",
        "kind": "road",
        # The coordinate may be a city-centre stand-in; the label is the
        # station the traveller actually has to reach.
        "origin_text": origin_label or origin.name,
        "destination_text": dest_label or dest.name,
        "label": label,
        "approx": True,
        "day": day,
        "depart": _iso(depart),
        "arrive": _iso(arrive),
        "duration_min": route.get("duration_min"),
        "distance_km": route.get("distance_km"),
        "modes": [route.get("mode", "driving")],
        "selected_candidate_id": None,
        "candidates": [],
    }
