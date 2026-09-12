"""SSE framing (framework §10)."""

from __future__ import annotations

import json

from app.events.types import Event


def format_sse(event: Event) -> str:
    payload = json.dumps(
        {
            "id": event.id,
            "session_id": event.session_id,
            "type": event.type.value,
            "ts": event.ts.isoformat(),
            "phase": event.phase.value,
            "plan_version_id": event.plan_version_id,
            "data": event.data,
            "replay": event.replay,
        },
        ensure_ascii=False,
    )
    return f"id: {event.id}\nevent: {event.type.value}\ndata: {payload}\n\n"


def keepalive() -> str:
    """Transport-level keepalive: a comment line, so it never consumes a seq."""

    return ":keepalive\n\n"
