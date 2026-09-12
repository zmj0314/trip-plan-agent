"""Domain error hierarchy.

Every error that can reach a user must be explainable (framework §7.8).
Infrastructure errors map to ``degradation``; domain violations raise.
"""

from __future__ import annotations


class TravelAgentError(Exception):
    """Base class for all agent errors."""

    code = "TRAVEL_AGENT_ERROR"

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, **self.context}


class CapabilityNotFound(TravelAgentError):
    """Raised when a capability is not registered.

    This is the structural enforcement of DC-5: because ``payment.*`` is never
    registered, any attempt to call it fails here rather than being blocked by
    a policy check that could be bypassed.
    """

    code = "CAPABILITY_NOT_FOUND"


class IllegalPreConsentCall(TravelAgentError):
    """A non-read-only capability was invoked before consent was recorded."""

    code = "ILLEGAL_PRE_CONSENT"


class PlanVersionStale(TravelAgentError):
    """Resume payload referenced a plan version that is no longer current (DE-2)."""

    code = "PLAN_VERSION_STALE"


class SlotValidationError(TravelAgentError):
    code = "SLOT_VALIDATION_ERROR"


class ConstraintViolation(TravelAgentError):
    """Cross-leg constraint failure (temporal chaining, budget, window)."""

    code = "CONSTRAINT_VIOLATION"

    def __init__(self, message: str, violations: list[dict[str, object]] | None = None) -> None:
        super().__init__(message, violations=violations or [])


class CoordinateSystemMismatch(TravelAgentError):
    """Coordinates were combined without normalising CRS.

    Framework §6.5: this is a *bug*, not a data-quality issue, so it raises
    instead of degrading.
    """

    code = "COORDINATE_SYSTEM_MISMATCH"


class ScopeBreach(TravelAgentError):
    """Abuse-grade out-of-scope input (prompt injection / role hijack)."""

    code = "SCOPE_BREACH"


class SessionExpired(TravelAgentError):
    code = "SESSION_EXPIRED"


class BudgetExceeded(TravelAgentError):
    """LLM budget guard tripped (DN-1..DN-6)."""

    code = "BUDGET_EXCEEDED"


class CredentialMissing(TravelAgentError):
    code = "CREDENTIAL_MISSING"
