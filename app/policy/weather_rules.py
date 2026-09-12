"""Weather risk factors (framework §6.2, DF-2).

The framework's central warning is that weather has **two paths**, and they
must not collapse into one:

* soft penalties tune the ranking -- "300 yuan cheaper" is allowed to win;
* hard vetoes are the safety valve -- a thunderstorm must never lose a price
  comparison.

So :func:`assess` returns both lists and never folds a veto into a penalty.

Pure: it reads a plain mapping produced by the ``weather.forecast`` capability
and makes no calls of its own.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

#: 8 级风（约 62 km/h）起，索道/轮渡/航班按硬否决处理。
_WIND_VETO_KPH = 62.0
#: 大到暴雨（约 50 mm/24h）起，自驾与户外活动升级为硬否决。
_HEAVY_RAIN_SEVERITY = 0.85
#: 航班起降的能见度底线。
_FLIGHT_VISIBILITY_KM = 1.0
#: 高温下户外活动的硬否决线。
_HEAT_VETO_C = 38.0

_FLIGHT_MODES = frozenset({"flight", "plane", "air", "航班", "飞机"})
_CABLE_MODES = frozenset({"cable", "cableway", "ropeway", "ferry", "索道", "缆车", "轮渡", "船"})
_DRIVE_MODES = frozenset({"drive", "car", "taxi", "自驾", "打车", "租车", "网约车"})
_OUTDOOR_MODES = frozenset({"walk", "bike", "hike", "outdoor", "步行", "骑行", "徒步", "爬山", "户外"})

_FALSE_TEXT = frozenset({"", "0", "no", "false", "none", "null", "无", "否"})

#: Longer keys first: "大暴雨" must win over "暴雨".
_PRECIP_TEXT: dict[str, float] = {
    "特大暴雨": 1.0,
    "大暴雨": 1.0,
    "暴雨": 0.9,
    "大雨": 0.7,
    "中雨": 0.45,
    "小雨": 0.2,
    "暴雪": 1.0,
    "大雪": 0.7,
    "中雪": 0.4,
    "小雪": 0.2,
    "storm": 1.0,
    "heavy rain": 0.8,
    "moderate rain": 0.45,
    "light rain": 0.2,
    "snow": 0.5,
}


class WeatherFactor(StrEnum):
    PRECIPITATION = "precipitation"
    WIND = "wind"
    THUNDERSTORM = "thunderstorm"
    VISIBILITY = "visibility"
    TEMPERATURE = "temperature"
    AQI = "aqi"
    ICE = "ice"


class FactorAssessment(BaseModel):
    factor: WeatherFactor
    severity: float = 0.0
    hard_veto: bool = False
    note: str = ""


class WeatherAssessment(BaseModel):
    factors: list[FactorAssessment] = Field(default_factory=list)
    vetoes: list[str] = Field(default_factory=list)
    penalties: list[float] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_TEXT
    return bool(value)


def _first(weather: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in weather and weather[name] is not None:
            return weather[name]
    return None


def _normalise_precipitation(value: Any) -> float | None:
    """Severity in [0, 1] from either millimetres or a textual grade."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        for name, severity in _PRECIP_TEXT.items():
            if name in text:
                return severity
        return None
    millimetres = _as_float(value)
    if millimetres is None:
        return None
    return _clamp01(millimetres / 50.0)


def _wind_kph(weather: Mapping[str, Any]) -> float | None:
    """Wind speed in km/h, accepting either unit.

    Values below 40 are read as m/s: a forecast above 40 m/s is hurricane
    force and never reaches this mapping, while 40 km/h is an ordinary windy
    day, so the split is safe in practice.
    """

    explicit = _as_float(
        _first(weather, "wind_kph", "wind_speed_kph", "wind_kmh", "wind_speed_kmh")
    )
    if explicit is not None:
        return explicit
    raw = _as_float(_first(weather, "wind_ms", "wind_speed_ms", "wind_speed", "wind", "风速"))
    if raw is None:
        return None
    return raw * 3.6 if raw < 40 else raw


def _mode_set(modes: Sequence[str]) -> set[str]:
    return {str(mode).strip().lower() for mode in (modes or ())}


def assess(weather: Mapping[str, Any], *, modes: Sequence[str]) -> WeatherAssessment:
    """Turn one weather snapshot into factors, soft penalties and vetoes."""

    if not weather:
        return WeatherAssessment()

    mode_set = _mode_set(modes)
    factors: list[FactorAssessment] = []
    vetoes: list[str] = []
    penalties: list[float] = []
    notes: list[str] = []

    def record(
        factor: WeatherFactor,
        severity: float,
        note: str,
        *,
        hard_veto: bool = False,
        veto_reason: str = "",
        relevance: float = 1.0,
    ) -> None:
        """Record one factor.

        ``relevance`` scales how much this factor matters *for the modes on this
        candidate*. It is applied to the severity itself, not to the penalty,
        because the severity is what the reason chain publishes: a factor that
        did not affect the score must not appear in the explanation as though it
        did. That was the bug behind a 34 km/h breeze outranking nine minutes of
        driving on a self-drive route.
        """

        rounded = round(_clamp01(_clamp01(severity) * _clamp01(relevance)), 3)
        factors.append(
            FactorAssessment(
                factor=factor, severity=rounded, hard_veto=hard_veto, note=note
            )
        )
        if hard_veto:
            vetoes.append(f"{factor.value}: {veto_reason or note}")
        elif rounded > 0:
            penalties.append(rounded)

    precipitation = _first(
        weather,
        "precip_mm",
        "precipitation_mm",
        "precipitation",
        "rain_mm",
        "rainfall_mm",
        "降水",
    )
    rain_severity = _normalise_precipitation(precipitation)
    if rain_severity is not None:
        relevant = bool(mode_set & (_DRIVE_MODES | _OUTDOOR_MODES))
        record(
            WeatherFactor.PRECIPITATION,
            rain_severity,
            f"降水强度 {rain_severity:.2f}",
            hard_veto=rain_severity >= _HEAVY_RAIN_SEVERITY and relevant,
            veto_reason="大到暴雨，自驾与户外活动不安全",
        )

    wind_kph = _wind_kph(weather)
    if wind_kph is not None:
        relevant = bool(mode_set & (_FLIGHT_MODES | _CABLE_MODES))
        # Wind is a hazard for a cable car, a discomfort when walking, and
        # barely a footnote when driving. Scoring it identically for all three
        # is what let it silently dominate route choice.
        if relevant:
            wind_relevance = 1.0
        elif mode_set & _OUTDOOR_MODES:
            wind_relevance = 0.5
        else:
            wind_relevance = 0.1
        record(
            WeatherFactor.WIND,
            wind_kph / 100.0,
            f"风速约 {wind_kph:.0f} km/h",
            hard_veto=wind_kph >= _WIND_VETO_KPH and relevant,
            veto_reason="大风，航班/索道/轮渡有停运风险",
            relevance=wind_relevance,
        )

    if _truthy(_first(weather, "thunderstorm", "thunder", "convective_index", "雷暴")):
        # 无 mode 信息时按最保守处理：无法证明安全即视为不安全。
        relevant = bool(mode_set & (_OUTDOOR_MODES | _FLIGHT_MODES)) or not mode_set
        record(
            WeatherFactor.THUNDERSTORM,
            1.0,
            "雷暴天气",
            hard_veto=relevant,
            veto_reason="雷暴，山区/户外活动与航班不安全",
        )

    visibility = _as_float(_first(weather, "visibility_km", "visibility", "能见度"))
    if visibility is not None:
        record(
            WeatherFactor.VISIBILITY,
            (10.0 - visibility) / 10.0,
            f"能见度约 {visibility:.1f} km",
            hard_veto=visibility < _FLIGHT_VISIBILITY_KM and bool(mode_set & _FLIGHT_MODES),
            veto_reason="能见度低于航班起降标准",
        )

    temperature = _as_float(
        _first(
            weather,
            "temperature_c",
            "temp_c",
            "temperature",
            "temp",
            "temperature_max_c",
            "温度",
        )
    )
    if temperature is not None:
        severity = max(0.0, temperature - 32.0) / 10.0 + max(0.0, 0.0 - temperature) / 20.0
        record(
            WeatherFactor.TEMPERATURE,
            severity,
            f"气温约 {temperature:.0f}℃",
            hard_veto=temperature >= _HEAT_VETO_C and bool(mode_set & _OUTDOOR_MODES),
            veto_reason="高温，户外活动有中暑风险",
        )
        if temperature >= 35.0:
            notes.append("建议避开正午出发时段，并准备防晒与补水")
        elif temperature <= 0.0:
            notes.append("建议携带保暖装备，出发时段尽量选在白天")

    aqi = _as_float(_first(weather, "aqi", "air_quality_index", "空气质量"))
    if aqi is not None:
        record(WeatherFactor.AQI, (aqi - 100.0) / 200.0, f"AQI 约 {aqi:.0f}")
        if aqi >= 150.0:
            notes.append("空气质量较差，建议降低户外强度或改选室内景点")

    icing = _truthy(_first(weather, "ice", "icing", "road_ice", "black_ice", "结冰"))
    ice_note = "路面结冰"
    if not icing and temperature is not None and temperature <= 0.0 and rain_severity is not None:
        icing = True
        ice_note = "气温低于 0℃ 且有降水，路面可能结冰"
    if icing:
        record(
            WeatherFactor.ICE,
            1.0,
            ice_note,
            hard_veto=bool(mode_set & (_DRIVE_MODES | _OUTDOOR_MODES)),
            veto_reason="结冰，自驾与步行不安全",
        )

    return WeatherAssessment(factors=factors, vetoes=vetoes, penalties=penalties, notes=notes)
