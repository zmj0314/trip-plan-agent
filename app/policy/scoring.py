"""Preference scoring (framework §6.3, DF-3).

The framework fixes the formula, so this module implements it literally::

    Score(c) = Σ_i w_i × (1 − f_i(c))   # 偏好软加权
             − Σ_j penalty_j(c)         # 天气软惩罚
             − λ × u(c)                 # 数据不确定度惩罚

Higher is better. Features are *badness* values in [0, 1] -- 0 means ideal --
so ``1 − f`` turns them into a contribution.

Missing dimensions are neither zero (which would look optimal) nor a neutral
filler (which would hide the gap): the feature is dropped from the weighted
sum, the remaining weights are renormalised, and the uncertainty term is
lifted to at least the D2 level.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, Field

from app.capabilities.contract import DegradationLevel

#: The eight preference features, in canonical output order (§6.1).
FEATURE_KEYS: tuple[str, ...] = (
    "f_time",
    "f_cost",
    "f_transfer",
    "f_walk",
    "f_punctual",
    "f_comfort",
    "f_access",
    "f_risk",
)

#: Framework default (λ). Penalties are already on the same scale as features.
DEFAULT_LAMBDA = 0.15

#: A candidate scored without some of its features is at best as uncertain as
#: one built from non-live data, so u(c) is floored at the D2 value (0.30).
MISSING_FEATURE_UNCERTAINTY = DegradationLevel.D2.uncertainty


class ScoreBreakdown(BaseModel):
    total: float
    contributions: dict[str, float] = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)
    uncertainty_penalty: float = 0.0


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _as_number(value: object) -> float | None:
    """Parse a number, rejecting booleans and non-finite values."""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _coerce(value: object) -> float | None:
    """Parse a *feature* value and clamp it into [0, 1]."""

    number = _as_number(value)
    return None if number is None else _clamp01(number)


def normalize_features(raw: Mapping[str, float | None]) -> dict[str, float | None]:
    """Return every known feature in canonical order, clamped into [0, 1].

    Unknown keys are dropped rather than carried along, and unparseable values
    become ``None`` -- which the caller sees as "missing", not as zero.
    """

    source = raw or {}
    return {key: _coerce(source.get(key)) for key in FEATURE_KEYS}


def _weight_map(weights: Mapping[str, float]) -> dict[str, float]:
    result: dict[str, float] = {}
    for key in FEATURE_KEYS:
        # Weights are relative, so they are deliberately not clamped: only the
        # ratio between them matters once the sum is renormalised.
        value = _as_number((weights or {}).get(key))
        if value:
            result[key] = value
    return result


def score(
    *,
    features: Mapping[str, float | None],
    weights: Mapping[str, float],
    penalties: Sequence[float],
    uncertainty: DegradationLevel,
    lam: float = DEFAULT_LAMBDA,
) -> ScoreBreakdown:
    """Score one candidate. Higher is better; the caller ranks descending."""

    normalised = normalize_features(features)
    weighted = _weight_map(weights)
    level = DegradationLevel(int(uncertainty))

    available = {
        key: value
        for key, value in normalised.items()
        if key in weighted and value is not None
    }
    missing = [key for key in FEATURE_KEYS if key in weighted and normalised.get(key) is None]

    weight_total = sum(weighted[key] for key in available)
    contributions: dict[str, float] = {}
    if weight_total > 0:
        for key, value in available.items():
            contributions[key] = round((weighted[key] / weight_total) * (1.0 - value), 6)

    penalty_total = sum(_as_number(penalty) or 0.0 for penalty in penalties or ())
    u = max(level.uncertainty, MISSING_FEATURE_UNCERTAINTY) if missing else level.uncertainty
    uncertainty_penalty = round(lam * u, 6)

    total = sum(contributions.values()) - penalty_total - uncertainty_penalty
    return ScoreBreakdown(
        total=round(total, 6),
        contributions=contributions,
        missing=missing,
        uncertainty_penalty=uncertainty_penalty,
    )
