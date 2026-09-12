"""Browser channel (framework §7). Optional everywhere: absent driver, clean degradation."""

from app.browser.adapter import BrowserAdapter
from app.browser.contract import (
    PRIMITIVES,
    BrowserDriver,
    BrowserError,
    Expectation,
    PageObservation,
    Verification,
    verify,
)
from app.browser.slot import PageSlotLease, SlotConflict

__all__ = [
    "PRIMITIVES",
    "BrowserAdapter",
    "BrowserDriver",
    "BrowserError",
    "Expectation",
    "PageObservation",
    "PageSlotLease",
    "SlotConflict",
    "Verification",
    "verify",
]
