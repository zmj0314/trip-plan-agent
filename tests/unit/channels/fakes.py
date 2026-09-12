"""Test doubles for the channel layer."""

from __future__ import annotations

from typing import Any

from app.capabilities.contract import (
    CapabilityResult,
    ChannelKind,
    Degradation,
    DegradationLevel,
    Provenance,
    ResultStatus,
)
from app.capabilities.registry import CapabilityBinding
from app.channels.base import ChannelAdapter, HealthState, HealthStatus
from app.domain.timebase import now_local
from app.store import Database


def ok_result(data: Any = None, *, provider: str = "fake") -> CapabilityResult:
    return CapabilityResult(
        status=ResultStatus.OK,
        data=data,
        provenance=Provenance(channel=ChannelKind.MCP, provider=provider, fetched_at=now_local()),
        degradation=Degradation(level=DegradationLevel.D0),
    )


def memory_db() -> Database:
    db = Database(":memory:")
    db.migrate()
    return db


class FakeAdapter(ChannelAdapter):
    """Scriptable adapter: one entry of ``outcomes`` per invocation."""

    kind = ChannelKind.MCP

    def __init__(
        self,
        *,
        adapter_id: str = "fake",
        outcomes: list[Any] | None = None,
        result: CapabilityResult | None = None,
        accepts: bool = True,
    ) -> None:
        self.id = adapter_id
        self.outcomes = list(outcomes or [])
        self.result = result
        self.accepts = accepts
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def supports(self, capability_id: str, remote_name: str) -> bool:
        return self.accepts

    async def health(self) -> HealthStatus:
        return HealthStatus(adapter_id=self.id, state=HealthState.HEALTHY, last_check=now_local())

    async def invoke(
        self,
        binding: CapabilityBinding,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> CapabilityResult:
        self.calls.append((binding.remote_name, dict(params)))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return self.result or ok_result(data={"echo": params}, provider=self.id)
