"""Framework §3.6 / DG-1: what may be retried, and what may not."""

from __future__ import annotations

import asyncio
import socket

import pytest

from app.capabilities.contract import RiskLevel
from app.channels.base import ChannelError
from app.channels.resilience import (
    ErrorKind,
    ProviderTimeout,
    QuotaExceeded,
    classify,
    should_retry,
    with_timeout,
)
from app.errors import CoordinateSystemMismatch


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (asyncio.TimeoutError(), ErrorKind.TIMEOUT),
        (ProviderTimeout("slow"), ErrorKind.TIMEOUT),
        (ConnectionError("reset"), ErrorKind.NETWORK),
        (socket.gaierror("dns"), ErrorKind.NETWORK),
        (QuotaExceeded("403"), ErrorKind.QUOTA),
        (ValueError("bad date"), ErrorKind.BUSINESS),
        (KeyError("missing"), ErrorKind.BUSINESS),
        (CoordinateSystemMismatch("mixed"), ErrorKind.BUSINESS),
        (RuntimeError("?"), ErrorKind.UNKNOWN),
        (ChannelError("boom", kind="network"), ErrorKind.NETWORK),
        (ChannelError("boom", kind="business"), ErrorKind.BUSINESS),
    ],
)
def test_classification(exc, expected):
    assert classify(exc) is expected


def test_read_only_work_may_be_retried():
    assert should_retry(ErrorKind.NETWORK, risk=RiskLevel.L0) is True
    assert should_retry(ErrorKind.TIMEOUT, risk=RiskLevel.L0) is True


def test_side_effecting_work_is_never_retried():
    """DG-1: retrying a booking is placing a second booking."""

    for risk in (RiskLevel.L1, RiskLevel.L2):
        assert should_retry(ErrorKind.NETWORK, risk=risk) is False
        assert should_retry(ErrorKind.TIMEOUT, risk=risk) is False
        assert should_retry(ErrorKind.UNKNOWN, risk=risk) is False


def test_business_and_quota_errors_are_not_retried_at_any_risk():
    for kind in (ErrorKind.BUSINESS, ErrorKind.QUOTA):
        for risk in RiskLevel:
            assert should_retry(kind, risk=risk) is False


async def test_with_timeout_returns_the_value():
    async def quick():
        return "ok"

    assert await with_timeout(quick(), 1.0) == "ok"


async def test_with_timeout_converts_expiry_into_a_provider_timeout():
    async def slow():
        await asyncio.sleep(5)

    with pytest.raises(ProviderTimeout):
        await with_timeout(slow(), 0.01)
