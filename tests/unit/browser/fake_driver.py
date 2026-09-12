"""A scriptable page, for testing the channel's safety properties.

Not a mock of the adapter -- a fake *driver*. Everything above it (the adapter's
observe/act/verify cycle, the slot lease, the capability plumbing) is the real
code under test; only the browser is replaced. That is the only way to assert
"a click that changed nothing is a failure" without a real page.

It can also be told to misbehave in the specific ways a real browser does:
a click that silently does nothing, a page whose text cannot be read, and a
background tab that reports no content.
"""

from __future__ import annotations

from typing import Any, Callable

from app.browser.contract import BrowserError, PageObservation

#: A selector that "exists" on the fake page. Anything starting with ``#absent``
#: does not, which is how a test asks for an unmet expectation.
def _present(selector: str) -> bool:
    return not selector.startswith("#absent") and not selector.startswith(".missing")


class FakePage:
    """One page's state, mutated by the primitives."""

    def __init__(self, *, url: str = "about:blank", title: str = "", text: str = "") -> None:
        self.url = url
        self.title = title
        self.text = text
        self.selectors: set[str] = set()
        self.typed: dict[str, str] = {}
        self.clicks: list[str] = []
        # Set by the driver: a page rendered in a background tab reports nothing,
        # and a click may or may not move the page.
        self._foreground: bool = True
        self._click_effect: Any = None

    def observe(self, selectors: list[str] | None = None) -> PageObservation:
        if not self._foreground:
            return PageObservation(
                url=self.url,
                title=self.title,
                text="",
                present={},
                foreground=False,
                unreadable=["后台标签页不渲染"],
            )
        return PageObservation(
            url=self.url,
            title=self.title,
            text=self.text,
            present={selector: _present(selector) for selector in (selectors or [])},
        )

    def click(self, selector: str) -> None:
        self.clicks.append(selector)
        if self._click_effect:
            self._click_effect(self, selector)


class FakeBrowserDriver:
    """A driver whose behaviour is declared up front.

    ``click_effect`` is the interesting knob: by default a click changes nothing,
    which is exactly the case a real browser produces when a selector matched
    something that is not the button the plan meant.
    """

    id = "fake-browser"

    def __init__(
        self,
        *,
        pages: dict[str, FakePage] | None = None,
        click_effect: Callable[[FakePage, str], None] | None = None,
        healthy: bool = True,
        navigate_fails: bool = False,
    ) -> None:
        self.page = FakePage()
        self.pages = pages or {}
        self.click_effect = click_effect
        self._healthy = healthy
        self._navigate_fails = navigate_fails
        self.calls: list[tuple[str, str]] = []
        self.closed = False
        self.page._click_effect = click_effect
        self.page._foreground = True

    # --------------------------------------------------------------- contract
    async def healthy(self) -> bool:
        return self._healthy

    async def observe(self, *, selectors: list[str] | None = None) -> PageObservation:
        self.calls.append(("observe", ""))
        if self.closed:
            raise BrowserError("浏览器已关闭", kind="unavailable")
        return self.page.observe(selectors)

    async def navigate(self, url: str) -> PageObservation:
        self.calls.append(("navigate", url))
        if self._navigate_fails:
            raise BrowserError(f"无法打开 {url}", kind="network")
        target = self.pages.get(url)
        if target is None:
            target = FakePage(url=url, title=url)
            target._click_effect = self.click_effect
        target._foreground = True
        self.page = target
        return self.page.observe()

    async def read(self, *, selector: str | None = None) -> str:
        self.calls.append(("read", selector or ""))
        return self.page.text if selector is None else self.page.typed.get(selector, self.page.text)

    async def click(self, selector: str) -> None:
        self.calls.append(("click", selector))
        self.page.click(selector)

    async def type(self, selector: str, text: str) -> None:
        self.calls.append(("type", selector))
        self.page.typed[selector] = text
        # A real input keeps the text in a field rather than the page body, which
        # is why typing alone does not change the digest of the visible text.
        if self.click_effect is None and False:  # pragma: no cover - documented intent
            self.page.text += text

    async def wait_for(self, selector: str, *, timeout_seconds: float = 10.0) -> bool:
        self.calls.append(("wait", selector))
        return _present(selector) and selector in self.page.selectors or _present(selector)

    async def screenshot(self) -> bytes | None:
        self.calls.append(("screenshot", ""))
        return b"\x89PNG\r\n\x1a\n fake"

    async def aclose(self) -> None:
        self.closed = True


def reveal(self: FakePage, selector: str) -> None:
    """A click effect that makes the page visibly move -- the happy path."""

    self.selectors.add(selector)
    self.text = f"{self.text}\n已提交" if "submit" in selector else self.text


def do_nothing(self: FakePage, selector: str) -> None:
    """A click that returns without changing anything: the lie to guard against."""

    return None


def to_background(self: FakePage, selector: str) -> None:
    """A click that opens a new tab and leaves the old one unrendered."""

    self._foreground = False


__all__ = ["FakeBrowserDriver", "FakePage", "do_nothing", "reveal", "to_background"]
