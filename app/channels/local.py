"""In-process capabilities (M1 deep-links, M2 checklists, reminders).

These are the actions that must *never* be gated on consent: building a
deep-link or a checklist changes nothing in the outside world (framework
DC-4, P5). They are L0, they are computed here, and they are the reason the
offline path can still hand the user something actionable.

Payment is absent by construction: a deep-link carries the user into the
official app, and the purchase happens there (DC-5).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from app.capabilities.contract import (
    CapabilityResult,
    ChannelKind,
    Degradation,
    DegradationLevel,
    ResultStatus,
)
from app.capabilities.registry import CapabilityBinding
from app.channels.base import ChannelAdapter, HealthState, HealthStatus, unavailable

#: 12306's booking entry point. Deep-linking is the M1 mechanism: we prepare the
#: parameters, the user completes the purchase in the official client.
RAIL_DEEPLINK = (
    "https://kyfw.12306.cn/otn/leftTicket/init"
    "?linktypeid=dc&fs={from_station}&ts={to_station}&date={date}&flag=N,N,Y"
)


def _first(subject: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = subject.get(name)
        if value not in (None, "", []):
            return value
    return None


def build_deeplink(params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the parameters a user needs to complete a booking themselves."""

    subject = dict(params or {})
    from_station = _first(subject, "from_station", "from_code", "origin")
    to_station = _first(subject, "to_station", "to_code", "destination")
    date = _first(subject, "date", "depart_date", "travel_date")
    missing = [
        label
        for label, value in (("from_station", from_station), ("to_station", to_station), ("date", date))
        if value is None
    ]

    url = None
    if not missing:
        url = RAIL_DEEPLINK.format(from_station=from_station, to_station=to_station, date=date)

    return {
        "kind": "deeplink",
        "leg_id": subject.get("leg_id"),
        "url": url,
        "missing": missing,
        "payment": "user_side",
        "steps": [
            "在官方 App / H5 打开链接",
            "核对车次、日期、席别与乘车人",
            "由你本人完成支付",
        ],
    }


def export_checklist(params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Structured purchase list (M2) for people who would rather book manually."""

    subject = dict(params or {})
    legs = subject.get("legs") or []
    if not isinstance(legs, (list, tuple)):
        legs = [legs]

    items = []
    for index, leg in enumerate(legs):
        detail = dict(leg) if isinstance(leg, Mapping) else {"leg_id": leg}
        items.append(
            {
                "index": index,
                "leg_id": detail.get("leg_id"),
                "mode": detail.get("mode"),
                "depart": detail.get("depart"),
                "arrive": detail.get("arrive"),
                "seat_class": detail.get("seat_class"),
                "travelers": detail.get("travelers") or subject.get("travelers"),
                "alternatives": list(detail.get("alternatives") or []),
            }
        )
    return {
        "kind": "checklist",
        "items": items,
        "payment": "user_side",
        "note": "查询数据为尽力而为，下单前请以官方渠道为准。",
    }


def create_reminder(params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    subject = dict(params or {})
    items = list(subject.get("items") or [])
    if not items:
        items = ["出发前一天确认车次与余票。", "出发当天预留到站缓冲时间。"]
    return {"kind": "reminder", "items": items}


# ---------------------------------------------------------------- exporting --

#: Extension and media type per export format. Adding a format is a dictionary
#: entry here plus a renderer -- the capability itself never changes.
EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "markdown": ("md", "text/markdown;charset=utf-8"),
    "text": ("txt", "text/plain;charset=utf-8"),
    "html": ("html", "text/html;charset=utf-8"),
}


def _hhmm(value: Any) -> str:
    """``"2026-10-01T09:00:00"`` -> ``"09:00"``; anything unparseable passes through."""

    text = str(value or "")
    if "T" in text:
        return text.split("T", 1)[1][:5]
    return text


def _yuan(value: Any) -> str:
    """A price read by a human, not a float repr: ``480.0`` -> ``480``."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,.0f}" if number.is_integer() else f"{number:,.2f}"


def _render_text(plan: Mapping[str, Any], *, html: bool) -> str:
    """Render the plan payload the orchestration layer hands over.

    Accepts the flat shape (``{"legs": …, "days": …}``) and tolerates a nested
    ``{"trip": {...}}`` wrapper, because a caller that passes the whole snapshot
    should not silently produce an empty document.
    """

    source = plan.get("trip") if isinstance(plan.get("trip"), Mapping) else plan
    trip = dict(source or {})
    days = list(trip.get("days") or [])
    legs = list(trip.get("legs") or [])
    lines: list[str] = ["行程单"]

    segments = list(trip.get("segments") or [])
    if segments:
        lines.append("分段：" + " / ".join(f"{s.get('city')} {s.get('day_count')} 天" for s in segments))

    for day in days:
        head = f"Day {day.get('day_index')}（{day.get('date') or ''}）{day.get('city') or ''}"
        if day.get("is_transfer_day"):
            head += f" → {day.get('overnight_city') or '下一站'}"
        if day.get("theme"):
            head += f" · {day['theme']}"
        lines.append(head)
        for attraction in day.get("attractions") or []:
            if not attraction.get("name"):
                continue
            item = next(
                (i for i in (day.get("items") or []) if i.get("ref_id") == attraction.get("attraction_id")),
                None,
            )
            when = ""
            if item and item.get("start") and item.get("end"):
                when = f"{_hhmm(item['start'])}–{_hhmm(item['end'])}  "
            price = attraction.get("ticket_price")
            suffix = f"（¥{_yuan(price)} 参考价）" if price is not None else ""
            lines.append(f"  {when}{attraction['name']}{suffix}")
        for item in day.get("items") or []:
            if item.get("kind") == "city_transfer":
                label = (item.get("evidence") or [""])[0]
                lines.append(f"  转场 {label}".rstrip())
        for option in day.get("lodging") or []:
            price = option.get("nightly_price")
            lines.append(
                f"  今晚住 {option.get('area') or option.get('name')}"
                + (f"（参考价 ¥{_yuan(price)}/晚）" if price is not None else "（未获取实时房价）")
            )

    for index, leg in enumerate(legs, start=1):
        label = f"{leg.get('origin_text') or ''} → {leg.get('destination_text') or ''}"
        when = " ".join(part for part in (leg.get("depart"), leg.get("arrive")) if part)
        code = leg.get("train_code") or ""
        lines.append(f"{index}. {label} {when} {code}".rstrip())

    notes = list(trip.get("content_notes") or [])
    if notes:
        lines.append("注意事项：")
        lines.extend(f"- {note}" for note in notes)
    if trip.get("cost_note"):
        lines.append(f"费用：{trip['cost_note']}")

    body = "\n".join(lines)
    if not html:
        return body
    escaped = (
        body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>行程单</title></head><body><pre>" + escaped + "</pre></body></html>"
    )


def export_plan(params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Render the frozen plan as a file the user owns (L0, offline, free).

    Deliberately format-agnostic: markdown for people who will paste it, HTML for
    people who will print it. A real PDF renderer is a deployment choice, not
    something this layer should depend on.
    """

    subject = dict(params or {})
    requested = str(subject.get("format") or "markdown").strip().lower()
    fmt = requested if requested in EXPORT_FORMATS else "markdown"
    extension, media_type = EXPORT_FORMATS[fmt]
    content = _render_text(subject.get("plan") or {}, html=fmt == "html")
    return {
        "kind": "export",
        "format": fmt,
        # Reported rather than silently substituted: a caller that asked for PDF
        # must be able to tell that it received markdown.
        "requested_format": requested,
        "supported_formats": sorted(EXPORT_FORMATS),
        "filename": f"itinerary.{extension}",
        "media_type": media_type,
        "content": content,
        "bytes": len(content.encode("utf-8")),
        "payment": "user_side",
    }


def build_share(params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A self-contained, read-only copy of the itinerary.

    "Share" here means a file the user can send, not a hosted link: this project
    is a single-user local app, and a public URL would need auth, storage and a
    take-down story that do not exist yet. Saying so in the payload is better
    than minting a link that cannot be revoked.
    """

    subject = dict(params or {})
    content = _render_text(subject.get("plan") or {}, html=True)
    return {
        "kind": "share",
        "mode": "file",
        "filename": "itinerary.html",
        "media_type": "text/html;charset=utf-8",
        "content": content,
        "note": "这是离线快照文件，可直接分享；本版本不提供托管链接。",
        "payment": "user_side",
    }


#: ``remote_name`` from the capability binding -> in-process implementation.
LOCAL_OPERATIONS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    "build_deeplink": build_deeplink,
    "export_checklist": export_checklist,
    "create_reminder": create_reminder,
    "export_plan": export_plan,
    "build_share": build_share,
}


class LocalAdapter(ChannelAdapter):
    """Serves side-effect-free capabilities without leaving the process."""

    id = "local"
    # ``ChannelKind`` is frozen at three *transports* and has no LOCAL member.
    # The tag is only used for routing and health display -- and provenance is
    # deliberately ``None`` below, so no reason chain can claim that a local
    # computation came from an external source.
    kind = ChannelKind.HTTP

    def __init__(self, *, adapter_id: str = "local", operations: Mapping[str, Callable[..., dict[str, Any]]] | None = None) -> None:
        self.id = adapter_id
        self._operations = dict(operations or LOCAL_OPERATIONS)

    def supports(self, capability_id: str, remote_name: str) -> bool:
        return remote_name in self._operations

    async def health(self) -> HealthStatus:
        from app.domain.timebase import now_local

        return HealthStatus(adapter_id=self.id, state=HealthState.HEALTHY, last_check=now_local())

    async def invoke(
        self,
        binding: CapabilityBinding,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> CapabilityResult:
        operation = self._operations.get(binding.remote_name)
        if operation is None:
            return unavailable(self.id, f"unknown local operation: {binding.remote_name}")

        mapped = _apply_param_map(binding, params)
        try:
            data = operation(mapped)
        except Exception as exc:  # pragma: no cover - defensive
            return unavailable(self.id, f"local operation failed: {type(exc).__name__}")

        return CapabilityResult(
            status=ResultStatus.OK,
            data=_apply_result_map(binding, data),
            provenance=None,  # nothing was fetched from anywhere
            degradation=Degradation(level=DegradationLevel.D0),
        )


def _apply_param_map(binding: CapabilityBinding, params: Mapping[str, Any]) -> dict[str, Any]:
    if not binding.param_map:
        return dict(params or {})
    return {target: params.get(source) for source, target in binding.param_map.items() if source in params}


def _apply_result_map(binding: CapabilityBinding, data: Any) -> Any:
    if not binding.result_map or not isinstance(data, Mapping):
        return data
    return {target: data.get(source) for source, target in binding.result_map.items()}


__all__ = [
    "EXPORT_FORMATS",
    "LOCAL_OPERATIONS",
    "RAIL_DEEPLINK",
    "LocalAdapter",
    "build_deeplink",
    "build_share",
    "create_reminder",
    "export_checklist",
    "export_plan",
]
