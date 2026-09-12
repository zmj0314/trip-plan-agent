"""Declarative slot registry and the **information gate** (framework §4.2, §5.2).

Acceptance criterion §12 #1 ("the information gate blocks under-specified
plans 100% of the time") is implemented here as a pure function, because an LLM
cannot make a 100% guarantee.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Any, Mapping

from pydantic import BaseModel, Field

from app.domain.models import SlotStatus, SlotValue
from app.domain.timebase import parse_ymd


class Requirement(StrEnum):
    ALWAYS = "always"
    CONDITIONAL = "conditional"
    OPTIONAL = "optional"
    #: Computed from other slots: never asked, never blocks, but must have a
    #: value before planning can use it (F1: ``trip_days`` comes from
    #: ``return_date``, so the user answers one date question instead of two).
    DERIVED = "derived"


class SlotSpec(BaseModel):
    """One declarative slot definition (§4.2 point 3)."""

    slot_id: str
    title: str
    requirement: Requirement
    condition: str | None = None
    ask_template: str
    disambiguation: str | None = None
    default: Any = None


#: The slots of the handoff §3.1, expressed as a registry rather than as code,
#: so the gate, the clarification prompt and the assumption list all read from
#: one source of truth.
#:
#: Requirement levels encode *what actually blocks execution*:
#:
#: * ``ALWAYS`` -- without it there is no plan to preview at all (where from,
#:   where to, when, who, and whether this is advice or a booking). These are
#:   the slots acceptance criterion §12 #1 is written against: every one of
#:   them, emptied, must send the session back to clarification.
#: * ``OPTIONAL`` -- has a genuinely conservative default, so the honest move
#:   is to *state the assumption on the preview card* (DE-3) rather than hold
#:   the whole plan hostage to a question about, say, budget. They still get
#:   asked when they are cheap to ask, and the user can override any of them.
#: * ``CONDITIONAL`` -- becomes ``ALWAYS`` only for the stated condition.
#:
#: The split matters because "ask everything before doing anything" and "answer
#: what you were asked" are both promises; only the first one can be kept by
#: refusing to move.
SLOT_REGISTRY: tuple[SlotSpec, ...] = (
    SlotSpec(
        slot_id="origin_address",
        title="起点精确地址",
        requirement=Requirement.ALWAYS,
        ask_template="你从哪儿出发？给我一个具体到门牌的地址（例如「望京SOHO T1」）。",
        disambiguation="只说城区不够——朝阳区很大，望京/国贸/十里堡去不同的车站差别很大。",
        default="用户未提供，按城市中心估算",
    ),
    SlotSpec(
        slot_id="destination",
        title="目的地",
        requirement=Requirement.ALWAYS,
        ask_template="想去哪儿？如果是长城、古镇这类有多段的地点，请说明具体到哪一段/哪个门。",
        disambiguation="长城要消歧到具体段（八达岭 / 慕田峪 / 司马台），各段交通与门票完全不同。",
        default="用户未指定，取最主流的那一段",
    ),
    SlotSpec(
        slot_id="depart_date",
        title="出发日期",
        requirement=Requirement.ALWAYS,
        ask_template="哪一天出发？",
        disambiguation="相对日期（明天/下周）必须用 clock.today 校准，不要按模型记忆推算。",
        default="用户未指定，取最近的周末",
    ),
    SlotSpec(
        slot_id="return_date",
        title="返程日期",
        requirement=Requirement.ALWAYS,
        ask_template="哪天回来？（这一条决定了行程有几天）",
        disambiguation="返程日期优先于天数：只问日期，天数由日期推导。",
        default="用户未指定，按当日往返处理",
    ),
    SlotSpec(
        slot_id="trip_days",
        title="行程天数",
        requirement=Requirement.DERIVED,
        ask_template="",
        disambiguation="由 return_date - depart_date + 1 推导，不单独追问。",
        default=1,
    ),
    SlotSpec(
        slot_id="destinations",
        title="多目的地分段",
        requirement=Requirement.DERIVED,
        ask_template="",
        disambiguation="由目的地原话经 policy/segments.py 确定性解析，不单独追问。",
        default="",
    ),
    SlotSpec(
        slot_id="time_window",
        title="时间窗",
        requirement=Requirement.OPTIONAL,
        ask_template="出发和返回到几点之间方便？",
        default="用户未指定，按 08:00–20:00 宽松处理",
    ),
    SlotSpec(
        slot_id="travelers",
        title="人数与票种",
        requirement=Requirement.ALWAYS,
        ask_template="几个人出行？有没有学生、儿童、老人票？",
        default="用户未指定，按 1 名成人",
    ),
    SlotSpec(
        slot_id="transport_preference",
        title="交通偏好",
        requirement=Requirement.OPTIONAL,
        ask_template="交通上有偏好吗？最快 / 最省 / 换乘最少，还是都行？",
        default="用户未指定，按默认权重（时间 0.25 / 费用 0.20 / 换乘 0.20）",
    ),
    SlotSpec(
        slot_id="budget",
        title="预算",
        requirement=Requirement.OPTIONAL,
        ask_template="预算大概多少？是硬上限还是参考值？",
        default="用户未指定，不设硬上限，仅参与排序",
    ),
    SlotSpec(
        slot_id="mobility_needs",
        title="少走路 / 无障碍",
        requirement=Requirement.OPTIONAL,
        ask_template="有需要少走路或无障碍安排的人吗？",
        default="用户未说明，按无特殊通行需求处理",
    ),
    SlotSpec(
        slot_id="lodging",
        title="是否需要住宿",
        # D1: lodging is part of v1, so this is asked rather than assumed away.
        # It is not extra interrogation -- it replaces the question that used to
        # be skipped, and one answer ("自己订") settles it for the whole trip.
        requirement=Requirement.ALWAYS,
        ask_template="住宿我来安排吗？还是你自己订？（我只会给区域与参考价，付款始终由你来）",
        default="用户未说明，按需要安排住宿处理",
    ),
    SlotSpec(
        slot_id="lodging_area",
        title="住宿区域",
        requirement=Requirement.CONDITIONAL,
        condition="lodging:need",
        ask_template="想住哪个区域？不说的话我按当天主要活动区给建议。",
        default="用户未指定，按每天主要活动区域推荐",
    ),
    SlotSpec(
        slot_id="lodging_budget",
        title="每晚预算",
        requirement=Requirement.OPTIONAL,
        ask_template="每晚预算大概多少？",
        default="用户未指定，不限价位，只列参考价",
    ),
    SlotSpec(
        slot_id="trip_days_hint",
        title="行程天数（用户口述）",
        requirement=Requirement.OPTIONAL,
        ask_template="打算玩几天？",
        disambiguation="用户直接说了天数时用它校验日期区间，不作为闸门条件。",
        default="",
    ),
    SlotSpec(
        slot_id="intent",
        title="意图（建议 / 购票）",
        requirement=Requirement.ALWAYS,
        ask_template="你是只要一份建议，还是要我把票也备好（仍是你在官方渠道付款）？",
        default="用户未说明，按只出建议处理",
    ),
    SlotSpec(
        slot_id="rider_identity",
        title="乘车人实名信息",
        requirement=Requirement.CONDITIONAL,
        condition="intent:booking",
        ask_template="购票需要实名信息：每位乘车人的姓名与证件号（只在内存中使用，不落库）。",
        disambiguation="PII 不持久化：只用于生成深链或清单，生成后即释放（DI-4）。",
        default="用户未提供，改为只输出购票清单（D3）",
    ),
    SlotSpec(
        slot_id="stay_nights",
        title="住几晚 / 住宿偏好",
        requirement=Requirement.OPTIONAL,
        ask_template="如果住，住几晚、有没有位置或价位偏好？",
    ),
    SlotSpec(
        slot_id="day_rhythm",
        title="每日节奏",
        requirement=Requirement.OPTIONAL,
        ask_template="每天几点开始、几点收工？",
        default="用户未指定，按 09:00–20:00 安排",
    ),
    SlotSpec(
        slot_id="dining_pref",
        title="餐饮偏好",
        requirement=Requirement.OPTIONAL,
        ask_template="吃的方面有讲究吗？（v1 只做就近建议，不查真实餐厅）",
        default="用户未指定，按就近解决",
    ),
    SlotSpec(
        slot_id="content_depth",
        title="行程颗粒度",
        requirement=Requirement.OPTIONAL,
        ask_template="行程要概览还是要逐条细排？",
        default="用户未指定，按概览 + 逐日时段安排",
    ),
)

SLOTS_BY_ID: dict[str, SlotSpec] = {spec.slot_id: spec for spec in SLOT_REGISTRY}

#: Extraction vocabulary -> registry vocabulary.
#:
#: The semantic layer is free to name a field whatever reads best in a prompt
#: ("origin_text", "traveler_count"), but the gate can only ever look up a
#: *registry* id. Without this table the two vocabularies silently drift apart
#: and the gate reports every slot as missing forever -- which is exactly the
#: failure mode acceptance criterion §12 #1 is written against.
SLOT_ALIASES: dict[str, str] = {
    "origin": "origin_address",
    "origin_text": "origin_address",
    "start_address": "origin_address",
    "departure": "origin_address",
    "from": "origin_address",
    "destination_raw": "destination",
    "destination_text": "destination",
    "dest": "destination",
    "city": "destination",
    "to": "destination",
    "destinations_raw": "destinations",
    "destination_segments": "destinations",
    "multi_city": "destinations",
    "cities": "destinations",
    "depart_window": "time_window",
    "return_window": "time_window",
    "time_period": "time_window",
    "end_date": "return_date",
    "return": "return_date",
    "back_date": "return_date",
    "return_day": "return_date",
    "return_by": "return_date",
    "date_end": "return_date",
    "end": "return_date",
    "days": "trip_days_hint",
    "day_count": "trip_days_hint",
    "duration_days": "trip_days_hint",
    "total_days": "trip_days_hint",
    "nights": "trip_days_hint",
    "start": "depart_date",
    "date_start": "depart_date",
    "start_day": "depart_date",
    "traveler_count": "travelers",
    "traveler_types": "travelers",
    "travellers": "travelers",
    "traveler_accessibility": "mobility_needs",
    "accessibility": "mobility_needs",
    "preference_transport": "transport_preference",
    "preference_profile": "transport_preference",
    "transport": "transport_preference",
    "preference_budget": "budget",
    "stay_needed": "lodging",
    "lodging_needed": "lodging",
    "accommodation": "lodging",
    "hotel": "lodging",
    "stay_area": "lodging_area",
    "hotel_area": "lodging_area",
    "rhythm": "day_rhythm",
    "daily_window": "day_rhythm",
    "dining": "dining_pref",
    "food_preference": "dining_pref",
    "traveler_real_name": "rider_identity",
    "rider": "rider_identity",
    "passengers": "rider_identity",
}


#: Tokens that mean "the same day" rather than naming a date. A model that
#: answers ``return_date`` with "当天返回" has answered a different question, and
#: reading it as a date would silently produce a one-day trip or an exception.
SAME_DAY_TOKENS = ("当天", "当日", "同日", "当天返回", "当天往返", "当日往返", "当天来回", "当日来回")

_DATE_LEAD_RE = re.compile(r"\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}")


def normalise_date(value: Any, *, default_year: int | None = None) -> str | None:
    """Extract a ``YYYY-MM-DD`` date from what the model actually wrote.

    Two shapes show up in practice and both must survive:

    * ``"2026-09-12 当天返回"`` -- a date with prose appended;
    * ``"9月15日"`` -- no year, which needs one to become a gate value.

    Returns ``None`` when there is no date to find, or when the value is a
    same-day phrase rather than a date (the caller decides what that means).
    """

    if isinstance(value, date):  # already a date; nothing to normalise
        return value.isoformat()
    text = str(value).strip() if value is not None else ""
    if not text:
        return None

    # An explicit date wins over the surrounding wording: "2026-09-12 当天返回"
    # names both a date and a same-day intent, and the date is the more precise
    # of the two. Only when there is no date does the phrase decide.
    iso = _DATE_LEAD_RE.search(text)
    if iso:
        try:
            return parse_ymd(iso.group(0).replace("/", "-").replace(".", "-")).isoformat()
        except (TypeError, ValueError):
            return None

    if any(token in text for token in SAME_DAY_TOKENS):
        return None

    month_day = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", text)
    if month_day and default_year:
        try:
            return date(default_year, int(month_day.group(1)), int(month_day.group(2))).isoformat()
        except ValueError:
            return None
    return None


def canonical_slot_id(key: str) -> str:
    """Map an extraction field name onto its registry id.

    Matching is by *longest* alias first, not by dictionary order. ``destination``
    is a prefix of ``destinations``, so a plain substring scan turns a multi-city
    answer into a single destination -- silently deleting every city after the
    first. Longest-first makes the more specific alias win.

    Unknown keys are returned unchanged: they are legitimate *extra* context
    (e.g. ``preference_walk``) that no slot spec gates on, and dropping them
    would lose information the preview card may want to show.
    """

    text = str(key or "")
    if text in SLOTS_BY_ID:
        return text
    for alias in ALIASES_BY_LENGTH:
        if alias and alias in text:
            return SLOT_ALIASES[alias]
    return text


#: Aliases ordered longest-first, so ``destinations`` is tried before
#: ``destination``. Computed once at import: the ordering is what makes the
#: substring match safe.
ALIASES_BY_LENGTH: tuple[str, ...] = tuple(
    sorted(SLOT_ALIASES, key=len, reverse=True)
)


#: Negations that mean "do not arrange this for me". Checked *before* the
#: positive markers, because "不需要住宿" contains "住宿" -- matching the positive
#: form first would read a refusal as a request.
_NEGATIONS = ("不", "无", "免", "自己", "自订", "自理", "不用", "none", "no", "self")

#: Markers that mean "please arrange this". They are the model's own vocabulary
#: (``need`` / ``self_arranged`` / ``不需要住宿``), not a protocol we control, so the
#: two-vocabulary table lives here rather than in a prompt the model may ignore.
_LODGING_NEEDED = ("需要", "要住", "安排", "预订", "代订", "need", "yes", "true", "book")


def normalise_lodging(value: Any) -> str | None:
    """``"self_arranged"``/``"不需要住宿"`` -> ``"none"``; ``"需要"`` -> ``"need"``.

    Returns ``None`` when the answer is not recognisable, so the caller can keep
    the raw text as context instead of pretending it understood.
    """

    text = _stringify(value).strip() if value is not None else ""
    if not text:
        return None
    lowered = text.lower()
    # Strip the slot's own noun first so "不需要住宿" is judged on "不需要".
    subject_words = ("住宿", "酒店", "房子", "hotel", "lodging", "stay")
    stripped = lowered
    for word in subject_words:
        stripped = stripped.replace(word, "")
    if any(marker in stripped for marker in _NEGATIONS):
        return "none"
    if any(marker in stripped for marker in _LODGING_NEEDED):
        return "need"
    return None


def normalise_slot_value(slot_id: str, value: Any) -> Any:
    """Coerce an extracted value into the vocabulary the gate checks.

    Only slots with a closed or well-understood shape are touched. Free-form
    slots (an origin address, a budget) are passed through: inventing a canonical
    form for them would be a decision the model did not make.
    """

    if slot_id == "lodging":
        return normalise_lodging(value) or value
    if slot_id in {"depart_date", "return_date"}:
        # Keep the raw text when no date can be read: the gate then reports the
        # slot as unresolved and asks, which is better than storing prose that
        # every later step will fail to parse.
        return normalise_date(value) or value
    if slot_id == "destinations":
        # The model answers "北京 → 天津" as a *list*, sometimes with the day counts
        # it heard. Flattening it to "北京、天津" would discard those counts and
        # the parser would then have to invent a split, so the list is rendered
        # back into the shape the parser reads.
        if isinstance(value, (list, tuple)) and value:
            pieces: list[str] = []
            for item in value:
                if isinstance(item, Mapping):
                    city = str(item.get("city") or item.get("name") or "").strip()
                    days = item.get("days")
                    if city and days:
                        pieces.append(f"{city}{days}天")
                    elif city:
                        pieces.append(city)
                else:
                    text = str(item).strip()
                    if text:
                        pieces.append(text)
            return "，".join(pieces)
    return value


class SlotValidation(BaseModel):
    """Result of one evaluation of the information gate."""

    ok: bool
    missing: list[str] = Field(default_factory=list)
    resolved: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    #: Slots the gate filled in itself (F1: ``trip_days`` from the two dates).
    derived: dict[str, Any] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)


#: Longest trip this planner will lay out. Beyond the weather window there is no
#: forecast to plan against, so a longer request is trimmed and disclosed.
MAX_TRIP_DAYS = 15


def derive_trip_days(
    slots: Mapping[str, SlotValue],
) -> tuple[int | None, list[str], list[str]]:
    """Compute ``trip_days`` from the two dates (D2: ``return_date`` wins).

    Returns ``(days, issues, notes)``. ``issues`` are contradictions that must
    send the user back to clarification (a return before departure is not
    something to guess about); ``notes`` are honest adjustments the plan can
    carry as assumptions (the request was longer than the forecast window).
    """

    depart = slots.get("depart_date")
    ret = slots.get("return_date")
    depart_value = depart.value if _is_resolved(depart) else None
    return_value = ret.value if _is_resolved(ret) else None
    if not depart_value or not return_value:
        return None, [], []

    try:
        start = parse_ymd(depart_value)
    except (TypeError, ValueError):
        # Not a date we can read. That is a *missing value*, not a contradiction:
        # flagging it here would send a delegating user back to clarification
        # forever, which is exactly what DC-2 forbids.
        return None, [], []
    try:
        end = parse_ymd(return_value)
    except (TypeError, ValueError):
        return None, [], []

    days = (end - start).days + 1
    issues: list[str] = []
    notes: list[str] = []
    if days <= 0:
        return None, ["返程日期早于出发日期，请确认日期区间"], []
    if days > MAX_TRIP_DAYS:
        notes.append(f"行程共 {days} 天，超出可规划上限 {MAX_TRIP_DAYS} 天，仅安排前 {MAX_TRIP_DAYS} 天")
        days = MAX_TRIP_DAYS
    return days, issues, notes


def _explicit_days(slots: Mapping[str, SlotValue]) -> int | None:
    """A duration the user stated in words, when the dates did not supply one.

    ``trip_days_hint`` is what an extraction like ``duration_days: 5`` lands on.
    It never gates anything -- it is only used to fill the derived length when the
    two dates are missing or unreadable, which is the difference between "no plan"
    and "a plan of the length the user asked for".

    An over-long value is returned as stated so the caller can trim it *and say
    so*; rejecting it here would turn "40 days" into "we have no idea".
    """

    hint = slots.get("trip_days_hint")
    if not _is_resolved(hint):
        return None
    raw = hint.value
    if isinstance(raw, Mapping):
        raw = raw.get("days") or raw.get("count")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 1 else None


def derive_trip_days_from_hint(slots: Mapping[str, SlotValue]) -> tuple[int | None, list[str]]:
    """Day count from a stated duration, with the same disclosure rules."""

    days = _explicit_days(slots)
    if days is None:
        return None, []
    notes: list[str] = []
    if days > MAX_TRIP_DAYS:
        notes.append(f"行程 {days} 天超出上限 {MAX_TRIP_DAYS} 天，仅安排前 {MAX_TRIP_DAYS} 天")
        days = MAX_TRIP_DAYS
    return days, notes


def _stringify(value: Any) -> str:
    if isinstance(value, StrEnum):
        return str(value.value)
    return str(value)


def condition_met(condition: str, context: Mapping[str, Any]) -> bool:
    """Evaluate a ``"key:expected"`` condition against the slot context.

    Keeping conditions in this tiny DSL (instead of callables) is deliberate:
    the registry stays declarative and serialisable, and every branch is
    unit-testable.
    """

    key, _, expected = condition.partition(":")
    if not expected:
        return bool(context.get(key))
    actual = context.get(key)
    if isinstance(actual, (list, tuple, set, frozenset)):
        return expected in {_stringify(item) for item in actual}
    return _stringify(actual) == expected


def is_active(spec: SlotSpec, context: Mapping[str, Any]) -> bool:
    if spec.requirement is Requirement.ALWAYS:
        return True
    if spec.requirement is Requirement.CONDITIONAL:
        return bool(spec.condition) and condition_met(spec.condition or "", context)
    # DERIVED and OPTIONAL never block: one is computed, the other has a
    # conservative default that is disclosed on the preview card instead.
    return False


def active_required(context: Mapping[str, Any]) -> list[SlotSpec]:
    """Return the slots that must be resolved for this context (§4.2)."""

    return [spec for spec in SLOT_REGISTRY if is_active(spec, context)]


def assumption_text(spec: SlotSpec) -> str:
    """Human-readable description of the assumption used for ``spec``."""

    if spec.default in (None, ""):
        return f"{spec.title}：未提供，按系统保守默认处理"
    return f"{spec.title}：{spec.default}"


def _is_resolved(value: SlotValue | None) -> bool:
    return value is not None and value.status.resolved


def validate(
    slots: Mapping[str, SlotValue],
    *,
    context: Mapping[str, Any],
    rounds_used: int,
    max_rounds: int,
) -> SlotValidation:
    """The deterministic information gate.

    * ``FILLED`` and ``USER_DECLINED`` both count as resolved — otherwise a user
      who says "you decide" could never get past clarification (DC-2).
    * While rounds remain, any unresolved required slot keeps ``ok=False``, so
      the graph cannot reach execution.
    * Once the question budget is spent, the gate opens **with an explicit
      assumption list** instead of looping forever.
    """

    resolved: list[str] = []
    missing: list[str] = []
    for spec in active_required(context):
        if _is_resolved(slots.get(spec.slot_id)):
            resolved.append(spec.slot_id)
        else:
            missing.append(spec.slot_id)

    # DERIVED slots are computed here rather than asked. A contradiction in the
    # inputs they come from (a return before departure) is surfaced as an issue
    # so the caller can send the user back, instead of guessing a length.
    derived: dict[str, Any] = {}
    issues: list[str] = []
    notes: list[str] = []
    if SLOTS_BY_ID["trip_days"].requirement is Requirement.DERIVED:
        days, issues, notes = derive_trip_days(slots)
        if days is None and not issues:
            # The dates did not yield a length; a duration the user stated is the
            # next best source, and is far better than falling back to one day.
            days, hint_notes = derive_trip_days_from_hint(slots)
            notes = [*notes, *hint_notes]
        if days is not None:
            derived["trip_days"] = days

    if issues:
        return SlotValidation(
            ok=False,
            missing=["return_date"],
            resolved=resolved,
            assumptions=[*notes, *issues],
            derived=derived,
            issues=issues,
        )

    if not missing:
        return SlotValidation(
            ok=True, missing=[], resolved=resolved, assumptions=notes, derived=derived
        )

    if rounds_used >= max_rounds:
        assumptions = [assumption_text(SLOTS_BY_ID[slot_id]) for slot_id in missing]
        # A derived slot cannot be asked for, so if it never materialised the
        # assumption list has to say what the fallback was.
        if "trip_days" not in derived:
            assumptions.append("行程天数：未提供返程日期，按当日往返（1 天）处理")
            derived["trip_days"] = 1
        return SlotValidation(
            ok=True,
            missing=missing,
            resolved=resolved,
            assumptions=[*notes, *assumptions],
            derived=derived,
        )

    return SlotValidation(
        ok=False, missing=missing, resolved=resolved, assumptions=notes, derived=derived
    )


def apply_defaults(slots: dict[str, SlotValue], missing: list[str]) -> list[str]:
    """Fill unresolved required slots with their conservative defaults.

    Called only when the question budget is exhausted (``assume_fallback``).
    Returns the assumption texts that must be shown at the top of the preview
    card (DE-3).
    """

    assumptions: list[str] = []
    for slot_id in missing:
        spec = SLOTS_BY_ID.get(slot_id)
        if spec is None:
            continue
        existing = slots.get(slot_id)
        if _is_resolved(existing):
            continue
        slots[slot_id] = SlotValue.declined(spec.default)
        assumptions.append(assumption_text(spec))
    return assumptions


#: Phrases that mean "stop asking, use your judgement" (DC-2).
#:
#: These are matched deterministically rather than inferred by the model: a
#: user who delegates must never be asked the same question again, and the
#: *record* of that delegation has to be reproducible.
DELEGATION_MARKERS: tuple[str, ...] = (
    "你定",
    "你决定",
    "你来定",
    "你看着办",
    "帮我定",
    "帮忙定",
    "你安排",
    "你看着安排",
    "按你",
    "听你的",
    "都听你",
    "随便",
    "都行",
    "都可以",
    "都无所谓",
    "无所谓",
    "不限",
    "没要求",
    "没有要求",
    "没偏好",
    "没有偏好",
    "不确定",
    "没有特别",
)


def is_delegation(text: str) -> bool:
    """True when the user explicitly hands the decision back to the agent."""

    return any(marker in (text or "") for marker in DELEGATION_MARKERS)


def decline_remaining(
    slots: dict[str, SlotValue],
    *,
    context: Mapping[str, Any],
) -> list[str]:
    """Resolve every still-missing required slot as ``USER_DECLINED``.

    Called when the user delegates ("你定", "随便", "都行"). ``USER_DECLINED``
    counts as resolved, so the gate opens with the conservative defaults from
    the registry and the preview card can state them as assumptions (DE-3).
    """

    assumptions: list[str] = []
    for spec in active_required(context):
        existing = slots.get(spec.slot_id)
        if _is_resolved(existing):
            continue
        slots[spec.slot_id] = SlotValue.declined(spec.default)
        assumptions.append(assumption_text(spec))
    return assumptions


def unstated_optional(
    slots: Mapping[str, SlotValue],
    *,
    context: Mapping[str, Any],
) -> list[str]:
    """Assumption texts for optional slots the user never spoke about.

    DE-3 wants the assumption list at the top of the preview card, and these
    are exactly the decisions the agent is about to make on the user's behalf.
    Keeping them out of ``missing_required`` is what lets "先问清" and
    "don't interrogate" coexist: the plan proceeds, visibly.
    """

    assumptions: list[str] = []
    for spec in SLOT_REGISTRY:
        if spec.requirement is not Requirement.OPTIONAL:
            continue
        if _is_resolved(slots.get(spec.slot_id)):
            continue
        assumptions.append(assumption_text(spec))
    return assumptions


#: ``intent`` slot values -> the vocabulary used by ``SlotSpec.condition``.
#:
#: The schema exposes three intents (``advice_only`` / ``booking_list`` /
#: ``reservation``) but a spec only ever asks "is this a booking?" -- mapping
#: here keeps that two-branch question out of every call site.
_INTENT_CONDITION_VALUES: dict[str, str] = {
    "advice_only": "advice_only",
    "booking_list": "booking",
    "reservation": "booking",
}


def context_from_slots(slots: Mapping[str, SlotValue]) -> dict[str, Any]:
    """Build the ``condition_met`` context from resolved slots.

    ``SlotSpec.condition`` is written in a ``"key:expected"`` DSL, so the value
    has to be stored under the bare key in the vocabulary the spec expects --
    ``{"intent": "booking"}``, not ``{"intent": "reservation"}``.
    """

    context: dict[str, Any] = {}
    for slot_id, value in slots.items():
        if value.status.resolved:
            context[slot_id] = value.value
    intent = context.get("intent")
    if intent is not None:
        context["intent"] = _INTENT_CONDITION_VALUES.get(str(intent), str(intent))
    return context
