"""Slot validation, plan assembly and preview (M0 skeleton)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.domain.ids import new_id
from app.graph.deps import GraphDeps
from app.graph.nodes.streaming import close_stream, stream_callback
from app.graph.state import AgentState, emit
from app.graph.stub_fallback import llm_for
from app.graph.trip_planner import build_trip
from app.llm.context import assemble, summarise_state
from app.llm.schemas import PreviewOutput
from app.policy import slots as slot_policy

_PREVIEW_TASK = (
    "任务：把结构化计划渲染成一段中文「计划预览」。\n"
    "要求：说明将要做哪些事、会动用哪些数据、是否涉及费用或不可逆操作、"
    "以及你采用的假设（若有）。不得夸大，不得编造未提供的信息。"
)


def _slot_context(state: AgentState) -> dict[str, Any]:
    slots = state.get("slots") or {}
    ctx: dict[str, Any] = {}
    intent = (slots.get("intent") or {}).get("value")
    # ``active_required`` / ``condition_met`` read ``"<key>:<expected>"`` specs
    # against *this* mapping, so the value has to live under the bare key and
    # use the vocabulary the spec expects ("booking").
    ctx["intent"] = "booking" if intent in {"booking_list", "reservation"} else (intent or "advice_only")
    count = (slots.get("travelers") or {}).get("value")
    if isinstance(count, dict):
        count = count.get("count")
    try:
        ctx["traveler:multi"] = int(count) > 1
    except (TypeError, ValueError):
        ctx["traveler:multi"] = False
    ctx["trip:round_trip"] = bool((slots.get("return_by") or {}).get("value"))
    ctx["traveler:accessibility"] = bool((slots.get("mobility_needs") or {}).get("value"))
    ctx["scope:intercity"] = state.get("extras_scope_intercity", False)
    ctx["schedule:window"] = bool((slots.get("time_window") or {}).get("value"))
    return ctx


async def slot_validate(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    slots = state.get("slots") or {}
    result = slot_policy.validate(
        {k: slot_policy.SlotValue.model_validate(v) for k, v in slots.items()},
        context=_slot_context(state),
        rounds_used=int(state.get("question_rounds") or 0),
        max_rounds=deps.settings.question_max_rounds,
    )
    # The gate computes what it can instead of asking (F1: trip length from the
    # two dates). Carrying those values forward is what lets the day layer know
    # how many days to lay out without ever showing the user a "how many days?"
    # question.
    return {
        "missing_required": list(result.missing),
        "derived_slots": {**(state.get("derived_slots") or {}), **result.derived},
        "assumptions": list(result.assumptions) if result.assumptions else (state.get("assumptions") or []),
        "outbox": emit(
            state,
            "state_update",
            {
                "phase": "COLLECT",
                "missing": list(result.missing),
                "gate_ok": bool(result.ok),
                "derived": result.derived,
                "issues": result.issues,
            },
        ),
    }


async def assume_fallback(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    """Past the question budget: proceed on explicit, visible assumptions (DC-2)."""

    slots = {k: slot_policy.SlotValue.model_validate(v) for k, v in (state.get("slots") or {}).items()}
    missing = list(state.get("missing_required") or [])
    notes = slot_policy.apply_defaults(slots, missing)
    return {
        "slots": {k: v.model_dump(mode="json") for k, v in slots.items()},
        "assumptions": [*(state.get("assumptions") or []), *notes],
        "missing_required": [],
        "outbox": emit(state, "state_update", {"phase": "COLLECT", "assumptions": notes}),
    }


async def plan_draft(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    """Build the plan: real places, real routes, real weather (M1).

    Every step degrades instead of raising, so the flow still completes with no
    credentials -- and the gaps are labelled on the itinerary instead of being
    papered over with template text.
    """

    slots = state.get("slots") or {}
    degradation = list(state.get("degradation_log") or [])

    def slot(key: str):
        value = (slots.get(key) or {}).get("value")
        return value if value not in (None, "", []) else None

    origin_text = str(slot("origin_address") or "")
    destination_text = str(slot("destination") or "")
    day = slot("depart_date")
    day = str(day) if day else None

    today = await deps.call_capability("clock.today", {})
    if not today.ok:
        degradation.append(
            {
                "capability_id": "clock.today",
                "provider": None,
                "level": int(today.degradation.level),
                "reason": today.degradation.reason or (today.warnings[0] if today.warnings else "unavailable"),
            }
        )
    outbox = emit(state, "tool_call_end", {"capability": "clock.today", "ok": today.ok})

    plan = await build_trip(
        deps,
        origin_text=origin_text,
        destination_text=destination_text,
        day=day,
    )
    degradation.extend(plan.degradation)

    legs = plan.legs
    if plan.violations:
        notes = [f"衔接校验发现 {len(plan.violations)} 处问题"] + [
            v.get("detail", "") for v in plan.violations[:2]
        ]
        plan.notes.extend(n for n in notes if n)

    distance = max(
        ((c.get("distance_km") or 0) for leg in legs for c in (leg.get("candidates") or [])),
        default=0,
    )
    intercity = any(leg.get("kind") == "rail" for leg in legs) or distance >= 300

    outbox = [
        *outbox,
        *emit(
            state,
            "tool_result",
            {
                "capability": "plan.build",
                "ok": bool(legs),
                "legs": len(legs),
                "origin": origin_text,
                "destination": destination_text,
                "distance_km": round(distance, 1) or None,
                "violations": len(plan.violations),
            },
        ),
    ]

    return {
        "planned_legs": legs,
        "extras_scope_intercity": intercity,
        "degradation_log": degradation,
        "assumptions": [*(state.get("assumptions") or []), *plan.notes],
        "outbox": outbox,
    }


async def plan_finalize(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    """Freeze the plan into a versioned, hashable object (P3)."""

    legs = state.get("planned_legs") or []
    content = state.get("planned_content") or {}
    slots_snapshot = state.get("slots") or {}
    # DE-3: the preview card opens with the assumptions. Optional slots the
    # user never mentioned are decisions we are making *for* them, so they must
    # be visible before the consent gate rather than buried after it.
    slot_values = {k: slot_policy.SlotValue.model_validate(v) for k, v in slots_snapshot.items()}
    assumptions = list(state.get("assumptions") or [])
    for note in slot_policy.unstated_optional(slot_values, context=_slot_context(state)):
        if note not in assumptions:
            assumptions.append(note)
    # P3: the hash has to cover everything the user is consenting to. Omitting
    # the day plan would let "same plan, one extra day" share a hash with the old
    # consent, and the stale-consent check would pass when it must fail.
    canonical = json.dumps(
        {
            "legs": legs,
            "slots": slots_snapshot,
            "segments": content.get("segments") or [],
            "days": content.get("days") or [],
            "derived": state.get("derived_slots") or {},
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    plan_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    version_no = len(state.get("plan_versions") or []) + 1
    plan_version_id = new_id("pv")

    meta = {
        "plan_version_id": plan_version_id,
        "version_no": version_no,
        "plan_hash": plan_hash,
        "status": "previewed",
        "cost_estimate": content.get("cost_estimate"),
    }
    return {
        "plan_versions": [*(state.get("plan_versions") or []), meta],
        "current_plan_version_id": plan_version_id,
        "plan_hash": plan_hash,
        "plan_status": "previewed",
        "phase": "PREVIEW",
        "assumptions": assumptions,
        "outbox": emit(state, "plan_preview", {"plan_version_id": plan_version_id, "plan_hash": plan_hash}),
    }


async def preview_render(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    llm = llm_for(deps)
    prompt = assemble(
        task=_PREVIEW_TASK,
        schema_hint='{"text": "<计划预览文案>"}',
        state_summary=summarise_state(
            slots=state.get("slots") or {},
            phase="PREVIEW",
            plan_version_id=state.get("current_plan_version_id"),
            assumptions=state.get("assumptions") or [],
        ),
        user_input=json.dumps(state.get("planned_legs") or [], ensure_ascii=False),
    )
    response = await llm.structured(
        node="preview_render",
        schema=PreviewOutput,
        prompt=prompt,
        on_delta=stream_callback(deps, "preview_render"),
    )
    parsed = response.parsed if isinstance(response.parsed, PreviewOutput) else None
    text = parsed.text if parsed and parsed.text else "已生成行程方案，请确认后执行。"
    # Close the token stream: the raw stream was JSON, so the client is told to
    # discard it and render this text instead.
    await close_stream(deps, "preview_render", ok=bool(parsed and parsed.text))

    # The consent gate must show what is being consented to. Rendered copy can
    # be generic (a template, or a small local model); the structured summary
    # below is deterministic and therefore always says what will actually run.
    summary = _plan_summary(state.get("planned_legs") or [], state.get("planned_content") or {})
    if summary:
        text = f"{text}\n\n{summary}"
    return {
        "preview_text": text,
        "phase": "AWAIT_CONSENT",
        "outbox": emit(
            state,
            "state_update",
            {
                "phase": "AWAIT_CONSENT",
                "preview": text,
                "plan_version_id": state.get("current_plan_version_id"),
                "plan_hash": state.get("plan_hash"),
            },
        ),
    }


def _plan_summary(legs: list[dict[str, Any]], content: dict[str, Any] | None = None) -> str:
    """Deterministic summary of what is being consented to.

    Segment and day headers come first: the consent gate has to show the shape of
    the trip (how many cities, how many days) before it shows the legs.
    """

    lines: list[str] = []
    content = content or {}
    segments = content.get("segments") or []
    days = content.get("days") or []
    if segments:
        parts = [f"{segment.get('city')} {segment.get('day_count')} 天" for segment in segments]
        lines.append("行程分段：" + " / ".join(parts))
    for day in days:
        head = f"Day {day.get('day_index')}（{day.get('date') or ''}）"
        if day.get("city"):
            head += f" {day['city']}"
        if day.get("is_transfer_day"):
            head += f" → {day.get('overnight_city') or '下一站'}"
        theme = day.get("theme") or ""
        if theme:
            head += f" · {theme}"
        names = [item.get("name") for item in (day.get("attractions") or []) if item.get("name")]
        if names:
            head += "：" + "、".join(names)
        lines.append(head)

    for index, leg in enumerate(legs, start=1):
        candidates = leg.get("candidates") or []
        chosen = next(
            (c for c in candidates if c.get("candidate_id") == leg.get("selected_candidate_id")),
            None,
        )
        head = f"{index}. {leg.get('origin_text') or '起点'} → {leg.get('destination_text') or '终点'}"
        if leg.get("day"):
            head += f"（{leg['day']}）"
        if chosen:
            head += (
                f"　{chosen.get('duration_min', '?')} 分钟 · {chosen.get('distance_km', '?')} 公里"
            )
        lines.append(head)
        if chosen and chosen.get("weather_veto"):
            lines.append("   ⚠ 该方案触发天气硬性否决")

    cost_note = content.get("cost_note")
    if cost_note:
        lines.append(f"费用：{cost_note}")
    return "\n".join(lines)
