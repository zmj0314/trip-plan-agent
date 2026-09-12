"""LangGraph assembly for the trip flow (framework §5).

Node split is deliberate: nodes that call the LLM or a capability never contain
``interrupt()``, and nodes that contain ``interrupt()`` do no work before it.
LangGraph re-runs a node from the top on resume, so mixing the two silently
duplicates side effects.
"""

from __future__ import annotations

from functools import partial
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from app.graph.deps import GraphDeps
from app.graph.nodes import content as content_nodes
from app.graph.nodes import execution, gates, intake as intake_nodes, planning
from app.graph.state import AgentState

REFUSAL_VERDICTS = {"out_of_scope", "abuse"}


def route_after_intake(state: AgentState, deps: GraphDeps) -> Literal["refuse", "slot_validate"]:
    verdict = (state.get("scope") or {}).get("verdict")
    return "refuse" if verdict in REFUSAL_VERDICTS else "slot_validate"


def route_after_slots(state: AgentState, deps: GraphDeps) -> Literal["clarify", "assume_fallback", "plan_draft"]:
    missing = state.get("missing_required") or []
    if not missing:
        return "plan_draft"
    if int(state.get("question_rounds") or 0) >= deps.settings.question_max_rounds:
        return "assume_fallback"
    return "clarify"


def route_after_consent(
    state: AgentState, deps: GraphDeps
) -> Literal["plan_actions", "apply_edit", "preview_render", "__end__"]:
    """Consent outcome -> next step.

    * approved  -> execute
    * edit      -> apply the patch and re-render a fresh preview
    * reject    -> stop the turn and hand control back to the user

    A rejection must **end the turn** rather than fall through to another
    preview: re-rendering straight away would re-open the consent gate before
    the user has said what they want changed, which reads as "the agent
    ignored me".
    """

    if state.get("phase") == "EXECUTE":
        return "plan_actions"
    if (state.get("resume_payload") or {}).get("decision") == "edit":
        return "apply_edit"
    return END


def route_after_dispatch(state: AgentState, deps: GraphDeps) -> Literal["action_gate", "run_l0_batch"]:
    return "action_gate" if state.get("extras_gated_action_id") else "run_l0_batch"


def build_trip_graph(deps: GraphDeps, *, checkpointer: Any = None):
    g: StateGraph = StateGraph(AgentState)

    g.add_node("intake", partial(intake_nodes.intake, deps=deps))
    g.add_node("refuse", partial(intake_nodes.refuse, deps=deps))
    g.add_node("clarify", partial(intake_nodes.clarify, deps=deps))
    g.add_node("ask", partial(intake_nodes.ask, deps=deps))
    g.add_node("slot_validate", partial(planning.slot_validate, deps=deps))
    g.add_node("assume_fallback", partial(planning.assume_fallback, deps=deps))
    g.add_node("plan_draft", partial(planning.plan_draft, deps=deps))
    # Content layer (F1): build the day-by-day plan with no LLM, then write its
    # prose with one. Splitting them is what keeps "the itinerary survives the
    # model being unavailable" true.
    g.add_node("plan_content", partial(content_nodes.plan_content, deps=deps))
    g.add_node("content_render", partial(content_nodes.content_render, deps=deps))
    g.add_node("plan_finalize", partial(planning.plan_finalize, deps=deps))
    g.add_node("preview_render", partial(planning.preview_render, deps=deps))
    g.add_node("consent_gate", partial(gates.consent_gate, deps=deps))
    g.add_node("apply_edit", partial(gates.apply_edit, deps=deps))
    g.add_node("plan_actions", partial(execution.plan_actions, deps=deps))
    g.add_node("dispatch", partial(execution.dispatch, deps=deps))
    g.add_node("action_gate", partial(execution.action_gate, deps=deps))
    g.add_node("run_l0_batch", partial(execution.run_l0_batch, deps=deps))
    g.add_node("assemble_itinerary", partial(execution.assemble_itinerary, deps=deps))
    g.add_node("remind", partial(execution.remind, deps=deps))

    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", partial(route_after_intake, deps=deps), ["refuse", "slot_validate"])
    g.add_edge("refuse", END)
    g.add_conditional_edges(
        "slot_validate",
        partial(route_after_slots, deps=deps),
        ["clarify", "assume_fallback", "plan_draft"],
    )
    g.add_edge("clarify", "ask")
    g.add_edge("ask", "intake")  # loop back: the answer needs extraction
    g.add_edge("assume_fallback", "plan_draft")
    g.add_edge("plan_draft", "plan_content")
    g.add_edge("plan_content", "content_render")
    g.add_edge("content_render", "plan_finalize")
    g.add_edge("plan_finalize", "preview_render")
    g.add_edge("preview_render", "consent_gate")
    g.add_conditional_edges(
        "consent_gate",
        partial(route_after_consent, deps=deps),
        ["plan_actions", "apply_edit", "preview_render", END],
    )
    g.add_edge("apply_edit", "slot_validate")
    g.add_edge("plan_actions", "dispatch")
    g.add_conditional_edges("dispatch", partial(route_after_dispatch, deps=deps), ["action_gate", "run_l0_batch"])
    g.add_edge("action_gate", "run_l0_batch")
    g.add_edge("run_l0_batch", "assemble_itinerary")
    g.add_edge("assemble_itinerary", "remind")
    g.add_edge("remind", END)

    return g.compile(checkpointer=checkpointer)
