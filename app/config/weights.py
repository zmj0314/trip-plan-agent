"""Default preference weights (framework §6.1, figure "偏好分两类").

Two kinds of preference share one vocabulary but behave differently:

* **hard constraints** (无障碍, 必须当天到) are validated by ``policy.constraints``
  and never expressed as weights -- a weight can be outvoted, a requirement
  cannot.
* **soft preferences** (时间/费用/换乘/步行/准点/舒适) are weights, and the user
  can change them mid-conversation without re-planning from scratch.

These are the conservative defaults used when the user never states a
preference (``DC-2``: "you decide" resolves the slot with an explicit default
rather than another round of questions).
"""

from __future__ import annotations

from typing import Final

DEFAULT_WEIGHTS: Final[dict[str, float]] = {
    "f_time": 0.30,
    "f_cost": 0.20,
    "f_transfer": 0.20,
    "f_walk": 0.10,
    "f_punctual": 0.10,
    "f_comfort": 0.05,
    "f_access": 0.05,
    "f_risk": 0.00,
}

#: Named profiles the clarification loop can offer as one-tap answers.
WEIGHT_PROFILES: Final[dict[str, dict[str, float]]] = {
    "fastest": {**DEFAULT_WEIGHTS, "f_time": 0.50, "f_cost": 0.05},
    "cheapest": {**DEFAULT_WEIGHTS, "f_cost": 0.50, "f_time": 0.10},
    "fewest_changes": {**DEFAULT_WEIGHTS, "f_transfer": 0.50, "f_time": 0.15},
    "least_walking": {**DEFAULT_WEIGHTS, "f_walk": 0.45, "f_access": 0.20},
}


def default_weights() -> dict[str, float]:
    return dict(DEFAULT_WEIGHTS)


def weights_for(preferences: dict[str, object] | None) -> dict[str, float]:
    """Resolve explicit weights, else a named profile, else the defaults."""

    prefs = preferences or {}
    explicit = prefs.get("weights")
    if isinstance(explicit, dict) and explicit:
        return {str(k): float(v) for k, v in explicit.items()}  # type: ignore[arg-type]
    profile = prefs.get("profile")
    if isinstance(profile, str) and profile in WEIGHT_PROFILES:
        return dict(WEIGHT_PROFILES[profile])
    return default_weights()


__all__ = ["DEFAULT_WEIGHTS", "WEIGHT_PROFILES", "default_weights", "weights_for"]
