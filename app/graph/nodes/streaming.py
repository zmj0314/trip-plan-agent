"""Node-side helper for streaming LLM text (framework §10).

Keeps nodes from knowing how a delta reaches the client: they ask for a
callback and hand it to the LLM client. When there is no sink -- offline runs,
tests, or a deployment with streaming off -- the callback is simply absent and
the call behaves exactly as before.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from app.graph.deps import GraphDeps
from app.llm.streaming import DeltaSink

#: Nodes whose output is *prose the user reads*. Extraction nodes are excluded on
#: purpose: streaming their raw JSON would show the user a half-built object that
#: the system may reject, which is noise dressed up as progress.
STREAMING_NODES = frozenset({"preview_render", "remind", "itinerary_render"})


def sink_for(deps: GraphDeps) -> DeltaSink | None:
    sink = deps.extras.get("delta_sink") if deps.extras else None
    return sink if isinstance(sink, DeltaSink) else None


def stream_callback(deps: GraphDeps, node: str) -> Callable[[str | None], Awaitable[None]] | None:
    """The ``on_delta`` callback for one node, or ``None`` when not streaming."""

    sink = sink_for(deps)
    if sink is None or node not in STREAMING_NODES:
        return None
    sink.begin(node)

    async def _forward(text: str | None) -> None:
        await sink.delta(node, text)

    return _forward


async def close_stream(deps: GraphDeps, node: str, *, ok: bool, replaced: bool = False) -> None:
    sink = sink_for(deps)
    if sink is not None and sink.seen(node):
        await sink.finish(node, ok=ok, replaced=replaced)


__all__ = ["STREAMING_NODES", "close_stream", "sink_for", "stream_callback"]
