"""Upstream request de-duplication (DJ-6).

Clicking "approve" twice must not resume the graph twice.
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any


class RequestDedup:
    def __init__(self, window_seconds: float = 600.0) -> None:
        self._window = window_seconds
        self._seen: dict[str, tuple[float, Any]] = {}
        self._lock = Lock()

    def check(self, request_id: str | None) -> Any | None:
        if not request_id:
            return None
        now = time.time()
        with self._lock:
            self._gc(now)
            entry = self._seen.get(request_id)
            return entry[1] if entry else None

    def remember(self, request_id: str | None, result: Any) -> None:
        if not request_id:
            return
        now = time.time()
        with self._lock:
            self._gc(now)
            self._seen[request_id] = (now, result)

    def _gc(self, now: float) -> None:
        expired = [k for k, (ts, _) in self._seen.items() if now - ts > self._window]
        for key in expired:
            self._seen.pop(key, None)
