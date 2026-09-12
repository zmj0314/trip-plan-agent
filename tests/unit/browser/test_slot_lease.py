"""One page per session (framework §11 "多 agent 抢页").

Two conversations driving one browser is how a click lands in the wrong form.
The lease is the mechanism that refuses the second one outright, so its
precedence rules are pinned here.
"""

from __future__ import annotations

import pytest

from app.browser.slot import PageSlotLease, SlotConflict
from app.store import Database


@pytest.fixture
def lease() -> PageSlotLease:
    db = Database(":memory:")
    db.migrate()
    return PageSlotLease(db, minutes=15)


def test_a_session_can_take_a_free_page(lease: PageSlotLease) -> None:
    taken = lease.acquire("s1")
    assert taken["state"] == "leased"
    assert lease.holder() == "s1"


def test_a_second_session_is_refused_and_told_who_holds_it(lease: PageSlotLease) -> None:
    lease.acquire("s1")
    with pytest.raises(SlotConflict) as excinfo:
        lease.acquire("s2")
    assert excinfo.value.holder == "s1"
    assert lease.holder() == "s1", "a refused acquire must not change the holder"


def test_the_holder_can_re_enter_its_own_lease(lease: PageSlotLease) -> None:
    """Re-entering is normal: every primitive in one turn re-acquires."""

    lease.acquire("s1")
    lease.acquire("s1")
    assert lease.holder() == "s1"


def test_releasing_hands_the_page_to_the_next_session(lease: PageSlotLease) -> None:
    lease.acquire("s1")
    assert lease.release("s1") is True
    lease.acquire("s2")
    assert lease.holder() == "s2"


def test_only_the_holder_can_release(lease: PageSlotLease) -> None:
    lease.acquire("s1")
    assert lease.release("s2") is False
    assert lease.holder() == "s1"


def test_an_expired_lease_does_not_block_a_new_session() -> None:
    """A crashed session must not hold the browser hostage for the rest of the day."""

    db = Database(":memory:")
    db.migrate()
    lease = PageSlotLease(db, minutes=15)
    lease.acquire("s1")
    # Backdate the expiry rather than sleeping 15 minutes.
    db.execute(
        "UPDATE browser_slots SET expires_at = '2000-01-01T00:00:00' WHERE slot_id = 'browser'"
    )

    assert lease.holder() is None
    assert lease.acquire("s2")["state"] == "leased"
    assert lease.holder() == "s2"


def test_refresh_extends_only_this_sessions_lease(lease: PageSlotLease) -> None:
    lease.acquire("s1")
    assert lease.refresh("s1") is True
    assert lease.refresh("s2") is False


def test_sweeping_marks_stale_leases_expired(lease: PageSlotLease) -> None:
    lease.acquire("s1")
    lease._db.execute(
        "UPDATE browser_slots SET expires_at = '2000-01-01T00:00:00' WHERE slot_id = 'browser'"
    )
    assert lease.expire_stale() == 1
    row = lease._db.query_one("SELECT state FROM browser_slots WHERE slot_id = 'browser'")
    assert row["state"] == "expired"


def test_the_lease_is_durable_across_instances() -> None:
    """The lease lives in the store, so a restart does not hand the page to a
    second session while the first is still driving it."""

    db = Database(":memory:")
    db.migrate()
    PageSlotLease(db).acquire("s1")

    other = PageSlotLease(db)
    assert other.holder() == "s1"
    with pytest.raises(SlotConflict):
        other.acquire("s2")
