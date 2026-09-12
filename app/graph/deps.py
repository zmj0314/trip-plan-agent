"""Dependency container injected into graph nodes.

Nodes stay free of persistence: they read/write ``AgentState`` and call
capabilities or the LLM through this container. The runner owns the database
(framework DI-1: business tables are written by the orchestration shell).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.capabilities.contract import CapabilityResult, Degradation, DegradationLevel, ResultStatus
from app.capabilities.registry import CapabilityRegistry
from app.config.settings import Settings


class CapabilityCaller(Protocol):
    async def call(self, capability_id: str, params: dict[str, Any], *, call_context: Any = None) -> CapabilityResult: ...


class LLMCaller(Protocol):
    async def structured(
        self,
        *,
        node: str,
        schema: type,
        prompt: str,
        user_input: str = "",
        context: Any = None,
        **kwargs: Any,
    ) -> Any: ...


@dataclass
class GraphDeps:
    settings: Settings
    registry: CapabilityRegistry
    resolver: CapabilityCaller | None = None
    llm: LLMCaller | None = None
    #: Repository bundle, when this deployment has a store. Nodes read it for
    #: things that *must* survive a reconnect -- today the idempotency ledger, so
    #: a retried side effect loses the race in the database rather than in
    #: application code (P4). It is held here, never in ``AgentState``, because
    #: anything in state is serialised into the checkpoint.
    repositories: Any = None
    extras: dict[str, Any] = field(default_factory=dict)

    async def call_capability(
        self,
        capability_id: str,
        params: dict[str, Any] | None = None,
        *,
        call_context: Any = None,
    ) -> CapabilityResult:
        """Call a capability, degrading rather than raising when it is absent.

        Unregistered capabilities are reported as ``CAPABILITY_NOT_FOUND`` in the
        degradation log -- that is how ``payment.*`` stays structurally absent
        while still being observable (DC-5).
        """

        if not self.registry.has(capability_id):
            return CapabilityResult(
                status=ResultStatus.UNAVAILABLE,
                degradation=Degradation(level=DegradationLevel.D3, reason="CAPABILITY_NOT_FOUND"),
                warnings=[f"capability not registered: {capability_id}"],
            )
        if self.resolver is None:
            return CapabilityResult(
                status=ResultStatus.UNAVAILABLE,
                degradation=Degradation(level=DegradationLevel.D3, reason="no resolver configured"),
                warnings=[f"no channel available for: {capability_id}"],
            )
        return await self.resolver.call(capability_id, params or {}, call_context=call_context)
