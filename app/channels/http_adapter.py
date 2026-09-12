"""Plain-HTTP provider adapter.

Open-Meteo and OSRM are the sources in the plan that need neither a key nor an
MCP server (framework §6.7), so they use the HTTP transport directly. They are
the runbook for what a "second kind of channel" means: same contract, same
degradation ladder, same uniform envelope -- only the wire differs.

The adapter is deliberately generic: a provider supplies a handful of small
functions (path, query, normalise) and inherits the uniform envelope, the
error taxonomy and the retry semantics.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from app.capabilities.contract import (
    CapabilityResult,
    ChannelKind,
    Cost,
    Degradation,
    DegradationLevel,
    Provenance,
    ResultStatus,
)
from app.capabilities.registry import CapabilityBinding
from app.channels.base import ChannelAdapter, ChannelError, HealthState, HealthStatus, unavailable
from app.domain.timebase import now_local

FORECAST_HOURLY = "temperature_2m,precipitation,wind_speed_10m,visibility,weather_code,relative_humidity_2m"
AIR_HOURLY = "pm2_5,pm10,us_aqi"

#: These are public, rate-limited community services. Sending an identifying
#: User-Agent is both a courtesy and, for OpenStreetMap-run services, a policy
#: requirement; an anonymous client may simply be blocked.
POLITE_USER_AGENT = "travel-plan-agent/0.1 (personal, non-commercial)"

QueryFn = Callable[[dict[str, Any]], dict[str, Any]]
PathFn = Callable[[dict[str, Any]], str]
NormaliseFn = Callable[[Any], dict[str, Any]]


class HttpAdapter(ChannelAdapter):
    kind = ChannelKind.HTTP

    def __init__(
        self,
        *,
        adapter_id: str,
        base_url: str,
        endpoints: dict[str, str] | None = None,
        query_builders: dict[str, QueryFn] | None = None,
        path_builders: dict[str, PathFn] | None = None,
        normalisers: dict[str, NormaliseFn] | None = None,
        body_builders: dict[str, QueryFn] | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 8.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.id = adapter_id
        self.base_url = base_url.rstrip("/")
        self._endpoints = dict(endpoints or {})
        self._query_builders = dict(query_builders or {})
        self._path_builders = dict(path_builders or {})
        self._normalisers = dict(normalisers or {})
        self._body_builders = dict(body_builders or {})
        self._client = client
        self._transport = transport
        self._timeout = timeout
        self._headers = {"User-Agent": POLITE_USER_AGENT, **(headers or {})}
        self._owns_client = client is None

    # ----------------------------------------------------------------- client
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
                transport=self._transport,
                headers=self._headers,
            )
        return self._client

    def supports(self, capability_id: str, remote_name: str) -> bool:
        return remote_name in self._endpoints

    async def health(self) -> HealthStatus:
        # Probing a community service on every health tick would be rude, and
        # the capability ladder already handles a provider that turns out to be
        # down. Report configured, not verified.
        return HealthStatus(adapter_id=self.id, state=HealthState.HEALTHY, last_check=now_local())

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ----------------------------------------------------------------- invoke
    async def invoke(
        self,
        binding: CapabilityBinding,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> CapabilityResult:
        remote = binding.remote_name
        if remote not in self._endpoints:
            return unavailable(self.id, f"unsupported remote call: {remote}")

        try:
            path = self._path_for(remote, params)
            query = self._query_for(remote, params)
        except ChannelError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ChannelError(
                f"{self.id}.{remote} received unusable parameters: {exc}",
                kind="business",
                retryable=False,
            ) from exc

        try:
            body_builder = self._body_builders.get(remote)
            if body_builder is not None:
                response = await self._get_client().post(path, json=body_builder(params), timeout=timeout)
            else:
                response = await self._get_client().get(path, params=query, timeout=timeout)
        except httpx.HTTPError as exc:
            raise ChannelError(f"{self.id} request failed: {type(exc).__name__}", kind="network") from exc

        if response.status_code == 429:
            raise ChannelError(f"{self.id} rate limited", kind="quota", retryable=False)
        if response.status_code >= 500:
            raise ChannelError(f"{self.id} upstream {response.status_code}", kind="network")
        if response.status_code >= 400:
            raise ChannelError(
                f"{self.id} rejected the request ({response.status_code})",
                kind="business",
                retryable=False,
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ChannelError(f"{self.id} returned non-JSON", kind="unknown") from exc

        normaliser = self._normalisers.get(remote)
        data = normaliser(body) if normaliser is not None else _open_meteo_normalise(remote, body)

        return CapabilityResult(
            status=ResultStatus.OK,
            data=data,
            provenance=Provenance(
                channel=ChannelKind.HTTP,
                provider=self.id,
                fetched_at=now_local(),
                confidence=1.0,
            ),
            degradation=Degradation(level=DegradationLevel.D0),
            cost=Cost(units=0),
        )

    def _path_for(self, remote: str, params: dict[str, Any]) -> str:
        builder = self._path_builders.get(remote)
        return builder(params) if builder is not None else self._endpoints[remote]

    def _query_for(self, remote: str, params: dict[str, Any]) -> dict[str, Any]:
        builder = self._query_builders.get(remote)
        if builder is not None:
            return builder(params)
        query = _open_meteo_query(remote, params)
        if query is None:
            raise ChannelError(
                f"{remote} requires latitude/longitude", kind="business", retryable=False
            )
        return query


# --------------------------------------------------------------- open-meteo --


def _open_meteo_query(remote_name: str, params: dict[str, Any]) -> dict[str, Any] | None:
    lat = params.get("latitude", params.get("lat"))
    lon = params.get("longitude", params.get("lon"))
    if lat is None or lon is None:
        return None

    query: dict[str, Any] = {
        "latitude": lat,
        "longitude": lon,
        "timezone": params.get("timezone", "Asia/Shanghai"),
    }
    if remote_name == "air_quality":
        query["hourly"] = AIR_HOURLY
    else:
        query["hourly"] = params.get("hourly", FORECAST_HOURLY)
        query["forecast_days"] = int(params.get("forecast_days", 3))
    return query


def _open_meteo_normalise(remote_name: str, body: Any) -> dict[str, Any]:
    """Keep only what downstream decisions read, and keep the units explicit."""

    hourly = body.get("hourly") if isinstance(body, dict) else None
    payload: dict[str, Any] = {
        "provider": "open-meteo",
        "kind": remote_name,
        "timezone": body.get("timezone") if isinstance(body, dict) else None,
        "hourly": hourly if isinstance(hourly, dict) else {},
    }
    if isinstance(body, dict) and body.get("hourly_units"):
        payload["units"] = body["hourly_units"]
    return payload


def build_open_meteo(settings: Any) -> HttpAdapter:
    """The v1 weather channel (framework §6.7)."""

    return HttpAdapter(
        adapter_id="open_meteo",
        base_url=settings.open_meteo_base_url,
        endpoints={"forecast": "/forecast", "air_quality": "/air-quality"},
        timeout=settings.channel_connect_timeout_seconds,
    )


# --------------------------------------------------- open-meteo geocoding ----


def _geocoding_query(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("query") or params.get("name") or params.get("text")
    if not name:
        raise KeyError("query")
    return {
        "name": name,
        "count": int(params.get("count", 5)),
        "language": params.get("language", "zh"),
        "format": "json",
    }


def _geocoding_normalise(body: Any) -> dict[str, Any]:
    results = []
    items = (body or {}).get("results", []) if isinstance(body, dict) else []
    for item in items:
        results.append(
            {
                "name": item.get("name"),
                "latitude": item.get("latitude"),
                "longitude": item.get("longitude"),
                "country": item.get("country"),
                "admin1": item.get("admin1"),
                "admin2": item.get("admin2"),
                "feature_code": item.get("feature_code"),
                "population": item.get("population"),
                "timezone": item.get("timezone"),
            }
        )
    return {"provider": "open-meteo-geocoding", "kind": "place", "results": results}


def build_open_meteo_geocoding(settings: Any) -> HttpAdapter:
    """Keyless place resolution (framework §5.2, the "免 key 备" row)."""

    return HttpAdapter(
        adapter_id="open_meteo_geocoding",
        base_url=settings.open_meteo_geocoding_base_url,
        endpoints={"search": "/search"},
        query_builders={"search": _geocoding_query},
        normalisers={"search": _geocoding_normalise},
        timeout=settings.channel_connect_timeout_seconds,
    )


# ------------------------------------------------------------------- osrm ----

#: OSRM's public demo server exposes these profiles. It is fine for personal,
#: low-frequency use and explicitly not for production traffic.
OSRM_PROFILES = {
    "driving": "driving",
    "drive": "driving",
    "walking": "foot",
    "walk": "foot",
    "bicycling": "bike",
    "bike": "bike",
}


def _osrm_path(params: dict[str, Any]) -> str:
    start = params["from"]
    end = params["to"]
    key = str(params.get("mode") or params.get("profile") or "driving")
    profile = OSRM_PROFILES.get(key, "driving")
    return f"/route/v1/{profile}/{start['lon']},{start['lat']};{end['lon']},{end['lat']}"


def _osrm_query(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "overview": "false",
        "steps": "false",
        "alternatives": "true" if params.get("alternatives", True) else "false",
    }


def _osrm_normalise(body: Any) -> dict[str, Any]:
    routes = []
    items = (body or {}).get("routes", []) if isinstance(body, dict) else []
    for route in items:
        routes.append(
            {
                "distance_m": route.get("distance"),
                "duration_s": route.get("duration"),
                "legs": len(route.get("legs") or []),
            }
        )
    return {"provider": "osrm", "kind": "route", "routes": routes}


def build_osrm(settings: Any) -> HttpAdapter:
    """Keyless point-to-point routing.

    Only used for the driving profile: the public demo server returns driving
    numbers for ``/foot/`` and ``/bike/`` too, so any multi-modal comparison
    built on it would be three identical candidates wearing different labels.
    """

    return HttpAdapter(
        adapter_id="osrm",
        base_url=settings.osrm_base_url,
        endpoints={"route": "/route/v1/driving"},
        query_builders={"route": _osrm_query},
        path_builders={"route": _osrm_path},
        normalisers={"route": _osrm_normalise},
        timeout=settings.channel_connect_timeout_seconds,
    )


# --------------------------------------------------------------- valhalla ----

VALHALLA_COSTING = {
    "driving": "auto",
    "drive": "auto",
    "car": "auto",
    "walking": "pedestrian",
    "walk": "pedestrian",
    "bicycling": "bicycle",
    "bike": "bicycle",
    "cycling": "bicycle",
}


def _valhalla_body(params: dict[str, Any]) -> dict[str, Any]:
    start = params["from"]
    end = params["to"]
    key = str(params.get("mode") or params.get("profile") or "driving")
    return {
        "locations": [
            {"lat": start["lat"], "lon": start["lon"]},
            {"lat": end["lat"], "lon": end["lon"]},
        ],
        "costing": VALHALLA_COSTING.get(key, "auto"),
        "units": "kilometers",
    }


def _valhalla_normalise(body: Any) -> dict[str, Any]:
    trip = (body or {}).get("trip") if isinstance(body, dict) else None
    summary = (trip or {}).get("summary") or {}
    routes = []
    if summary:
        routes.append(
            {
                "distance_m": (summary.get("length") or 0) * 1000,
                "duration_s": summary.get("time") or 0,
                "legs": 1,
            }
        )
    return {"provider": "valhalla", "kind": "route", "routes": routes}


def build_valhalla(settings: Any) -> HttpAdapter:
    """Keyless routing that actually honours the travel mode."""

    return HttpAdapter(
        adapter_id="valhalla",
        base_url=settings.valhalla_base_url,
        endpoints={"route": "/route"},
        query_builders={"route": lambda _params: {}},
        body_builders={"route": _valhalla_body},
        normalisers={"route": _valhalla_normalise},
        timeout=settings.channel_connect_timeout_seconds,
    )


__all__ = [
    "AIR_HOURLY",
    "FORECAST_HOURLY",
    "POLITE_USER_AGENT",
    "HttpAdapter",
    "build_open_meteo",
    "build_open_meteo_geocoding",
    "build_osrm",
    "build_valhalla",
]
