"""Framework §3.3 / §4.1.2: the single door, and what it must refuse."""

from __future__ import annotations

import pytest

from app.capabilities.contract import DegradationLevel, ResultStatus
from app.capabilities.defaults import build_default_registry
from app.capabilities.registry import CapabilityBinding
from app.channels.cache import CacheStore
from app.channels.quota import QuotaLedger
from app.channels.resilience import ProviderTimeout
from app.channels.resolver import CallContext, CapabilityResolver
from app.policy.risk import Arbiter
from tests.unit.channels.fakes import FakeAdapter, memory_db, ok_result


def bind(registry, capability_id: str, *adapter_ids: str) -> None:
    registry.override_bindings(
        capability_id,
        *[
            CapabilityBinding(adapter_id=adapter_id, remote_name=f"tool_{adapter_id}", priority=index)
            for index, adapter_id in enumerate(adapter_ids)
        ],
    )


def make_resolver(adapters, *, registry=None, db=None, cache=None, quota=None, arbiter=True, **kwargs):
    registry = registry or build_default_registry()
    return CapabilityResolver(
        registry,
        adapters,
        cache=cache,
        quota=quota,
        arbiter=Arbiter(registry) if arbiter else None,
        sleep=_no_sleep,
        **kwargs,
    )


async def _no_sleep(_seconds: float) -> None:
    return None


# ------------------------------------------------------------------ happy path
async def test_calls_the_provider_and_returns_its_result():
    registry = build_default_registry()
    bind(registry, "clock.today", "variflight")
    adapter = FakeAdapter(result=ok_result({"date": "2026-09-10"}))

    result = await make_resolver({"variflight": adapter}, registry=registry).call("clock.today", {} )

    assert result.status is ResultStatus.OK
    assert result.data == {"date": "2026-09-10"}
    assert adapter.calls == [("tool_variflight", {})]


async def test_falls_through_to_the_backup_provider():
    registry = build_default_registry()
    bind(registry, "place.resolve", "amap", "osm")
    primary = FakeAdapter(adapter_id="amap", outcomes=[ConnectionError("refused")] * 3)
    backup = FakeAdapter(adapter_id="osm", result=ok_result({"source": "osm"}))

    result = await make_resolver({"amap": primary, "osm": backup}, registry=registry).call("place.resolve", {}, )

    assert result.status is ResultStatus.OK
    assert result.data == {"source": "osm"}
    assert primary.calls and backup.calls


async def test_unknown_capability_raises_rather_than_degrading():
    """DC-5: ``payment.*`` is unregistered, so it cannot be reached at all."""

    from app.errors import CapabilityNotFound

    with pytest.raises(CapabilityNotFound):
        await make_resolver({}).call("payment.submit", {})


# ------------------------------------------------------------- consent arbiter
async def test_read_only_calls_are_allowed_without_an_explicit_context():
    """The graph's internal L0 calls carry no consent context and must still run."""

    registry = build_default_registry()
    bind(registry, "clock.today", "variflight")
    result = await make_resolver({"variflight": FakeAdapter()}, registry=registry).call("clock.today", {})

    assert result.status is ResultStatus.OK


async def test_read_only_calls_are_refused_when_the_caller_says_consent_is_missing():
    """§12: zero non-read-only -- and zero *unconsented* -- calls slip through."""

    registry = build_default_registry()
    bind(registry, "place.resolve", "amap")
    adapter = FakeAdapter(adapter_id="amap")

    result = await make_resolver({"amap": adapter}, registry=registry).call(
        "place.resolve", {}, call_context=CallContext(plan_consented=False)
    )

    assert result.status is ResultStatus.ERROR
    assert result.degradation.reason == "ILLEGAL_PRE_CONSENT"
    assert adapter.calls == []


async def test_side_effecting_calls_are_refused_without_an_explicit_context():
    registry = build_default_registry()
    bind(registry, "browser.click", "browser")
    adapter = FakeAdapter(adapter_id="browser")

    result = await make_resolver({"browser": adapter}, registry=registry).call("browser.click", {})

    assert result.status is ResultStatus.ERROR
    assert result.degradation.reason == "ILLEGAL_PRE_CONSENT"
    assert adapter.calls == []


async def test_l1_needs_action_consent_and_l2_needs_the_confirm_token():
    registry = build_default_registry()
    bind(registry, "browser.click", "browser")
    bind(registry, "booking.order.submit", "browser")
    resolver = make_resolver({"browser": FakeAdapter(adapter_id="browser")}, registry=registry)

    l1 = await resolver.call(
        "browser.click",
        {},
        call_context=CallContext(plan_consented=True, consent_ref="con_1", has_action_consent=False),
    )
    assert l1.status is ResultStatus.ERROR
    assert l1.degradation.reason == "MISSING_ACTION_CONSENT"

    l2 = await resolver.call(
        "booking.order.submit",
        {},
        call_context=CallContext(
            plan_consented=True,
            consent_ref="con_1",
            has_action_consent=True,
            confirm_token_ok=False,
        ),
    )
    assert l2.status is ResultStatus.ERROR
    assert l2.degradation.reason == "MISSING_CONFIRM_TOKEN"

    allowed = await resolver.call(
        "booking.order.submit",
        {},
        call_context=CallContext(
            plan_consented=True,
            consent_ref="con_1",
            has_action_consent=True,
            confirm_token_ok=True,
        ),
    )
    assert allowed.status is ResultStatus.OK


async def test_network_can_be_switched_off_per_call_without_disabling_local_work():
    registry = build_default_registry()
    bind(registry, "place.resolve", "amap")
    bind(registry, "booking.deeplink.build", "local")
    amap = FakeAdapter(adapter_id="amap")
    local = FakeAdapter(adapter_id="local")
    resolver = make_resolver({"amap": amap, "local": local}, registry=registry)
    offline = CallContext(plan_consented=True, consent_ref="con_1", allow_network=False)

    assert (await resolver.call("place.resolve", {}, call_context=offline)).status is not ResultStatus.OK
    assert amap.calls == []

    assert (await resolver.call("booking.deeplink.build", {}, call_context=offline)).status is ResultStatus.OK
    assert local.calls


# ------------------------------------------------------------------- retrying
async def test_transient_read_only_failures_are_retried():
    registry = build_default_registry()
    bind(registry, "clock.today", "variflight")
    adapter = FakeAdapter(
        adapter_id="variflight",
        outcomes=[ConnectionError("reset"), ok_result({"date": "2026-09-10"})],
    )

    result = await make_resolver({"variflight": adapter}, registry=registry).call("clock.today", {})

    assert result.status is ResultStatus.OK
    assert len(adapter.calls) == 2


async def test_side_effecting_calls_are_never_retried():
    """DG-1: a retried booking is a second booking."""

    registry = build_default_registry()
    bind(registry, "booking.order.submit", "browser")
    adapter = FakeAdapter(adapter_id="browser", outcomes=[ConnectionError("reset")])
    context = CallContext(
        plan_consented=True, consent_ref="con_1", has_action_consent=True, confirm_token_ok=True
    )

    result = await make_resolver({"browser": adapter}, registry=registry).call(
        "booking.order.submit", {}, call_context=context
    )

    assert result.status is not ResultStatus.OK
    assert len(adapter.calls) == 1


async def test_business_errors_are_not_retried():
    registry = build_default_registry()
    bind(registry, "clock.today", "variflight")
    adapter = FakeAdapter(adapter_id="variflight", outcomes=[KeyError("bad params")])

    result = await make_resolver({"variflight": adapter}, registry=registry).call("clock.today", {})

    assert result.status is not ResultStatus.OK
    assert len(adapter.calls) == 1


# -------------------------------------------------------------- degradation
async def test_total_failure_walks_down_the_declared_ladder():
    registry = build_default_registry()
    bind(registry, "weather.forecast", "open_meteo")
    adapter = FakeAdapter(adapter_id="open_meteo", outcomes=[ProviderTimeout("slow")] * 3)

    result = await make_resolver({"open_meteo": adapter}, registry=registry).call("weather.forecast", {"lat": 1, "lon": 2})

    # weather.forecast declares no explicit ladder, so the default D1→D2→D3 applies.
    assert result.status is ResultStatus.DEGRADED
    assert result.degradation.level in {DegradationLevel.D2, DegradationLevel.D3}
    assert result.warnings


async def test_cache_hit_avoids_calling_the_provider_again():
    registry = build_default_registry()
    bind(registry, "place.resolve", "amap")
    adapter = FakeAdapter(adapter_id="amap", result=ok_result({"lat": 39.9}))
    store = CacheStore(memory_db())
    resolver = make_resolver({"amap": adapter}, registry=registry, cache=store)

    first = await resolver.call("place.resolve", {"address": "望京"})
    second = await resolver.call("place.resolve", {"address": "望京"})

    assert first.status is ResultStatus.OK
    assert second.degradation.level is DegradationLevel.D1
    assert second.provenance is not None and second.provenance.cache_hit
    assert len(adapter.calls) == 1


# ------------------------------------------------------------------- quota
async def test_exhausted_quota_skips_the_provider_entirely():
    registry = build_default_registry()
    bind(registry, "place.resolve", "amap", "osm")
    amap = FakeAdapter(adapter_id="amap")
    osm = FakeAdapter(adapter_id="osm", result=ok_result({"source": "osm"}))

    quota = QuotaLedger(memory_db(), limits={"amap_lbs": 0}, warn_ratio=0.8, degrade_ratio=0.95)
    resolver = make_resolver({"amap": amap, "osm": osm}, registry=registry, quota=quota)

    result = await resolver.call("place.resolve", {})

    # An exhausted pool is never spent on a guess: the capability degrades
    # along its ladder instead (framework §4.1.4).
    assert result.status is ResultStatus.DEGRADED
    assert any("quota exhausted" in warning for warning in result.warnings)
    assert amap.calls == []
    assert osm.calls == []


async def test_a_pool_close_to_its_limit_still_works_but_says_so():
    registry = build_default_registry()
    bind(registry, "place.resolve", "amap")
    quota = QuotaLedger(memory_db(), limits={"amap_lbs": 10}, warn_ratio=0.8, degrade_ratio=0.95)
    resolver = make_resolver({"amap": FakeAdapter(adapter_id="amap")}, registry=registry, quota=quota)

    for _ in range(7):
        quiet = await resolver.call("place.resolve", {"address": "望京"})
    assert quiet.status is ResultStatus.OK
    assert quiet.warnings == []

    warned = await resolver.call("place.resolve", {"address": "望京"})  # 8/10 = the warn line
    assert warned.status is ResultStatus.OK
    assert any("warn" in warning for warning in warned.warnings)


async def test_successful_calls_commit_their_reservation():
    registry = build_default_registry()
    bind(registry, "place.resolve", "amap")
    quota = QuotaLedger(memory_db(), limits={"amap_lbs": 100})
    resolver = make_resolver({"amap": FakeAdapter(adapter_id="amap")}, registry=registry, quota=quota)

    await resolver.call("place.resolve", {"address": "望京"})

    assert quota.used("amap_lbs") == 1


async def test_failed_calls_roll_their_reservation_back():
    registry = build_default_registry()
    bind(registry, "place.resolve", "amap")
    quota = QuotaLedger(memory_db(), limits={"amap_lbs": 100})
    adapter = FakeAdapter(adapter_id="amap", outcomes=[KeyError("bad params")])
    resolver = make_resolver({"amap": adapter}, registry=registry, quota=quota)

    await resolver.call("place.resolve", {"address": "望京"})

    assert quota.used("amap_lbs") == 0


# ------------------------------------------------------------------ timeouts
async def test_a_wedged_provider_cannot_hold_the_turn_open():
    """An adapter that never returns must degrade, not hang the conversation."""

    import asyncio

    from app.capabilities.contract import CapabilitySpec

    class HangingAdapter(FakeAdapter):
        async def invoke(self, binding, params, *, timeout):
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

    registry = build_default_registry()
    registry.register(
        CapabilitySpec(
            capability_id="test.hangs",
            timeout_seconds=0.02,
            degradation_ladder=[DegradationLevel.D2],
        ),
        CapabilityBinding(adapter_id="hang", remote_name="anything", priority=1),
    )
    resolver = make_resolver({"hang": HangingAdapter(adapter_id="hang")}, registry=registry, timeout_grace=0.0)

    result = await resolver.call("test.hangs", {})

    assert result.status is ResultStatus.DEGRADED
    assert result.degradation.level is DegradationLevel.D2
