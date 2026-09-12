"""Consent gate and edit handling (DE-1..DE-5)."""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from app.domain.ids import new_id
from app.graph.deps import GraphDeps
from app.graph.state import AgentState, emit
from app.policy import slots as slot_policy


async def consent_gate(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    """Wait for the user's decision. Pure before ``interrupt``."""

    plan_version_id = state.get("current_plan_version_id")
    plan_hash = state.get("plan_hash")
    decision = interrupt(
        {
            "kind": "gate2_full",
            "plan_version_id": plan_version_id,
            "plan_hash": plan_hash,
            "preview": state.get("preview_text") or "",
            "assumptions": state.get("assumptions") or [],
            "options": ["approve", "reject", "edit"],
        }
    )
    payload = decision if isinstance(decision, dict) else {"decision": str(decision)}
    action = payload.get("decision") or "reject"

    if action == "approve":
        if payload.get("plan_hash") not in (None, plan_hash):
            # DE-2: the user approved a plan that is no longer current.
            return {
                "phase": "PREVIEW",
                "plan_status": "previewed",
                "pending_interrupt": None,
                "resume_payload": None,
                "outbox": emit(
                    state,
                    "error",
                    {"code": "PLAN_VERSION_STALE", "recoverable": True, "message": "计划已更新，请重新确认"},
                ),
            }
        consent = {
            "consent_id": new_id("cons"),
            "plan_version_id": plan_version_id,
            "plan_hash": plan_hash,
            "scope_kind": "gate2_full",
            "action_id": None,
        }
        return {
            "phase": "EXECUTE",
            "plan_status": "approved",
            "consents": [*(state.get("consents") or []), consent],
            "pending_interrupt": None,
            "resume_payload": None,
            "outbox": emit(state, "state_update", {"phase": "EXECUTE", "consent_id": consent["consent_id"]}),
        }

    if action == "edit":
        target = payload.get("target", "plan")
        return {
            "phase": "COLLECT" if target == "slots" else "PREVIEW",
            "plan_status": "superseded",
            "pending_interrupt": None,
            "resume_payload": payload,
            "outbox": emit(state, "state_update", {"phase": "COLLECT" if target == "slots" else "PREVIEW"}),
        }

    return {
        "phase": "COLLECT",
        "plan_status": "superseded",
        "pending_interrupt": None,
        "resume_payload": payload,
        "outbox": emit(state, "state_update", {"phase": "COLLECT", "reason": "user_rejected"}),
    }


async def apply_edit(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    """Deterministic bookkeeping after an edit; routing happens on the edge."""

    payload = state.get("resume_payload") or {}
    edits = payload.get("slot_patch") or {}
    slots = dict(state.get("slots") or {})
    for key, value in edits.items():
        if value in (None, "", []):
            continue
        slots[slot_policy.canonical_slot_id(str(key))] = {
            "value": value,
            "source": "user",
            "confidence": 1.0,
            "status": "filled",
            "confirmed_at": None,
        }
    return {
        "slots": slots,
        "plan_versions": list(state.get("plan_versions") or []),
        "assumptions": [],
        "outbox": emit(state, "state_update", {"phase": "COLLECT", "edited": sorted(edits)}),
    }
