"""SQLite state for the command baseline mode.

State is used for idempotency across repeated Stop/SessionEnd hooks and for
recording which segment a batch of messages belongs to.  It is intentionally
small; the M2 sidecar can reuse the same schema.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import threading
from typing import Iterable, Optional, Set, Tuple

from .ids import span_id

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS sessions (
  session_id      TEXT PRIMARY KEY,
  transcript_path TEXT,
  version         TEXT,
  cwd             TEXT,
  created_at      TEXT,
  updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS segments (
  session_id      TEXT NOT NULL,
  prompt_id       TEXT NOT NULL,
  seq             INTEGER NOT NULL,
  trace_id        BLOB NOT NULL,
  root_span_id    BLOB NOT NULL,
  started_ns      INTEGER,
  ended_ns        INTEGER,
  created_at      TEXT,
  PRIMARY KEY (session_id, prompt_id, seq)
);

CREATE TABLE IF NOT EXISTS messages (
  session_id      TEXT NOT NULL,
  message_id      TEXT NOT NULL,
  prompt_id       TEXT,
  span_id         BLOB,
  emitted_at      TEXT,
  PRIMARY KEY (session_id, message_id)
);

CREATE TABLE IF NOT EXISTS tools (
  session_id      TEXT NOT NULL,
  tool_use_id     TEXT NOT NULL,
  prompt_id       TEXT,
  span_id         BLOB,
  emitted_at      TEXT,
  PRIMARY KEY (session_id, tool_use_id)
);

CREATE TABLE IF NOT EXISTS meta (
  key             TEXT PRIMARY KEY,
  value           TEXT
);
"""

# Run TTL cleanup at most once per this many seconds.
PRUNE_INTERVAL_S = 24 * 3600

# Tables whose rows belong to a session and are removed with it.
_SESSION_TABLES = ("segments", "messages", "tools")


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _now() -> str:
    return _utcnow().isoformat()


class StateStore:
    def __init__(self, state_dir: str):
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, "state.db")
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ensure_session(
        self,
        session_id: str,
        transcript_path: Optional[str] = None,
        version: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO sessions(session_id, transcript_path, version, cwd, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  transcript_path=COALESCE(excluded.transcript_path, sessions.transcript_path),
                  version=COALESCE(excluded.version, sessions.version),
                  cwd=COALESCE(excluded.cwd, sessions.cwd),
                  updated_at=excluded.updated_at
                """,
                (session_id, transcript_path, version, cwd, now, now),
            )

    def emitted_message_ids(self, session_id: str) -> Set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT message_id FROM messages WHERE session_id=?",
                (session_id,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def begin_segment(
        self,
        session_id: str,
        prompt_id: str,
        trace_id: int,
        started_ns: int,
    ) -> Tuple[int, int]:
        """Reserve the next segment and return ``(seq, root_span_id)``.

        The seq is computed inside a write transaction, so concurrent Stop and
        SessionEnd hooks cannot allocate the same segment.
        """

        trace_bytes = int(trace_id).to_bytes(16, "big")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), -1) + 1 FROM segments WHERE session_id=? AND prompt_id=?",
                    (session_id, prompt_id),
                ).fetchone()
                seq = int(row[0]) if row else 0
                root_sid = span_id(
                    "invoke_agent", "%s:%s:%d" % (session_id, prompt_id, seq)
                )
                self._conn.execute(
                    """
                    INSERT INTO segments(session_id, prompt_id, seq, trace_id, root_span_id, started_ns, created_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        prompt_id,
                        seq,
                        trace_bytes,
                        int(root_sid).to_bytes(8, "big"),
                        started_ns,
                        _now(),
                    ),
                )
                self._conn.commit()
                return seq, root_sid
            except Exception:
                self._conn.rollback()
                raise

    def finish_segment(
        self,
        session_id: str,
        prompt_id: str,
        seq: int,
        ended_ns: int,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE segments SET ended_ns=? WHERE session_id=? AND prompt_id=? AND seq=?",
                (ended_ns, session_id, prompt_id, seq),
            )

    def abort_segment(self, session_id: str, prompt_id: str, seq: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM segments WHERE session_id=? AND prompt_id=? AND seq=?",
                (session_id, prompt_id, seq),
            )

    def mark_messages(
        self,
        session_id: str,
        records: Iterable[Tuple[str, str, int]],
    ) -> None:
        """records: (message_id, prompt_id, span_id)."""

        now = _now()
        with self._lock, self._conn:
            self._conn.executemany(
                """
                INSERT INTO messages(session_id, message_id, prompt_id, span_id, emitted_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(session_id, message_id) DO NOTHING
                """,
                [
                    (
                        session_id,
                        message_id,
                        prompt_id,
                        int(span).to_bytes(8, "big"),
                        now,
                    )
                    for message_id, prompt_id, span in records
                ],
            )

    def mark_all_messages(
        self,
        session_id: str,
        message_ids: Iterable[Tuple[str, str]],
    ) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.executemany(
                """
                INSERT INTO messages(session_id, message_id, prompt_id, span_id, emitted_at)
                VALUES(?, ?, ?, NULL, ?)
                ON CONFLICT(session_id, message_id) DO NOTHING
                """,
                [(session_id, message_id, prompt_id, now) for message_id, prompt_id in message_ids],
            )

    def emitted_tool_ids(self, session_id: str) -> Set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tool_use_id FROM tools WHERE session_id=?",
                (session_id,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def tool_emitted(self, session_id: str, tool_use_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tools WHERE session_id=? AND tool_use_id=?",
                (session_id, tool_use_id),
            ).fetchone()
        return row is not None

    def mark_tool(
        self,
        session_id: str,
        tool_use_id: str,
        prompt_id: str,
        span_id: int,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO tools(session_id, tool_use_id, prompt_id, span_id, emitted_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(session_id, tool_use_id) DO NOTHING
                """,
                (
                    session_id,
                    tool_use_id,
                    prompt_id,
                    int(span_id).to_bytes(8, "big"),
                    _now(),
                ),
            )

    def mark_tools(
        self,
        session_id: str,
        records: Iterable[Tuple[str, str, int]],
    ) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.executemany(
                """
                INSERT INTO tools(session_id, tool_use_id, prompt_id, span_id, emitted_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(session_id, tool_use_id) DO NOTHING
                """,
                [
                    (
                        session_id,
                        tool_use_id,
                        prompt_id,
                        int(span).to_bytes(8, "big"),
                        now,
                    )
                    for tool_use_id, prompt_id, span in records
                ],
            )

    def tool_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM tools").fetchone()
        return int(row[0]) if row else 0

    def prune_expired(
        self, ttl_days: float, now: Optional[datetime.datetime] = None
    ) -> int:
        """Delete sessions idle for longer than ``ttl_days`` and their rows.

        A session counts as idle from its ``updated_at``, which every hook
        refreshes, so an active session is never pruned.  ``ttl_days <= 0``
        disables cleanup.  Returns the number of sessions removed.
        """

        if ttl_days <= 0:
            return 0
        now = now or _utcnow()
        cutoff = (now - datetime.timedelta(days=ttl_days)).isoformat()
        expired = "SELECT session_id FROM sessions WHERE updated_at < ?"
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for table in _SESSION_TABLES:
                    self._conn.execute(
                        "DELETE FROM %s WHERE session_id IN (%s)" % (table, expired),
                        (cutoff,),
                    )
                cur = self._conn.execute(
                    "DELETE FROM sessions WHERE updated_at < ?", (cutoff,)
                )
                self._conn.execute(
                    """
                    INSERT INTO meta(key, value) VALUES('last_prune_at', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (now.isoformat(),),
                )
                self._conn.commit()
                return int(cur.rowcount)
            except Exception:
                self._conn.rollback()
                raise

    def maybe_prune(
        self,
        ttl_days: float,
        interval_s: float = PRUNE_INTERVAL_S,
        now: Optional[datetime.datetime] = None,
    ) -> Optional[int]:
        """Run :meth:`prune_expired` unless it already ran within ``interval_s``.

        Returns the number of sessions removed, or ``None`` when skipped.
        """

        if ttl_days <= 0:
            return None
        now = now or _utcnow()
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='last_prune_at'"
            ).fetchone()
        if row and row[0]:
            try:
                last = datetime.datetime.fromisoformat(str(row[0]))
                if 0 <= (now - last).total_seconds() < interval_s:
                    return None
            except ValueError:
                pass
        return self.prune_expired(ttl_days, now)

    def health(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    def counts(self) -> Tuple[int, int, int]:
        with self._lock:
            sessions = self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            segments = self._conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
            messages = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return int(sessions), int(segments), int(messages)
