"""LLM budget guard (DN-1..DN-6).

Three gates, not one: a per-session cap, a daily cap and a monthly cap. A
monthly cap alone has an obvious hole -- one runaway loop can burn the whole
month in a day.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from app.config.settings import Settings
from app.llm.types import LLMUsage


class BudgetLevel(StrEnum):
    OK = "ok"
    WARN = "warn"
    DEGRADE = "degrade"
    EXHAUSTED = "exhausted"


class BudgetStatus(BaseModel):
    level: BudgetLevel = BudgetLevel.OK
    monthly_used: float = 0.0
    monthly_limit: float = 0.0
    daily_used: float = 0.0
    daily_limit: float = 0.0
    session_used: float = 0.0
    session_limit: float = 0.0
    reason: str = ""


def standard_equivalent(usage: LLMUsage, settings: Settings) -> float:
    """Convert a mixed usage record into "standard tokens" (DN-4)."""

    return (
        usage.input_tokens * settings.llm_weight_input
        + usage.output_tokens * settings.llm_weight_output
        + usage.cached_input_tokens * settings.llm_weight_cached_input
    )


class BudgetGuard:
    def __init__(self, settings: Settings, repo: object | None = None) -> None:
        self._settings = settings
        self._repo = repo

    def evaluate(self, *, monthly_used: float, daily_used: float, session_used: float) -> BudgetStatus:
        s = self._settings
        status = BudgetStatus(
            monthly_used=monthly_used,
            monthly_limit=float(s.llm_monthly_token_budget),
            daily_used=daily_used,
            daily_limit=float(s.llm_daily_token_budget),
            session_used=session_used,
            session_limit=float(s.llm_session_token_cap),
        )
        if session_used >= s.llm_session_token_cap:
            status.level = BudgetLevel.EXHAUSTED
            status.reason = "单会话 token 上限已触发"
            return status
        if daily_used >= s.llm_daily_token_budget:
            status.level = BudgetLevel.EXHAUSTED
            status.reason = "当日 token 预算已用尽"
            return status
        if monthly_used >= s.llm_monthly_token_budget:
            status.level = BudgetLevel.EXHAUSTED
            status.reason = "当月 token 预算已用尽"
            return status
        ratio = max(
            monthly_used / max(1.0, float(s.llm_monthly_token_budget)),
            daily_used / max(1.0, float(s.llm_daily_token_budget)),
        )
        if ratio >= s.llm_degrade_ratio:
            status.level = BudgetLevel.DEGRADE
            status.reason = "接近预算上限，切换到精简模式（跳过 LLM 渲染节点）"
        elif ratio >= s.llm_warn_ratio:
            status.level = BudgetLevel.WARN
            status.reason = "预算使用已超过 80%"
        return status
