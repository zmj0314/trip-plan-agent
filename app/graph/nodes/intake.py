"""Intake, scope gate and clarification (R10 + DC-2).

Division of labour (P1): the LLM *classifies and extracts*; deterministic code
*decides*. The scope verdict is therefore re-checked in code before it can
affect routing.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from app.domain.models import ScopeVerdict
from app.graph.deps import GraphDeps
from app.graph.state import AgentState, emit
from app.graph.stub_fallback import llm_for
from app.llm.context import assemble, summarise_state
from app.llm.schemas import ClarifyOutput, IntakeOutput
from app.policy import scope as scope_policy
from app.policy import slots as slot_policy

_INTAKE_TASK = (
    "任务：从用户输入中抽取旅行需求槽位，并判定是否属于旅行领域。\n"
    "只抽取用户明确表达或可高置信推断的信息；不要臆造日期、人数。\n"
    "slot_patch 的 key 必须来自槽位白名单：origin_address、destination、destinations、"
    "depart_date、return_date、time_window、travelers、rider_identity、mobility_needs、"
    "transport_preference、budget、lodging、intent。\n"
    "白名单之外的自由字段（如 duration_days、preference_walk）可以保留，"
    "但不要指望它们参与闸门校验。\n"
    "日期一律输出 YYYY-MM-DD；只输出日期本身，不要附带“当天返回”之类的说明文字。\n"
    "若用户提到多个目的地，destination 写成“城市A、城市B”，"
    "并在 destinations 里按顺序给出 [{\"city\": \"城市A\", \"days\": 3}]，"
    "天数以用户说的为准，没说的写 null——**不要为了凑总天数而平均分配**。"
)

_CLARIFY_TASK = (
    "任务：根据下方「待解决槽位」，生成 1 到 3 个中文追问。\n"
    "要求：一次问完、口语化、不要重复询问已收集的信息、不要询问列表之外的内容。"
)


def _schema_hint(model: type) -> str:
    parts = ["{"]
    for name, field in model.model_fields.items():  # type: ignore[attr-defined]
        parts.append(f'  "{name}": <{field.annotation}>')
    parts.append("}")
    return "\n".join(parts)


def _merge_slots(slots: dict[str, dict[str, Any]], patch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Apply a *patch*, never a full table.

    A model that loses track of the conversation must not be able to erase
    earlier answers by omitting them.

    Field names are canonicalised on the way in (:data:`app.policy.slots.SLOT_ALIASES`),
    so the gate and the extractor never disagree about what a slot is called.
    """

    merged = dict(slots)
    for key, value in patch.items():
        if value in (None, "", []):
            continue
        canonical = slot_policy.canonical_slot_id(str(key))
        merged[canonical] = {
            "value": slot_policy.normalise_slot_value(canonical, value),
            "source": "user",
            "confidence": 1.0,
            "status": "filled",
            "confirmed_at": None,
        }
    return merged


async def intake(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    text = state.get("last_user_input") or ""
    scope_payload = dict(state.get("scope") or {})

    slots = dict(state.get("slots") or {})
    # The prefilter is a *veto*, not a substitute for extraction. A confident
    # `in_scope` verdict only means "this turn does not need a scope decision";
    # the slots in the same sentence still have to be extracted, otherwise a
    # perfectly well-specified request walks into the gate with an empty table.
    prefiltered = scope_policy.prefilter(text)
    verdict = prefiltered.verdict if prefiltered is not None else None
    is_injection = bool(prefiltered.is_injection) if prefiltered is not None else False

    refusable = verdict in {ScopeVerdict.OUT_OF_SCOPE, ScopeVerdict.ABUSE}
    parsed: IntakeOutput | None = None
    if not refusable:
        llm = llm_for(deps)
        prompt = assemble(
            task=_INTAKE_TASK,
            schema_hint=_schema_hint(IntakeOutput),
            state_summary=summarise_state(
                slots=slots,
                phase=state.get("phase") or "COLLECT",
                open_questions=state.get("proposed_questions") or [],
                rejected_count=int(scope_payload.get("rejected_count", 0) or 0),
            ),
            user_input=text,
        )
        response = await llm.structured(node="intake", schema=IntakeOutput, prompt=prompt, user_input=text)
        parsed = response.parsed if isinstance(response.parsed, IntakeOutput) else None
        if parsed is None:
            # A failed extraction must never be swallowed -- not even when the
            # prefilter already formed an opinion. Swallowing it here is how a
            # 401 from the model turns into "the assistant just seems dumb":
            # the user gets template questions forever and no signal at all.
            return {
                "errors": [
                    *(state.get("errors") or []),
                    {"node": "intake", "error": response.error or "invalid"},
                ],
                "outbox": emit(
                    state,
                    "error",
                    {
                        "node": "intake",
                        "recoverable": True,
                        "message": "没能解析你的需求（模型调用未成功）",
                        "detail": response.error or "invalid response",
                    },
                ),
            }
        else:
            if verdict is None:
                verdict = parsed.scope
                is_injection = parsed.is_injection
            # P1: a deterministic verdict outranks the model's, so we only ever
            # *adopt* the model's scope when the prefilter abstained.
            if verdict in {ScopeVerdict.IN_SCOPE, ScopeVerdict.AMBIGUOUS}:
                patch = dict(parsed.slot_patch)
                # ``intent`` is its own field in the schema but is also a slot; fold it
                # in so the information gate has a single source of truth.
                if parsed.intent and not patch.get("intent"):
                    patch["intent"] = parsed.intent
                if patch:
                    slots = _merge_slots(slots, patch)

    if verdict is None:
        verdict = ScopeVerdict.AMBIGUOUS

    updated = scope_policy.apply(
        scope_policy.ScopeState.model_validate(scope_payload),
        verdict,
        is_injection=is_injection,
    )

    # DC-2: "你定 / 随便 / 都行" resolves the slots the user is declining to
    # specify, so a delegating user is never asked the same question twice.
    delegations: list[str] = []
    if verdict in {ScopeVerdict.IN_SCOPE, ScopeVerdict.AMBIGUOUS} and slot_policy.is_delegation(text):
        slot_values = {k: slot_policy.SlotValue.model_validate(v) for k, v in slots.items()}
        delegations = slot_policy.decline_remaining(
            slot_values,
            context=slot_policy.context_from_slots(slot_values),
        )
        slots = {k: v.model_dump(mode="json") for k, v in slot_values.items()}

    outbox = emit(state, "state_update", {"phase": state.get("phase"), "scope": verdict.value})
    if delegations:
        outbox = [
            *outbox,
            *emit(
                state,
                "state_update",
                {"phase": state.get("phase"), "assumptions": delegations, "reason": "user_delegated"},
            ),
        ]
    return {
        "slots": slots,
        "destination_segments": (
            list(parsed.destination_segments)
            if parsed is not None and parsed.destination_segments
            else list(state.get("destination_segments") or [])
        ),
        "assumptions": [*(state.get("assumptions") or []), *delegations],
        "scope": updated.model_dump(mode="json"),
        "outbox": outbox,
    }


async def refuse(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    """Deterministic refusal text -- never generated by the LLM (P7)."""

    scope_state = scope_policy.ScopeState.model_validate(state.get("scope") or {})
    text = scope_policy.refusal_text(scope_state)
    fused = scope_policy.is_fused(scope_state, limit=deps.settings.scope_strike_limit)
    outbox = emit(
        state,
        "scope_rejected",
        {
            "text": text,
            "verdict": scope_state.verdict.value,
            "strikes": scope_state.strikes,
            "rejected_count": scope_state.rejected_count,
            "fused": fused,
        },
    )
    update: dict[str, Any] = {"outbox": outbox}
    if fused:
        update["phase"] = "FAILED"
        update["outbox"] = [*outbox, *emit(state, "done", {"status": "failed", "reason": "SCOPE_ABUSE"})]
    return update


async def clarify(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    """Generate questions. Deliberately contains **no** interrupt (see ``ask``)."""

    missing = state.get("missing_required") or []
    llm = llm_for(deps)
    prompt = assemble(
        task=_CLARIFY_TASK,
        schema_hint=_schema_hint(ClarifyOutput),
        state_summary=summarise_state(
            slots=state.get("slots") or {},
            phase=state.get("phase") or "COLLECT",
            open_questions=missing,
        ),
        user_input=state.get("last_user_input") or "",
    )
    response = await llm.structured(node="clarify", schema=ClarifyOutput, prompt=prompt)
    parsed = response.parsed if isinstance(response.parsed, ClarifyOutput) else None
    questions = list(parsed.questions) if parsed and parsed.questions else _template_questions(missing)
    return {"proposed_questions": questions[:3]}


def _template_questions(missing: list[str]) -> list[str]:
    templates = {
        "origin_address": "你的出发地是哪里？请尽量精确到门牌或地标。",
        "destination": "你想去哪里？",
        "depart_date": "计划哪天出发？",
        "time_window": "出发和返回到几点之间方便？",
        "travelers": "一共几个人出行？有没有学生/儿童/老人票？",
        "transport_preference": "交通上有偏好吗？最快 / 最省 / 换乘最少，还是都行？",
        "budget": "预算大概多少？是硬上限还是参考值？",
        "mobility_needs": "有需要少走路或无障碍安排的人吗？",
        "lodging": "要不要安排住宿？",
        "intent": "你是想先要个方案建议，还是需要我准备好购票清单？",
        "rider_identity": "购票需要实名信息：每位乘车人的姓名与证件号。",
    }
    return [templates.get(m, f"请补充：{m}") for m in missing][:3] or ["请补充你的出行需求。"]


async def ask(state: AgentState, deps: GraphDeps) -> dict[str, Any]:
    """Pure before ``interrupt``.

    LangGraph re-executes a node from its top on resume, so anything with a side
    effect (an LLM call, a capability call) must live in a *previous* node. This
    node only reads state, then suspends.
    """

    questions = state.get("proposed_questions") or ["请补充你的出行需求。"]
    answer = interrupt(
        {
            "kind": "question",
            "questions": questions,
            "prompt": "\n".join(questions),
        }
    )
    if isinstance(answer, dict):
        text = str(answer.get("text", ""))
    else:
        text = str(answer)
    return {
        "last_user_input": text,
        "question_rounds": int(state.get("question_rounds") or 0) + 1,
        "proposed_questions": [],
        "outbox": emit(state, "state_update", {"phase": "COLLECT", "questions": questions}),
    }
