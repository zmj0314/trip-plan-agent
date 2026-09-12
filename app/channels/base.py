"""The adapter contract every channel implements (framework §3.1 / §4.1)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel

from app.capabilities.contract import (
    CapabilityResult,
    ChannelKind,
    Degradation,
    DegradationLevel,
    ResultStatus,
)
from app.capabilities.registry import CapabilityBinding


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class HealthStatus(BaseModel):
    adapter_id: str
    state: HealthState = HealthState.HEALTHY
    last_check: datetime | None = None
    fail_count: int = 0
    detail: str = ""


class ChannelError(RuntimeError):
    """A provider refused or failed. The resolver decides what that means."""

    def __init__(self, message: str, *, kind: str = "UNKNOWN", retryable: bool = True) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


def unavailable(adapter_id: str, reason: str, *, level: DegradationLevel = DegradationLevel.D3) -> CapabilityResult:
    """Uniform "this source could not answer" envelope.

    Failure is always *explainable* (framework §6.10.6): the reason travels with
    the result so the reason chain can say which source was down.
    """

    return CapabilityResult(
        status=ResultStatus.UNAVAILABLE,
        degradation=Degradation(level=level, reason=reason),
        warnings=[f"{adapter_id}: {reason}"],
    )


class ChannelAdapter(ABC):
    """One provider on one transport."""

    id: ClassVar[str] = "adapter"
    kind: ClassVar[ChannelKind] = ChannelKind.MCP

    @abstractmethod
    def supports(self, capability_id: str, remote_name: str) -> bool:
        """Whether this adapter can serve the named remote tool."""

    @abstractmethod
    async def health(self) -> HealthStatus:
        """Protocol-level liveness only. Never a business call (DG-6)."""

    @abstractmethod
    async def invoke(
        self,
        binding: CapabilityBinding,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> CapabilityResult:
        ...

    async def aclose(self) -> None:  # pragma: no cover - default is a no-op
        return None


__all__ = [
    "ChannelAdapter",
    "ChannelError",
    "HealthState",
    "HealthStatus",
    "unavailable",
]
