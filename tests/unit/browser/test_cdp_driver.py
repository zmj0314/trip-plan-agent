"""The CDP driver against a real websocket peer.

A stub Chrome is served on localhost, so this exercises the actual driver code --
message framing, reply matching, event skipping, error translation -- rather than
a mock of it. What it cannot prove is that Chrome behaves like the stub; that
needs a browser, and is documented as unverified rather than implied by a green
test.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.browser.cdp import CdpBrowserDriver
from app.browser.contract import BrowserError

pytest.importorskip("websockets")

import websockets  # noqa: E402


class StubChrome:
    """Answers CDP commands, and records what it was asked."""

    def __init__(
        self,
        *,
        replies: dict[str, object] | None = None,
        errors: dict[str, str] | None = None,
        interleave_events: bool = True,
    ) -> None:
        self.replies = replies or {}
        self.errors = errors or {}
        self.interleave_events = interleave_events
        self.seen: list[tuple[str, dict]] = []
        self._server: object | None = None
        self.port: int | None = None

    async def start(self) -> str:
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, websocket) -> None:
        async for raw in websocket:
            message = json.loads(raw)
            method = str(message.get("method") or "")
            self.seen.append((method, dict(message.get("params") or {})))
            if method in self.errors:
                await websocket.send(
                    json.dumps({"id": message["id"], "error": {"message": self.errors[method]}})
                )
                continue
            if method in self.replies:
                payload = self.replies[method]
                value = payload(**message.get("params", {})) if callable(payload) else payload
            else:
                value = {}
            if self.interleave_events:
                # Real CDP interleaves unsolicited events; the driver must skip
                # them rather than mistake one for its reply.
                await websocket.send(json.dumps({"method": "Page.loadEventFired", "params": {}}))
            await websocket.send(json.dumps({"id": message["id"], "result": value}))


def _evaluate_value(table: dict[str, object]):
    """Reply to ``Runtime.evaluate`` by matching on the expression."""

    def _reply(expression: str = "", **_):
        for needle, value in table.items():
            if needle in expression:
                if isinstance(value, Exception):
                    raise value
                return {"result": {"type": "string", "value": value}}
        return {"result": {"type": "undefined"}}

    return _reply


@pytest.fixture
async def chrome():
    server = StubChrome()
    url = await server.start()
    try:
        yield server, url
    finally:
        await server.stop()


async def test_it_reads_the_page_over_the_protocol(chrome) -> None:
    server, url = chrome
    server.replies["Runtime.evaluate"] = _evaluate_value(
        {
            "location.href": "https://kyfw.12306.cn/otn/leftTicket/init",
            "document.title": "车票预订",
            "document.body.innerText": "G1 次 有票",
            "document.querySelector": True,
            "document.readyState": "complete",
        }
    )
    driver = CdpBrowserDriver()
    driver._ws = await _connect(driver, url)
    driver._list_targets = lambda: _no_targets()  # type: ignore[assignment]

    observation = await driver.observe(selectors=["#query_ticket"])

    assert "有票" in observation.text
    assert observation.present == {"#query_ticket": True}
    assert observation.unreadable == []


async def test_unsolicited_events_are_skipped(chrome) -> None:
    """A driver that mistook an event for a reply would hang or misparse."""

    server, url = chrome
    server.replies["Runtime.evaluate"] = _evaluate_value({"1": 1})
    driver = CdpBrowserDriver()
    driver._ws = await _connect(driver, url)

    assert await driver.healthy() is True


async def test_a_cdp_error_becomes_a_browser_error(chrome) -> None:
    server, url = chrome
    server.errors["Runtime.evaluate"] = "Cannot find context"
    driver = CdpBrowserDriver()
    driver._ws = await _connect(driver, url)

    with pytest.raises(BrowserError) as excinfo:
        await driver.evaluate("document.title")
    assert "Cannot find context" in str(excinfo.value)
    assert excinfo.value.kind == "browser"


async def test_a_page_script_exception_is_reported_not_swallowed(chrome) -> None:
    server, url = chrome
    server.replies["Runtime.evaluate"] = {"exceptionDetails": {"text": "SyntaxError"}}
    driver = CdpBrowserDriver()
    driver._ws = await _connect(driver, url)

    with pytest.raises(BrowserError) as excinfo:
        await driver.evaluate("!!!")
    assert "SyntaxError" in str(excinfo.value)


async def test_clicking_a_missing_element_is_a_business_error(chrome) -> None:
    server, url = chrome
    server.replies["Runtime.evaluate"] = _evaluate_value({"document.querySelector": False})
    driver = CdpBrowserDriver()
    driver._ws = await _connect(driver, url)

    with pytest.raises(BrowserError) as excinfo:
        await driver.click("#buy")
    assert excinfo.value.kind == "business"


async def test_reading_a_missing_element_is_a_business_error(chrome) -> None:
    server, url = chrome
    server.replies["Runtime.evaluate"] = _evaluate_value({"document.querySelector": None})
    driver = CdpBrowserDriver()
    driver._ws = await _connect(driver, url)

    with pytest.raises(BrowserError):
        await driver.read(selector="#tickets")


async def test_typing_dispatches_input_events(chrome) -> None:
    """Assigning ``value`` alone leaves frameworks unaware the field changed."""

    server, url = chrome
    server.replies["Runtime.evaluate"] = _evaluate_value({"document.querySelector": True})
    driver = CdpBrowserDriver()
    driver._ws = await _connect(driver, url)

    await driver.type("#from", "北京南")

    expressions = [params.get("expression", "") for name, params in server.seen if name == "Runtime.evaluate"]
    assert any("dispatchEvent(new Event('input'" in item for item in expressions)


async def test_a_navigation_waits_for_the_document(chrome) -> None:
    server, url = chrome
    server.replies["Page.navigate"] = {"frameId": "f1"}
    server.replies["Runtime.evaluate"] = _evaluate_value(
        {"document.readyState": "complete", "location.href": "https://x/", "document.title": "X"}
    )
    driver = CdpBrowserDriver()
    driver._ws = await _connect(driver, url)

    observation = await driver.navigate("https://x/")

    methods = [name for name, _ in server.seen]
    assert "Page.enable" in methods
    assert "Page.navigate" in methods
    assert observation.url == "https://x/"


async def test_the_debug_port_being_absent_degrades_as_unavailable() -> None:
    """The default state: Chrome was not started with a debug port."""

    driver = CdpBrowserDriver(endpoint="http://127.0.0.1:1", ready_timeout_ms=100)
    with pytest.raises(BrowserError) as excinfo:
        await driver.healthy() or await driver._list_targets()
    assert excinfo.value.kind == "unavailable"


async def test_health_is_false_rather_than_raising_when_chrome_is_gone() -> None:
    driver = CdpBrowserDriver(endpoint="http://127.0.0.1:1")
    assert await driver.healthy() is False


async def _connect(driver: CdpBrowserDriver, url: str):
    import websockets

    return await websockets.connect(url, max_size=None)


async def _no_targets():
    return []
