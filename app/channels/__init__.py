"""Connection adapters (L6).

A capability is a stable interface; a *channel* is a replaceable way of
fulfilling it (P2, P5). Adapters know how to talk to one provider and nothing
about the trip, the user, or consent.
"""

from app.channels.base import ChannelAdapter, ChannelError, HealthState, HealthStatus
from app.channels.cache import CachedEntry, CacheStore
from app.channels.local import LocalAdapter
from app.channels.quota import QuotaDecision, QuotaLedger, QuotaState
from app.channels.resilience import ErrorKind, ProviderTimeout, QuotaExceeded, classify, should_retry, with_timeout
from app.channels.resolver import CallContext, CapabilityResolver

__all__ = [
    "CacheStore",
    "CachedEntry",
    "CallContext",
    "CapabilityResolver",
    "ChannelAdapter",
    "ChannelError",
    "ErrorKind",
    "HealthState",
    "HealthStatus",
    "LocalAdapter",
    "ProviderTimeout",
    "QuotaDecision",
    "QuotaExceeded",
    "QuotaLedger",
    "QuotaState",
    "classify",
    "should_retry",
    "with_timeout",
]
