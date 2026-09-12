"""Framework §3.4 / §6.10.4: canonical keys, and what is never stored."""

from __future__ import annotations

from datetime import timedelta

from app.capabilities.contract import CapabilityResult, Degradation, ResultStatus
from app.channels.cache import CacheStore, contains_pii
from app.domain.geo import Coord, Crs
from app.domain.timebase import now_local
from tests.unit.channels.fakes import memory_db, ok_result


def store(**kwargs) -> CacheStore:
    return CacheStore(memory_db(), **kwargs)


def test_key_ignores_parameter_order():
    cache = store()
    assert cache.make_key("place.resolve", {"address": "望京", "city": "北京"}) == cache.make_key(
        "place.resolve", {"city": "北京", "address": "望京"}
    )


def test_key_is_stable_across_date_formats():
    cache = store()
    assert cache.make_key("route.plan", {"date": "2026-09-12"}) == cache.make_key(
        "route.plan", {"date": "2026/9/12"}
    )


def test_key_collapses_crs_and_coordinate_precision():
    """Two providers describing the same gate must share one cache entry."""

    cache = store(coord_precision=4)
    gcj = Coord(lat=39.9087, lon=116.3975, crs=Crs.GCJ02)
    wgs = gcj.to_wgs84()

    assert cache.make_key("route.plan", {"origin": gcj}) == cache.make_key("route.plan", {"origin": wgs})
    assert cache.make_key("route.plan", {"origin": gcj}) != cache.make_key(
        "route.plan", {"origin": Coord(lat=39.95, lon=116.40, crs=Crs.WGS84)}
    )


def test_different_capabilities_never_share_a_key():
    cache = store()
    assert cache.make_key("route.plan", {"a": 1}) != cache.make_key("route.matrix", {"a": 1})


def test_fresh_entries_are_returned_and_stale_ones_are_flagged():
    cache = store()
    key = cache.make_key("clock.today", {})
    cache.put(key, ok_result({"date": "2026-09-10"}), ttl_seconds=3600, capability_id="clock.today")

    fresh = cache.get_fresh(key)
    assert fresh is not None and fresh.payload == {"date": "2026-09-10"}
    assert fresh.stale is False
    assert fresh.ttl_left() > 0

    later = now_local() + timedelta(hours=2)
    assert cache.get_fresh(key, now=later) is None
    stale = cache.get(key, now=later)
    assert stale is not None and stale.stale is True


def test_errors_are_not_cached():
    """A transient outage must not become a permanent answer."""

    cache = store()
    failed = CapabilityResult(status=ResultStatus.ERROR, degradation=Degradation(reason="boom"))
    assert cache.put("k", failed, ttl_seconds=60) is False
    assert cache.get("k") is None


def test_pii_is_never_written_to_disk():
    cache = store()
    payload = ok_result({"rider_identity": {"name": "张三", "id_card": "110101199001011234"}})

    assert cache.put("k", payload, ttl_seconds=60) is False
    assert cache.get("k") is None


def test_pii_detection_is_recursive():
    assert contains_pii({"a": [{"nested": {"phone": "13800000000"}}]}) is True
    assert contains_pii({"a": [{"nested": {"city": "北京"}}]}) is False


def test_ttl_of_zero_is_not_stored():
    cache = store()
    assert cache.put("k", ok_result({"x": 1}), ttl_seconds=0) is False
