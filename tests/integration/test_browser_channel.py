"""The browser channel inside the capability layer.

Two properties are worth an integration test rather than a unit one:

* an absent browser must not truncate the provider chain -- 12306 availability
  has to fall through to the MCP search, which is the whole reason the browser is
  listed *after* it in the registry;
* a browser that is present but whose click did nothing must surface as a hard
  error, because that is a side effect that did not happen.
"""

from __future__ import annotations

import pytest

from app.browser.adapter import BrowserAdapter
from app.browser.contract import BrowserError
from app.capabilities.defaults import build_default_registry
from app.channels.bootstrap import build_adapters, build_resolver
from app.config.settings import Settings
from app.policy.risk import Arbiter
from tests.unit.browser.fake_driver import FakeBrowserDriver, do_nothing, reveal
from tests.unit.channels.fakes import FakeAdapter, ok_result


def _settings(tmp_path, **overrides) -> Settings:
    settings = Settings(llm_offline=True, data_dir=tmp_path, **overrides)
    settings.ensure_dirs()
    return settings


def test_the_browser_adapter_is_built_from_settings(tmp_path) -> None:
    """A debug endpoint means the channel exists; absent means it does not."""

    with_browser = build_adapters(_settings(tmp_path, browser_cdp_endpoint="http://127.0.0.1:9222"))
    assert "browser" in with_browser

    without = build_adapters(_settings(tmp_path, browser_cdp_endpoint=""))
    assert "browser" not in without, "no endpoint means the channel must not appear at all"


class UnavailableDriver:
    """A driver that reports the browser as absent, the way the real one does.

    ``CdpBrowserDriver`` raises ``BrowserError(kind="unavailable")`` when the
    debug port is not listening; that path is what makes the capability report
    UNAVAILABLE so the resolver moves on.
    """

    id = "fake-browser"

    async def healthy(self) -> bool:
        return False

    async def observe(self, *, selectors=None):
        raise BrowserError("连接不到 Chrome 调试端口", kind="unavailable")

    async def navigate(self, url: str):
        raise BrowserError("连接不到 Chrome 调试端口", kind="unavailable")

    async def read(self, *, selector=None):
        raise BrowserError("连接不到 Chrome 调试端口", kind="unavailable")

    async def click(self, selector: str) -> None:
        raise BrowserError("连接不到 Chrome 调试端口", kind="unavailable")

    async def type(self, selector: str, text: str) -> None:
        raise BrowserError("连接不到 Chrome 调试端口", kind="unavailable")

    async def wait_for(self, selector: str, *, timeout_seconds: float = 10.0) -> bool:
        raise BrowserError("连接不到 Chrome 调试端口", kind="unavailable")

    async def screenshot(self):
        raise BrowserError("连接不到 Chrome 调试端口", kind="unavailable")

    async def aclose(self) -> None:
        return None


async def test_an_unavailable_browser_falls_through_to_the_next_provider(tmp_path) -> None:
    """The load-bearing ordering property.

    ``intercity.rail.availability`` binds the browser first and the 12306 MCP
    second. With no browser attached, the capability must still return the seat
    availability the MCP provider can give.
    """

    settings = _settings(tmp_path)
    registry = build_default_registry()

    mcp = FakeAdapter(
        adapter_id="rail12306",
        result=ok_result({"data": [{"start_train_code": "G1", "start_time": "06:00"}]}),
    )
    resolver = build_resolver(
        settings,
        registry,
        adapters={"browser": BrowserAdapter(UnavailableDriver()), "rail12306": mcp},
    )

    result = await resolver.call(
        "intercity.rail.availability",
        {"fromStation": "北京", "toStation": "上海", "date": "2026-10-03"},
    )

    assert mcp.calls, "the fallback provider must actually have been called"
    assert result.status.value == "ok", "the second provider's data must be returned as-is"
    assert result.data["data"][0]["start_train_code"] == "G1"


async def test_a_browser_that_did_nothing_is_recorded_as_a_verification_failure(tmp_path) -> None:
    """The fact that a side effect did not happen must survive resolution.

    A browser failure is an ERROR, and the resolver then tries other providers or
    degrades to self-service -- so what matters is that the *evidence* reaches the
    final result instead of being reduced to "browser: error".
    """

    settings = _settings(tmp_path)
    registry = build_default_registry()
    adapter = BrowserAdapter(FakeBrowserDriver(click_effect=do_nothing))
    resolver = build_resolver(settings, registry, adapters={"browser": adapter})

    result = await resolver.call(
        "browser.click",
        {"selector": "#submitOrder", "expect": {"page_changed": True}},
        call_context=_consented(),
    )

    assert result.ok is False, "a click that changed nothing must never read as ok"
    joined = " ".join(str(warning) for warning in result.warnings)
    assert "没有任何变化" in joined, f"the verification failure was lost: {result.warnings}"


async def test_a_side_effecting_capability_never_degrades_to_success(tmp_path) -> None:
    """``DEGRADED`` counts as ``ok``, so the ladder must not touch side effects.

    The ladder exists so a *read* stays useful without live data. Applying it to
    a failed booking would report success for an action that never happened -- and
    "go do it yourself" is not a degradation of "we did it for you".
    """

    settings = _settings(tmp_path)
    registry = build_default_registry()
    # No adapters at all: every L2 provider is absent.
    resolver = build_resolver(settings, registry, adapters={})

    result = await resolver.call(
        "booking.order.submit",
        {"order": "x"},
        call_context=_consented(),
    )

    assert result.ok is False
    assert result.status.value == "error"
    assert result.data is None, "no self-service fallback payload for an action"


async def test_a_read_only_capability_still_degrades(tmp_path) -> None:
    """The other half of the rule: reads keep their fallback."""

    settings = _settings(tmp_path)
    registry = build_default_registry()
    resolver = build_resolver(settings, registry, adapters={})

    result = await resolver.call("weather.forecast", {"latitude": 39.9, "longitude": 116.4})

    assert result.status.value in {"degraded", "unavailable"}
    assert result.ok is True or result.status.value == "unavailable"
    """L0 by design: reading availability is how the plan gets its numbers."""

    settings = _settings(tmp_path)
    registry = build_default_registry()
    driver = FakeBrowserDriver()
    await driver.navigate("https://kyfw.12306.cn/")
    driver.page.text = "G1 次 有票"
    resolver = build_resolver(settings, registry, adapters={"browser": BrowserAdapter(driver)})

    result = await resolver.call("browser.read", {"selector": "#tickets"})
    assert result.ok is True
    assert "有票" in result.data["text"]


async def test_a_side_effecting_click_without_consent_is_refused(tmp_path) -> None:
    """The arbiter, not the adapter, is what stops an unconsented click."""

    settings = _settings(tmp_path)
    registry = build_default_registry()
    driver = FakeBrowserDriver(click_effect=reveal)
    resolver = build_resolver(settings, registry, adapters={"browser": BrowserAdapter(driver)})

    result = await resolver.call("browser.click", {"selector": "#buy"})

    assert result.ok is False
    assert driver.calls == [] or all(name != "click" for name, _ in driver.calls), (
        "an unconsented side effect must not reach the page at all"
    )


def _consented():
    """A call context that says plan consent was granted, for the L1 path."""

    from app.channels.resolver import CallContext

    return CallContext(
        plan_consented=True,
        consent_ref="c1",
        has_action_consent=True,
        confirm_token_ok=True,
    )
