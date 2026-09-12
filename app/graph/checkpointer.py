"""Checkpointer wiring (framework §5.5).

Checkpoints are durable so a suspended session survives a process restart, but
they are **not** the source of truth: business tables are (DI-1). We therefore
degrade to an in-memory saver rather than failing to start.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def build_checkpointer(path: Path | None) -> Any:
    """In-memory saver.

    LangGraph's synchronous ``SqliteSaver`` cannot serve an async graph (it
    raises ``NotImplementedError`` from ``aget_tuple``), so durable
    checkpointing is wired through :func:`build_async_checkpointer` during app
    startup instead of being guessed at here.
    """

    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


async def build_async_checkpointer(path: Path | None) -> Any:
    """Durable checkpointer for the async graph, with an in-memory fallback.

    Checkpoints are a recovery vehicle, not the source of truth (DI-1), so a
    failure here degrades instead of blocking startup.
    """

    if path is None:
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    try:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path))
        await conn.execute("PRAGMA journal_mode=WAL")
        saver = AsyncSqliteSaver(conn)
        setup = saver.setup()
        if hasattr(setup, "__await__"):
            await setup
        return saver
    except Exception as exc:  # pragma: no cover - fallback path
        logger.warning("durable checkpointer unavailable (%s); using in-memory", type(exc).__name__)
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
