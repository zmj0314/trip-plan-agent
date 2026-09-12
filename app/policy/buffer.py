"""Dynamic transfer buffers (framework §6.5).

A connection is valid only when ``leg[i].arrive + buffer <= leg[i+1].depart``,
and the buffer is not one constant: it models how much the *previous* leg can
slip before the next one is missed. A flight gets two hours; a same-city metro
hop gets forty-five minutes.

Degraded provenance widens every buffer by 1.5x: a cached or estimated
schedule is itself less trustworthy, so the safety margin has to grow.

Pure computation -- no IO, no clock, no LLM.
"""

from __future__ import annotations

import math

from app.capabilities.contract import DegradationLevel

#: Base buffer in minutes, keyed by the previous leg's mode (framework §6.5).
BUFFER_TABLE_MINUTES: dict[str, int] = {
    "flight": 120,  # 航班
    "high_speed": 60,  # 高铁
    "train": 60,  # 火车
    "local_transfer": 45,  # 同城中转
    "cross_station": 90,  # 跨站换乘
}

#: Uncertainty multiplier by degradation level (framework §6.5).
UNCERTAINTY_BUFFER_MULTIPLIER: dict[int, float] = {0: 1.0, 1: 1.5, 2: 1.5, 3: 1.5}

#: Used when the caller names a mode the table does not know.
DEFAULT_BUFFER_MINUTES: int = BUFFER_TABLE_MINUTES["local_transfer"]

#: ``TransportMode`` values folded onto the table's five buckets. Whether a
#: rail leg is "high_speed" or "train" is a property of the service, not of the
#: enum, so both land on the same 60-minute base.
_MODE_ALIASES: dict[str, str] = {
    "flight": "flight",
    "plane": "flight",
    "air": "flight",
    "航班": "flight",
    "飞机": "flight",
    "high_speed": "high_speed",
    "highspeed": "high_speed",
    "gaotie": "high_speed",
    "hsr": "high_speed",
    "高铁": "high_speed",
    "动车": "high_speed",
    "train": "train",
    "rail": "train",
    "railway": "train",
    "列车": "train",
    "火车": "train",
    "local_transfer": "local_transfer",
    "transfer": "local_transfer",
    "metro": "local_transfer",
    "subway": "local_transfer",
    "bus": "local_transfer",
    "taxi": "local_transfer",
    "drive": "local_transfer",
    "car": "local_transfer",
    "bike": "local_transfer",
    "walk": "local_transfer",
    "同城中转": "local_transfer",
    "cross_station": "cross_station",
    "crossstation": "cross_station",
    "station_change": "cross_station",
    "跨站换乘": "cross_station",
}


def _bucket(prev_mode: str) -> str:
    key = str(prev_mode or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _MODE_ALIASES.get(key, key)


def base_buffer_minutes(*, prev_mode: str, cross_station: bool) -> int:
    """Unscaled buffer for one connection, before the uncertainty multiplier."""

    base = BUFFER_TABLE_MINUTES.get(_bucket(prev_mode), DEFAULT_BUFFER_MINUTES)
    if cross_station:
        # Moving between stations *is* the worst case, whatever arrived before.
        base = max(base, BUFFER_TABLE_MINUTES["cross_station"])
    return base


def required_buffer_minutes(
    *,
    prev_mode: str,
    cross_station: bool,
    uncertainty: DegradationLevel,
) -> int:
    """Minimum minutes between the previous arrival and the next departure.

    Rounded up: a buffer is a safety margin, and truncating it would trade
    safety for a connection that does not actually exist.
    """

    base = base_buffer_minutes(prev_mode=prev_mode, cross_station=cross_station)
    multiplier = UNCERTAINTY_BUFFER_MULTIPLIER.get(int(uncertainty), 1.5)
    return int(math.ceil(base * multiplier))
