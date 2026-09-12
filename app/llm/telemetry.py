"""LLM call telemetry (framework §11.3.6).

Answers the question "was the model actually called?" from the service itself
rather than from inference. Also the raw input for the budget guard (DN-4).
"""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.domain.timebase import now_local


@dataclass
class LLMTelemetry:
    total: int = 0
    failures: int = 0
    by_client: Counter[str] = field(default_factory=Counter)
    by_node: Counter[str] = field(default_factory=Counter)
    input_tokens: int = 0
    output_tokens: int = 0
    last_at: datetime | None = None
    last_error: str | None = None
    last_key_source: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(
        self,
        *,
        node: str,
        client: str,
        ok: bool = True,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self.total += 1
            self.by_client[client] += 1
            self.by_node[node] += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.last_at = now_local()
            if not ok:
                self.failures += 1
                self.last_error = (error or "unknown")[:200]

    def note_key_source(self, source: str | None) -> None:
        with self._lock:
            self.last_key_source = source

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self.failures = 0
            self.by_client.clear()
            self.by_node.clear()
            self.input_tokens = 0
            self.output_tokens = 0
            self.last_at = None
            self.last_error = None
            self.last_key_source = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total": self.total,
                "failures": self.failures,
                "by_client": dict(self.by_client),
                "by_node": dict(self.by_node),
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "last_at": self.last_at.isoformat() if self.last_at else None,
                "last_error": self.last_error,
                "last_key_source": self.last_key_source,
            }


#: Process-wide counter. Single-process (v1) by design; a multi-worker
#: deployment would need to move this into the store.
TELEMETRY = LLMTelemetry()
