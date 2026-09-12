"""M0 acceptance, end to end with real channels wired in.

Framework §8 defines M0 as "at least one capability callable" plus an empty run
through clarify → gate → execute. §12 adds the two properties that must hold
whatever else changes: nothing with side effects runs before consent, and a
plan that cannot be verified says so instead of guessing.
"""

from __future__ import annotations

import pytest

from app.capabilities.defaults import build_default_registry
from app.channels.bootstrap import build_resolver
from app.channels.local import LocalAdapter
from app.config.settings import Settings
from app.graph.checkpointer import build_checkpointer
from app.graph.deps import GraphDeps
from app.graph.runner import GraphRunner
from app.llm.stub import OfflineStubLLM
from app.store import Database, build_repositories
from tests.conftest import FULL_REQUEST
from tests.unit.channels.fakes import FakeAdapter, ok_result


@pytest.fixture
def app_env(tmp_path):
    settings = Settings(llm_offline=True, data_dir=tmp_path)
    settings.ensure_dirs()
    db = Database(settings.db_path)
    db.migrate()
    repositories = build_repositories(db)
    registry = build_default_registry()

    clock = FakeAdapter(adapter_id="variflight", result=ok_result({"today": "2026-09-10"}))
    # Resolving a place is what the content layer needs to place stops on days;
    # without it the day plan is legitimately empty and every content assertion
    # below would be testing nothing.
    places = FakeAdapter(
        adapter_id="open_meteo_geocoding",
        result=ok_result(
            {"results": [{"name": "测试地点", "latitude": 40.35, "longitude": 116.0, "country": "中国"}]}
        ),
    )
    # A route is what makes an itinerary an itinerary: the leg planner and the
    # day timeline both read it, so the offline environment has to answer it.
    routes = FakeAdapter(
        adapter_id="valhalla",
        outcomes=[
            ok_result({"routes": [{"distance_m": 24000, "duration_s": 1800}]}, provider="valhalla")
            for _ in range(12)
        ],
    )
    resolver = build_resolver(
        settings,
        registry,
        db=db,
        adapters={
            "local": LocalAdapter(),
            "variflight": clock,
            "open_meteo_geocoding": places,
            "valhalla": routes,
        },
    )
    deps = GraphDeps(settings=settings, registry=registry, resolver=resolver, llm=OfflineStubLLM())
    runner = GraphRunner(
        deps=deps,
        checkpointer=build_checkpointer(None),
        repositories=repositories,
    )
    return {
        "settings": settings,
        "db": db,
        "repositories": repositories,
        "registry": registry,
        "resolver": resolver,
        "runner": runner,
        "clock": clock,
        "places": places,
    }


def _interrupt(events):
    for event in events:
        if event.type.value == "interrupt":
            return event.data
    return None


async def test_local_capabilities_actually_run_after_consent(app_env):
    runner = app_env["runner"]
    session_id = (await runner.start())["session_id"]

    preview = _interrupt(await runner.send(session_id, FULL_REQUEST))
    assert preview is not None and preview["kind"] == "gate2_full"

    events = await runner.resume(session_id, {"decision": "approve", "plan_hash": preview["plan_hash"]})
    assert any(event.type.value == "done" for event in events)

    snapshot = await runner.snapshot(session_id)
    assert snapshot["phase"] == "DONE"
    statuses = {action["capability_id"]: action["status"] for action in snapshot["actions"]}
    assert statuses["booking.checklist.export"] == "completed"
    assert snapshot["degradation_log"] == []


async def test_a_deeplink_is_only_built_for_something_bookable(app_env):
    """A road leg has no timetable, so a deep-link for it would point at nothing."""

    runner = app_env["runner"]
    session_id = (await runner.start())["session_id"]
    preview = _interrupt(await runner.send(session_id, FULL_REQUEST))
    await runner.resume(session_id, {"decision": "approve", "plan_hash": preview["plan_hash"]})

    snapshot = await runner.snapshot(session_id)
    legs = snapshot["planned_legs"]
    bookable = [leg for leg in legs if leg.get("kind") == "rail" or leg.get("train_code")]
    deeplinks = [
        action for action in snapshot["actions"] if action["capability_id"] == "booking.deeplink.build"
    ]
    assert len(deeplinks) == len(bookable), "one deep-link per bookable leg, and none for the rest"


async def test_every_action_carries_an_idempotency_key(app_env):
    """P4: an action with no key cannot be de-duplicated by the database."""

    runner = app_env["runner"]
    session_id = (await runner.start())["session_id"]
    preview = _interrupt(await runner.send(session_id, FULL_REQUEST))
    await runner.resume(session_id, {"decision": "approve", "plan_hash": preview["plan_hash"]})

    snapshot = await runner.snapshot(session_id)
    assert snapshot["actions"], "the plan produced actions"
    for action in snapshot["actions"]:
        assert action["idem_key"], f"{action['capability_id']} has no idempotency key"
        assert ":" in action["idem_key"]

    ledger = app_env["repositories"].ledger
    assert ledger is not None
    rows = app_env["db"].query("SELECT idem_key, status FROM action_ledger")
    assert rows, "the ledger must record what ran"
    assert {row["status"] for row in rows} <= {"completed", "failed", "in_flight"}


async def test_no_capability_runs_before_the_information_gate(app_env):
    """§12: nothing may execute -- read-only or otherwise -- before the gate."""

    runner = app_env["runner"]
    session_id = (await runner.start())["session_id"]

    await runner.send(session_id, "我想去长城")

    assert app_env["clock"].calls == []
    snapshot = await runner.snapshot(session_id)
    assert snapshot["phase"] == "COLLECT"
    assert snapshot["actions"] == []


async def test_the_session_and_its_events_survive_in_the_store(app_env):
    runner = app_env["runner"]
    repositories = app_env["repositories"]
    session_id = (await runner.start())["session_id"]

    await runner.send(session_id, FULL_REQUEST)
    record = repositories.sessions.get(session_id)
    rows = repositories.events.list_since(session_id, 0)

    assert record is not None and record["phase"] == "AWAIT_CONSENT"
    assert rows, "replayable events must be persisted for reconnect"
    assert [row["seq"] for row in rows] == sorted(row["seq"] for row in rows)
    assert all(row["replay"] is True for row in rows)


async def test_the_resolver_is_the_only_door_to_the_outside(app_env):
    """A capability with no live provider degrades instead of raising."""

    result = await app_env["resolver"].call("weather.forecast", {"latitude": 40.43, "longitude": 116.56})
    assert result.status.value in {"degraded", "unavailable"}
    assert result.degradation.reason


async def test_the_assembled_application_guards_its_own_side_effects(tmp_path):
    """The composition root must hand back a resolver that is already gated."""

    from app.api.context import build_context
    from app.capabilities.contract import ResultStatus

    context = build_context(Settings(llm_offline=True, data_dir=tmp_path))
    try:
        assert context.db is not None
        assert context.repositories.sessions is not None
        assert context.resolver is not None
        assert "local" in context.resolver.adapters

        local = await context.resolver.call("booking.deeplink.build", {"leg_id": "leg_1"})
        assert local.status is ResultStatus.OK
        assert local.data["payment"] == "user_side"

        gated = await context.resolver.call("booking.order.submit", {})
        assert gated.status is ResultStatus.ERROR
        assert gated.degradation.reason == "ILLEGAL_PRE_CONSENT"
    finally:
        await context.resolver.aclose()
