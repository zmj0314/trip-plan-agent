"""HTTP transport behaviour, driven by a mock transport (framework §6.7)."""

from __future__ import annotations

import httpx
import pytest

from app.capabilities.contract import ChannelKind, ResultStatus
from app.capabilities.registry import CapabilityBinding
from app.channels.base import ChannelError
from app.channels.http_adapter import HttpAdapter


def adapter_with(handler) -> HttpAdapter:
    return HttpAdapter(
        adapter_id="open_meteo",
        base_url="https://api.open-meteo.test/v1",
        endpoints={"forecast": "/forecast", "air_quality": "/air-quality"},
        transport=httpx.MockTransport(handler),
    )


async def test_forecast_is_normalised_and_provenanced():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "timezone": "Asia/Shanghai",
                "hourly_units": {"temperature_2m": "°C"},
                "hourly": {"time": ["2026-09-12T08:00"], "temperature_2m": [18.5]},
            },
        )

    result = await adapter_with(handler).invoke(
        CapabilityBinding(adapter_id="open_meteo", remote_name="forecast"),
        {"latitude": 40.43, "longitude": 116.56},
        timeout=5.0,
    )

    assert result.status is ResultStatus.OK
    assert result.data["kind"] == "forecast"
    assert result.data["hourly"]["temperature_2m"] == [18.5]
    assert result.data["units"] == {"temperature_2m": "°C"}
    assert result.provenance is not None
    assert result.provenance.channel is ChannelKind.HTTP
    assert "latitude=40.43" in seen["url"].replace("%2C", ",")


async def test_missing_coordinates_is_a_business_error_not_a_retry():
    adapter = adapter_with(lambda request: httpx.Response(200, json={}))

    with pytest.raises(ChannelError) as caught:
        await adapter.invoke(
            CapabilityBinding(adapter_id="open_meteo", remote_name="forecast"),
            {"address": "长城"},
            timeout=5.0,
        )

    assert caught.value.kind == "business"
    assert caught.value.retryable is False


async def test_rate_limiting_is_classified_as_a_quota_problem():
    adapter = adapter_with(lambda request: httpx.Response(429, json={"reason": "daily limit"}))

    with pytest.raises(ChannelError) as caught:
        await adapter.invoke(
            CapabilityBinding(adapter_id="open_meteo", remote_name="forecast"),
            {"latitude": 1, "longitude": 2},
            timeout=5.0,
        )

    assert caught.value.kind == "quota"


async def test_server_errors_are_retryable():
    adapter = adapter_with(lambda request: httpx.Response(503, text="unavailable"))

    with pytest.raises(ChannelError) as caught:
        await adapter.invoke(
            CapabilityBinding(adapter_id="open_meteo", remote_name="forecast"),
            {"latitude": 1, "longitude": 2},
            timeout=5.0,
        )

    assert caught.value.kind == "network"
    assert caught.value.retryable is True


async def test_network_failures_are_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(ChannelError) as caught:
        await adapter_with(handler).invoke(
            CapabilityBinding(adapter_id="open_meteo", remote_name="forecast"),
            {"latitude": 1, "longitude": 2},
            timeout=5.0,
        )

    assert caught.value.kind == "network"


async def test_air_quality_uses_its_own_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/air-quality")
        return httpx.Response(200, json={"hourly": {"us_aqi": [42]}})

    result = await adapter_with(handler).invoke(
        CapabilityBinding(adapter_id="open_meteo", remote_name="air_quality"),
        {"latitude": 1, "longitude": 2},
        timeout=5.0,
    )

    assert result.data["kind"] == "air_quality"
    assert result.data["hourly"]["us_aqi"] == [42]
