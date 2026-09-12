"""Capability contract (framework §4.1).

The orchestration layer only ever sees these types. It never sees an MCP tool
name, an HTTP endpoint or a browser selector -- that is the whole point of the
capability layer (P2).
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.domain.timebase import now_local


class RiskLevel(IntEnum):
    """Consent requirement, independent of transport channel (P5)."""

    L0 = 0  # read-only: batch-execute after plan consent
    L1 = 1  # side effects: confirm per call
    L2 = 2  # irreversible: strong confirm + one-time token

    @property
    def requires_consent(self) -> bool:
        return self >= RiskLevel.L1


class ChannelKind(StrEnum):
    MCP = "mcp"
    HTTP = "http"
    BROWSER = "browser"


class DegradationLevel(IntEnum):
    D0 = 0  # live, complete
    D1 = 1  # cached, timestamped
    D2 = 2  # skeleton, explicitly marked as unverified
    D3 = 3  # deep-link + checklist, user self-serves

    @property
    def uncertainty(self) -> float:
        """Feeds the scoring penalty (DF-3)."""

        return {0: 0.00, 1: 0.10, 2: 0.30, 3: 0.50}[int(self)]


class ResultStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class Provenance(BaseModel):
    """Where a value came from and how fresh it is."""

    channel: ChannelKind
    provider: str
    fetched_at: datetime = Field(default_factory=now_local)
    cache_hit: bool = False
    ttl_left_seconds: int | None = None
    confidence: float = 1.0


class Degradation(BaseModel):
    level: DegradationLevel = DegradationLevel.D0
    reason: str = ""


class Cost(BaseModel):
    units: int = 0
    currency: str = "CNY"
    quota_pool: str | None = None


class CapabilityResult(BaseModel):
    """Uniform envelope returned by every capability, on every channel."""

    status: ResultStatus = ResultStatus.OK
    data: Any = None
    provenance: Provenance | None = None
    degradation: Degradation = Field(default_factory=Degradation)
    cost: Cost = Field(default_factory=Cost)
    warnings: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {ResultStatus.OK, ResultStatus.DEGRADED}


class CapabilitySpec(BaseModel):
    """Declarative definition of one stable capability."""

    capability_id: str
    description: str = ""
    risk: RiskLevel = RiskLevel.L0
    timeout_seconds: float = 10.0
    cache_ttl_seconds: int | None = None
    quota_pool: str | None = None
    units_per_call: int = 0
    idempotent: bool = True
    #: Ordered fallback ladder; each step names a provider and what it costs us.
    degradation_ladder: list[DegradationLevel] = Field(default_factory=lambda: [DegradationLevel.D1, DegradationLevel.D2, DegradationLevel.D3])

    @property
    def requires_consent(self) -> bool:
        return self.risk.requires_consent
