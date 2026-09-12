"""Content layer, end to end (F1): multi-day + multi-city + lodging.

The unit tests pin the policy functions; these pin the *promise*: a multi-city
request produces an ordered day-by-day plan, the plan is what the consent gate
freezes, and the itinerary survives the model being unavailable.
"""

from __future__ import annotations

import pytest

from app.capabilities.defaults import build_default_registry
from app.capabilities.contract import CapabilityResult
from app.channels.base import ChannelAdapter, HealthState, HealthStatus
from app.channels.bootstrap import build_resolver
from app.channels.local import LocalAdapter
from app.config.settings import Settings
from app.domain.timebase import now_local
from app.graph.checkpointer import build_checkpointer
from app.graph.deps import GraphDeps
from app.graph.runner import GraphRunner
from app.llm.stub import OfflineStubLLM
from tests.unit.channels.fakes import FakeAdapter, ok_result

#: Beijing 3 天 + 天津 2 天 over five calendar days, staying self-arranged.
MULTI_CITY = (
    "从北京市朝阳区望京SOHO出发，北京3天+天津2天，2026-10-01出发，2026-10-05回来，"
    "住宿我自己订，两个人，只要建议"
)


class GeoByQuery(ChannelAdapter):
    """Returns a different coordinate per query.

    A fake that answers every lookup with one point makes every distance zero,
    which silently disables the cross-city path -- the transfer test would then
    be testing nothing.
    """

    kind = FakeAdapter.kind
    id = "open_meteo_geocoding"

    def __init__(self, points: dict[str, tuple[float, float]]) -> None:
        self.points = points

    def supports(self, capability_id: str, remote_name: str) -> bool:
        return True

    async def health(self) -> HealthStatus:
        return HealthStatus(adapter_id=self.id, state=HealthState.HEALTHY, last_check=now_local())

    async def invoke(self, binding, params, *, timeout):
        query = str(params.get("query") or "")
        lat, lon = next(
            (value for key, value in self.points.items() if key in query), (39.9, 116.4)
        )
        return CapabilityResult.model_validate(
            ok_result({"results": [{"name": query, "latitude": lat, "longitude": lon}]}).model_dump()
        )


class RailWithTrains(ChannelAdapter):
    """A minimal 12306-shaped answer, so the rail chain has something to chain."""

    kind = FakeAdapter.kind
    id = "rail12306"

    def __init__(self, *, price: int = 55) -> None:
        self.price = price

    def supports(self, capability_id: str, remote_name: str) -> bool:
        return True

    async def health(self) -> HealthStatus:
        return HealthStatus(adapter_id=self.id, state=HealthState.HEALTHY, last_check=now_local())

    async def invoke(self, binding, params, *, timeout):
        trains = [
            {
                "start_train_code": "C2001",
                "from_station": "北京南",
                "to_station": "天津",
                "start_time": "06:08",
                "arrive_time": "06:40",
                "lishi": "00:32",
                "prices": [{"seat_name": "二等座", "num": "有", "price": self.price, "short": "ze"}],
            },
            {
                "start_train_code": "C2003",
                "from_station": "北京南",
                "to_station": "天津",
                "start_time": "07:10",
                "arrive_time": "07:45",
                "lishi": "00:35",
                "prices": [{"seat_name": "二等座", "num": "有", "price": self.price, "short": "ze"}],
            },
        ]
        return ok_result({"data": trains}, provider="rail12306")


@pytest.fixture
def env(tmp_path):
    settings = Settings(llm_offline=True, data_dir=tmp_path)
    settings.ensure_dirs()
    registry = build_default_registry()

    places = GeoByQuery(
        {
            "北京": (39.9042, 116.4074),
            "望京": (39.9968, 116.4728),
            "长城": (40.3566, 116.0200),
            "八达岭": (40.3566, 116.0200),
            "天津": (39.0842, 117.2009),
        }
    )
    routes = FakeAdapter(
        adapter_id="valhalla",
        result=ok_result({"routes": [{"distance_m": 18000, "duration_s": 1500}]}, provider="valhalla"),
    )
    resolver = build_resolver(
        settings,
        registry,
        adapters={
            "local": LocalAdapter(),
            "open_meteo_geocoding": places,
            # 12306 is keyless and already wired in production, so the offline
            # environment answers it too: without it the cross-city hop silently
            # falls back to a road estimate and the transfer test would be
            # asserting on the fallback rather than on a real chain.
            "rail12306": RailWithTrains(),
            "valhalla": routes,
        },
    )
    deps = GraphDeps(settings=settings, registry=registry, resolver=resolver, llm=OfflineStubLLM())
    return GraphRunner(deps=deps, checkpointer=build_checkpointer(None), repositories=None)


def _interrupt(events):
    for event in events:
        if event.type.value == "interrupt":
            return event.data
    return None


async def test_multi_city_request_produces_a_day_by_day_plan(env):
    session_id = (await env.start())["session_id"]
    preview = _interrupt(await env.send(session_id, MULTI_CITY))

    assert preview is not None and preview["kind"] == "gate2_full"

    snapshot = await env.snapshot(session_id)
    assert snapshot["derived_slots"]["trip_days"] == 5

    content = snapshot.get("planned_content") or {}
    days = content.get("days") or []
    segments = content.get("segments") or []
    assert [segment["city"] for segment in segments] == ["北京", "天津"]
    assert [segment["day_count"] for segment in segments] == [3, 2]
    assert len(days) == 5
    assert [day["city"] for day in days] == ["北京", "北京", "北京", "天津", "天津"]


async def test_the_plan_carries_lodging_for_every_night(env):
    session_id = (await env.start())["session_id"]
    await env.send(session_id, MULTI_CITY)

    snapshot = await env.snapshot(session_id)
    content = snapshot.get("planned_content") or {}
    days = content.get("days") or []
    assert days
    for day in days:
        assert day["lodging"], f"night {day['day_index']} has no lodging entry"


async def test_the_preview_shows_the_trip_shape_before_consent(env):
    session_id = (await env.start())["session_id"]
    preview = _interrupt(await env.send(session_id, MULTI_CITY))

    assert "北京" in preview["preview"] and "天津" in preview["preview"]
    assert "Day 1" in preview["preview"], "the consent gate must show the day granularity"


async def test_the_plan_hash_covers_the_day_plan(env):
    """P3: the hash must change when the day plan changes."""

    session_id = (await env.start())["session_id"]
    preview_a = _interrupt(await env.send(session_id, MULTI_CITY))
    hash_a = preview_a["plan_hash"]

    # Approving a different plan version must be rejected: this is the mechanism
    # that stops an old consent from authorising a new itinerary.
    edits = await env.resume(
        session_id,
        {"decision": "edit", "plan_hash": hash_a, "slot_patch": {"return_date": "2026-10-04"}},
    )
    assert edits is not None

    preview_b = None
    for event in edits:
        if event.type.value == "interrupt":
            preview_b = event.data
    if preview_b is None:
        snapshot = await env.snapshot(session_id)
        preview_b = snapshot.get("pending_interrupt")
    assert preview_b is not None
    assert preview_b["plan_hash"] != hash_a, "editing the trip length must produce a new plan version"


async def test_no_content_capability_runs_before_consent(env):
    """F1 must not weaken §12 #2: consent still gates every side effect."""

    session_id = (await env.start())["session_id"]
    await env.send(session_id, MULTI_CITY)

    snapshot = await env.snapshot(session_id)
    assert snapshot["actions"] == [], "planning must not execute anything"
    assert snapshot["phase"] == "AWAIT_CONSENT"


async def test_the_cross_city_hop_is_a_real_chain_on_the_transfer_day(env):
    """F1.5: a city change is a train with a timetable, not a gap in the plan."""

    session_id = (await env.start())["session_id"]
    await env.send(session_id, MULTI_CITY)

    snapshot = await env.snapshot(session_id)
    content = snapshot.get("planned_content") or {}
    transfers = content.get("transfers") or []
    assert len(transfers) == 1, "one hop between two cities"

    leg = transfers[0]
    assert leg["train_code"] == "C2001"
    assert leg["day"] == "2026-10-03", "the move happens on the last day of 北京"
    assert str(leg["depart"]).startswith("2026-10-03T06:08")
    assert str(leg["arrive"]).startswith("2026-10-03T06:40")

    day3 = next(day for day in content["days"] if day["day_index"] == 3)
    assert day3["is_transfer_day"] is True
    assert day3["overnight_city"] == "天津", "the transfer day sleeps in the arrival city"
    assert any(item["kind"] == "city_transfer" for item in day3["items"])


async def test_the_transfer_fare_reaches_the_cost_estimate(env):
    session_id = (await env.start())["session_id"]
    await env.send(session_id, MULTI_CITY)

    snapshot = await env.snapshot(session_id)
    cost_note = (snapshot.get("planned_content") or {}).get("cost_note") or ""
    assert "城际交通" in cost_note


async def test_a_cross_city_hop_without_trains_still_has_a_clock(env, monkeypatch):
    """A road fallback must still land on the timeline with real times."""

    from app.channels.base import ChannelAdapter, HealthState, HealthStatus

    class NoTrains(ChannelAdapter):
        kind = "mcp"
        id = "rail12306"

        def supports(self, capability_id: str, remote_name: str) -> bool:
            return True

        async def health(self):
            return HealthStatus(adapter_id=self.id, state=HealthState.HEALTHY, last_check=now_local())

        async def invoke(self, binding, params, *, timeout):
            return ok_result({"data": []}, provider="rail12306")

    monkeypatch.setitem(env.deps.resolver._adapters, "rail12306", NoTrains())

    session_id = (await env.start())["session_id"]
    await env.send(session_id, MULTI_CITY)

    snapshot = await env.snapshot(session_id)
    transfers = (snapshot.get("planned_content") or {}).get("transfers") or []
    assert transfers, "the fallback still produces a transfer"
    leg = transfers[0]
    assert leg["depart"] and leg["arrive"], "a leg with no clock cannot be shown on a timeline"
    assert leg["approx"] is True, "an unrouted road estimate must say so"


async def test_a_plan_without_a_day_layer_still_loads(env):
    """Old checkpoints predate the content layer and must keep deserialising.

    Plans are replayed from SQLite, so a schema addition that breaks an existing
    stored conversation would silently lose a user's session.
    """

    from app.domain.trip import Trip

    legacy = Trip(trip_id="trip_old", session_id="s_old")
    assert legacy.legs == []
    assert not hasattr(legacy, "days"), "the day layer lives on TripContent, not on Trip"

    from app.domain.trip import DayPlan

    day = DayPlan.model_validate({"day_index": 1})
    assert day.city == ""
    assert day.attractions == []
    assert day.lodging == []


async def test_a_failed_renderer_still_yields_a_complete_itinerary(env, monkeypatch):
    """The wording is allowed to fall back; the plan is not.

    Only the renderer fails: the extractor still works, so this tests exactly
    what the two-node split buys -- a dead model costs wording, not the trip.
    """

    from app.llm.stub import OfflineStubLLM
    from app.llm.types import LLMResponse

    stub = OfflineStubLLM()

    class RendererDown:
        model = "renderer-down"

        async def structured(self, *, node, schema, prompt, user_input="", context=None, **kwargs):
            if node == "itinerary_render":
                return LLMResponse(ok=False, error="model unavailable", parsed=None)
            return await stub.structured(
                node=node, schema=schema, prompt=prompt, user_input=user_input, context=context
            )

    monkeypatch.setattr(env.deps, "llm", RendererDown())

    session_id = (await env.start())["session_id"]
    preview = _interrupt(await env.send(session_id, MULTI_CITY))
    assert preview is not None, "a dead model must not block the consent gate"

    snapshot = await env.snapshot(session_id)
    days = (snapshot.get("planned_content") or {}).get("days") or []
    assert len(days) == 5
    assert all(day["theme"] for day in days), "template themes must fill in"
    assert any(
        "itinerary_render" in note.get("capability_id", "")
        for note in snapshot["degradation_log"]
    ), "a dead renderer must be recorded, not swallowed"
