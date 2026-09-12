"""Per-session event fan-out for the SSE downlink (framework §4.3, L2).

Two properties the protocol depends on:

* **Monotonic sequence numbers.** ``Event.id`` is the reconnect cursor (``GET
  /events?since=<id>``), so a subscriber that reconnects must be able to ask for
  "everything after 12" without gaps or duplicates.
* **Replay is not consumption.** A reconnect replays a suffix of the same
  ordered log to any number of readers. Draining the buffer on the first read
  would make a second browser tab -- or a retry after a dropped connection --
  silently miss history (DJ-2).

Persisted events live in the store; this buffer is the in-process fast path for
the session that is currently active. Retention is bounded so a long session
cannot grow without limit.
"""

from __future__ import annotations

import asyncio
from collections import deque
from threading import Lock
from typing import Any, Deque, Iterable

from app.events.types import Event

DEFAULT_RETENTION = 1000

#: Bounded on purpose: a slow SSE reader must not let an unbounded queue grow.
#: Overflow drops the oldest item, and the stream says so rather than silently
#: skipping -- a client that misses deltas still has the final rendered text.
DEFAULT_SUBSCRIBER_QUEUE = 512


class EventBus:
    def __init__(self, *, retention: int = DEFAULT_RETENTION) -> None:
        self._retention = max(1, int(retention))
        self._buffers: dict[str, Deque[Event]] = {}
        self._seq: dict[str, int] = {}
        #: session_id -> live subscriber queues. Deltas are only useful while a
        #: reader is attached, so nothing accumulates when nobody is watching.
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._lock = Lock()

    # ------------------------------------------------------------- publishing
    def publish(self, session_id: str, events: Iterable[Event]) -> int:
        """Append events, assigning sequence numbers where the caller has none.

        Returns the highest sequence number now known for the session. An event
        that already carries an id (one that came back from the store) keeps it,
        so the SSE cursor never disagrees with ``GET /events``.
        """

        with self._lock:
            buffer = self._buffers.setdefault(session_id, deque(maxlen=self._retention))
            seq = self._seq.get(session_id, 0)
            for event in events:
                if event.id and event.id > seq:
                    seq = event.id
                elif not event.id:
                    seq += 1
                    event = event.model_copy(update={"id": seq})
                buffer.append(event)
                self._fanout(session_id, event)
            self._seq[session_id] = seq
            return seq

    def _fanout(self, session_id: str, event: Event) -> None:
        """Hand one event to every live reader, dropping the oldest on overflow."""

        queues = self._subscribers.get(session_id)
        if not queues:
            return
        for queue in list(queues):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                    pass

    # ---------------------------------------------------------- subscriptions
    def subscribe(self, session_id: str, *, maxsize: int = DEFAULT_SUBSCRIBER_QUEUE) -> asyncio.Queue:
        """Attach a live reader.

        Subscribing does not replay: the caller asks for the backlog separately
        with :meth:`replay`, so the boundary between "what already happened" and
        "what happens next" is explicit and cannot be duplicated.
        """

        queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, int(maxsize)))
        with self._lock:
            self._subscribers.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            queues = self._subscribers.get(session_id)
            if not queues:
                return
            self._subscribers[session_id] = [item for item in queues if item is not queue]
            if not self._subscribers[session_id]:
                self._subscribers.pop(session_id, None)

    def subscriber_count(self, session_id: str) -> int:
        with self._lock:
            return len(self._subscribers.get(session_id, ()))


    # --------------------------------------------------------------- reading
    def replay(self, session_id: str, *, since: int = 0) -> list[Event]:
        """Ordered suffix of the log, for any number of readers."""

        with self._lock:
            buffer = self._buffers.get(session_id)
            if not buffer:
                return []
            return [event for event in buffer if event.id > since]

    def last_seq(self, session_id: str) -> int:
        with self._lock:
            return self._seq.get(session_id, 0)

    def sessions(self) -> list[str]:
        with self._lock:
            return sorted(self._buffers)

    def drop(self, session_id: str) -> None:
        """Forget a session; the store remains the durable copy (DI-1)."""

        with self._lock:
            self._buffers.pop(session_id, None)
            self._seq.pop(session_id, None)
            self._subscribers.pop(session_id, None)


__all__ = ["DEFAULT_RETENTION", "DEFAULT_SUBSCRIBER_QUEUE", "EventBus"]
