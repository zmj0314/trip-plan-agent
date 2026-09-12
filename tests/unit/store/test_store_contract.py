"""Framework §4.3 invariants, asserted against a real SQLite file."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.timebase import now_local
from app.events.types import Event, EventType
from app.store import Database, build_repositories

TABLES = {
    "sessions",
    "plan_versions",
    "consents",
    "action_ledger",
    "events",
    "cache_entries",
    "quota_entries",
    "receipts",
    "audit_log",
    "browser_slots",
    "llm_usage",
}


@pytest.fixture
def repos(tmp_path):
    db = Database(tmp_path / "travel.sqlite3")
    db.migrate()
    return build_repositories(db)


def _event(session_id: str, *, id: int = 0) -> Event:
    return Event(id=id, session_id=session_id, type=EventType.STATE_UPDATE, data={"phase": "COLLECT"})


def test_migration_is_idempotent_and_creates_every_table(repos):
    repos.db.migrate()
    names = {row["name"] for row in repos.db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names


# --------------------------------------------------------------------- sessions
def test_sessions_round_trip(repos):
    repos.sessions.create(session_id="ses_1", user_id="u1", phase="COLLECT", scope={"verdict": "in_scope"})

    record = repos.sessions.get("ses_1")
    assert record["user_id"] == "u1"
    assert record["phase"] == "COLLECT"
    assert record["scope"] == {"verdict": "in_scope"}

    repos.sessions.update_phase("ses_1", "AWAIT_CONSENT")
    repos.sessions.set_scope("ses_1", {"verdict": "ambiguous"})
    assert repos.sessions.get("ses_1")["phase"] == "AWAIT_CONSENT"
    assert repos.sessions.get("ses_1")["scope"] == {"verdict": "ambiguous"}


def test_only_expired_sessions_are_listed(repos):
    repos.sessions.create(session_id="ses_old", expires_at=now_local() - timedelta(minutes=1))
    repos.sessions.create(session_id="ses_new", expires_at=now_local() + timedelta(hours=1))

    expired = [row["session_id"] for row in repos.sessions.list_expired()]
    assert expired == ["ses_old"]


def test_suspended_sessions_are_expirable(repos):
    """DE-5: a gate that is parked too long expires rather than blocking forever."""

    repos.sessions.create(session_id="ses_1")
    repos.sessions.set_suspended("ses_1", until=now_local() - timedelta(minutes=1))

    assert [row["session_id"] for row in repos.sessions.list_expired()] == ["ses_1"]


# ------------------------------------------------------------------------ plans
def test_plan_versions_are_numbered_per_session(repos):
    first = repos.plans.insert(session_id="ses_1", plan_hash="h1")
    second = repos.plans.insert(session_id="ses_1", plan_hash="h2")

    assert repos.plans.get(first)["version_no"] == 1
    assert repos.plans.get(second)["version_no"] == 2
    assert repos.plans.latest_for_session("ses_1")["plan_hash"] == "h2"


def test_superseding_keeps_only_the_current_version(repos):
    first = repos.plans.insert(session_id="ses_1", plan_hash="h1", status="previewed")
    second = repos.plans.insert(session_id="ses_1", plan_hash="h2", status="approved")

    repos.plans.supersede_all_but("ses_1", keep=second)

    assert repos.plans.get(first)["status"] == "superseded"
    assert repos.plans.get(second)["status"] == "approved"


# --------------------------------------------------------------------- consents
def test_superseded_consent_stops_authorising_execution(repos):
    repos.consents.insert(session_id="ses_1", plan_version_id="pv_1", plan_hash="h1")
    assert repos.consents.get_active("ses_1", "h1") is not None

    repos.consents.supersede_all_for_session("ses_1")
    assert repos.consents.get_active("ses_1", "h1") is None


def test_consent_is_bound_to_a_plan_hash(repos):
    repos.consents.insert(session_id="ses_1", plan_version_id="pv_1", plan_hash="h1")
    assert repos.consents.get_active("ses_1", "h2") is None


# ----------------------------------------------------------------------- ledger
def test_idempotency_key_uniqueness_is_enforced_by_the_database(repos):
    """P4 / DI-5: the second attempt loses in SQL, not in application code."""

    kwargs = dict(idem_key="act:abc", session_id="ses_1", capability_id="booking.order.submit")
    assert repos.ledger.insert_in_flight(**kwargs) is True
    assert repos.ledger.insert_in_flight(**kwargs) is False
    assert repos.ledger.get("act:abc")["status"] == "in_flight"


def test_a_retried_key_never_resurrects_a_settled_action(repos):
    """A reconnect that replays an action must not reopen a finished one."""

    kwargs = dict(idem_key="act:xyz", session_id="ses_1", capability_id="booking.order.submit")
    repos.ledger.insert_in_flight(**kwargs)
    repos.ledger.mark_completed("act:xyz", result_ref="receipt_1")

    assert repos.ledger.insert_in_flight(**kwargs) is False
    assert repos.ledger.get("act:xyz")["status"] == "completed"


def test_ledger_terminal_states(repos):
    repos.ledger.insert_in_flight(idem_key="a", session_id="ses_1", capability_id="c1", action_id="act_1")
    repos.ledger.insert_in_flight(idem_key="b", session_id="ses_1", capability_id="c2")

    repos.ledger.mark_completed("a", result_ref="receipt_1")
    repos.ledger.mark_failed("b", {"reason": "timeout"})

    assert repos.ledger.get("a")["status"] == "completed"
    assert repos.ledger.get("a")["result_ref"] == "receipt_1"
    assert repos.ledger.get("b")["status"] == "failed"

    repos.ledger.skip_remaining("ses_1")
    assert all(row["status"] != "in_flight" for row in repos.ledger.list_for_session("ses_1"))


def test_a_user_receipt_is_what_completes_an_action(repos):
    """DH-6: order state advances on the user's say-so, not on our inference."""

    repos.ledger.insert_in_flight(idem_key="a", session_id="ses_1", capability_id="c1", action_id="act_1")
    repos.ledger.mark_completed_by_action("ses_1", "act_1", source="user_receipt")

    record = repos.ledger.get("a")
    assert record["status"] == "completed"
    assert record["result_ref"] == "user_receipt"


# ----------------------------------------------------------------------- events
def test_events_receive_monotonic_sequence_numbers(repos):
    first = repos.events.append("ses_1", _event("ses_1"))
    second = repos.events.append("ses_1", _event("ses_1"))

    assert second > first
    assert [row["seq"] for row in repos.events.list_since("ses_1", 0)] == [first, second]
    assert [row["seq"] for row in repos.events.list_since("ses_1", first)] == [second]
    assert repos.events.latest_seq("ses_1") == second


def test_non_replayable_events_are_not_persisted(repos):
    """DI-2: deltas are rebuilt from a snapshot, never replayed as text."""

    delta = Event(id=1, session_id="ses_1", type=EventType.TEXT_DELTA, data={"text": "你"})
    with pytest.raises(ValueError):
        repos.events.append("ses_1", delta)
    assert repos.events.list_since("ses_1", 0) == []


def test_events_are_pruned_by_age(repos):
    repos.events.append("ses_1", _event("ses_1"))
    assert repos.events.prune(before=now_local() + timedelta(minutes=1)) == 1
    assert repos.events.list_since("ses_1", 0) == []


# ------------------------------------------------------------------------ quota
def test_quota_reservation_is_idempotent_per_idem(repos):
    assert repos.quota.reserve(idem="q1", pool_id="amap_search", window_key="2026-09", units=1) is True
    assert repos.quota.reserve(idem="q1", pool_id="amap_search", window_key="2026-09", units=1) is False
    assert repos.quota.used("amap_search", "2026-09") == 1


def test_rolling_back_a_reservation_frees_the_quota(repos):
    repos.quota.reserve(idem="q1", pool_id="amap_lbs", window_key="2026-09", units=5)
    assert repos.quota.used("amap_lbs", "2026-09") == 5

    repos.quota.set_state("q1", "rolled_back")
    assert repos.quota.used("amap_lbs", "2026-09") == 0


def test_quota_windows_do_not_leak_into_each_other(repos):
    repos.quota.reserve(idem="q1", pool_id="amap_lbs", window_key="2026-09", units=7)
    assert repos.quota.used("amap_lbs", "2026-10") == 0


# ------------------------------------------------------------------------ cache
def test_cache_entries_round_trip_and_expire(repos):
    repos.cache.upsert(
        cache_key="k1",
        capability_id="place.resolve",
        payload_json='{"lat": 1}',
        provenance_json=None,
        fetched_at=now_local().isoformat(),
        ttl_seconds=60,
        expires_at=(now_local() - timedelta(seconds=1)).isoformat(),
    )
    assert repos.cache.get("k1")["payload_json"] == '{"lat": 1}'

    assert repos.cache.purge_expired(before=now_local()) == 1
    assert repos.cache.get("k1") is None


def test_cache_upsert_overwrites(repos):
    for payload in ("1", "2"):
        repos.cache.upsert(
            cache_key="k",
            capability_id="clock.today",
            payload_json=payload,
            provenance_json=None,
            fetched_at=now_local().isoformat(),
            ttl_seconds=60,
            expires_at=now_local().isoformat(),
        )
    assert repos.cache.get("k")["payload_json"] == "2"


# ------------------------------------------------------- receipts / audit / usage
def test_receipts_and_audit_are_append_only(repos):
    receipt_id = repos.receipts.insert(session_id="ses_1", action_id="act_1", order_no="E123")
    audit_id = repos.audit.record(event_kind="consent.granted", session_id="ses_1", subject="gate2_full")

    assert repos.receipts.list_for_session("ses_1")[0]["receipt_id"] == receipt_id
    assert repos.audit.list_for_session("ses_1")[0]["audit_id"] == audit_id


def test_llm_usage_totals_are_per_window(repos):
    repos.llm_usage.add(window_day="2026-09-10", session_id="ses_1", standard_equivalent=12.5)
    repos.llm_usage.add(window_day="2026-09-10", session_id="ses_2", standard_equivalent=7.5)
    repos.llm_usage.add(window_day="2026-09-11", session_id="ses_1", standard_equivalent=99.0)

    assert repos.llm_usage.totals(window_day="2026-09-10")["standard_equivalent"] == 20.0
    assert repos.llm_usage.totals(window_day="2026-09-10", session_id="ses_1")["standard_equivalent"] == 12.5
