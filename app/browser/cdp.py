"""Chrome DevTools Protocol driver (framework §7).

Why CDP and not Playwright: the five primitives need little more than
``Runtime.evaluate``, ``websockets`` is already a dependency (through
``uvicorn[standard]``), and adding a browser-automation framework to a free-tier
personal project is a supply-chain and licensing decision this feature does not
justify.

Why a debug port and not an extension: connecting to a *running* Chrome means the
user's own login state is reused, which is the whole point -- 12306 availability
and attraction reservations live behind a session nobody can script around. The
cost is that the user has to start Chrome with ``--remote-debugging-port``, and
the connection is therefore absent by default. That is the honest trade: the
channel degrades cleanly instead of pretending to work.

What this driver does **not** do: decide whether an action was allowed (the
registry and arbiter do), or decide whether it worked (the adapter verifies it
against the page).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.browser.contract import BrowserError, PageObservation

#: Milliseconds the page is given to finish loading before we read it. CDP has
#: proper lifecycle events; polling the readyState is far less code and enough
#: for the pages this channel touches.
DEFAULT_READY_TIMEOUT_MS = 8000

#: Cap on the text we pull out of the page. The observation travels into events
#: and the audit log, so an unbounded dump of a train timetable would be costly
#: and useless.
MAX_TEXT_CHARS = 20000


class CdpBrowserDriver:
    """One attached page, driven over the DevTools Protocol."""

    id = "chrome-cdp"

    def __init__(
        self,
        *,
        endpoint: str = "http://127.0.0.1:9222",
        target_url_contains: str | None = None,
        ready_timeout_ms: int = DEFAULT_READY_TIMEOUT_MS,
        max_text_chars: int = MAX_TEXT_CHARS,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._target_hint = target_url_contains
        self._ready_timeout_ms = max(0, int(ready_timeout_ms))
        self._max_text_chars = max(200, int(max_text_chars))
        self._ws: Any = None
        self._next_id = 0
        self._target: dict[str, Any] | None = None

    # ------------------------------------------------------------- discovery
    async def _list_targets(self) -> list[dict[str, Any]]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=4.0) as http:
                response = await http.get(f"{self._endpoint}/json/list")
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            raise BrowserError(
                f"连接不到 Chrome 调试端口 {self._endpoint}：{type(exc).__name__}"
                "（需要以 --remote-debugging-port=9222 启动）",
                kind="unavailable",
            ) from exc
        pages = [
            item
            for item in payload
            if isinstance(item, dict) and item.get("type") == "page" and item.get("webSocketDebuggerUrl")
        ]
        if not pages:
            raise BrowserError("Chrome 没有可用的页面标签", kind="unavailable")
        return pages

    def _pick_target(self, pages: list[dict[str, Any]]) -> dict[str, Any]:
        if self._target_hint:
            for page in pages:
                if self._target_hint in str(page.get("url") or ""):
                    return page
        return pages[0]

    async def _ensure_connected(self) -> None:
        if self._ws is not None:
            return
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - dependency is present
            raise BrowserError("缺少 websockets 依赖", kind="unavailable") from exc

        pages = await self._list_targets()
        target = self._pick_target(pages)
        try:
            self._ws = await websockets.connect(
                str(target["webSocketDebuggerUrl"]),
                max_size=None,
                open_timeout=6.0,
            )
        except Exception as exc:
            raise BrowserError(f"无法连接页面调试通道：{type(exc).__name__}", kind="unavailable") from exc
        self._target = target

    async def _send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """One CDP call, matched to its reply by id.

        Events are skipped rather than queued: this driver only issues requests,
        and buffering unsolicited events would grow without bound on a chatty
        page.
        """

        await self._ensure_connected()
        self._next_id += 1
        message_id = self._next_id
        payload = {"id": message_id, "method": method}
        if params:
            payload["params"] = params
        try:
            await self._ws.send(json.dumps(payload))
        except Exception as exc:
            self._ws = None
            raise BrowserError(f"调试通道已断开：{type(exc).__name__}", kind="network") from exc

        while True:
            raw = await self._ws.recv()
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise BrowserError(
                    f"CDP {method} 失败：{message['error'].get('message')}", kind="browser"
                )
            return message.get("result") or {}

    # ------------------------------------------------------------- primitives
    async def healthy(self) -> bool:
        try:
            if self._ws is not None:
                await self._send("Runtime.evaluate", {"expression": "1", "returnByValue": True})
            else:
                await self._list_targets()
        except BrowserError:
            return False
        except Exception:  # pragma: no cover - defensive
            return False
        return True

    async def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        result = await self._send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )
        if result.get("exceptionDetails"):
            raise BrowserError(
                str(result["exceptionDetails"].get("text") or "页面脚本报错"), kind="browser"
            )
        return (result.get("result") or {}).get("value")

    async def observe(self, *, selectors: list[str] | None = None) -> PageObservation:
        url = str(await self.evaluate("location.href") or "")
        title = str(await self.evaluate("document.title") or "")
        text = str(
            await self.evaluate(
                "(() => { const t = document.body ? document.body.innerText : '';"
                f" return t.slice(0, {self._max_text_chars}); }})()"
            )
            or ""
        )

        present: dict[str, bool] = {}
        unreadable: list[str] = []
        for selector in selectors or []:
            try:
                found = await self.evaluate(
                    "(() => { try { return !!document.querySelector(%s); } catch (e) { return null; } })()"
                    % json.dumps(selector)
                )
            except BrowserError:
                found = None
            if found is None:
                # A selector the page cannot evaluate (bad syntax, or a frame we
                # cannot reach) is *unreadable*, not absent: reporting it as
                # absent would fail an expectation for the wrong reason.
                unreadable.append(f"{selector} 无法判定")
                present[selector] = False
            else:
                present[selector] = bool(found)

        return PageObservation(
            url=url, title=title, text=text, present=present, foreground=True, unreadable=unreadable
        )

    async def navigate(self, url: str) -> PageObservation:
        await self._send("Page.enable")
        await self._send("Page.navigate", {"url": url})
        await self._await_ready()
        return await self.observe()

    async def _await_ready(self) -> None:
        # Polling readyState: the pages here are documents, not SPA route
        # transitions, and this avoids holding a CDP event subscription open.
        waited = 0
        step = 200
        while waited <= self._ready_timeout_ms:
            try:
                state = await self.evaluate("document.readyState")
            except BrowserError:
                state = None
            if state in {"interactive", "complete"}:
                return
            await asyncio.sleep(step / 1000)
            waited += step

    async def read(self, *, selector: str | None = None) -> str:
        if selector is None:
            return str(await self.evaluate("document.body ? document.body.innerText : ''") or "")
        value = await self.evaluate(
            "(() => { const el = document.querySelector(%s);"
            " return el ? (el.innerText || el.textContent || '') : null; })()" % json.dumps(selector)
        )
        if value is None:
            raise BrowserError(f"元素不存在：{selector}", kind="business")
        return str(value)

    async def click(self, selector: str) -> None:
        # Scrolls into view first: a click on an off-screen element is silently
        # ignored by the page, which would look exactly like a selector that
        # matched the wrong node.
        clicked = await self.evaluate(
            "(() => { const el = document.querySelector(%s); if (!el) return false;"
            " el.scrollIntoView({block: 'center'}); el.click(); return true; })()"
            % json.dumps(selector)
        )
        if not clicked:
            raise BrowserError(f"元素不存在，无法点击：{selector}", kind="business")

    async def type(self, selector: str, text: str) -> None:
        done = await self.evaluate(
            "(() => { const el = document.querySelector(%s); if (!el) return false;"
            " el.focus(); el.value = %s;"
            " el.dispatchEvent(new Event('input', {bubbles: true}));"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " return true; })()" % (json.dumps(selector), json.dumps(text))
        )
        if not done:
            raise BrowserError(f"元素不存在，无法输入：{selector}", kind="business")

    async def wait_for(self, selector: str, *, timeout_seconds: float = 10.0) -> bool:
        deadline = max(0.0, float(timeout_seconds))
        waited = 0.0
        step = 0.25
        while waited <= deadline:
            found = await self.evaluate(
                "(() => { try { return !!document.querySelector(%s); } catch (e) { return false; } })()"
                % json.dumps(selector)
            )
            if found:
                return True
            await asyncio.sleep(step)
            waited += step
        return False

    async def screenshot(self) -> bytes | None:
        import base64

        result = await self._send("Page.captureScreenshot", {"format": "png"})
        data = result.get("data")
        if not data:
            return None
        try:
            return base64.b64decode(data)
        except Exception:  # pragma: no cover - defensive
            return None

    async def aclose(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # pragma: no cover - closing must not raise
                pass
            self._ws = None


__all__ = ["DEFAULT_READY_TIMEOUT_MS", "MAX_TEXT_CHARS", "CdpBrowserDriver"]
