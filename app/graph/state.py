"""Graph state (framework §4.2).

**Invariant: no credential ever appears here.** ``AgentState`` is serialised
into the LangGraph checkpoint, so anything placed in it is written to disk.
API keys travel through a per-call context object instead.
"""

from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    # session
    session_id: str
    user_id: str
    phase: str

    # R10 boundary
    scope: dict[str, Any]

    # slots
    slots: dict[str, dict[str, Any]]
    missing_required: list[str]
    #: Slots the gate computed instead of asking (F1: ``trip_days`` from the two
    #: dates, so the user answers one date question rather than two).
    derived_slots: dict[str, Any]
    #: Multi-city stays as the extractor heard them, before normalisation.
    destination_segments: list[dict[str, Any]]
    question_rounds: int
    assumptions: list[str]
    proposed_questions: list[str]

    # plan + consent
    plan_versions: list[dict[str, Any]]
    current_plan_version_id: str | None
    plan_hash: str | None
    plan_status: str
    preview_text: str
    consents: list[dict[str, Any]]
    pending_interrupt: dict[str, Any] | None

    # execution
    planned_legs: list[dict[str, Any]]
    #: Content layer (F1): the serialised ``TripContent`` built by ``plan_content``
    #: and rendered by ``content_render``. Kept as plain dicts so the checkpoint
    #: stays schema-tolerant.
    planned_content: dict[str, Any]
    actions: list[dict[str, Any]]
    action_cursor: int
    trip: dict[str, Any] | None

    # bookkeeping
    degradation_log: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    usage: dict[str, float]

    # per-turn io
    last_user_input: str
    resume_payload: dict[str, Any] | None
    outbox: list[dict[str, Any]]


def initial_state(*, session_id: str, user_id: str = "local") -> AgentState:
    return AgentState(
        session_id=session_id,
        user_id=user_id,
        phase="COLLECT",
        scope={
            "verdict": "in_scope",
            "last_verdict": None,
            "strikes": 0,
            "rejected_count": 0,
            "off_topic_streak": 0,
        },
        slots={},
        missing_required=[],
        derived_slots={},
        destination_segments=[],
        question_rounds=0,
        assumptions=[],
        proposed_questions=[],
        plan_versions=[],
        current_plan_version_id=None,
        plan_hash=None,
        plan_status="none",
        preview_text="",
        consents=[],
        pending_interrupt=None,
        planned_legs=[],
        planned_content={},
        actions=[],
        action_cursor=0,
        trip=None,
        degradation_log=[],
        errors=[],
        usage={"standard_tokens": 0.0},
        last_user_input="",
        resume_payload=None,
        outbox=[],
    )


def emit(state: AgentState, event_type: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build one outbox entry. The runner turns these into persisted Events."""

    return [{"type": event_type, "data": data}]
