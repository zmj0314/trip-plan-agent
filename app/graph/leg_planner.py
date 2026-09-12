"""M1 leg planning: real places, real routes, real weather.

This is where "出计划" stops being a skeleton. It resolves the two ends,
fans out over the plausible destinations, gets an actual route and an actual
forecast for each, scores them against the preference weights (with weather
participating on both its hard and soft paths), and records *why* the winner
won.

Two findings shape the design, both observed against the live free sources:

* **Generic landmark names must never be geocoded directly.** A search for 长城
  returns a village called 长城 in Shanxi -- a silent, confident, wrong answer
  1100 km away. Ambiguous landmarks are therefore expanded to their concrete
  sections *before* any lookup (framework §3.1).
* **A failed lookup is data, not an exception.** Every step appends to the
  degradation list, so the plan can still be shown with its gaps labelled.
"""

from __future__ import annotations

import json
import math
from typing import Any

from pydantic import BaseModel, Field

from app.domain.ids import new_id
from app.policy import reason_trace as trace_policy
from app.policy import scoring, weather_rules

#: Generic names the geocoder answers confidently and wrongly. Keyed by what the
#: user says, valued by concrete, resolvable sections.
LANDMARK_SECTIONS: dict[str, list[tuple[str, str]]] = {
    "长城": [
        ("八达岭长城", "八达岭"),
        ("慕田峪长城", "慕田峪"),
        ("司马台长城", "司马台"),
    ],
}

#: Suffixes that stop a place-name lookup from matching anything.
TRIMMABLE_SUFFIXES = (
    "soho",
    "SOHO",
    "大厦",
    "广场",
    "中心",
    "站",
    "机场",
    "火车站",
    "地铁站",
    "小区",
    "院",
)

#: Administrative markers. Addresses arrive far more specific than any geocoder
#: can handle -- "北京市朝阳区望京SOHO" matches nothing, while "望京" matches
#: exactly -- so the progressively shorter tails are tried in order.
ADMIN_MARKERS = "省市区县镇乡"

DEFAULT_MODE = "driving"

#: Multi-modal comparison. Only modes that are physically plausible for the
#: distance are offered -- a three-day walk is not a choice, it is noise.
MULTIMODAL: tuple[tuple[str, str, int], ...] = (
    ("driving", "驾车", 3000),
    ("bicycling", "骑行", 40),
    ("walking", "步行", 8),
)

#: Beyond this straight-line distance a road trip stops being the natural
#: answer and rail is queried first.
RAIL_MIN_KM = 150


class PlaceMatch(BaseModel):
    query: str
    name: str | None = None
    lat: float | None = None
    lon: float | None = None
    admin1: str | None = None
    admin2: str | None = None
    country: str | None = None
    feature_code: str | None = None
    alternatives: list[str] = Field(default_factory=list)
    #: True when the point stands in for a place we could not resolve exactly.
    approx: bool = False

    @property
    def found(self) -> bool:
        return self.lat is not None and self.lon is not None


class LegPlan(BaseModel):
    leg: dict[str, Any]
    origin: PlaceMatch
    destination: PlaceMatch
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    selected_candidate_id: str | None = None
    reason_trace: dict[str, Any] | None = None
    degradation: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _degradation(capability_id: str, level: int, reason: str) -> dict[str, Any]:
    return {"capability_id": capability_id, "provider": None, "level": level, "reason": reason}


def _candidate_queries(text: str) -> list[str]:
    """Progressively simpler spellings of the same place.

    Ordered most-specific-first, but *every* candidate is tried, so a full
    street address degrades into something a global geocoder actually knows.
    """

    base = (text or "").strip()
    if not base:
        return []

    queries: list[str] = [base]
    # strip building/POI suffixes
    for suffix in TRIMMABLE_SUFFIXES:
        for existing in list(queries):
            if existing.endswith(suffix) and len(existing) > len(suffix):
                queries.append(existing[: -len(suffix)].strip())
    # strip administrative prefixes, longest first, repeatedly
    for existing in list(queries):
        for index, char in enumerate(existing):
            if char in ADMIN_MARKERS and index + 1 < len(existing):
                queries.append(existing[index + 1 :].strip())
    return [q for q in dict.fromkeys(q for q in queries if len(q) >= 2)]


def haversine_km(a: PlaceMatch, b: PlaceMatch) -> float:
    if not (a.found and b.found):
        return 0.0
    radius = 6371.0088
    p1, p2 = math.radians(a.lat or 0), math.radians(b.lat or 0)
    dp = p2 - p1
    dl = math.radians((b.lon or 0) - (a.lon or 0))
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(h)))


def city_of(match: PlaceMatch) -> str | None:
    """The city a rail query needs, which is rarely the POI the user named."""

    for candidate in (match.admin2, match.name, match.admin1):
        if not candidate:
            continue
        text = str(candidate).strip()
        for suffix in ("特别行政区", "自治州", "地区", "盟", "市", "省", "县", "区"):
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[: -len(suffix)]
                break
        if text:
            return text
    return None


async def resolve_place(deps: Any, text: str) -> tuple[PlaceMatch, list[dict[str, Any]]]:
    """Resolve a place name, retrying with simpler spellings before giving up."""

    degradation: list[dict[str, Any]] = []
    seen: list[str] = []

    for query in _candidate_queries(text):
        result = await deps.call_capability("place.resolve", {"query": query, "count": 5})
        if not result.ok:
            degradation.append(
                _degradation("place.resolve", int(result.degradation.level), result.degradation.reason or "unavailable")
            )
            continue
        rows = (result.data or {}).get("results") or []
        seen.extend(str(row.get("name")) for row in rows if row.get("name"))
        if rows:
            top = rows[0]
            return (
                PlaceMatch(
                    query=query,
                    name=top.get("name"),
                    lat=top.get("latitude"),
                    lon=top.get("longitude"),
                    admin1=top.get("admin1"),
                    admin2=top.get("admin2"),
                    country=top.get("country"),
                    feature_code=top.get("feature_code"),
                    alternatives=seen[:5],
                ),
                degradation,
            )

    return PlaceMatch(query=text, alternatives=seen[:5]), degradation


async def resolve_destinations(deps: Any, text: str) -> tuple[list[PlaceMatch], list[dict[str, Any]]]:
    """Expand an ambiguous landmark into sections, otherwise resolve directly."""

    degradation: list[dict[str, Any]] = []
    sections = LANDMARK_SECTIONS.get((text or "").strip())
    if sections:
        matches: list[PlaceMatch] = []
        for label, query in sections:
            match, notes = await resolve_place(deps, query)
            degradation.extend(notes)
            if match.found:
                matches.append(match.model_copy(update={"name": label}))
        return matches, degradation

    match, notes = await resolve_place(deps, text)
    degradation.extend(notes)
    return ([match] if match.found else []), degradation


async def route_between(deps: Any, origin: PlaceMatch, dest: PlaceMatch, mode: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not (origin.found and dest.found):
        return None, []
    result = await deps.call_capability(
        "route.plan",
        {
            "from": {"lat": origin.lat, "lon": origin.lon},
            "to": {"lat": dest.lat, "lon": dest.lon},
            "mode": mode,
        },
    )
    if not result.ok:
        return None, [
            _degradation("route.plan", int(result.degradation.level), result.degradation.reason or "unavailable")
        ]
    routes = (result.data or {}).get("routes") or []
    if not routes:
        return None, [_degradation("route.plan", 2, "no route returned")]
    best = min(routes, key=lambda r: r.get("duration_s") or 1e12)
    return {
        "mode": mode,
        "distance_km": round((best.get("distance_m") or 0) / 1000, 1),
        "duration_min": round((best.get("duration_s") or 0) / 60),
        "provider": (result.provenance.provider if result.provenance else None),
    }, []


async def forecast_for(deps: Any, dest: PlaceMatch, day: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not dest.found:
        return {}, []
    result = await deps.call_capability(
        "weather.forecast",
        {"latitude": dest.lat, "longitude": dest.lon, "forecast_days": 7},
    )
    if not result.ok:
        return {}, [
            _degradation("weather.forecast", int(result.degradation.level), result.degradation.reason or "unavailable")
        ]
    return summarise_day((result.data or {}).get("hourly") or {}, day), []


def summarise_day(hourly: dict[str, Any], day: str | None) -> dict[str, Any]:
    """Reduce an hourly series to the single day the trip happens on."""

    times = list(hourly.get("time") or [])
    if not times:
        return {}
    if day:
        indices = [i for i, stamp in enumerate(times) if str(stamp).startswith(str(day))]
    else:
        indices = list(range(min(24, len(times))))
    if not indices:
        # The date is outside the forecast horizon; say so rather than guess.
        return {"out_of_range": True}

    def series(name: str) -> list[float]:
        values = hourly.get(name) or []
        return [float(values[i]) for i in indices if i < len(values) and values[i] is not None]

    precipitation = series("precipitation")
    wind = series("wind_speed_10m")
    temperature = series("temperature_2m")
    visibility = series("visibility")
    codes = series("weather_code")

    summary: dict[str, Any] = {"day": day, "hours": len(indices)}
    if precipitation:
        summary["precipitation_mm"] = max(precipitation)
    if wind:
        summary["wind_speed_ms"] = max(wind)
    if temperature:
        summary["temperature_c"] = max(temperature)
        summary["temperature_min_c"] = min(temperature)
    if visibility:
        summary["visibility_m"] = min(visibility)
    if codes:
        summary["weather_code"] = int(max(codes))
    return summary


async def build_leg(
    deps: Any,
    *,
    origin_text: str,
    destination_text: str,
    day: str | None,
    weights: dict[str, float] | None = None,
) -> LegPlan:
    """Resolve, route, forecast, score and explain one leg."""

    degradation: list[dict[str, Any]] = []
    notes: list[str] = []

    origin, notes_origin = await resolve_place(deps, origin_text)
    degradation.extend(notes_origin)
    if not origin.found:
        notes.append(f"出发地「{origin_text}」没有解析到坐标，无法计算路线")

    destinations, notes_dest = await resolve_destinations(deps, destination_text)
    degradation.extend(notes_dest)
    if not destinations:
        notes.append(f"目的地「{destination_text}」没有解析到坐标")
    elif len(destinations) > 1:
        notes.append(
            f"「{destination_text}」是泛称，已展开为 {len(destinations)} 个具体去处分别比较"
        )

    candidates: list[dict[str, Any]] = []
    for dest in destinations:
        weather, notes_weather = await forecast_for(deps, dest, day)
        degradation.extend(notes_weather)

        straight = haversine_km(origin, dest)
        if straight >= RAIL_MIN_KM:
            rail, notes_rail = await rail_candidates(
                deps, origin=origin, dest=dest, day=day, weather=weather, weights=weights
            )
            degradation.extend(notes_rail)
            if rail:
                candidates.extend(rail)
                notes.append(f"距离约 {straight:.0f} 公里，已按铁路方案查询")
                continue
            notes.append("铁路查询没有返回可用的车次，已回退到公路方案")

        road, notes_road = await road_candidates(
            deps, origin=origin, dest=dest, straight=straight, weather=weather, weights=weights
        )
        degradation.extend(notes_road)
        if not road and not notes_road:
            degradation.append(_degradation("route.plan", 2, "no route available"))
        candidates.extend(road)

    # A vetoed candidate is only chosen if nothing else is left, and never
    # silently: the veto travels with it.
    score_candidates(candidates, weights=weights, degradation=degradation)
    ranked = sorted(
        candidates,
        key=lambda c: (not c["weather_veto"], c["score"]),
        reverse=True,
    )
    selected = ranked[0] if ranked else None

    leg = {
        "leg_id": new_id("leg"),
        "scope": "local",
        "origin_text": origin.name or origin_text,
        "destination_text": (selected or {}).get("destination_name") or destination_text,
        "day": day,
        "selected_candidate_id": (selected or {}).get("candidate_id"),
        "candidates": candidates,
    }
    return LegPlan(
        leg=leg,
        origin=origin,
        destination=destinations[0] if destinations else PlaceMatch(query=destination_text),
        candidates=candidates,
        selected_candidate_id=(selected or {}).get("candidate_id"),
        reason_trace=(selected or {}).get("reason_trace"),
        degradation=degradation,
        notes=notes,
    )


def _assess_and_score(
    *,
    weather: dict[str, Any],
    modes: list[str],
    duration_min: float | None,
    price: float | None,
    weights: dict[str, float] | None,
    degradation: list[dict[str, Any]],
    evidence_extra: dict[str, Any],
):
    assessment = weather_rules.assess(weather or {}, modes=modes)
    features: dict[str, float | None] = {
        "f_time": (duration_min or 0) / 240 if duration_min else None,
        # Rail fares are known; road costs are not modelled yet.
        "f_cost": (price or 0) / 800 if price else None,
        "f_transfer": (evidence_extra.get("transfers") or 0) / 3
        if evidence_extra.get("transfers") is not None
        else None,
        "f_walk": None,
        "f_punctual": None,
        "f_comfort": None,
        "f_access": None,
        "f_risk": None,
    }
    return assessment


def _normalise(value: float | None, low: float | None, high: float | None) -> float | None:
    """Min-max within the candidate set.

    The absolute-scale version this replaces clamped every long train's time
    feature to 1.0, which made all of them equally bad on time and let price
    alone decide -- the fastest train ranked last.
    """

    if value is None or low is None or high is None:
        return None
    if high - low < 1e-9:
        return 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def score_candidates(
    candidates: list[dict[str, Any]],
    *,
    weights: dict[str, float] | None,
    degradation: list[dict[str, Any]],
) -> None:
    """Score a candidate *set*, because a score only means something relative."""

    durations = [c["duration_min"] for c in candidates if c.get("duration_min")]
    prices = [c["price"] for c in candidates if c.get("price")]
    t_lo, t_hi = (min(durations), max(durations)) if durations else (None, None)
    c_lo, c_hi = (min(prices), max(prices)) if prices else (None, None)

    for candidate in candidates:
        features: dict[str, float | None] = {
            "f_time": _normalise(candidate.get("duration_min"), t_lo, t_hi),
            "f_cost": _normalise(candidate.get("price"), c_lo, c_hi),
            "f_transfer": None,
            "f_walk": None,
            "f_punctual": None,
            "f_comfort": None,
            "f_access": None,
            "f_risk": None,
        }
        weather = candidate.get("weather") or {}
        assessment = candidate.pop("_assessment", None)
        if assessment is None:
            assessment = weather_rules.assess(weather, modes=[str(candidate.get("mode"))])
        breakdown = scoring.score(
            features=features,
            weights=weights or {k: 1.0 for k in features},
            penalties=assessment.penalties,
            uncertainty=3 if weather.get("out_of_range") else 0,
        )
        trace = trace_policy.build(
            new_id("cand"),
            breakdown,
            assessment,
            evidence={
                "f_time": {"duration_min": candidate.get("duration_min")},
                "f_cost": {"price": candidate.get("price")},
                "weather": {"summary": weather},
            },
            degradation=[d["reason"] for d in degradation],
        )
        candidate["score"] = breakdown.total
        candidate["score_breakdown"] = breakdown.model_dump(mode="json")
        candidate["reason_trace"] = trace.model_dump(mode="json")
        candidate["explainable"] = trace_policy.is_explainable(trace)


async def road_candidates(
    deps: Any,
    *,
    origin: PlaceMatch,
    dest: PlaceMatch,
    straight: float,
    weather: dict[str, Any],
    weights: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One candidate per plausible travel mode."""

    candidates: list[dict[str, Any]] = []
    degradation: list[dict[str, Any]] = []
    for mode, label, max_km in MULTIMODAL:
        if straight > max_km:
            continue
        route, notes = await route_between(deps, origin, dest, mode)
        degradation.extend(notes)
        if route is None:
            continue
        assessment = _assess_and_score(
            weather=weather, modes=[mode], duration_min=route.get("duration_min"),
            price=None, weights=weights, degradation=degradation,
            evidence_extra={"route": route},
        )
        candidates.append(
            {
                "candidate_id": new_id("cand"),
                "destination_name": dest.name,
                "destination_admin1": dest.admin1,
                "lat": dest.lat,
                "lon": dest.lon,
                "mode": mode,
                "mode_label": label,
                "distance_km": route.get("distance_km"),
                "duration_min": route.get("duration_min"),
                "price": None,
                "route_provider": route.get("provider"),
                "weather": weather,
                "weather_veto": bool(assessment.vetoes),
                "weather_notes": list(assessment.notes),
                "_assessment": assessment,
            }
        )
    return candidates, degradation


async def rail_candidates(
    deps: Any,
    *,
    origin: PlaceMatch,
    dest: PlaceMatch,
    day: str | None,
    weather: dict[str, Any],
    weights: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rail options between two cities (the intercity case)."""

    straight = haversine_km(origin, dest)
    if not day:
        return [], [_degradation("intercity.rail.search", 2, "no departure date")]
    from_city, to_city = city_of(origin), city_of(dest)
    if not (from_city and to_city):
        return [], [_degradation("intercity.rail.search", 2, "could not derive city names")]

    result = await deps.call_capability(
        "intercity.rail.search",
        {
            "date": day,
            "fromStation": from_city,
            "toStation": to_city,
            "format": "json",
            "limitedNum": 6,
        },
    )
    if not result.ok:
        return [], [
            _degradation(
                "intercity.rail.search",
                int(result.degradation.level),
                result.degradation.reason or "unavailable",
            )
        ]

    trains = _parse_trains(result.data)
    if not trains:
        return [], [_degradation("intercity.rail.search", 2, "no trains returned")]

    candidates: list[dict[str, Any]] = []
    degradation: list[dict[str, Any]] = []
    for train in trains:
        seats = train.get("prices") or []
        second = next((s for s in seats if s.get("short") == "ze"), None)
        price = (second or (seats[0] if seats else {})).get("price")
        duration = _parse_lishi(train.get("lishi"))
        assessment = _assess_and_score(
            weather=weather, modes=["train"], duration_min=duration,
            price=float(price) if price else None, weights=weights,
            degradation=degradation, evidence_extra={"train": train.get("start_train_code")},
        )
        candidates.append(
            {
                "candidate_id": new_id("cand"),
                "destination_name": dest.name,
                "destination_admin1": dest.admin1,
                "lat": dest.lat,
                "lon": dest.lon,
                "mode": "rail",
                "mode_label": "铁路",
                "distance_km": round(straight, 1),
                "duration_min": duration,
                "price": price,
                "train_code": train.get("start_train_code"),
                "depart_time": train.get("start_time"),
                "arrive_time": train.get("arrive_time"),
                "from_station": train.get("from_station"),
                "to_station": train.get("to_station"),
                "seats": [
                    {"name": s.get("seat_name"), "left": s.get("num"), "price": s.get("price")}
                    for s in seats[:4]
                ],
                "route_provider": (result.provenance.provider if result.provenance else None),
                "weather": weather,
                "weather_veto": bool(assessment.vetoes),
                "weather_notes": list(assessment.notes),
                "_assessment": assessment,
            }
        )
    return candidates, degradation


def _parse_trains(data: Any) -> list[dict[str, Any]]:
    """The 12306 MCP returns either structured JSON or a text table."""

    payload = data
    if isinstance(payload, dict) and "text" in payload:
        text = str(payload.get("text") or "").strip()
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return []
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        payload = payload["data"]
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _parse_lishi(value: Any) -> float | None:
    """``"05:56"`` -> 356 minutes."""

    if not value:
        return None
    text = str(value)
    if ":" not in text:
        return None
    parts = text.split(":")
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return None
    return hours * 60 + minutes
