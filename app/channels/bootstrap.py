"""Channel wiring (framework §6.8).

One place decides which providers exist for this deployment. A provider whose
credential is absent is simply not constructed, which is how the plan's
"removable capability" requirement works in practice: no key, no adapter, and
every capability that depended on it degrades along its declared ladder instead
of raising.

Credentials are read from settings (environment) and passed straight to the
child process environment. They are never logged, never stored in graph state,
and never written to the database (framework §6.11).
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from app.capabilities.registry import CapabilityRegistry
from app.channels.base import ChannelAdapter
from app.channels.cache import CacheStore
from app.channels.http_adapter import (
    build_open_meteo,
    build_open_meteo_geocoding,
    build_osrm,
    build_valhalla,
)
from app.channels.local import LocalAdapter
from app.channels.mcp.adapter import McpStdioAdapter, npx_command
from app.channels.quota import QuotaLedger
from app.channels.resolver import CapabilityResolver
from app.config.settings import Settings
from app.policy.risk import Arbiter

logger = logging.getLogger(__name__)

#: adapter id -> (npx package, credential setting, environment variable)
MCP_SOURCES: dict[str, tuple[str, str | None, str]] = {
    "amap": ("amap-mcp-server", "amap_key", "AMAP_KEY"),
    "osm": ("@cyanheads/openstreetmap-mcp-server", None, ""),
    "rail12306": ("12306-mcp", None, ""),
    "variflight": ("@variflight-ai/variflight-mcp", "variflight_api_key", "VARIFLIGHT_API_KEY"),
    "meituan": ("@meituan-travel/ht-ai", "meituan_ht_token", "MEITUAN_HT_TOKEN"),
}

#: Sources that need a credential are only built when it is present. Sources
#: that need none would otherwise *always* join the chain and, because they run
#: through `npx`, stall the first turn while the package downloads. They stay
#: opt-in so the default path is fast and predictable.
KEYLESS_MCP_SOURCES = frozenset({"osm", "rail12306"})


def local_mcp_entry(settings: Settings, package: str) -> Path | None:
    """A locally installed copy of an MCP server, if there is one.

    Launching `npx` from a long-running server on Windows is unreliable: the
    shim is a batch file, the path contains a space, and a parent without a
    console sees the child exit immediately. A pinned local install removes all
    three variables at once -- and pins the version while it is at it.
    """

    root = Path(settings.data_dir) / "mcp" / "node_modules" / package
    manifest = root / "package.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    bin_field = data.get("bin")
    relative = bin_field if isinstance(bin_field, str) else (bin_field or {}).get(package)
    if not relative:
        return None
    entry = (root / relative).resolve()
    return entry if entry.is_file() else None


def mcp_launch(settings: Settings, package: str) -> tuple[str, list[str]]:
    """How to start one MCP server: local entry first, `npx` as the fallback."""

    entry = local_mcp_entry(settings, package)
    node = shutil.which("node") or "node"
    if entry is not None:
        return node, [str(entry)]
    return npx_command(), ["-y", package]


def build_adapters(settings: Settings) -> dict[str, ChannelAdapter]:
    """Construct every adapter this deployment can actually use."""

    adapters: dict[str, ChannelAdapter] = {
        "local": LocalAdapter(),
        "open_meteo": build_open_meteo(settings),
        # Keyless fallbacks. They are ordinary providers: if a keyed source is
        # configured it wins on priority, and these keep M1 working without one.
        "open_meteo_geocoding": build_open_meteo_geocoding(settings),
        # Valhalla outranks OSRM because it honours the travel mode (see the
        # note on build_osrm); OSRM stays as the second fallback for driving.
        "valhalla": build_valhalla(settings),
        "osrm": build_osrm(settings),
    }

    for adapter_id, (package, credential_field, env_name) in MCP_SOURCES.items():
        env: dict[str, str] = {}
        if credential_field is not None:
            credential = getattr(settings, credential_field, None)
            if not credential:
                # No credential -> provider absent -> clean degradation.
                continue
            env[env_name] = str(credential)
        elif adapter_id in KEYLESS_MCP_SOURCES and not getattr(settings, "enable_mcp_sources", False):
            continue
        command, args = mcp_launch(settings, package)
        adapters[adapter_id] = McpStdioAdapter(
            adapter_id=adapter_id,
            command=command,
            args=args,
            env=env,
            connect_timeout=getattr(settings, "mcp_connect_timeout_seconds", settings.channel_connect_timeout_seconds),
        )

    adapters.update(build_browser_adapters(settings))
    return adapters


def build_browser_adapters(settings: Settings) -> dict[str, ChannelAdapter]:
    """The browser channel, present only when a debug port is configured.

    Unlike the MCP sources this does not spawn anything: it attaches to a Chrome
    the *user* is already running, so their login state is available. That also
    means it is normally absent, and the adapter is built lazily -- the probe
    happens on the first call rather than at startup, so a missing browser costs
    one capability call's worth of latency instead of slowing every startup.
    """

    endpoint = getattr(settings, "browser_cdp_endpoint", None)
    if not endpoint:
        return {}

    from app.browser.adapter import BrowserAdapter
    from app.browser.cdp import CdpBrowserDriver

    driver = CdpBrowserDriver(
        endpoint=str(endpoint),
        target_url_contains=getattr(settings, "browser_target_url_contains", None),
    )
    return {"browser": BrowserAdapter(driver)}


def build_resolver(
    settings: Settings,
    registry: CapabilityRegistry,
    *,
    db: Any = None,
    adapters: dict[str, ChannelAdapter] | None = None,
    cache: CacheStore | None = None,
    quota: QuotaLedger | None = None,
) -> CapabilityResolver:
    adapters = adapters if adapters is not None else build_adapters(settings)

    if cache is None and db is not None:
        cache = CacheStore(db, coord_precision=settings.cache_coord_precision)
    if quota is None and db is not None:
        quota = QuotaLedger(
            db,
            limits={
                "amap_lbs": settings.quota_pool_amap_lbs,
                # The search pool is 30x smaller than the LBS pool, which is
                # exactly why it is accounted separately (framework §11).
                "amap_search": settings.quota_pool_amap_search,
                "variflight_points": settings.quota_pool_variflight_points,
            },
            warn_ratio=settings.quota_warn_ratio,
            degrade_ratio=settings.quota_degrade_ratio,
        )

    return CapabilityResolver(
        registry,
        adapters,
        cache=cache,
        quota=quota,
        arbiter=Arbiter(registry),
        retry_max_attempts=settings.channel_retry_max_attempts,
    )


__all__ = ["MCP_SOURCES", "build_adapters", "build_browser_adapters", "build_resolver"]
