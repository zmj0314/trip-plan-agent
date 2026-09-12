"""Failure classification and retry policy (DG-1 … DG-6).

The rule that matters most is the negative one: **L1/L2 calls are never
retried.** A retried booking is a second booking. Read-only calls may be
retried because doing so is idempotent by construction.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable
from enum import StrEnum
from typing import TypeVar

from app.capabilities.contract import RiskLevel
from app.errors import TravelAgentError

T = TypeVar("T")


class ErrorKind(StrEnum):
    NETWORK = "network"
    BUSINESS = "business"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class ProviderTimeout(TimeoutError):
    """The provider did not answer inside its budget."""


class QuotaExceeded(RuntimeError):
    """The provider's free allowance is gone (e.g. Variflight returns 403)."""


#: Exceptions that mean "the network, not the request, was wrong".
_NETWORK_TYPES = (ConnectionError, socket.gaierror, socket.timeout, OSError)

#: Exceptions that mean "this request can never succeed as written".
#: Domain errors belong here too: a mixed-CRS coordinate or a stale plan version
#: is a deterministic bug, and retrying it just burns quota.
_BUSINESS_TYPES = (KeyError, TypeError, LookupError, ValueError, TravelAgentError)


def classify(exc: BaseException) -> ErrorKind:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ErrorKind.TIMEOUT
    if isinstance(exc, QuotaExceeded):
        return ErrorKind.QUOTA
    if isinstance(exc, _BUSINESS_TYPES):
        return ErrorKind.BUSINESS
    if isinstance(exc, _NETWORK_TYPES):
        return ErrorKind.NETWORK
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str):
        try:
            return ErrorKind(kind.lower())
        except ValueError:
            return ErrorKind.UNKNOWN
    return ErrorKind.UNKNOWN


def should_retry(kind: ErrorKind, *, risk: RiskLevel) -> bool:
    """Retrying is only ever acceptable for read-only capabilities."""

    if risk.requires_consent:
        return False
    return kind in {ErrorKind.NETWORK, ErrorKind.TIMEOUT, ErrorKind.UNKNOWN}


async def with_timeout(coro: Awaitable[T], seconds: float) -> T:
    """Await ``coro``, converting expiry into :class:`ProviderTimeout`.

    ``asyncio.wait_for`` cancels the inner task, which is what stops a hung MCP
    server from holding a session open forever.
    """

    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError as exc:
        raise ProviderTimeout(f"provider did not answer within {seconds}s") from exc


def backoff_seconds(attempt: int, *, base: float = 0.2, cap: float = 2.0) -> float:
    return min(cap, base * (2 ** max(0, attempt - 1)))


__all__ = [
    "ErrorKind",
    "ProviderTimeout",
    "QuotaExceeded",
    "backoff_seconds",
    "classify",
    "should_retry",
    "with_timeout",
]
