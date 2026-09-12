"""Cross-leg hard-constraint validation (framework §6.5).

Two kinds of violation, and the difference matters:

* **hard** (``recoverable=False``) -- the candidate must be dropped. A leg
  that arrives after the next one departs is not "suboptimal", it is
  impossible.
* **soft** (``recoverable=True``) -- the plan still builds; the caller should
  re-rank or warn. A budget overrun is a sorting input, not a rejection.

One check is deliberately *not* reported here: mixed coordinate systems. Per
§6.5 that is a bug in our own pipeline rather than a property of the
candidate, so it raises ``CoordinateSystemMismatch`` and never degrades.

Still uncovered because the constraint mapping carries no data for it: 服务日
(running days) and 票种可得 (ticket-class availability).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Any

from pydantic import BaseModel, ValidationError

from app.capabilities.contract import DegradationLevel
from app.domain.geo import Coord, assert_same_crs
from app.domain.timebase import LOCAL_TZ, minutes_between, parse_ymd
from app.policy.buffer import required_buffer_minutes

#: Distance within which the last leg still counts as "closed the loop" (§6.5).
DEFAULT_RETURN_CLOSURE_METERS = 5_000.0

#: Attribute values that mean "this leg cannot be used step-free".
_BARRIER_VALUES = frozenset({"none", "no", "limited", "poor", "不可", "无", "部分"})


class Violation(BaseModel):
    kind: str
    leg_index: int
    detail: str
    recoverable: bool


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read one field from either a pydantic model or a plain mapping."""

    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _coord(value: Any) -> Coord | None:
    if isinstance(value, Coord):
        return value
    if isinstance(value, Mapping) and "lat" in value and "lon" in value:
        try:
            return Coord.model_validate(dict(value))
        except ValidationError:
            return None
    return None


def _moment(value: Any) -> datetime | None:
    """Coerce a leg timestamp to an aware datetime, or ``None``."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=LOCAL_TZ)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=LOCAL_TZ)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.combine(parse_ymd(text), time.min)
            except ValueError:
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=LOCAL_TZ)
    return None


def _clock(value: Any) -> time | None:
    if isinstance(value, datetime):
        return value.timetz().replace(tzinfo=None)
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        hour, _, minute = value.strip().partition(":")
        if hour.strip().isdigit() and minute.strip().isdigit():
            try:
                return time(int(hour), int(minute))
            except ValueError:
                return None
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _degradation(value: Any) -> DegradationLevel:
    try:
        return DegradationLevel(int(value))
    except (TypeError, ValueError):
        return DegradationLevel.D0


def _leg_mode(leg: Any) -> str:
    """Best-effort transport mode for a leg, used to pick its buffer."""

    mode = _get(leg, "mode")
    if isinstance(mode, str) and mode:
        return mode
    for field in ("modes", "selected_mode"):
        value = _get(leg, field)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, Sequence) and value:
            return str(value[0])
    selected = _get(leg, "selected_candidate_id")
    for candidate in _get(leg, "candidates") or ():
        if selected is not None and _get(candidate, "candidate_id") != selected:
            continue
        modes = _get(candidate, "modes")
        if isinstance(modes, Sequence) and modes:
            return str(modes[0])
    return ""


def _leg_price(leg: Any) -> float | None:
    price = _number(_get(leg, "price"))
    if price is not None:
        return price
    selected = _get(leg, "selected_candidate_id")
    for candidate in _get(leg, "candidates") or ():
        if selected is not None and _get(candidate, "candidate_id") != selected:
            continue
        candidate_price = _number(_get(candidate, "price"))
        if candidate_price is not None:
            return candidate_price
    return None


def _has_barrier(leg: Any) -> bool:
    attributes = _get(leg, "attributes")
    if not isinstance(attributes, Mapping):
        return False
    if attributes.get("step_free") is False or attributes.get("stairs") is True:
        return True
    accessibility = attributes.get("accessibility")
    return isinstance(accessibility, str) and accessibility.strip().lower() in _BARRIER_VALUES


def _window_violation(
    window: Mapping[str, Any], depart: datetime, index: int
) -> Violation | None:
    """Check one departure against ``{"earliest": ..., "latest": ...}``.

    Bounds may be absolute timestamps or plain ``HH:MM`` clock times, because
    the user says both kinds ("must leave after 8am" vs "after 9/12 08:00").
    """

    for key, is_lower in (("earliest", True), ("latest", False)):
        raw = window.get(key)
        if raw is None:
            continue
        bound = _moment(raw)
        if bound is None:
            clock = _clock(raw)
            if clock is None:
                continue
            bound = depart.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
        if is_lower and depart < bound:
            return Violation(
                kind="depart_window",
                leg_index=index,
                detail=f"发车时间早于用户时间窗下限 {raw}",
                recoverable=False,
            )
        if not is_lower and depart > bound:
            return Violation(
                kind="depart_window",
                leg_index=index,
                detail=f"发车时间晚于用户时间窗上限 {raw}",
                recoverable=False,
            )
    return None


def _return_closure_violation(legs: list[Any], rules: Mapping[str, Any]) -> Violation | None:
    if not legs or not rules.get("return_to_origin"):
        return None

    origin = _coord(rules.get("origin"))
    destination = _coord(_get(legs[-1], "destination"))
    if origin is None or destination is None:
        return None

    limit = _number(rules.get("return_closure_meters")) or DEFAULT_RETURN_CLOSURE_METERS
    distance = origin.distance_m(destination)
    if distance <= limit:
        return None
    return Violation(
        kind="return_closure",
        leg_index=len(legs) - 1,
        detail=f"末段终点距出发地约 {distance / 1000:.1f} km，未闭合回到原点",
        recoverable=False,
    )


def _require_single_crs(legs: Sequence[Any]) -> None:
    coords: list[Coord] = []
    for leg in legs:
        for field in ("origin", "destination"):
            coord = _coord(_get(leg, field))
            if coord is not None:
                coords.append(coord)
    if coords:
        assert_same_crs(coords)


def validate_trip(legs: Sequence[Any], *, constraints: Mapping[str, Any]) -> list[Violation]:
    """Validate one candidate itinerary against the cross-leg constraint set.

    Returns every violation found, hard and soft, so the caller can both drop
    impossible candidates and explain the soft ones. Raises instead of
    returning when coordinates from different CRS were combined.
    """

    ordered = list(legs or ())
    rules = constraints or {}
    _require_single_crs(ordered)

    violations: list[Violation] = []
    level = _degradation(rules.get("uncertainty"))
    cross_station = bool(rules.get("cross_station"))
    raw_window = rules.get("depart_window")
    window = raw_window if isinstance(raw_window, Mapping) else None
    require_step_free = bool(rules.get("require_step_free") or rules.get("accessibility_required"))
    arrive_by = _moment(rules.get("arrive_by"))
    budget = _number(rules.get("budget_total"))
    budget_is_hard = bool(rules.get("budget_hard"))
    min_buffer = _number(rules.get("min_buffer_minutes"))

    total_price = 0.0
    priced_legs = 0

    for index, leg in enumerate(ordered):
        depart = _moment(_get(leg, "depart"))
        arrive = _moment(_get(leg, "arrive"))

        if depart is not None and arrive is not None and arrive < depart:
            violations.append(
                Violation(
                    kind="temporal_order",
                    leg_index=index,
                    detail="该段到达时间早于出发时间",
                    recoverable=False,
                )
            )

        if window is not None and depart is not None:
            window_violation = _window_violation(window, depart, index)
            if window_violation is not None:
                violations.append(window_violation)

        if require_step_free and _has_barrier(leg):
            violations.append(
                Violation(
                    kind="accessibility",
                    leg_index=index,
                    detail="该段含台阶或不可无障碍通行，与“必须无障碍”冲突",
                    recoverable=False,
                )
            )

        price = _leg_price(leg)
        if price is not None:
            total_price += price
            priced_legs += 1

    for index in range(len(ordered) - 1):
        previous_arrive = _moment(_get(ordered[index], "arrive"))
        next_depart = _moment(_get(ordered[index + 1], "depart"))
        if previous_arrive is None or next_depart is None:
            continue
        if next_depart < previous_arrive:
            violations.append(
                Violation(
                    kind="temporal_overlap",
                    leg_index=index + 1,
                    detail="下一段发车早于上一段到达",
                    recoverable=False,
                )
            )
            continue

        buffer_minutes = required_buffer_minutes(
            prev_mode=_leg_mode(ordered[index]),
            cross_station=cross_station,
            uncertainty=level,
        )
        if min_buffer is not None:
            buffer_minutes = max(buffer_minutes, int(min_buffer))
        gap = minutes_between(previous_arrive, next_depart)
        if gap < buffer_minutes:
            violations.append(
                Violation(
                    kind="buffer_shortfall",
                    leg_index=index + 1,
                    detail=f"衔接时间 {gap} 分钟，少于要求的 {buffer_minutes} 分钟",
                    recoverable=True,
                )
            )

    if budget is not None and priced_legs and total_price > budget:
        violations.append(
            Violation(
                kind="budget",
                leg_index=-1,
                detail=f"总价 {total_price:.2f} 超出预算 {budget:.2f}",
                recoverable=not budget_is_hard,
            )
        )

    if arrive_by is not None and ordered:
        last_arrive = _moment(_get(ordered[-1], "arrive"))
        if last_arrive is not None and last_arrive > arrive_by:
            violations.append(
                Violation(
                    kind="arrive_by",
                    leg_index=len(ordered) - 1,
                    detail="末段到达时间晚于用户要求的到达时间",
                    recoverable=False,
                )
            )

    closure = _return_closure_violation(ordered, rules)
    if closure is not None:
        violations.append(closure)

    return violations
