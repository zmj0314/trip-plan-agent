"""Plan-derived idempotency keys (P4).

The failure this guards is specific and expensive: replanning the same trip mints
fresh ids, so a key derived from an id would differ, the ledger would accept the
second attempt, and the same booking would be placed twice. These tests pin the
key to the *business act* instead.
"""

from __future__ import annotations

from app.policy import idempotency as idem


def _subject(**overrides):
    subject = {
        "leg_id": "leg_abc",
        "kind": "rail",
        "date": "2026-10-03",
        "from_station": "北京南",
        "to_station": "天津",
        "train_code": "C2001",
        "depart_time": "06:08",
        "travelers": 2,
    }
    subject.update(overrides)
    return subject


def test_the_same_act_replanned_keeps_its_key() -> None:
    """Only regenerated ids differ; the act is identical."""

    first = idem.plan_action_key(
        session_id="s1", action_kind="booking.deeplink.build", params=_subject(leg_id="leg_a")
    )
    second = idem.plan_action_key(
        session_id="s1", action_kind="booking.deeplink.build", params=_subject(leg_id="leg_b")
    )
    assert first == second


def test_a_different_train_is_a_different_act() -> None:
    base = idem.plan_action_key(
        session_id="s1", action_kind="booking.deeplink.build", params=_subject(train_code="C2001")
    )
    other = idem.plan_action_key(
        session_id="s1", action_kind="booking.deeplink.build", params=_subject(train_code="C2003")
    )
    assert base != other


def test_a_different_date_is_a_different_act() -> None:
    base = idem.plan_action_key(
        session_id="s1", action_kind="booking.deeplink.build", params=_subject(date="2026-10-03")
    )
    other = idem.plan_action_key(
        session_id="s1", action_kind="booking.deeplink.build", params=_subject(date="2026-10-04")
    )
    assert base != other


def test_a_different_traveler_count_is_a_different_act() -> None:
    base = idem.plan_action_key(
        session_id="s1", action_kind="booking.deeplink.build", params=_subject(travelers=2)
    )
    other = idem.plan_action_key(
        session_id="s1", action_kind="booking.deeplink.build", params=_subject(travelers=3)
    )
    assert base != other


def test_a_new_session_is_a_new_intent() -> None:
    """DH-2: repeating a booking in a new session is not a retry."""

    first = idem.plan_action_key(
        session_id="s1", action_kind="booking.deeplink.build", params=_subject()
    )
    second = idem.plan_action_key(
        session_id="s2", action_kind="booking.deeplink.build", params=_subject()
    )
    assert first != second


def test_a_different_action_kind_never_collides() -> None:
    deeplink = idem.plan_action_key(
        session_id="s1", action_kind="booking.deeplink.build", params=_subject()
    )
    checklist = idem.plan_action_key(
        session_id="s1", action_kind="booking.checklist.export", params=_subject()
    )
    assert deeplink != checklist
    assert deeplink.startswith("booking.deeplink.build:")
    assert checklist.startswith("booking.checklist.export:")


def test_presentation_does_not_change_the_key() -> None:
    """A caller that writes the date differently still means the same booking."""

    slashed = idem.plan_action_key(
        session_id="s1",
        action_kind="booking.deeplink.build",
        params=_subject(date="2026/10/03"),
    )
    dashed = idem.plan_action_key(
        session_id="s1",
        action_kind="booking.deeplink.build",
        params=_subject(date="2026-10-03"),
    )
    assert slashed == dashed


def test_idem_subject_strips_regenerated_ids_only() -> None:
    stripped = idem.idem_subject(
        {
            "leg_id": "leg_a",
            "trip_id": "trip_a",
            "attraction_id": "attr_a",
            "segment_id": "seg_a",
            "date": "2026-10-03",
            "legs": [{"leg_id": "leg_b", "train_code": "C2001"}],
        }
    )
    assert "leg_id" not in stripped
    assert "trip_id" not in stripped
    assert "attraction_id" not in stripped
    assert stripped["date"] == "2026-10-03"
    assert stripped["legs"] == [{"train_code": "C2001"}]


def test_key_is_stable_across_process_runs() -> None:
    """The key is a pure function of its inputs, not of in-memory state."""

    key = idem.plan_action_key(
        session_id="s1", action_kind="booking.deeplink.build", params=_subject()
    )
    assert key == idem.plan_action_key(
        session_id="s1", action_kind="booking.deeplink.build", params=_subject()
    )
    assert len(key.split(":", 1)[1]) == 32


async def test_the_ledger_rejects_a_second_attempt_at_the_same_act() -> None:
    """The race is lost in the database, not in application code."""

    from tests.unit.channels.fakes import memory_db

    from app.store import build_repositories

    db = memory_db()
    ledger = build_repositories(db).ledger

    first = ledger.insert_in_flight(
        idem_key="k1", session_id="s1", capability_id="booking.reserve.submit"
    )
    second = ledger.insert_in_flight(
        idem_key="k1", session_id="s1", capability_id="booking.reserve.submit"
    )
    assert first is True
    assert second is False, "a duplicate must lose the primary-key race"

    ledger.mark_completed("k1", result_ref="act_1")
    assert ledger.insert_in_flight(
        idem_key="k1", session_id="s1", capability_id="booking.reserve.submit"
    ) is False, "a settled action must not be resurrected"
