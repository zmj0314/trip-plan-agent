"""Reason traces (framework §6.4).

The model is allowed to explain a recommendation, but only from a structure
the code built. That is what turns "must cite evidence" from a prompt request
into a guarantee the model cannot talk its way out of.

Three hard rules from §6.4 are enforced here:

1. only the ``|contribution|`` top-k entries are citable;
2. a trace is only *explainable* when it carries at least one
   ``preference``/``weather`` entry with a non-zero contribution;
3. when everything citable is a ``constraint``, the rendering must say so
   instead of dressing "was not eliminated" up as "was chosen".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.config.weights import DEFAULT_WEIGHTS
from app.policy.scoring import ScoreBreakdown
from app.policy.weather_rules import WeatherAssessment


class ContributorKind(StrEnum):
    PREFERENCE = "preference"
    WEATHER = "weather"
    CONSTRAINT = "constraint"
    UNCERTAINTY = "uncertainty"


class Contributor(BaseModel):
    kind: ContributorKind
    key: str
    weight: float = 0.0
    feature: str | None = None
    contribution: float = 0.0
    evidence: dict[str, Any] = Field(default_factory=dict)


class ReasonTrace(BaseModel):
    candidate_id: str
    score_total: float
    contributors: list[Contributor] = Field(default_factory=list)
    vetoes: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    degradation: list[str] = Field(default_factory=list)


def build(
    candidate_id: str,
    breakdown: ScoreBreakdown,
    weather: WeatherAssessment | None,
    *,
    evidence: Mapping[str, dict[str, Any]],
    assumptions: Iterable[str] = (),
    degradation: Iterable[str] = (),
) -> ReasonTrace:
    """Assemble the citable structure behind one candidate's score."""

    sources = evidence or {}
    contributors: list[Contributor] = []

    for key, contribution in breakdown.contributions.items():
        contributors.append(
            Contributor(
                kind=ContributorKind.PREFERENCE,
                key=key,
                # The nominal default weight is recorded for display; the
                # authoritative number is ``contribution``, which already
                # reflects any renormalisation.
                weight=float(DEFAULT_WEIGHTS.get(key, 0.0)),
                feature=key,
                contribution=float(contribution),
                evidence=dict(sources.get(key) or {}),
            )
        )

    for factor in weather.factors if weather is not None else ():
        kind = ContributorKind.CONSTRAINT if factor.hard_veto else ContributorKind.WEATHER
        contributors.append(
            Contributor(
                kind=kind,
                key=f"weather.{factor.factor.value}",
                weight=1.0,
                feature=None,
                contribution=float(factor.severity),
                evidence={"note": factor.note, "hard_veto": factor.hard_veto},
            )
        )

    if breakdown.uncertainty_penalty:
        contributors.append(
            Contributor(
                kind=ContributorKind.UNCERTAINTY,
                key="uncertainty",
                weight=1.0,
                feature=None,
                contribution=float(breakdown.uncertainty_penalty),
                evidence={"missing": list(breakdown.missing)},
            )
        )

    return ReasonTrace(
        candidate_id=candidate_id,
        score_total=float(breakdown.total),
        contributors=contributors,
        vetoes=list(weather.vetoes) if weather is not None else [],
        assumptions=list(assumptions),
        degradation=list(degradation),
    )


def citable(trace: ReasonTrace, k: int = 3) -> list[Contributor]:
    """The only entries a rendered reason may cite, biggest first."""

    ranked = sorted(trace.contributors, key=lambda entry: (-abs(entry.contribution), entry.key))
    return ranked[: max(0, k)]


def is_explainable(trace: ReasonTrace) -> bool:
    """§6.4 rule 2: at least one non-zero preference or weather entry."""

    return any(
        entry.kind in {ContributorKind.PREFERENCE, ContributorKind.WEATHER}
        and entry.contribution != 0.0
        for entry in trace.contributors
    )


def _evidence_text(evidence: Mapping[str, Any]) -> str:
    if not evidence:
        return ""
    pairs = ", ".join(f"{key}={value}" for key, value in sorted(evidence.items()))
    return f"（依据：{pairs}）"


_KIND_LABELS: dict[ContributorKind, str] = {
    ContributorKind.PREFERENCE: "偏好",
    ContributorKind.WEATHER: "天气",
    ContributorKind.CONSTRAINT: "约束",
    ContributorKind.UNCERTAINTY: "不确定度",
}


def render_hint(trace: ReasonTrace, k: int = 3) -> str:
    """Structured hint handed to the LLM when it writes the visible reason.

    This text is an *input* to rendering, not something a user sees, so it may
    name internal keys -- the output guard is what keeps those out of the
    reply.
    """

    top = citable(trace, k)
    lines = [f"候选 {trace.candidate_id} 得分 {trace.score_total:.3f}，可指认依据 top-{len(top)}："]
    for index, entry in enumerate(top, start=1):
        lines.append(
            f"{index}. [{_KIND_LABELS[entry.kind]}] {entry.key} "
            f"权重 {entry.weight:.2f}，贡献 {entry.contribution:+.3f}"
            f"{_evidence_text(entry.evidence)}"
        )
    if trace.vetoes:
        lines.append("否决项：" + "；".join(trace.vetoes))
    if trace.assumptions:
        lines.append("假设：" + "；".join(trace.assumptions))
    if trace.degradation:
        lines.append("降级：" + "；".join(trace.degradation))
    if not is_explainable(trace):
        lines.append("注意：缺少可指认的偏好或天气依据，只能说明“未被淘汰”，不得渲染为主动选择。")
    return "\n".join(lines)
