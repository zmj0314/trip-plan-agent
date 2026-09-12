"""HTTP surface (framework §4.3.3)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.dedup import RequestDedup
from app.api.sse import format_sse, keepalive
from app.events.types import REPLAYABLE_EVENT_TYPES, Event

router = APIRouter()

#: How long an idle live stream waits before emitting a heartbeat. Long enough to
#: cost nothing, short enough that a proxy does not close an apparently dead
#: connection.
HEARTBEAT_SECONDS = 15.0


class StartSessionRequest(BaseModel):
    user_id: str = "local"


class MessageRequest(BaseModel):
    text: str
    client_request_id: str | None = None


class ResumeRequest(BaseModel):
    # A free-text answer to a clarification question is not a "decision", so
    # `decision` is optional: the pending interrupt determines which shape is
    # expected.
    decision: str | None = None
    text: str | None = None
    plan_hash: str | None = None
    plan_version_id: str | None = None
    target: str | None = None
    slot_patch: dict[str, Any] = Field(default_factory=dict)
    confirm_token: str | None = None
    client_request_id: str | None = None


class ReceiptRequest(BaseModel):
    action_id: str
    order_no: str | None = None
    status: str = "completed"
    note: str | None = None
    client_request_id: str | None = None


def _ctx(request: Request):
    return request.app.state.ctx


def _dedup(request: Request) -> RequestDedup:
    return request.app.state.dedup


def _check_access(request: Request, token: str | None) -> None:
    """DL-3: minimal access control. Never log or echo the configured token."""

    expected = _ctx(request).settings.access_token
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


@router.post("/sessions")
async def start_session(
    body: StartSessionRequest,
    request: Request,
    x_access_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_access(request, x_access_token)
    return await _ctx(request).runner.start(user_id=body.user_id)


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: MessageRequest,
    request: Request,
    x_access_token: str | None = Header(default=None),
    x_llm_api_key: str | None = Header(default=None),
    x_llm_provider: str | None = Header(default=None),
    x_llm_base_url: str | None = Header(default=None),
    x_llm_model: str | None = Header(default=None),
    x_client_request_id: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_access(request, x_access_token)
    rid = body.client_request_id or x_client_request_id
    cached = _dedup(request).check(rid)
    if cached is not None:
        return cached

    ctx = _ctx(request)
    ctx.apply_request_credentials(
        x_llm_api_key,
        profile=ctx.profile_from_headers(
            provider=x_llm_provider, base_url=x_llm_base_url, model=x_llm_model, api_key=x_llm_api_key
        ),
    )
    events = await ctx.runner.send(session_id, body.text)
    result = {"session_id": session_id, "events": [e.model_dump(mode="json") for e in events]}
    _dedup(request).remember(rid, result)
    return result


@router.post("/sessions/{session_id}/resume")
async def resume(
    session_id: str,
    body: ResumeRequest,
    request: Request,
    x_access_token: str | None = Header(default=None),
    x_llm_api_key: str | None = Header(default=None),
    x_llm_provider: str | None = Header(default=None),
    x_llm_base_url: str | None = Header(default=None),
    x_llm_model: str | None = Header(default=None),
    x_client_request_id: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_access(request, x_access_token)
    rid = body.client_request_id or x_client_request_id
    cached = _dedup(request).check(rid)
    if cached is not None:
        return cached

    ctx = _ctx(request)
    ctx.apply_request_credentials(
        x_llm_api_key,
        profile=ctx.profile_from_headers(
            provider=x_llm_provider, base_url=x_llm_base_url, model=x_llm_model, api_key=x_llm_api_key
        ),
    )
    payload = body.model_dump(exclude_none=True)
    events = await ctx.runner.resume(session_id, payload)
    if any(e.data.get("code") == "PLAN_VERSION_STALE" for e in events):
        raise HTTPException(status_code=409, detail="plan version stale")
    result = {"session_id": session_id, "events": [e.model_dump(mode="json") for e in events]}
    _dedup(request).remember(rid, result)
    return result


@router.post("/sessions/{session_id}/receipt")
async def receipt(
    session_id: str,
    body: ReceiptRequest,
    request: Request,
    x_access_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """DH-6: only a user receipt may mark an order as completed."""

    _check_access(request, x_access_token)
    ctx = _ctx(request)
    repo = getattr(ctx.repositories, "receipts", None)
    record = {
        "session_id": session_id,
        "action_id": body.action_id,
        "order_no": body.order_no,
        "status": body.status,
        "note": body.note,
    }
    if repo is not None:
        repo.insert(**record)
    ledger = getattr(ctx.repositories, "ledger", None)
    if ledger is not None and body.status == "completed":
        ledger.mark_completed_by_action(session_id, body.action_id, source="user_receipt")
    return {"session_id": session_id, "action_id": body.action_id, "status": body.status}


@router.get("/sessions/{session_id}/snapshot")
async def snapshot(
    session_id: str,
    request: Request,
    x_access_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_access(request, x_access_token)
    return await _ctx(request).runner.snapshot(session_id)


@router.get("/sessions/{session_id}/events")
async def events_since(
    session_id: str,
    request: Request,
    since: int = Query(default=0, ge=0),
    x_access_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check_access(request, x_access_token)
    repo = getattr(_ctx(request).repositories, "events", None)
    if repo is None:
        return {"session_id": session_id, "events": []}
    rows = repo.list_since(session_id, since)
    return {"session_id": session_id, "events": rows}


@router.get("/sessions/{session_id}/stream")
async def stream(
    session_id: str,
    request: Request,
    since: int = Query(default=0, ge=0),
    live: bool = Query(default=True, description="keep the connection open for new events"),
    x_access_token: str | None = Header(default=None),
):
    """SSE downlink, replay *plus* live.

    Replay semantics (DJ-2): replayed events are followed by a full state
    snapshot, because deltas are never persisted and a client may have lost its
    local state.

    Live semantics (§10): the subscription is opened *before* the backlog is
    read, so an event published during the read cannot slip between the two and
    be lost. Streamed ``text_delta`` chunks only ever arrive here -- they are not
    persisted, because a reconnect rebuilds the screen from a snapshot and
    replaying half a sentence would be worse than useless.
    """

    _check_access(request, x_access_token)
    ctx = _ctx(request)
    bus = request.app.state.bus
    queue = bus.subscribe(session_id) if live else None

    async def generator():
        try:
            yield keepalive()
            for event in bus.replay(session_id, since=since):
                if event.type in REPLAYABLE_EVENT_TYPES:
                    yield format_sse(event.model_copy(update={"replay": True}))

            snapshot_payload = await ctx.runner.snapshot(session_id)
            yield (
                "event: state_update\n"
                f"data: {__import__('json').dumps({'snapshot': snapshot_payload}, ensure_ascii=False, default=str)}\n\n"
            )
            if queue is None:
                yield "event: done\ndata: {}\n\n"
                return

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield keepalive()
                    continue
                yield format_sse(event)
        finally:
            if queue is not None:
                bus.unsubscribe(session_id, queue)

    return StreamingResponse(generator(), media_type="text/event-stream")
