"""LLM token streaming, end to end (framework §10, ``text_delta``).

The property under test is narrow and important: the *same* turn that used to
return one lump of events now additionally publishes incremental chunks, without
changing what is trusted, what is persisted, or what a reconnect sees.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.capabilities.defaults import build_default_registry
from app.channels.bootstrap import build_resolver
from app.channels.local import LocalAdapter
from app.config.settings import Settings
from app.domain.models import ScopeVerdict
from app.events.types import REPLAYABLE_EVENT_TYPES, EventType
from app.graph.checkpointer import build_checkpointer
from app.graph.deps import GraphDeps
from app.graph.runner import GraphRunner
from app.llm.schemas import IntakeOutput, PreviewOutput, RemindOutput
from app.llm.stub import OfflineStubLLM
from app.llm.types import LLMResponse, LLMUsage
from app.store import Database, build_repositories
from tests.conftest import FULL_REQUEST
from tests.unit.channels.fakes import FakeAdapter, ok_result

#: What the model "says" token by token. The raw stream is JSON (the node asks
#: for a structured object), which is exactly why the client is told to discard
#: it once the node's rendered text arrives.
CHUNKS = ['{"text"', ': "', "已生成", "行程", '方案"}']
RENDERED = "已生成行程方案"


class StreamingStub:
    """Stub that also drives ``on_delta`` for the render nodes."""

    model = "streaming-stub"

    def __init__(self, *, chunks: list[str] | None = None) -> None:
        self._stub = OfflineStubLLM()
        self._chunks = list(CHUNKS if chunks is None else chunks)
        self.streamed_nodes: list[str] = []
        self.non_streamed_nodes: list[str] = []

    async def structured(self, *, node, schema, prompt, user_input="", context=None, on_delta=None, **kw):
        if on_delta is None:
            self.non_streamed_nodes.append(node)
            return await self._stub.structured(
                node=node, schema=schema, prompt=prompt, user_input=user_input, context=context
            )
        self.streamed_nodes.append(node)
        for chunk in self._chunks:
            await on_delta(chunk)
        if schema is PreviewOutput:
            parsed: object = PreviewOutput(text=RENDERED)
        elif schema is RemindOutput:
            parsed = RemindOutput(items=["出发前一天确认余票"])
        else:
            parsed = schema()
        return LLMResponse(parsed=parsed, raw_text="".join(self._chunks), usage=LLMUsage(model=self.model))


@pytest.fixture
def wired(tmp_path):
    settings = Settings(llm_offline=True, data_dir=tmp_path)
    settings.ensure_dirs()
    db = Database(settings.db_path)
    db.migrate()
    repositories = build_repositories(db)
    registry = build_default_registry()
    resolver = build_resolver(
        settings,
        registry,
        db=db,
        adapters={
            "local": LocalAdapter(),
            "open_meteo_geocoding": FakeAdapter(
                adapter_id="open_meteo_geocoding",
                result=ok_result({"results": [{"name": "X", "latitude": 39.9, "longitude": 116.4}]}),
            ),
            "valhalla": FakeAdapter(
                adapter_id="valhalla",
                result=ok_result({"routes": [{"distance_m": 18000, "duration_s": 1500}]}),
            ),
        },
    )
    llm = StreamingStub()
    deps = GraphDeps(settings=settings, registry=registry, resolver=resolver, llm=llm)
    from app.events.bus import EventBus

    bus = EventBus()
    runner = GraphRunner(
        deps=deps,
        checkpointer=build_checkpointer(None),
        repositories=repositories,
        bus=bus,
    )
    return {"runner": runner, "llm": llm, "bus": bus, "db": db, "settings": settings, "repositories": repositories}


async def test_deltas_are_emitted_for_the_render_node(wired):
    runner = wired["runner"]
    session_id = (await runner.start())["session_id"]
    events = await runner.send(session_id, FULL_REQUEST)

    deltas = [e for e in events if e.type is EventType.TEXT_DELTA]
    assert deltas, "the render node must stream"
    streamed_nodes = {str(e.data.get("node")) for e in deltas}
    assert "preview_render" in streamed_nodes

    preview = [e for e in deltas if e.data.get("node") == "preview_render"]
    text = "".join(str(e.data.get("text") or "") for e in preview if not e.data.get("done"))
    assert text == "".join(CHUNKS), "chunks arrive in order and complete"

    final = [e for e in preview if e.data.get("done")]
    assert final and final[-1].data.get("ok") is True


async def test_extraction_nodes_do_not_stream(wired):
    """Streaming raw JSON for an extractor would show the user a half-built object."""

    runner = wired["runner"]
    session_id = (await runner.start())["session_id"]
    await runner.send(session_id, FULL_REQUEST)

    assert "intake" in wired["llm"].non_streamed_nodes
    assert "preview_render" in wired["llm"].streamed_nodes


async def test_deltas_are_not_persisted_but_are_replayable_in_principle(wired):
    """A reconnect rebuilds from a snapshot; replaying half a sentence is noise."""

    assert EventType.TEXT_DELTA not in REPLAYABLE_EVENT_TYPES

    runner = wired["runner"]
    session_id = (await runner.start())["session_id"]
    await runner.send(session_id, FULL_REQUEST)

    rows = wired["db"].query("SELECT type FROM events WHERE session_id = ?", (session_id,))
    assert rows, "replayable events were persisted"
    assert all(row["type"] != "text_delta" for row in rows)


async def test_deltas_reach_a_live_subscriber(wired):
    """The bus is how an attached SSE reader sees tokens as they are produced."""

    bus = wired["bus"]
    runner = wired["runner"]
    session_id = (await runner.start())["session_id"]
    queue = bus.subscribe(session_id)

    await runner.send(session_id, FULL_REQUEST)

    seen: list[dict] = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    assert any(event.type is EventType.TEXT_DELTA for event in seen), "live reader got no tokens"


async def test_streaming_does_not_change_the_rendered_text(wired):
    """Streaming changes *when* text appears, not what the system trusts."""

    runner = wired["runner"]
    session_id = (await runner.start())["session_id"]
    await runner.send(session_id, FULL_REQUEST)

    snapshot = await runner.snapshot(session_id)
    assert snapshot["preview_text"].startswith(RENDERED), (
        "the node's own rendered text is what lands in state, not the raw stream"
    )


async def test_a_repair_attempt_tells_the_client_to_reset():
    """A rejected answer must not leave half of itself on screen."""

    from app.llm.streaming import DeltaSink

    seen: list[dict] = []

    async def handler(data: dict) -> None:
        seen.append(data)

    sink = DeltaSink(handler=handler)
    sink.begin("preview_render")
    await sink.delta("preview_render", "第一批")
    await sink.delta("preview_render", None)  # the reset signal
    await sink.delta("preview_render", "第二批")

    resets = [item for item in seen if item.get("reset")]
    assert len(resets) == 1
    assert seen[-1]["text"] == "第二批"
    assert sink.text_for("preview_render") == "第二批", "the rejected attempt is dropped"


def test_sse_stream_includes_live_deltas(tmp_path):
    """The endpoint replays, snapshots, then keeps the connection open."""

    settings = Settings(llm_offline=True, data_dir=tmp_path)
    settings.ensure_dirs()
    app = create_app(settings)

    with TestClient(app) as client:
        created = client.post("/sessions", json={"user_id": "local"}).json()
        session_id = created["session_id"]

        # A live reader is attached before the turn runs; the SSE body is read
        # once the turn has published into the bus.
        with client.stream("GET", f"/sessions/{session_id}/stream?live=false") as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())
        assert "event: state_update" in body
        assert "event: done" in body
