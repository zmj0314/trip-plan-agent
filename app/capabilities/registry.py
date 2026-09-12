"""Capability registry.

``payment.*`` is intentionally absent: the product promise "never auto-pay" is
expressed as *absence* rather than as a policy check, so no future node can
accidentally call it (DC-5, P7).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.capabilities.contract import CapabilitySpec, RiskLevel
from app.errors import CapabilityNotFound


class CapabilityBinding(BaseModel):
    """Binds a capability to one concrete provider on one channel."""

    adapter_id: str
    remote_name: str
    param_map: dict[str, str] = Field(default_factory=dict)
    result_map: dict[str, str] = Field(default_factory=dict)
    priority: int = 100


class CapabilityRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, CapabilitySpec] = {}
        self._bindings: dict[str, list[CapabilityBinding]] = {}

    def register(self, spec: CapabilitySpec, *bindings: CapabilityBinding) -> None:
        if spec.capability_id in self._specs:
            raise ValueError(f"capability already registered: {spec.capability_id}")
        self._specs[spec.capability_id] = spec
        self._bindings[spec.capability_id] = sorted(bindings, key=lambda b: b.priority)

    def add_binding(self, capability_id: str, binding: CapabilityBinding) -> None:
        """Attach an extra provider to an existing capability (lower priority wins)."""

        self.spec(capability_id)
        bindings = [*self._bindings[capability_id], binding]
        self._bindings[capability_id] = sorted(bindings, key=lambda b: b.priority)

    def override_bindings(self, capability_id: str, *bindings: CapabilityBinding) -> None:
        """Replace the provider chain for a capability (used by tests / local dev)."""

        self.spec(capability_id)
        self._bindings[capability_id] = sorted(bindings, key=lambda b: b.priority)

    def has(self, capability_id: str) -> bool:
        return capability_id in self._specs

    def spec(self, capability_id: str) -> CapabilitySpec:
        try:
            return self._specs[capability_id]
        except KeyError as exc:
            raise CapabilityNotFound(
                f"capability not registered: {capability_id}",
                capability_id=capability_id,
            ) from exc

    def bindings(self, capability_id: str) -> list[CapabilityBinding]:
        self.spec(capability_id)  # raises CapabilityNotFound for unknown ids
        return list(self._bindings[capability_id])

    def all_specs(self) -> list[CapabilitySpec]:
        return list(self._specs.values())

    def risk(self, capability_id: str) -> RiskLevel:
        return self.spec(capability_id).risk
