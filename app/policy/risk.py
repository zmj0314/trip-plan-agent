"""Consent arbitration (DH-1, framework §5.3 / P5).

Risk level -- not transport channel -- decides what a call needs. Nothing here
is stateful: the caller passes in what consent it claims to hold, and the
arbiter answers allow/deny. That keeps the rule in one place instead of
scattered across nodes.

``payment.*`` is absent from the registry entirely, so "never auto-pay" is
enforced structurally: no amount of consent makes an unregistered capability
callable.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from app.capabilities.contract import RiskLevel
from app.capabilities.registry import CapabilityRegistry


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Arbitration(BaseModel):
    decision: Decision
    reason: str
    capability_id: str
    risk: RiskLevel

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


class Arbiter:
    """The single place that answers "may this capability run right now?"."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def decide(
        self,
        *,
        capability_id: str,
        plan_consented: bool,
        consent_ref: str | None,
        has_action_consent: bool = False,
        confirm_token_ok: bool = False,
    ) -> Arbitration:
        spec = self._registry.spec(capability_id)  # unknown id -> CapabilityNotFound
        risk = spec.risk

        def deny(reason: str) -> Arbitration:
            return Arbitration(
                decision=Decision.DENY,
                reason=reason,
                capability_id=capability_id,
                risk=risk,
            )

        # No capability of any level may run before the plan itself is agreed,
        # which is what makes "zero side effects before consent" true.
        if not plan_consented:
            return deny("ILLEGAL_PRE_CONSENT")

        level = int(risk)
        if level >= 1 and not has_action_consent:
            return deny("MISSING_ACTION_CONSENT")
        if level >= 2 and not confirm_token_ok:
            return deny("MISSING_CONFIRM_TOKEN")

        # An allow without a reference to the consent it relied on would be
        # un-auditable, so it is refused rather than recorded as an exception.
        if not consent_ref:
            return deny("MISSING_CONSENT_REF")

        return Arbitration(
            decision=Decision.ALLOW,
            reason="CONSENT_OK",
            capability_id=capability_id,
            risk=risk,
        )
