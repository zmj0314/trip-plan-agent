"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.context import AppContext, build_context
from app.api.dedup import RequestDedup
from app.api.routes import llm_router, sessions_router
from app.config.settings import Settings, get_settings
from app.graph.checkpointer import build_async_checkpointer
from app.llm.telemetry import TELEMETRY
from app.events.bus import EventBus

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, ctx: AppContext | None = None) -> FastAPI:
    context = ctx or build_context(settings or get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        checkpointer = await build_async_checkpointer(context.settings.data_dir / "checkpoints.sqlite3")
        context.runner.rebuild(checkpointer=checkpointer)
        logger.info(
            "travel-agent started",
            extra={"llm": context.credentials.describe(), "capabilities": len(context.registry.all_specs())},
        )
        yield
        closer = getattr(context.resolver, "aclose", None)
        if closer:
            await closer()

    app = FastAPI(title="travel-plan-agent", version="0.0.1", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.ctx = context
    app.state.dedup = RequestDedup()
    # Share the context's bus rather than creating a second one: the runner
    # publishes into it, and a separate instance here would make streaming look
    # broken while every test of the pieces still passed.
    app.state.bus = context.bus if context.bus is not None else EventBus()
    app.include_router(sessions_router)
    app.include_router(llm_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        from app.api.context import active_llm_name

        return {
            "status": "ok",
            "capabilities": len(context.registry.all_specs()),
            "llm": context.credentials.describe(),
            "llm_runtime": {
                "active_client": active_llm_name(context.runner.deps),
                "key_source": context.key_source(),
                "calls": TELEMETRY.snapshot(),
            },
            "persistence": context.db is not None,
            "channels": context.resolver is not None,
        }

    return app


app = None  # populated by ``uvicorn app.api.app:create_app --factory``
