"""Streaming event contract (framework §4.3, §10).

Three deliberate omissions from the original handoff:

* ``reasoning_delta`` does not exist (DC-1). Model reasoning stays in the local
  trace; it never reaches the wire. Removing the type entirely is stronger than
  "remember not to emit it".
* ``payment.*`` capability events do not exist (DC-5).
* ``text_delta`` exists but is deliberately **not replayable**: it is published
  live to attached SSE readers and never written to the store. A reconnect
  rebuilds the screen from a snapshot, so replaying a partial sentence would add
  noise without adding state. ``state_update`` with the snapshot is what makes
  the rebuild correct, which is also why the stream sends it right after replay.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.domain.models import Phase
from app.domain.timebase import now_local


class EventType(StrEnum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_END = "tool_call_end"
    TOOL_RESULT = "tool_result"
    PLAN_PREVIEW = "plan_preview"
    INTERRUPT = "interrupt"
    STATE_UPDATE = "state_update"
    SCOPE_REJECTED = "scope_rejected"
    DEGRADATION_NOTICE = "degradation_notice"
    USAGE = "usage"
    ERROR = "error"
    DONE = "done"
    HEARTBEAT = "heartbeat"


#: Only these are persisted for replay (DI-2). Incremental deltas are excluded
#: because reconnect rebuilds UI from a snapshot, not from replayed text.
REPLAYABLE_EVENT_TYPES = frozenset(
    {
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_END,
        EventType.TOOL_RESULT,
        EventType.PLAN_PREVIEW,
        EventType.INTERRUPT,
        EventType.STATE_UPDATE,
        EventType.SCOPE_REJECTED,
        EventType.DEGRADATION_NOTICE,
        EventType.USAGE,
        EventType.ERROR,
        EventType.DONE,
    }
)


class DoneStatus(StrEnum):
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"


class Event(BaseModel):
    """Envelope that carries its own state coordinates (DJ-1)."""

    id: int
    session_id: str
    type: EventType
    ts: datetime = Field(default_factory=now_local)
    phase: Phase = Phase.COLLECT
    plan_version_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    replay: bool = False

    @property
    def replayable(self) -> bool:
        return self.type in REPLAYABLE_EVENT_TYPES
