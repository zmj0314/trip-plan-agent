"""Framework §12 + DJ-2: the SSE cursor must be gapless and re-readable."""

from __future__ import annotations

from app.events.bus import EventBus
from app.events.types import Event, EventType


def _event(session_id: str, *, id: int = 0) -> Event:
    return Event(id=id, session_id=session_id, type=EventType.STATE_UPDATE, data={"k": id})


def test_publish_assigns_monotonic_sequence_numbers():
    bus = EventBus()
    last = bus.publish("ses_1", [_event("ses_1"), _event("ses_1")])
    assert last == 2
    assert [e.id for e in bus.replay("ses_1")] == [1, 2]


def test_replay_is_not_consumption():
    """A reconnecting client -- or a second tab -- must see the same history."""

    bus = EventBus()
    bus.publish("ses_1", [_event("ses_1"), _event("ses_1")])

    assert [e.id for e in bus.replay("ses_1", since=1)] == [2]
    assert [e.id for e in bus.replay("ses_1", since=1)] == [2]
    assert [e.id for e in bus.replay("ses_1")] == [1, 2]


def test_store_sequence_numbers_are_preserved():
    """Persisted ids win, so the bus cursor matches ``GET /events?since=``."""

    bus = EventBus()
    bus.publish("ses_1", [_event("ses_1", id=41), _event("ses_1", id=42)])
    assert bus.last_seq("ses_1") == 42

    bus.publish("ses_1", [_event("ses_1")])
    assert bus.last_seq("ses_1") == 43


def test_sessions_are_isolated():
    bus = EventBus()
    bus.publish("ses_1", [_event("ses_1")])
    bus.publish("ses_2", [_event("ses_2")])

    assert bus.sessions() == ["ses_1", "ses_2"]
    assert [e.session_id for e in bus.replay("ses_2")] == ["ses_2"]


def test_retention_is_bounded_but_the_cursor_keeps_growing():
    bus = EventBus(retention=3)
    bus.publish("ses_1", [_event("ses_1") for _ in range(5)])

    assert [e.id for e in bus.replay("ses_1")] == [3, 4, 5]
    assert bus.last_seq("ses_1") == 5


def test_drop_forgets_only_that_session():
    bus = EventBus()
    bus.publish("ses_1", [_event("ses_1")])
    bus.drop("ses_1")

    assert bus.replay("ses_1") == []
    assert bus.last_seq("ses_1") == 0
