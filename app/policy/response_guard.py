"""Single visible-output exit, layer 4 of R10 (framework §2 P7, §5.2).

P7: *the model has no free-form output channel*. Every string that can reach a
user passes through :func:`check`, which enforces length, blocks internal
identifiers and provider plumbing, and refuses to echo credentials or PII.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

#: Upper bound for one visible message, so a runaway model cannot flood the UI.
MAX_VISIBLE_CHARS = 4_000

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("credential", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}")),
    ("credential", re.compile(r"\b(?:amap|variflight|meituan|deepseek)[_-]?(?:key|token|secret)\b\s*[:=＝]", re.I)),
    ("credential", re.compile(r"\b[A-Fa-f0-9]{32,}\b")),
    ("pii_id_card", re.compile(r"\b\d{17}[\dXx]\b")),
    ("pii_phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("pii_bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
)

#: Internal names that must never surface: they reveal strategy and plumbing.
_INTERNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:capability_id|idem_key|plan_hash|consent_ref|confirm_token|scope_strike)\b", re.I),
    re.compile(r"\b(?:amap-mcp-server|amap_mcp_server|variflight-mcp|tripmatch-mcp|12306-mcp|huashu-chrome)\b", re.I),
    re.compile(r"\bapp\.(?:policy|channels|graph|store)\.[a-z_]+", re.I),
    re.compile(r"\b(?:arbiter|ActionLedger|PlanVersion|AgentState|LangGraph)\b"),
)

#: Injection markers echoed back by a jailbroken model.
_ECHO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(ignore|disregard)\s+(all\s+)?(previous|prior)\s+(instruction|prompt)", re.I),
    re.compile(r"(忽略|无视)(上面|之前|以上)的?(所有)?(指令|提示)"),
    re.compile(r"(system\s*prompt|系统提示词)\s*[:：]"),
)


class GuardResult(BaseModel):
    ok: bool
    reason: str = ""
    replacement: str | None = None


def check(text: str) -> GuardResult:
    """Validate one candidate visible message.

    Returns ``GuardResult(ok=True)`` when the text may be shown verbatim. When
    it may not, ``replacement`` carries the deterministic fallback text and
    ``reason`` names the rule that fired (for the local trace, not the user).
    """

    if text is None:
        return GuardResult(ok=False, reason="empty", replacement=fallback_text("empty"))
    stripped = text.strip()
    if not stripped:
        return GuardResult(ok=False, reason="empty", replacement=fallback_text("empty"))

    for reason, pattern in _SECRET_PATTERNS:
        if pattern.search(stripped):
            return GuardResult(ok=False, reason=reason, replacement=fallback_text(reason))

    for pattern in _INTERNAL_PATTERNS:
        if pattern.search(stripped):
            return GuardResult(ok=False, reason="internal_detail", replacement=fallback_text("internal_detail"))

    for pattern in _ECHO_PATTERNS:
        if pattern.search(stripped):
            return GuardResult(ok=False, reason="injection_echo", replacement=fallback_text("injection_echo"))

    if len(stripped) > MAX_VISIBLE_CHARS:
        return GuardResult(
            ok=False,
            reason="too_long",
            replacement=stripped[:MAX_VISIBLE_CHARS] + "\n…（内容过长，已截断）",
        )

    return GuardResult(ok=True)


def fallback_text(reason: str) -> str:
    """Deterministic replacement copy, keyed by the guard rule that fired."""

    mapping = {
        "credential": "这条回复里可能含敏感凭证，我已经拦下了。请重新问一次，或者换个说法。",
        "pii_id_card": "出于隐私考虑，我不会复述证件号一类的实名信息。票务需要的信息只在内存里用于生成购票清单。",
        "pii_phone": "出于隐私考虑，我不会复述手机号。需要联系信息时我会提示你手工填写。",
        "pii_bank_card": "出于隐私考虑，我不会复述银行卡号。付款永远在官方渠道由你本人完成。",
        "internal_detail": "这条回复涉及系统内部细节，已改为对外说法。你可以继续问行程本身。",
        "injection_echo": "我不会执行或复述这类指令。需要规划行程的话，告诉我起点、目的地和日期。",
        "too_long": "内容太长，我已经截断。要不要我按分段发给你？",
        "empty": "我这边没生成出有效内容，请再问一次。",
    }
    return mapping.get(reason, "这条回复没通过输出校验，我换一种说法再来一次。")


def guard_or_fallback(text: str) -> str:
    """Convenience wrapper: always return something showable."""

    result = check(text)
    if result.ok:
        return text
    return result.replacement or fallback_text(result.reason)
