"""Action DAG execution (framework §8).

Every capability call in this module goes through ``deps.call_capability``,
which consults the resolver and therefore the arbiter. There is no other way to
reach a channel from the graph.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from app.domain.ids import new_id
from app.graph.deps import GraphDeps
from app.graph.nodes.streaming import close_stream, stream_callback
from app.graph.state import AgentState, emit
from app.graph.stub_fallback import llm_for
from app.llm.context import assemble, summarise_state
from app.llm.schemas import RemindOutput
from app.policy import idempotency as idem_policy

_REMIND_TASK = "任务：根据行程生成 1 到 3 条中文可执行提醒。"


def _traveler_count(state: AgentState) -> int:
    raw = ((state.get("slots") or {}).get("travelers") or {}).get("value")
    if isinstance(raw, dict):
        try:
            return int(raw.get("count") or 1)
        except (TypeError, ValueError):
            return 1
    try:
        return int(raw) if raw else 1
    except (TypeError, ValueError):
        return 1


def _leg_subject(leg: dict[str, Any], *, travelers: int) -> dict[str, Any]:
    """Business subject of one leg's booking action.

    Carries both what the operation needs (``leg_id`` for the deep-link) and the
    durable facts the idempotency key is derived from. ``leg_id`` is deliberately
    *not* part of that key -- see ``idem_policy.idem_subject``.
    """

    candidates = leg.get("candidates") or []
    chosen = next(
        (c for c in candidates if c.get("candidate_id") == leg.get("selected_candidate_id")),
        None,
    )
    subject = {
        "leg_id": leg.get("leg_id"),
        "kind": leg.get("kind") or "road",
        "date": leg.get("day"),
        "from_station": (chosen or {}).get("from_station") or leg.get("origin_text"),
        "to_station": (chosen or {}).get("to_station") or leg.get("destination_text"),
        "train_code": (chosen or {}).get("train_code") or leg.get("train_code"),
        "travelers": travelers,
    }
    depart = str(leg.get("depart") or "")
    if "T" in depart:
        subject["depart_time"] = depart.split("T", 1)[1][:5]
    return {key: value for key, value in subject.items() if value not in (None, "", [])}


def _action(
    *,
    capability_id: str,
    risk: int,
    subject: dict[str, Any],
    session_id: str,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "action_id": new_id("act"),
        "capability_id": capability_id,
        "risk": risk,
        "depends_on": list(depends_on or []),
        "subject": subject,
        # P4: the key comes from the business subject, so a retry after a
        # replan -- or after a reconnect -- lands on the same ledger row instead
        # of placing the same booking twice.
        "idem_key": idem_policy.plan_action_key(
            session_id=session_id, action_kind=capability_id, params=subject
        ),
        "status": "pending",
        "consent_ref": None,
        "evidence": [],
    }


async def plan_actions(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    """Turn the frozen plan into an action DAG.

    M0 emits only side-effect-free local actions. L1/L2 actions appear when the
    intent requires booking a reservation, and are handled by ``action_gate``.
    """

    legs = state.get("planned_legs") or []
    travelers = _traveler_count(state)
    session_id = str(state.get("session_id") or "")
    actions: list[dict[str, Any]] = []

    for leg in legs:
        # Only a leg with a bookable identity is worth a deep-link. A road leg has
        # no timetable, so a deeplink for it would be a link to nothing.
        subject = _leg_subject(leg, travelers=travelers)
        if not subject.get("train_code") and subject.get("kind") != "rail":
            continue
        actions.append(
            _action(
                capability_id="booking.deeplink.build",
                risk=0,
                subject=subject,
                session_id=session_id,
            )
        )

    actions.append(
        _action(
            capability_id="booking.checklist.export",
            risk=0,
            subject={
                "legs": [
                    {
                        **_leg_subject(leg, travelers=travelers),
                        "mode": leg.get("kind"),
                        "depart": leg.get("depart"),
                        "arrive": leg.get("arrive"),
                    }
                    for leg in legs
                ],
                "travelers": travelers,
            },
            session_id=session_id,
        )
    )

    # Export and share are trip-wide, so they need the plan itself rather than one
    # leg. The content layer's days travel with it, which is what makes the export
    # a real itinerary instead of a list of train times.
    plan_payload = {
        "trip_id": state.get("session_id"),
        "legs": legs,
        "days": (state.get("planned_content") or {}).get("days") or [],
        "segments": (state.get("planned_content") or {}).get("segments") or [],
        "content_notes": (state.get("planned_content") or {}).get("content_notes") or [],
        "cost_note": (state.get("planned_content") or {}).get("cost_note") or "",
    }
    actions.append(
        _action(
            capability_id="plan.export",
            risk=0,
            subject={"plan": plan_payload, "format": "markdown"},
            session_id=session_id,
        )
    )
    actions.append(
        _action(
            capability_id="plan.share",
            risk=0,
            subject={"plan": plan_payload},
            session_id=session_id,
        )
    )

    intent = ((state.get("slots") or {}).get("intent") or {}).get("value")
    if intent == "reservation":
        # Dormant in M0 (no browser channel); wired now so the gate has a caller.
        actions.append(
            _action(
                capability_id="booking.reserve.submit",
                risk=2,
                subject={
                    "legs": [_leg_subject(leg, travelers=travelers) for leg in legs],
                    "travelers": travelers,
                },
                session_id=session_id,
            )
        )

    return {
        "actions": actions,
        "action_cursor": 0,
        "outbox": emit(
            state,
            "state_update",
            {"phase": "EXECUTE", "actions": len(actions), "with_idem_key": sum(1 for a in actions if a["idem_key"])},
        ),
    }


def next_gated_action(state: AgentState) -> dict[str, Any] | None:
    for action in state.get("actions") or []:
        if action.get("status") == "pending" and int(action.get("risk", 0)) >= 1:
            return action
    return None


def has_pending_l0(state: AgentState) -> bool:
    return any(
        a.get("status") == "pending" and int(a.get("risk", 0)) == 0 for a in (state.get("actions") or [])
    )


async def dispatch(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    """Deterministic fork: batch the read-only actions, gate the rest."""

    gated = next_gated_action(state)
    return {
        "extras_gated_action_id": gated["action_id"] if gated else None,
        "outbox": emit(state, "state_update", {"phase": "EXECUTE", "pending_gate": bool(gated)}),
    }


async def action_gate(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    """L1/L2 confirmation. Pure before ``interrupt`` (DH-1, DH-5)."""

    action_id = state.get("extras_gated_action_id")
    action = next((a for a in (state.get("actions") or []) if a["action_id"] == action_id), None)
    if action is None:
        return {}

    decision = interrupt(
        {
            "kind": "action",
            "action_id": action_id,
            "capability_id": action["capability_id"],
            "risk": int(action["risk"]),
            "requires_second_confirmation": int(action["risk"]) >= 2,
            "options": ["approve", "reject"],
        }
    )
    payload = decision if isinstance(decision, dict) else {"decision": str(decision)}
    approved = payload.get("decision") == "approve"
    confirm_ok = (not int(action["risk"]) >= 2) or bool(payload.get("confirm_token"))

    actions = list(state.get("actions") or [])
    for item in actions:
        if item["action_id"] == action_id:
            if approved and confirm_ok:
                item["status"] = "pending"
                item["consent_ref"] = payload.get("consent_id") or "action-consent"
            elif approved and not confirm_ok:
                item["status"] = "blocked"
            else:
                item["status"] = "skipped"
                # Anything depending on a skipped action is blocked, not failed.
                for downstream in actions:
                    if action_id in (downstream.get("depends_on") or []):
                        downstream["status"] = "blocked"
    return {"actions": actions, "extras_gated_action_id": None}


async def run_l0_batch(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    """Execute pending read-only actions; a single failure must not abort the batch.

    L1/L2 actions are executed here only when they carry a ``consent_ref`` --
    i.e. they already passed ``action_gate``. Everything else stays pending for
    the gate to pick up.
    """

    actions = list(state.get("actions") or [])
    degradation = list(state.get("degradation_log") or [])
    results: list[dict[str, Any]] = []
    consent_ref = (state.get("consents") or [{}])[-1].get("consent_id") if state.get("consents") else None

    for action in actions:
        if action.get("status") != "pending":
            continue
        gated = int(action.get("risk", 0)) >= 1
        if gated and not action.get("consent_ref"):
            continue

        # P4: claim the key *before* the call. A duplicate loses the race here,
        # in the database, rather than in application logic that a retry path
        # could bypass.
        subject = dict(action.get("subject") or {})
        action["idem_key"] = action.get("idem_key") or idem_policy.plan_action_key(
            session_id=str(state.get("session_id") or ""),
            action_kind=str(action.get("capability_id") or ""),
            params=subject,
        )
        adopted = _claim(
            deps,
            state,
            idem_key=action["idem_key"],
            capability_id=action["capability_id"],
            subject=subject,
        )
        if adopted is False:
            action["status"] = "skipped"
            action["error"] = {"reason": "IDEMPOTENT_REPLAY"}
            results.append(
                {
                    "action_id": action["action_id"],
                    "capability": action["capability_id"],
                    "ok": True,
                    "duplicate": True,
                }
            )
            continue

        result = await deps.call_capability(
            action["capability_id"],
            subject,
            call_context=None,
        )
        action["started_at"] = None
        if result.ok:
            action["status"] = "completed"
            # Kept on the action so the deliverable survives the checkpoint: an
            # export whose content only existed in the event stream would be lost
            # on a reconnect, and the user's file with it.
            action["result"] = result.data if isinstance(result.data, dict) else None
            _settle(deps, idem_key=action["idem_key"], ok=True, result_ref=action["action_id"])
        else:
            action["status"] = "failed"
            action["error"] = {"reason": result.degradation.reason, "warnings": result.warnings}
            _settle(deps, idem_key=action["idem_key"], ok=False, failure=action["error"])
            degradation.append(
                {
                    "capability_id": action["capability_id"],
                    "provider": None,
                    "level": int(result.degradation.level),
                    "reason": result.degradation.reason or "unavailable",
                }
            )
        action["consent_ref"] = consent_ref
        results.append({"action_id": action["action_id"], "capability": action["capability_id"], "ok": result.ok})

    outbox = emit(state, "tool_result", {"batch": results})
    for note in degradation[len(state.get("degradation_log") or []) :]:
        outbox = [*outbox, *emit(state, "degradation_notice", note)]

    return {"actions": actions, "degradation_log": degradation, "outbox": outbox}


def _ledger(deps: GraphDeps) -> Any:
    """The idempotency ledger, when this deployment has one.

    Read from ``deps`` rather than from state on purpose: the ledger is a live
    object with a database handle, and anything placed in ``AgentState`` is
    serialised into the LangGraph checkpoint (the same trap that keeps API keys
    out of state).
    """

    repositories = getattr(deps, "repositories", None)
    return getattr(repositories, "ledger", None) if repositories is not None else None


def _claim(
    deps: GraphDeps,
    state: AgentState,
    *,
    idem_key: str,
    capability_id: str,
    subject: dict[str, Any],
) -> bool | None:
    """Try to own the ledger row for this attempt.

    Returns ``None`` when there is no ledger (offline runs), which means "no
    opinion" and the action proceeds. ``False`` means another attempt already
    owns the key, so this one must not call the capability.
    """

    ledger = _ledger(deps)
    if ledger is None:
        return None
    return bool(
        ledger.insert_in_flight(
            idem_key=idem_key,
            session_id=str(state.get("session_id") or ""),
            capability_id=capability_id,
            plan_version_id=state.get("current_plan_version_id"),
            action_id=None,
            subject_fingerprint=idem_policy.subject_fingerprint(capability_id, subject),
        )
    )


def _settle(
    deps: GraphDeps,
    *,
    idem_key: str,
    ok: bool,
    result_ref: str | None = None,
    failure: dict[str, Any] | None = None,
) -> None:
    ledger = _ledger(deps)
    if ledger is None:
        return
    if ok:
        ledger.mark_completed(idem_key, result_ref=result_ref)
    else:
        ledger.mark_failed(idem_key, failure)


async def assemble_itinerary(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    content = state.get("planned_content") or {}
    trip = {
        "trip_id": new_id("trip"),
        "session_id": state.get("session_id"),
        "legs": state.get("planned_legs") or [],
        "assumptions": state.get("assumptions") or [],
        "degradation": state.get("degradation_log") or [],
        # F1 content layer: the day-by-day plan travels with the itinerary so
        # the card can render segments and days without a second call.
        "segments": content.get("segments") or [],
        "days": content.get("days") or [],
        "content_notes": content.get("content_notes") or [],
        "cost_estimate": content.get("cost_estimate"),
        "cost_note": content.get("cost_note") or "",
        "actions": [
            {"action_id": a["action_id"], "capability_id": a["capability_id"], "status": a["status"]}
            for a in (state.get("actions") or [])
        ],
    }
    return {"trip": trip, "outbox": emit(state, "state_update", {"phase": "EXECUTE", "trip": trip["trip_id"]})}


async def remind(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    llm = llm_for(deps)
    prompt = assemble(
        task=_REMIND_TASK,
        schema_hint='{"items": ["<提醒>"]}',
        state_summary=summarise_state(
            slots=state.get("slots") or {},
            phase="EXECUTE",
            assumptions=state.get("assumptions") or [],
        ),
        user_input=str((state.get("trip") or {}).get("legs", [])),
    )
    response = await llm.structured(
        node="remind",
        schema=RemindOutput,
        prompt=prompt,
        on_delta=stream_callback(deps, "remind"),
    )
    parsed = response.parsed if isinstance(response.parsed, RemindOutput) else None
    items = list(parsed.items) if parsed and parsed.items else ["出发前一天确认车次与余票。"]
    await close_stream(deps, "remind", ok=bool(parsed and parsed.items))
    trip = dict(state.get("trip") or {})
    trip["reminders"] = items
    return {
        "trip": trip,
        "phase": "DONE",
        "outbox": [
            *emit(state, "state_update", {"phase": "DONE", "reminders": items}),
            *emit(state, "done", {"status": "done"}),
        ],
    }
