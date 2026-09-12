"""SQLite persistence (framework §4.3, DD-1).

The business tables -- not the LangGraph checkpoint -- are the source of truth.
The checkpoint is a recovery vehicle for the conversation; an order's history
must survive even if the graph is rebuilt from scratch.

Three properties this module owns:

* **One writer.** SQLite in WAL mode still serialises writers, so every write
  goes through :meth:`Database.transaction`, which holds a process-wide lock.
  Readers use :meth:`Database.read` and never block on that lock.
* **Idempotent migration.** ``migrate()`` is safe to call on every start.
* **Constraints live in the schema.** Uniqueness that correctness depends on
  (``action_ledger.idem_key``) is enforced by the database, not by a
  read-then-write check that two processes can both win.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL DEFAULT 'local',
    phase           TEXT NOT NULL DEFAULT 'COLLECT',
    created_at      TEXT NOT NULL,
    last_active_at  TEXT NOT NULL,
    suspended_until TEXT,
    expires_at      TEXT,
    scope_json      TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS plan_versions (
    plan_version_id    TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    version_no         INTEGER NOT NULL,
    plan_hash          TEXT NOT NULL,
    slots_snapshot_json TEXT NOT NULL DEFAULT '{}',
    legs_json          TEXT NOT NULL DEFAULT '[]',
    actions_json       TEXT NOT NULL DEFAULT '[]',
    cost_estimate      REAL,
    degradation_json   TEXT NOT NULL DEFAULT '[]',
    assumptions_json   TEXT NOT NULL DEFAULT '[]',
    status             TEXT NOT NULL DEFAULT 'draft',
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consents (
    consent_id      TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    plan_version_id TEXT NOT NULL,
    plan_hash       TEXT NOT NULL,
    scope_kind      TEXT NOT NULL,
    action_id       TEXT,
    granted_at      TEXT NOT NULL,
    channel         TEXT,
    superseded_by   TEXT
);

CREATE TABLE IF NOT EXISTS action_ledger (
    idem_key            TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL,
    plan_version_id     TEXT,
    action_id           TEXT,
    capability_id       TEXT NOT NULL,
    subject_fingerprint TEXT NOT NULL DEFAULT '',
    params_hash         TEXT NOT NULL DEFAULT '',
    consent_ref         TEXT,
    status              TEXT NOT NULL DEFAULT 'in_flight',
    attempt             INTEGER NOT NULL DEFAULT 1,
    started_at          TEXT,
    finished_at         TEXT,
    result_ref          TEXT,
    evidence_json       TEXT NOT NULL DEFAULT '[]',
    failure_json        TEXT
);

CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    ts           TEXT NOT NULL,
    replayable   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS cache_entries (
    cache_key       TEXT PRIMARY KEY,
    capability_id   TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    provenance_json TEXT,
    fetched_at      TEXT NOT NULL,
    ttl_seconds     INTEGER NOT NULL,
    expires_at      TEXT NOT NULL,
    contains_pii    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quota_entries (
    idem       TEXT PRIMARY KEY,
    pool_id    TEXT NOT NULL,
    window_key TEXT NOT NULL,
    units      INTEGER NOT NULL DEFAULT 0,
    state      TEXT NOT NULL,
    ts         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_id     TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    action_id      TEXT,
    idem_key       TEXT,
    order_no       TEXT,
    status         TEXT NOT NULL,
    note           TEXT,
    attachment_ref TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id      TEXT PRIMARY KEY,
    session_id    TEXT,
    ts            TEXT NOT NULL,
    actor         TEXT NOT NULL DEFAULT 'system',
    event_kind    TEXT NOT NULL,
    subject       TEXT,
    ref_id        TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS browser_slots (
    slot_id    TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    leased_at  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'leased'
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id           TEXT,
    window_day           TEXT NOT NULL,
    input_tokens         INTEGER NOT NULL DEFAULT 0,
    output_tokens        INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens  INTEGER NOT NULL DEFAULT 0,
    standard_equivalent  REAL NOT NULL DEFAULT 0,
    ts                   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires      ON sessions (expires_at);
CREATE INDEX IF NOT EXISTS idx_plans_session         ON plan_versions (session_id, version_no);
CREATE INDEX IF NOT EXISTS idx_consents_hash         ON consents (session_id, plan_hash, superseded_by);
CREATE INDEX IF NOT EXISTS idx_ledger_session        ON action_ledger (session_id, plan_version_id);
CREATE INDEX IF NOT EXISTS idx_events_session        ON events (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_cache_expires         ON cache_entries (expires_at);
CREATE INDEX IF NOT EXISTS idx_quota_pool_window     ON quota_entries (pool_id, window_key);
CREATE INDEX IF NOT EXISTS idx_quota_state           ON quota_entries (state);
CREATE INDEX IF NOT EXISTS idx_receipts_session      ON receipts (session_id, action_id);
CREATE INDEX IF NOT EXISTS idx_usage_day             ON llm_usage (window_day);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = RLock()
        # An in-memory database exists only inside its connection, so every
        # "connection" for ``:memory:`` must be the same one. File-backed
        # databases get a fresh connection per operation instead.
        self._shared = str(path) == ":memory:"
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------- lifecycle
    def connect(self) -> sqlite3.Connection:
        """Open a connection for one operation.

        Connections are per-operation rather than shared: sqlite3 objects are
        bound to the thread that created them, and the API layer runs handlers
        on a thread pool. The exception is ``:memory:``, which only exists
        inside its connection and therefore reuses one.
        """

        if self._shared:
            if self._conn is None:
                self._conn = self._open()
            return self._conn
        return self._open()

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def migrate(self) -> None:
        """Create every table and index. Safe to run repeatedly."""

        # ``executescript`` implicitly commits any pending transaction, so it
        # must not run inside :meth:`transaction`; DDL is idempotent anyway.
        with self._lock:
            conn = self.connect()
            try:
                conn.executescript(SCHEMA_SQL)
            finally:
                self._release(conn)

    # --------------------------------------------------------------- access
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Single-writer, all-or-nothing block.

        The lock is held for the duration so a read-modify-write pair (insert
        the idempotency key, then mark it in flight) cannot interleave with a
        competing writer.
        """

        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - already rolled back
                    pass
                raise
            finally:
                self._release(conn)

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            self._release(conn)

    def _release(self, conn: sqlite3.Connection) -> None:
        if not self._shared:
            conn.close()

    def close(self) -> None:
        """Release a shared in-memory connection. A no-op for file databases."""

        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---------------------------------------------------------------- helpers
    def execute(self, sql: str, params: tuple | list = ()) -> int:
        """Run one statement in its own transaction; returns ``rowcount``."""

        with self.transaction() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self.read() as conn:
            return list(conn.execute(sql, params))

    def query_one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        with self.read() as conn:
            return conn.execute(sql, params).fetchone()


__all__ = ["SCHEMA_SQL", "Database"]
