"""Where streamed text goes (framework §10, DELTA events).

The LLM client does not know about SSE, the event bus or the graph. It calls one
async function per chunk. This module owns that function, which keeps the
transport decision -- "publish it now" versus "collect it for the turn's
returned event list" -- in one place instead of inside every node.

The sink is **per turn** and held on ``GraphDeps``, never in ``AgentState``:
state is serialised into the checkpoint, and a sink holds an asyncio queue.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

#: ``(data) -> None``; awaited for every chunk.
DeltaHandler = Callable[[dict[str, Any]], Awaitable[None]]


class DeltaSink:
    """Collects and forwards LLM text deltas for one turn.

    Deltas are *not* persisted (``REPLAYABLE_EVENT_TYPES`` excludes them): a
    reconnect rebuilds from a snapshot, so replaying half a sentence would be
    worse than useless. The sink therefore keeps only what the current turn needs
    and forgets it afterwards.
    """

    def __init__(self, handler: DeltaHandler | None = None) -> None:
        self._handler = handler
        self._per_node: dict[str, int] = {}
        self._chunks: dict[str, list[str]] = {}

    def begin(self, node: str) -> None:
        """Reset the per-node sequence so a repair attempt does not interleave."""

        self._per_node[node] = 0
        self._chunks.setdefault(node, [])

    async def delta(self, node: str, text: str | None) -> None:
        """Forward one chunk.

        ``None`` is the *reset* signal: the model's previous attempt is being
        thrown away for a repair, so everything already sent for this node must
        be discarded. It is emitted as an explicit event rather than left to the
        client to infer, because a client that keeps half of a rejected answer
        would render text the system never accepted.
        """

        if text is None:
            self._chunks[node] = []
            self._per_node[node] = self._per_node.get(node, 0) + 1
            if self._handler is not None:
                await self._handler(
                    {"node": node, "seq": self._per_node[node], "text": "", "done": False, "reset": True}
                )
            return
        if not text:
            return
        seq = self._per_node.get(node, 0)
        self._per_node[node] = seq + 1
        self._chunks.setdefault(node, []).append(text)
        if self._handler is not None:
            await self._handler({"node": node, "seq": seq, "text": text, "done": False, "reset": False})

    async def finish(self, node: str, *, ok: bool, replaced: bool = False) -> None:
        """Close a node's stream.

        ``ok=False`` or ``replaced=True`` tells the client that whatever it
        received for this node must be discarded -- the visible truth is the
        node's final structured output, not the raw stream (which is JSON).
        """

        if self._handler is not None:
            await self._handler(
                {
                    "node": node,
                    "seq": self._per_node.get(node, 0),
                    "text": "",
                    "done": True,
                    "ok": ok,
                    "replaced": replaced,
                }
            )

    def text_for(self, node: str) -> str:
        return "".join(self._chunks.get(node, []))

    def seen(self, node: str) -> bool:
        return bool(self._chunks.get(node))


__all__ = ["DeltaSink", "DeltaHandler"]
