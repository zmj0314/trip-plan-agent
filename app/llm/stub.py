"""Deterministic offline LLM.

Lets the full graph run with no key and no network: tests, CI and the M0
"clarification -> gate -> execution" acceptance run all use it. It is a
*plumbing* stub, not a language model.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from pydantic import BaseModel

from app.domain.models import ScopeVerdict

#: Year used when the user writes "9月15日" without one. An offline stub cannot
#: resolve a date against the world clock the way the graph does; this only has
#: to be internally consistent for the no-key path.
CURRENT_YEAR = date.today().year
from app.llm.schemas import (
    ClarifyOutput,
    IntakeOutput,
    PreviewOutput,
    ReasonOutput,
    RemindOutput,
)
from app.llm.types import LLMResponse, LLMUsage
from app.llm.telemetry import TELEMETRY

_INJECTION_MARKERS = (
    "忽略之前",
    "忽略以上",
    "ignore previous",
    "ignore all previous",
    "你现在是",
    "system prompt",
    "系统提示词",
    "越狱",
)

_TRAVEL_HINTS = (
    "去", "到", "出行", "旅行", "旅游", "行程", "车", "票", "机票", "高铁", "火车",
    "酒店", "住宿", "景点", "门票", "天气", "路线", "机场", "车站", "长城", "玩",
)

#: Destinations the offline stub recognises. The real extractor is the model;
#: this list only keeps the no-key path usable for the common demo cases.
_KNOWN_DESTINATIONS = (
    "八达岭", "慕田峪", "司马台", "长城", "香山", "环球影城", "故宫", "颐和园",
    "成都", "上海", "杭州", "西安", "重庆", "厦门", "青岛", "南京", "广州", "深圳",
    "北京", "天津", "苏州", "承德", "武汉", "长沙", "昆明", "丽江", "大理", "三亚",
)

_KNOWN_ORIGINS = (
    "望京SOHO", "望京soho", "望京", "北京", "上海", "杭州", "成都", "广州", "深圳",
    "国贸", "中关村", "三里屯", "十里堡",
)

_OUT_OF_SCOPE_HINTS = (
    "写代码", "python", "脚本", "作文", "文案", "翻译", "诊断", "股票", "基金",
    "法律", "起诉", "政治", "作业",
)

#: Dates in the shapes people write: 2026-09-12 / 2026/9/12 / 2026 9 12 / 9月12日.
_ISO_DATE_RE = re.compile(r"(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})")
_MD_DATE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")

_RETURN_MARKERS = ("回", "返", "回程", "返程", "回来", "回到")

#: A city plus a stay length: "北京3天", "天津玩两天". Used to keep the multi-city
#: shape of a request instead of collapsing it to one city name.
_CITY_DAYS_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,6}?)\s*(?:玩|待|呆|住|停留)?\s*"
    r"(\d{1,2}|[零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾]{1,3})\s*[天日]"
)
_CITY_PREFIX_FILLER = ("从", "去", "到", "在", "然后去", "然后", "再去", "最后回", "最后", "回")


class OfflineStubLLM:
    """Rule-based stand-in with the same interface as the real client."""

    model = "offline-stub"

    async def structured(
        self,
        *,
        node: str,
        schema: type[BaseModel],
        prompt: str,
        user_input: str = "",
        context: Any = None,
        **_ignored: Any,
    ) -> LLMResponse:
        usage = LLMUsage(
            model=self.model,
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=16,
        )
        parsed = self._build(node, schema, user_input)
        TELEMETRY.record(
            node=node,
            client=self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
        return LLMResponse(parsed=parsed, raw_text=parsed.model_dump_json(), usage=usage)

    def _build(self, node: str, schema: type[BaseModel], text: str) -> BaseModel:
        lowered = text.lower()
        if node == "intake":
            if any(m in text or m in lowered for m in _INJECTION_MARKERS):
                return IntakeOutput(scope=ScopeVerdict.ABUSE, scope_reason="injection marker", is_injection=True)
            if any(m in text or m in lowered for m in _OUT_OF_SCOPE_HINTS) and not any(
                m in text for m in _TRAVEL_HINTS
            ):
                return IntakeOutput(scope=ScopeVerdict.OUT_OF_SCOPE, scope_reason="out of travel domain")
            if any(m in text for m in _TRAVEL_HINTS):
                return IntakeOutput(
                    scope=ScopeVerdict.IN_SCOPE,
                    slot_patch=_extract_slots(text),
                    intent="advice_only",
                )
            # Still extract: an answer to a clarifying question ("从望京SOHO出发，
            # 一个人") often carries no domain keyword at all, and returning an
            # empty patch here would silently replace what the user just said
            # with conservative defaults.
            patch = _extract_slots(text)
            return IntakeOutput(
                scope=ScopeVerdict.IN_SCOPE if patch else ScopeVerdict.AMBIGUOUS,
                scope_reason="no clear signal" if not patch else "",
                slot_patch=patch,
                intent="advice_only" if patch else None,
            )
        if node == "clarify":
            return ClarifyOutput(questions=["请补充你的出发地和目的地，以及计划出发的日期。"])
        if node == "preview_render":
            return PreviewOutput(text="已根据你的需求生成行程方案，请确认后执行。")
        if node == "reason":
            return ReasonOutput(text="该方案在时间与费用之间取得了平衡。")
        if node == "remind":
            return RemindOutput(items=["出发前一天再次确认车次余票。"])
        return schema()  # type: ignore[call-arg]


def _extract_slots(text: str) -> dict[str, Any]:
    """Very small extractor: enough to drive the M0 happy path.

    It emits **canonical registry ids** (``origin_address`` / ``destination`` /
    ``travelers`` / ``depart_date`` / ``return_date``), so the deterministic
    information gate can actually see the values it extracted. The real
    extractor is the model; this list only keeps the no-key path usable.
    """

    patch: dict[str, Any] = {}
    for marker in sorted(_KNOWN_ORIGINS, key=len, reverse=True):
        if marker in text:
            patch["origin_address"] = marker
            break
    # Prefer the most specific destination match: "八达岭长城" means 八达岭, not
    # 长城. The origin's own city is skipped -- "从北京出发，北京3天+天津2天" must
    # not read the origin as the destination.
    origin = str(patch.get("origin_address") or "")
    segments: list[dict[str, Any]] = []
    for match in _CITY_DAYS_RE.finditer(text):
        city = match.group(1).strip("，,、。 的和与及")
        for filler in _CITY_PREFIX_FILLER:
            if city.startswith(filler) and len(city) > len(filler):
                city = city[len(filler) :]
        city = city.strip("，,、。 的和与及")
        if city and not (origin and city in origin):
            segments.append({"city": city, "days": _days(match.group(2))})
    if segments:
        patch["destinations"] = segments
        patch.setdefault("destination", segments[0]["city"])
    else:
        for marker in sorted(_KNOWN_DESTINATIONS, key=len, reverse=True):
            if marker in text and not (origin and marker in origin and f"去{marker}" not in text):
                patch["destination"] = marker
                break

    dates = _extract_dates(text)
    if dates:
        patch["depart_date"] = dates[0]
        if len(dates) > 1:
            patch["return_date"] = dates[1]

    for marker, count in (("一个人", 1), ("俩", 2), ("两个人", 2), ("三个人", 3), ("四个人", 4)):
        if marker in text:
            patch["travelers"] = {"count": count, "types": ["adult"]}
            break
    else:
        for token in text.replace("，", " ").replace(",", " ").split():
            if token.endswith("人") and token[:-1].isdigit():
                patch["travelers"] = {"count": int(token[:-1]), "types": ["adult"]}
                break
    if "只要建议" in text or "只要方案" in text:
        patch["intent"] = "advice_only"

    # Lodging (D1) is a required slot, so the offline path has to answer it too:
    # a self-arranged stay is an answer, not a missing value.
    if any(marker in text for marker in ("我自己订", "自己订", "自订", "不用订", "不需要住宿", "我订")):
        patch["lodging"] = "none"
    elif any(marker in text for marker in ("住宿", "酒店", "住哪", "订房", "住一晚", "过夜")):
        patch["lodging"] = "need"
    return patch


def _days(token: str) -> int | None:
    """``"3"`` -> 3, ``"两"`` -> 2. Reuses the policy table so the two never drift."""

    from app.policy.segments import cn_to_int

    return cn_to_int(token)


def _extract_dates(text: str) -> list[str]:
    """Ordered ``[depart, return]`` as ``YYYY-MM-DD``.

    A second date is only read as the return date when the wording says so
    ("...，9月15日回"). Two bare dates would otherwise turn any mention of a
    date into a trip length.
    """

    found: list[tuple[int, str, bool]] = []
    for match in _ISO_DATE_RE.finditer(text):
        year, month, day = match.groups()
        found.append((match.start(), f"{year}-{int(month):02d}-{int(day):02d}", _is_return(text, match)))
    if not found:
        for match in _MD_DATE_RE.finditer(text):
            month, day = match.groups()
            found.append((match.start(), f"{CURRENT_YEAR}-{int(month):02d}-{int(day):02d}", _is_return(text, match)))
    found.sort(key=lambda item: item[0])

    if not found:
        return []
    depart = found[0][1]
    returns = [value for _start, value, is_return in found[1:] if is_return]
    return [depart, returns[0]] if returns else [depart]


def _is_return(text: str, match: re.Match[str]) -> bool:
    window = text[max(0, match.start() - 4) : match.end() + 4]
    return any(marker in window for marker in _RETURN_MARKERS)
