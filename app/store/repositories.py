"""Repositories over the business tables (framework §4.3, DD-1).

Every read and write the orchestration shell performs goes through this module,
which keeps SQL out of the graph and keeps the persistence rules in one place:

* the idempotency key is the ledger's primary key, so "have I already started
  this action?" is answered by the database rather than by application logic;
* the key insert and the ``in_flight`` marker share one transaction (DI-5), so a
  crash cannot leave a key recorded as pending while the caller believes it
  started;
* only replayable events are persisted (DI-2) -- the stream's incremental
  deltas are rebuilt from a snapshot on reconnect, never replayed as text.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from app.domain.ids import new_id
from app.domain.timebase import now_local, to_ymd
from app.events.types import REPLAYABLE_EVENT_TYPES, Event
from app.store.db import Database


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(text: Any, default: Any) -> Any:
    if text in (None, ""):
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _stamp(value: datetime | None = None) -> str:
    return (value or now_local()).isoformat()


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
class SessionsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        *,
        session_id: str,
        user_id: str = "local",
        phase: str = "COLLECT",
        scope: Mapping[str, Any] | None = None,
        expires_at: datetime | None = None,
        created_at: datetime | None = None,
    ) -> str:
        stamp = _stamp(created_at)
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, user_id, phase, created_at,
                                      last_active_at, expires_at, scope_json, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(session_id) DO NOTHING
                """,
                (
                    session_id,
                    user_id,
                    phase,
                    stamp,
                    stamp,
                    expires_at.isoformat() if expires_at else None,
                    _dumps(dict(scope or {})),
                ),
            )
        return session_id

    def get(self, session_id: str) -> dict[str, Any] | None:
        record = _row(self.db.query_one("SELECT * FROM sessions WHERE session_id = ?", (session_id,)))
        if record is None:
            return None
        record["scope"] = _loads(record.pop("scope_json", None), {})
        return record

    def update_phase(self, session_id: str, phase: str) -> int:
        return self.db.execute(
            "UPDATE sessions SET phase = ?, last_active_at = ? WHERE session_id = ?",
            (str(phase), _stamp(), session_id),
        )

    def touch(self, session_id: str) -> int:
        return self.db.execute(
            "UPDATE sessions SET last_active_at = ? WHERE session_id = ?",
            (_stamp(), session_id),
        )

    def set_scope(self, session_id: str, scope: Mapping[str, Any]) -> int:
        return self.db.execute(
            "UPDATE sessions SET scope_json = ? WHERE session_id = ?",
            (_dumps(dict(scope)), session_id),
        )

    def set_suspended(self, session_id: str, *, until: datetime | None) -> int:
        """Park a session at a gate (DE-5). ``None`` clears the suspension."""

        return self.db.execute(
            "UPDATE sessions SET suspended_until = ? WHERE session_id = ?",
            (until.isoformat() if until else None, session_id),
        )

    def set_status(self, session_id: str, status: str) -> int:
        return self.db.execute("UPDATE sessions SET status = ? WHERE session_id = ?", (status, session_id))

    def list_expired(self, *, at: datetime | None = None) -> list[dict[str, Any]]:
        moment = _stamp(at)
        rows = self.db.query(
            """
            SELECT * FROM sessions
             WHERE status = 'active'
               AND ((expires_at IS NOT NULL AND expires_at <= ?)
                 OR (suspended_until IS NOT NULL AND suspended_until <= ?))
             ORDER BY created_at
            """,
            (moment, moment),
        )
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# plan versions
# --------------------------------------------------------------------------- #
class PlansRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(
        self,
        *,
        session_id: str,
        plan_hash: str,
        plan_version_id: str | None = None,
        version_no: int | None = None,
        slots_snapshot: Mapping[str, Any] | None = None,
        legs: Sequence[Any] | None = None,
        actions: Sequence[Any] | None = None,
        cost_estimate: float | None = None,
        degradation: Sequence[Any] | None = None,
        assumptions: Sequence[Any] | None = None,
        status: str = "draft",
        created_at: datetime | None = None,
    ) -> str:
        plan_version_id = plan_version_id or new_id("pv")
        with self.db.transaction() as conn:
            if version_no is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 AS next FROM plan_versions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                version_no = int(row["next"]) if row else 1
            conn.execute(
                """
                INSERT INTO plan_versions (plan_version_id, session_id, version_no, plan_hash,
                                           slots_snapshot_json, legs_json, actions_json, cost_estimate,
                                           degradation_json, assumptions_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_version_id,
                    session_id,
                    int(version_no),
                    plan_hash,
                    _dumps(dict(slots_snapshot or {})),
                    _dumps(list(legs or [])),
                    _dumps(list(actions or [])),
                    cost_estimate,
                    _dumps(list(degradation or [])),
                    _dumps(list(assumptions or [])),
                    status,
                    _stamp(created_at),
                ),
            )
        return plan_version_id

    def get(self, plan_version_id: str) -> dict[str, Any] | None:
        return _row(self.db.query_one(
            "SELECT * FROM plan_versions WHERE plan_version_id = ?", (plan_version_id,)
        ))

    def latest_for_session(self, session_id: str) -> dict[str, Any] | None:
        return _row(self.db.query_one(
            "SELECT * FROM plan_versions WHERE session_id = ? ORDER BY version_no DESC LIMIT 1",
            (session_id,),
        ))

    def set_status(self, plan_version_id: str, status: str) -> int:
        return self.db.execute(
            "UPDATE plan_versions SET status = ? WHERE plan_version_id = ?",
            (str(status), plan_version_id),
        )

    def supersede_all_but(self, session_id: str, *, keep: str, status: str = "superseded") -> int:
        """P3: agreeing to a new version invalidates the old one immediately."""

        return self.db.execute(
            "UPDATE plan_versions SET status = ? WHERE session_id = ? AND plan_version_id <> ?",
            (status, session_id, keep),
        )


# --------------------------------------------------------------------------- #
# consents
# --------------------------------------------------------------------------- #
class ConsentsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(
        self,
        *,
        session_id: str,
        plan_version_id: str,
        plan_hash: str,
        scope_kind: str = "gate2_full",
        action_id: str | None = None,
        channel: str | None = None,
        consent_id: str | None = None,
        granted_at: datetime | None = None,
    ) -> str:
        consent_id = consent_id or new_id("con")
        self.db.execute(
            """
            INSERT INTO consents (consent_id, session_id, plan_version_id, plan_hash,
                                  scope_kind, action_id, granted_at, channel, superseded_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                consent_id,
                session_id,
                plan_version_id,
                plan_hash,
                str(scope_kind),
                action_id,
                _stamp(granted_at),
                channel,
            ),
        )
        return consent_id

    def get(self, consent_id: str) -> dict[str, Any] | None:
        return _row(self.db.query_one("SELECT * FROM consents WHERE consent_id = ?", (consent_id,)))

    def get_active(self, session_id: str, plan_hash: str) -> dict[str, Any] | None:
        """The consent that authorises *this* plan version.

        A superseded consent must never authorise execution -- that is what makes
        "同意留痕" meaningful rather than decorative.
        """

        return _row(self.db.query_one(
            """
            SELECT * FROM consents
             WHERE session_id = ? AND plan_hash = ? AND superseded_by IS NULL
             ORDER BY granted_at DESC LIMIT 1
            """,
            (session_id, plan_hash),
        ))

    def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.query(
                "SELECT * FROM consents WHERE session_id = ? ORDER BY granted_at", (session_id,)
            )
        ]

    def supersede_all_for_session(self, session_id: str, *, by: str | None = None) -> int:
        return self.db.execute(
            "UPDATE consents SET superseded_by = ? WHERE session_id = ? AND superseded_by IS NULL",
            (by or new_id("con"), session_id),
        )


# --------------------------------------------------------------------------- #
# action ledger
# --------------------------------------------------------------------------- #
class LedgerRepo:
    """The idempotency ledger (P4).

    ``idem_key`` is the primary key. Insert-or-conflict *is* the duplicate
    check, which is why a reconnect cannot place a second booking: the second
    attempt loses the race in the database rather than in application code.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def insert_in_flight(
        self,
        *,
        idem_key: str,
        session_id: str,
        capability_id: str,
        plan_version_id: str | None = None,
        action_id: str | None = None,
        subject_fingerprint: str = "",
        params_hash: str = "",
        consent_ref: str | None = None,
    ) -> bool:
        """Return ``True`` when this attempt owns the key, ``False`` on duplicate.

        DI-5: the key insert and the ``in_flight`` marker happen inside one
        transaction, so there is no window in which a key exists but the action
        is not marked as started.
        """

        stamp = _stamp()
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO action_ledger (idem_key, session_id, plan_version_id, action_id,
                                           capability_id, subject_fingerprint, params_hash,
                                           consent_ref, status, attempt, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?)
                ON CONFLICT(idem_key) DO NOTHING
                """,
                (
                    idem_key,
                    session_id,
                    plan_version_id,
                    action_id,
                    capability_id,
                    subject_fingerprint,
                    params_hash,
                    consent_ref,
                    stamp,
                ),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                "UPDATE action_ledger SET status = 'in_flight' WHERE idem_key = ?",
                (idem_key,),
            )
        return True

    def get(self, idem_key: str) -> dict[str, Any] | None:
        return _row(self.db.query_one("SELECT * FROM action_ledger WHERE idem_key = ?", (idem_key,)))

    def mark_completed(self, idem_key: str, *, result_ref: str | None = None) -> int:
        return self.db.execute(
            """
            UPDATE action_ledger
               SET status = 'completed', finished_at = ?, result_ref = COALESCE(?, result_ref)
             WHERE idem_key = ?
            """,
            (_stamp(), result_ref, idem_key),
        )

    def mark_failed(self, idem_key: str, failure: Mapping[str, Any] | None = None) -> int:
        return self.db.execute(
            """
            UPDATE action_ledger
               SET status = 'failed', finished_at = ?, failure_json = ?
             WHERE idem_key = ?
            """,
            (_stamp(), _dumps(dict(failure or {})), idem_key),
        )

    def mark_completed_by_action(self, session_id: str, action_id: str, *, source: str = "user_receipt") -> int:
        """DH-6: only a user receipt may declare an order complete."""

        return self.db.execute(
            """
            UPDATE action_ledger
               SET status = 'completed', finished_at = ?, result_ref = ?
             WHERE session_id = ? AND action_id = ?
            """,
            (_stamp(), source, session_id, action_id),
        )

    def skip_remaining(self, session_id: str, *, plan_version_id: str | None = None) -> int:
        """Abandon everything still pending for a plan version."""

        sql = (
            "UPDATE action_ledger SET status = 'skipped', finished_at = ? "
            " WHERE session_id = ? AND status IN ('pending', 'in_flight')"
        )
        params: list[Any] = [_stamp(), session_id]
        if plan_version_id is not None:
            sql += " AND plan_version_id = ?"
            params.append(plan_version_id)
        return self.db.execute(sql, params)

    def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.query(
                "SELECT * FROM action_ledger WHERE session_id = ? ORDER BY started_at", (session_id,)
            )
        ]


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
class EventsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def append(self, session_id: str, event: Event) -> int:
        """Persist one replayable event and return its sequence number.

        DI-2: incremental deltas are *not* stored. Raising here is deliberate --
        the caller keeps such an event in memory for the live stream and does
        not get a fake cursor value for something the client cannot replay.
        """

        if event.type not in REPLAYABLE_EVENT_TYPES:
            raise ValueError(f"event type is not replayable: {event.type.value}")
        payload = {
            "id": event.id,
            "phase": event.phase.value,
            "plan_version_id": event.plan_version_id,
            "ts": event.ts.isoformat(),
            "data": event.data,
        }
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO events (session_id, type, payload_json, ts, replayable)
                VALUES (?, ?, ?, ?, 1)
                """,
                (session_id, event.type.value, _dumps(payload), event.ts.isoformat()),
            )
            return int(cur.lastrowid)

    def list_since(self, session_id: str, since: int = 0) -> list[dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT seq, session_id, type, payload_json, ts
              FROM events WHERE session_id = ? AND seq > ?
             ORDER BY seq
            """,
            (session_id, int(since)),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["payload"] = _loads(record.pop("payload_json", None), {})
            record["replay"] = True
            out.append(record)
        return out

    def latest_seq(self, session_id: str) -> int:
        row = self.db.query_one("SELECT COALESCE(MAX(seq), 0) AS seq FROM events WHERE session_id = ?", (session_id,))
        return int(row["seq"]) if row else 0

    def prune(self, *, before: datetime | str) -> int:
        cutoff = before if isinstance(before, str) else before.isoformat()
        return self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))


# --------------------------------------------------------------------------- #
# quota
# --------------------------------------------------------------------------- #
class QuotaRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def reserve(self, *, idem: str, pool_id: str, window_key: str, units: int) -> bool:
        """Reserve once per ``idem``; a repeat is a no-op, not a second charge."""

        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO quota_entries (idem, pool_id, window_key, units, state, ts)
                VALUES (?, ?, ?, ?, 'reserved', ?)
                ON CONFLICT(idem) DO NOTHING
                """,
                (idem, pool_id, window_key, int(units), _stamp()),
            )
            return cur.rowcount > 0

    def set_state(self, idem: str, state: str) -> int:
        return self.db.execute("UPDATE quota_entries SET state = ? WHERE idem = ?", (str(state), idem))

    def get(self, idem: str) -> dict[str, Any] | None:
        return _row(self.db.query_one("SELECT * FROM quota_entries WHERE idem = ?", (idem,)))

    def used(self, pool_id: str, window_key: str) -> int:
        """Committed plus reserved (framework §4.1.4)."""

        row = self.db.query_one(
            """
            SELECT COALESCE(SUM(units), 0) AS used FROM quota_entries
             WHERE pool_id = ? AND window_key = ? AND state IN ('reserved', 'committed')
            """,
            (pool_id, window_key),
        )
        return int(row["used"]) if row else 0


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #
class CacheRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(
        self,
        *,
        cache_key: str,
        capability_id: str,
        payload_json: str,
        provenance_json: str | None,
        fetched_at: str,
        ttl_seconds: int,
        expires_at: str,
        contains_pii: bool = False,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO cache_entries (cache_key, capability_id, payload_json, provenance_json,
                                       fetched_at, ttl_seconds, expires_at, contains_pii)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload_json = excluded.payload_json,
                provenance_json = excluded.provenance_json,
                fetched_at = excluded.fetched_at,
                ttl_seconds = excluded.ttl_seconds,
                expires_at = excluded.expires_at,
                contains_pii = excluded.contains_pii
            """,
            (
                cache_key,
                capability_id,
                payload_json,
                provenance_json,
                fetched_at,
                int(ttl_seconds),
                expires_at,
                1 if contains_pii else 0,
            ),
        )

    def get(self, cache_key: str) -> dict[str, Any] | None:
        return _row(self.db.query_one("SELECT * FROM cache_entries WHERE cache_key = ?", (cache_key,)))

    def purge_expired(self, *, before: datetime | str) -> int:
        cutoff = before if isinstance(before, str) else before.isoformat()
        return self.db.execute("DELETE FROM cache_entries WHERE expires_at < ?", (cutoff,))


# --------------------------------------------------------------------------- #
# receipts / audit / llm usage
# --------------------------------------------------------------------------- #
class ReceiptsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(
        self,
        *,
        session_id: str,
        action_id: str | None = None,
        idem_key: str | None = None,
        order_no: str | None = None,
        status: str = "completed",
        note: str | None = None,
        attachment_ref: str | None = None,
        receipt_id: str | None = None,
        created_at: datetime | None = None,
        **_: Any,
    ) -> str:
        receipt_id = receipt_id or new_id("rcp")
        self.db.execute(
            """
            INSERT INTO receipts (receipt_id, session_id, action_id, idem_key, order_no,
                                  status, note, attachment_ref, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                session_id,
                action_id,
                idem_key,
                order_no,
                str(status),
                note,
                attachment_ref,
                _stamp(created_at),
            ),
        )
        return receipt_id

    def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.query(
                "SELECT * FROM receipts WHERE session_id = ? ORDER BY created_at", (session_id,)
            )
        ]


class AuditRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        *,
        event_kind: str,
        session_id: str | None = None,
        actor: str = "system",
        subject: str | None = None,
        ref_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        audit_id: str | None = None,
        ts: datetime | None = None,
    ) -> str:
        audit_id = audit_id or new_id("aud")
        self.db.execute(
            """
            INSERT INTO audit_log (audit_id, session_id, ts, actor, event_kind,
                                   subject, ref_id, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                session_id,
                _stamp(ts),
                actor,
                str(event_kind),
                subject,
                ref_id,
                _dumps(dict(metadata or {})),
            ),
        )
        return audit_id

    def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.query(
                "SELECT * FROM audit_log WHERE session_id = ? ORDER BY ts", (session_id,)
            )
        ]


class LlmUsageRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(
        self,
        *,
        window_day: str,
        session_id: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        standard_equivalent: float = 0.0,
        ts: datetime | None = None,
    ) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO llm_usage (session_id, window_day, input_tokens, output_tokens,
                                       cached_input_tokens, standard_equivalent, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    window_day,
                    int(input_tokens),
                    int(output_tokens),
                    int(cached_input_tokens),
                    float(standard_equivalent),
                    _stamp(ts),
                ),
            )
            return int(cur.lastrowid)

    def totals(self, *, window_day: str | None = None, session_id: str | None = None) -> dict[str, float]:
        if window_day is None:
            window_day = to_ymd(now_local())
        sql = "SELECT COALESCE(SUM(standard_equivalent), 0) AS total FROM llm_usage WHERE window_day = ?"
        params: list[Any] = [window_day]
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        row = self.db.query_one(sql, params)
        return {"window_day": window_day, "standard_equivalent": float(row["total"]) if row else 0.0}


# --------------------------------------------------------------------------- #
# bundle
# --------------------------------------------------------------------------- #
@dataclass
class Repositories:
    """Everything the orchestration shell is allowed to persist."""

    db: Database
    sessions: SessionsRepo = field(init=False)
    plans: PlansRepo = field(init=False)
    consents: ConsentsRepo = field(init=False)
    ledger: LedgerRepo = field(init=False)
    events: EventsRepo = field(init=False)
    quota: QuotaRepo = field(init=False)
    cache: CacheRepo = field(init=False)
    receipts: ReceiptsRepo = field(init=False)
    audit: AuditRepo = field(init=False)
    llm_usage: LlmUsageRepo = field(init=False)

    def __post_init__(self) -> None:
        self.sessions = SessionsRepo(self.db)
        self.plans = PlansRepo(self.db)
        self.consents = ConsentsRepo(self.db)
        self.ledger = LedgerRepo(self.db)
        self.events = EventsRepo(self.db)
        self.quota = QuotaRepo(self.db)
        self.cache = CacheRepo(self.db)
        self.receipts = ReceiptsRepo(self.db)
        self.audit = AuditRepo(self.db)
        self.llm_usage = LlmUsageRepo(self.db)


def build_repositories(db: Database) -> Repositories:
    return Repositories(db)


__all__ = [
    "AuditRepo",
    "CacheRepo",
    "ConsentsRepo",
    "EventsRepo",
    "LedgerRepo",
    "LlmUsageRepo",
    "PlansRepo",
    "QuotaRepo",
    "ReceiptsRepo",
    "Repositories",
    "SessionsRepo",
    "build_repositories",
]
