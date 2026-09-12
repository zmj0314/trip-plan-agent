"""Browser channel: the driver contract and its vocabulary (framework §7).

The browser is the fallback for everything without an API -- attraction
reservations, ride hailing, and 12306 availability, which must be read from the
page rather than an endpoint. It is also the only channel whose "success" can be
a lie: a click that returned HTTP 200 while the page did nothing is a failure,
and treating it as success would let the agent tell the user a booking was made.

So the contract is built around **observation**, not intent:

* every mutating primitive declares the ``expect_*`` fields that must become true;
* the adapter verifies them against the page *after* acting;
* an unmet expectation is a failed action with the evidence attached, never a
  success with a caveat.

Deliberately no Playwright: ``websockets`` is already a dependency (via
``uvicorn[standard]``), and raw CDP is enough for the five primitives. Adding a
browser-automation framework to a free-tier personal project would be a much
larger supply-chain and licensing decision than this feature warrants.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

#: The five primitives. Kept small on purpose: anything larger becomes an
#: automation framework, and the risk of an unbounded action vocabulary is that
#: nobody can say what the agent is allowed to do.
PRIMITIVES = ("navigate", "read", "click", "type", "wait")


class PageSlotState(StrEnum):
    LEASED = "leased"
    RELEASED = "released"
    EXPIRED = "expired"


class PageObservation(BaseModel):
    """What the page looks like right now.

    This is the only thing the verifier trusts. A driver that cannot produce an
    observation cannot claim an action succeeded.
    """

    url: str = ""
    title: str = ""
    #: Visible text, truncated. Enough to look for a marker without shipping an
    #: entire DOM through the event stream.
    text: str = ""
    #: Selector -> presence, for the specific things the action cares about.
    present: dict[str, bool] = Field(default_factory=dict)
    #: True when the tab is actually rendering. A background tab in a SPA often
    #: reports empty content, which would make every expectation fail.
    foreground: bool = True
    #: Anything the driver could not look at (missing selector, blocked frame).
    unreadable: list[str] = Field(default_factory=list)

    def digest(self) -> str:
        """A cheap fingerprint, used to tell "the page changed" from "it did not"."""

        import hashlib

        payload = f"{self.url}|{self.title}|{self.text[:2000]}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class Expectation(BaseModel):
    """What must be true *after* an action, for it to count as done.

    All fields are optional; an action with no expectations can only be verified
    as "no exception", which the adapter reports as unverified rather than
    successful.
    """

    #: Substring that must appear in the page text.
    text_contains: str | None = None
    #: Selector that must be present.
    selector_present: str | None = None
    #: Selector that must be absent (the modal closed, the button disappeared).
    selector_absent: str | None = None
    #: URL substring, for a navigation that lands somewhere specific.
    url_contains: str | None = None
    #: The page must differ from the observation taken before the action.
    page_changed: bool = False

    @property
    def any(self) -> bool:
        return any(
            (
                self.text_contains,
                self.selector_present,
                self.selector_absent,
                self.url_contains,
                self.page_changed,
            )
        )


class Verification(BaseModel):
    """The result of checking one action against the page."""

    verified: bool = False
    checked: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    #: True when there was nothing to check. Reported so a caller cannot mistake
    #: "we did not look" for "it worked".
    unverified: bool = False

    @property
    def ok(self) -> bool:
        """True when nothing contradicted the expectation.

        "Unverified" counts as ok *at this level* on purpose: a read-only
        primitive has nothing to prove. The distinction is carried by
        ``verified``, and the adapter is what refuses to call an unchecked
        mutation a success.
        """

        return not self.failures


class BrowserDriver(Protocol):
    """One attached browser. Implementations: CDP (real) and a fake for tests."""

    id: str

    async def healthy(self) -> bool: ...

    async def observe(self, *, selectors: list[str] | None = None) -> PageObservation: ...

    async def navigate(self, url: str) -> PageObservation: ...

    async def read(self, *, selector: str | None = None) -> str: ...

    async def click(self, selector: str) -> None: ...

    async def type(self, selector: str, text: str) -> None: ...

    async def wait_for(self, selector: str, *, timeout_seconds: float = 10.0) -> bool: ...

    async def screenshot(self) -> bytes | None: ...

    async def aclose(self) -> None: ...


class BrowserError(RuntimeError):
    """A browser step that could not be carried out at all."""

    def __init__(self, message: str, *, kind: str = "browser") -> None:
        super().__init__(message)
        self.kind = kind


def verify(expectation: Expectation, before: PageObservation, after: PageObservation) -> Verification:
    """Check an expectation against the page, and say exactly what failed.

    Pure: the caller supplies both observations, which is what makes the safety
    property unit-testable without a browser.
    """

    if not expectation.any:
        return Verification(
            verified=False,
            unverified=True,
            failures=[],
            checked=[],
        )

    failures: list[str] = []
    checked: list[str] = []

    if expectation.url_contains is not None:
        checked.append("url_contains")
        if expectation.url_contains not in after.url:
            failures.append(f"URL 未包含 {expectation.url_contains!r}（当前 {after.url!r}）")

    if expectation.text_contains is not None:
        checked.append("text_contains")
        if expectation.text_contains not in after.text:
            failures.append(f"页面文本未出现 {expectation.text_contains!r}")
        elif after.unreadable:
            # Text was found, but part of the page could not be read: the finding
            # may be a coincidence of what was legible, so it is not a pass.
            failures.append(f"页面有不可读区域，文本判定不可靠：{'、'.join(after.unreadable[:3])}")

    if expectation.selector_present is not None:
        checked.append("selector_present")
        if not after.present.get(expectation.selector_present, False):
            failures.append(f"元素未出现：{expectation.selector_present}")

    if expectation.selector_absent is not None:
        checked.append("selector_absent")
        if after.present.get(expectation.selector_absent, False):
            failures.append(f"元素仍然存在：{expectation.selector_absent}")

    if expectation.page_changed:
        checked.append("page_changed")
        if before.digest() == after.digest():
            failures.append("页面没有任何变化（点击可能没生效）")

    return Verification(verified=not failures, checked=checked, failures=failures)


__all__ = [
    "PRIMITIVES",
    "BrowserDriver",
    "BrowserError",
    "Expectation",
    "PageObservation",
    "PageSlotState",
    "Verification",
    "verify",
]
