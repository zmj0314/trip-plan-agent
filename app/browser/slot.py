"""One page per session (framework §11 "多 agent 抢页").

Two conversations driving the same browser is the failure this prevents: one
session navigates away while the other is mid-form, and the second one's click
lands somewhere else entirely. The rule is a lease, and a conflict is refused at
the moment it happens rather than discovered later as a wrong booking.

The lease is deliberately persisted (``browser_slots`` has existed since M0): an
in-process lock would forget a crashed session's lease and leave the page
permanently "in use", or worse, hand it to a second session that then fights a
browser tab nobody is driving.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.domain.timebase import now_local

#: How long a lease survives without being refreshed. Long enough to read a page
#: and fill a form; short enough that a crashed session does not hold the browser
#: hostage for the rest of the day.
DEFAULT_LEASE_MINUTES = 15


class SlotConflict(RuntimeError):
    """Another live session currently owns the page."""

    def __init__(self, holder: str, expires_at: str) -> None:
        super().__init__(f"浏览器页面已被会话 {holder} 占用（至 {expires_at}）")
        self.holder = holder
        self.expires_at = expires_at


class PageSlotLease:
    """Leases the single browser page to one session at a time."""

    def __init__(self, db: Any, *, minutes: int = DEFAULT_LEASE_MINUTES) -> None:
        self._db = db
        self._minutes = max(1, int(minutes))

    def _expiry(self) -> str:
        return (now_local() + timedelta(minutes=self._minutes)).isoformat()

    def acquire(self, session_id: str, *, slot_id: str = "browser") -> dict[str, Any]:
        """Take the lease, or raise :class:`SlotConflict` naming the holder.

        Sweep, read and write happen on one connection inside one transaction:
        splitting them would let a competing session slip between the check and
        the write, which is the exact race the lease exists to prevent.
        """

        now = now_local().isoformat()
        expires = self._expiry()
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE browser_slots SET state = 'expired' WHERE state = 'leased' AND expires_at <= ?",
                (now,),
            )
            row = conn.execute(
                "SELECT session_id, expires_at, state FROM browser_slots WHERE slot_id = ?",
                (slot_id,),
            ).fetchone()
            if row is not None and row["state"] == "leased" and row["session_id"] != session_id:
                raise SlotConflict(str(row["session_id"]), str(row["expires_at"]))
            conn.execute(
                """
                INSERT INTO browser_slots (slot_id, session_id, leased_at, expires_at, state)
                VALUES (?, ?, ?, ?, 'leased')
                ON CONFLICT(slot_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    leased_at  = excluded.leased_at,
                    expires_at = excluded.expires_at,
                    state      = 'leased'
                """,
                (slot_id, session_id, now, expires),
            )
        return {"slot_id": slot_id, "session_id": session_id, "expires_at": expires, "state": "leased"}

    def refresh(self, session_id: str, *, slot_id: str = "browser") -> bool:
        """Extend a lease this session already holds; ``False`` when it does not."""

        expires = self._expiry()
        with self._db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE browser_slots
                   SET expires_at = ?
                 WHERE slot_id = ? AND session_id = ? AND state = 'leased'
                """,
                (expires, slot_id, session_id),
            )
            return bool(cur.rowcount)

    def release(self, session_id: str, *, slot_id: str = "browser") -> bool:
        """Give the page back. Only the holder can release it."""

        released = now_local().isoformat()
        with self._db.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE browser_slots
                   SET state = 'released', expires_at = ?
                 WHERE slot_id = ? AND session_id = ? AND state = 'leased'
                """,
                (released, slot_id, session_id),
            )
            return bool(cur.rowcount)

    def holder(self, *, slot_id: str = "browser") -> str | None:
        """The session holding a *live* lease, if any."""

        now = now_local().isoformat()
        row = self._db.query_one(
            """
            SELECT session_id FROM browser_slots
             WHERE slot_id = ? AND state = 'leased' AND expires_at > ?
            """,
            (slot_id, now),
        )
        return str(row["session_id"]) if row is not None else None

    def expire_stale(self) -> int:
        """Sweep expired leases. Returns how many were cleared."""

        return int(
            self._db.execute(
                "UPDATE browser_slots SET state = 'expired' WHERE state = 'leased' AND expires_at <= ?",
                (now_local().isoformat(),),
            )
        )


__all__ = ["DEFAULT_LEASE_MINUTES", "PageSlotLease", "SlotConflict"]
