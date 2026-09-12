"""Domain models shared across layers."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.domain.timebase import now_local


class Phase(StrEnum):
    COLLECT = "COLLECT"
    READY = "READY"
    PREVIEW = "PREVIEW"
    AWAIT_CONSENT = "AWAIT_CONSENT"
    EXECUTE = "EXECUTE"
    DONE = "DONE"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


TERMINAL_PHASES = frozenset({Phase.DONE, Phase.FAILED, Phase.EXPIRED})


class ScopeVerdict(StrEnum):
    IN_SCOPE = "in_scope"
    AMBIGUOUS = "ambiguous"
    OUT_OF_SCOPE = "out_of_scope"
    ABUSE = "abuse"


class ScopeState(BaseModel):
    verdict: ScopeVerdict = ScopeVerdict.IN_SCOPE
    last_verdict: ScopeVerdict | None = None
    strikes: int = 0
    rejected_count: int = 0
    off_topic_streak: int = 0


class SlotStatus(StrEnum):
    FILLED = "filled"
    UNKNOWN = "unknown"
    USER_DECLINED = "user_declined"

    @property
    def resolved(self) -> bool:
        """``user_declined`` counts as resolved (DC-2 / §4.2).

        The gate blocks *missing* values; it must not block a user who said
        "you decide", otherwise clarification never terminates.
        """

        return self in {SlotStatus.FILLED, SlotStatus.USER_DECLINED}


class SlotSource(StrEnum):
    USER = "user"
    INFERRED = "inferred"
    DEFAULT = "default"


class SlotValue(BaseModel):
    value: Any = None
    source: SlotSource = SlotSource.USER
    confidence: float = 1.0
    status: SlotStatus = SlotStatus.UNKNOWN
    confirmed_at: datetime | None = None

    @classmethod
    def filled(cls, value: Any, source: SlotSource = SlotSource.USER, confidence: float = 1.0) -> "SlotValue":
        return cls(value=value, source=source, confidence=confidence, status=SlotStatus.FILLED, confirmed_at=now_local())

    @classmethod
    def declined(cls, value: Any = None) -> "SlotValue":
        return cls(value=value, source=SlotSource.DEFAULT, status=SlotStatus.USER_DECLINED, confirmed_at=now_local())


class PendingInterruptKind(StrEnum):
    QUESTION = "question"
    GATE2 = "gate2_full"
    ACTION = "action"
    CONFIRM_TOKEN = "confirm_token"


class PendingInterrupt(BaseModel):
    kind: PendingInterruptKind
    plan_version_id: str | None = None
    plan_hash: str | None = None
    action_id: str | None = None
    prompt: str | None = None
    options: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now_local)


class PlanVersionMeta(BaseModel):
    plan_version_id: str
    version_no: int
    plan_hash: str
    status: str = "draft"  # draft | previewed | approved | superseded
    created_at: datetime = Field(default_factory=now_local)
    cost_estimate: float | None = None


class ConsentScopeKind(StrEnum):
    GATE2_FULL = "gate2_full"
    ACTION = "action"


class ConsentRef(BaseModel):
    consent_id: str
    session_id: str
    plan_version_id: str
    plan_hash: str
    scope_kind: ConsentScopeKind
    action_id: str | None = None
    granted_at: datetime = Field(default_factory=now_local)
    superseded_by: str | None = None


class ActionStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class ActionState(BaseModel):
    action_id: str
    capability_id: str
    status: ActionStatus = ActionStatus.PENDING
    depends_on: list[str] = Field(default_factory=list)
    idem_key: str | None = None
    consent_ref: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: dict[str, Any] | None = None
    evidence: list[str] = Field(default_factory=list)


class DegradationNote(BaseModel):
    capability_id: str
    provider: str | None = None
    level: int = 0
    reason: str = ""
    created_at: datetime = Field(default_factory=now_local)
