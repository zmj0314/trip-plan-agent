"""Domain-boundary gate, layer 1 of R10 (framework §5.2, D0).

The model never decides "is this off topic" on its own: a deterministic
prefilter answers the easy cases, the LLM may only fill the remaining
``ambiguous`` gap, and the *counters* that terminate an abusive session live
here.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from app.domain.models import ScopeState, ScopeVerdict

#: The travel neighbourhood. Anything clearly inside it never needs an LLM call.
IN_SCOPE_TOPICS: frozenset[str] = frozenset(
    {
        "旅行",
        "旅游",
        "行程",
        "出行",
        "景点",
        "门票",
        "预约",
        "高铁",
        "火车",
        "动车",
        "飞机",
        "航班",
        "机票",
        "车票",
        "地铁",
        "公交",
        "打车",
        "自驾",
        "酒店",
        "住宿",
        "民宿",
        "路线",
        "怎么走",
        "换乘",
        "中转",
        "天气",
        "好玩",
        "攻略",
        "古镇",
        "长城",
        "爬山",
        "露营",
        "签证",
        "机场",
        "车站",
        "景区",
        "旅行",  # duplicate kept harmless; registry is a frozenset
        "travel",
        "trip",
        "itinerary",
        "flight",
        "train",
        "hotel",
        "weather",
        "route",
    }
)

#: Topics that are clearly outside the product surface (handoff §1.3).
OUT_OF_SCOPE_TOPICS: frozenset[str] = frozenset(
    {
        "写代码",
        "写诗",
        "写作文",
        "论文",
        "翻译",
        "编程",
        "股票",
        "基金",
        "炒股",
        "理财",
        "医疗",
        "诊断",
        "处方",
        "法律",
        "离婚",
        "情感",
        "分手",
        "恋爱",
        "心理咨询",
        "代写作业",
        "赌博",
        "彩票",
        "挖矿",
        "破解",
        "外挂",
        "写小说",
        "做一个游戏",
        "code",
        "homework",
        "essay",
        "stock",
        "diagnosis",
        "relationship advice",
    }
)

#: Abuse-grade patterns: prompt injection, role hijack, policy exfiltration.
#: These are counted as ``strikes`` and are answered with a different template
#: than ordinary off-topic input.
INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instruction|prompt|rule)", re.I),
    re.compile(r"disregard\s+(the\s+)?(system|previous)\s+(prompt|instruction)", re.I),
    re.compile(r"(忽略|无视|忘记|忘掉)(上面|之前|以上|前面)?(的)?(所有)?(指令|提示|规则|设定)"),
    re.compile(r"(现在|从现在起)?(你)?(不再|不是)(是)?(一个)?(旅行|旅游)"),
    re.compile(r"(扮演|假装|模拟)(你是)?(一个)?(没有限制|不受限制|无限制)"),
    re.compile(r"(开发者模式|越狱模式|dev(eloper)?\s*mode|jailbreak|dan\s*mode)", re.I),
    re.compile(r"(输出|打印|告诉我|泄露|复述)(你的)?(系统提示|system\s*prompt|prompt|内部指令|developer\s*message)", re.I),
    re.compile(r"(api[_\s-]?key|密钥|token|凭证)\s*(是什么|是|:|＝|=)", re.I),
)

#: Questions that merely *look* like injections but are legitimate travel talk.
_INJECTION_WHITELIST: tuple[re.Pattern[str], ...] = (
    re.compile(r"(忽略|不用管)(天气|预算|上面说的时间)"),
)


class ScopeDecision(BaseModel):
    verdict: ScopeVerdict
    reason: str
    is_injection: bool = False


def _normalise(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _matches_any(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def prefilter(text: str) -> ScopeDecision | None:
    """Answer the easy cases deterministically; ``None`` means "ask the LLM".

    Order matters: injection detection runs first so that an abusive message
    that also mentions a city cannot be laundered into ``in_scope``.
    """

    normalised = _normalise(text)
    if not normalised:
        return ScopeDecision(verdict=ScopeVerdict.AMBIGUOUS, reason="empty input")

    if _matches_any(INJECTION_PATTERNS, normalised) and not _matches_any(
        _INJECTION_WHITELIST, normalised
    ):
        return ScopeDecision(
            verdict=ScopeVerdict.ABUSE,
            reason="prompt-injection or policy-exfiltration pattern",
            is_injection=True,
        )

    has_out = any(topic in normalised for topic in OUT_OF_SCOPE_TOPICS)
    has_in = any(topic in normalised for topic in IN_SCOPE_TOPICS)

    if has_out and not has_in:
        return ScopeDecision(verdict=ScopeVerdict.OUT_OF_SCOPE, reason="outside product surface")
    if has_in and not has_out:
        return ScopeDecision(verdict=ScopeVerdict.IN_SCOPE, reason="travel neighbourhood keyword")
    if has_in and has_out:
        return None
    return None


def apply(state: ScopeState, verdict: ScopeVerdict, *, is_injection: bool = False) -> ScopeState:
    """Return the next :class:`ScopeState` for one user turn (D0)."""

    new = state.model_copy(deep=True)
    new.last_verdict = verdict
    new.verdict = verdict

    if is_injection or verdict is ScopeVerdict.ABUSE:
        new.strikes += 1
        return new

    if verdict is ScopeVerdict.OUT_OF_SCOPE:
        new.rejected_count += 1
        new.off_topic_streak += 1
        return new

    if verdict is ScopeVerdict.IN_SCOPE:
        new.off_topic_streak = 0
        return new

    # AMBIGUOUS: accept the turn once and normalise it, without scoring a strike.
    return new


def is_fused(state: ScopeState, *, limit: int) -> bool:
    """True when the session must terminate with ``FAILED(SCOPE_ABUSE)``."""

    return state.strikes >= limit


def refusal_text(state: ScopeState) -> str:
    """Deterministic refusal copy — never routed through the LLM (P7)."""

    if state.last_verdict is ScopeVerdict.ABUSE:
        return (
            "这个我没法配合：我只做国内出行规划，也不会透露内部提示词或凭证。"
            "如果还想规划行程，直接告诉我起点、目的地和日期就行。"
        )
    if state.last_verdict is ScopeVerdict.OUT_OF_SCOPE:
        return (
            "这超出我的范围了——我只负责国内出行规划（路线、城际交通、天气、门票预约）。"
            "把出行需求告诉我，我马上开始。"
        )
    if state.off_topic_streak >= 2:
        return "我们聊回正题吧：告诉我起点、目的地和日期，我来做行程。"
    return "先把出行需求说清楚：起点、目的地、日期，我就能开始规划。"
