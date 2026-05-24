import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from models import Signal


CLAIM_TIMEOUT_SECONDS = 300


class Blackboard:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id         TEXT PRIMARY KEY,
                    type       TEXT NOT NULL,
                    payload    TEXT NOT NULL,
                    from_role  TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    claimed_by TEXT DEFAULT '',
                    claimed_at TEXT DEFAULT '',
                    status     TEXT DEFAULT 'open'
                )
            """)

    def write(self, signal: Signal):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO signals (id, type, payload, from_role, created_at, status) "
                "VALUES (?, ?, ?, ?, ?, 'open')",
                (signal.id, signal.type, json.dumps(signal.payload),
                 signal.from_role, signal.created_at),
            )

    def claim_next(self, responds_to: list[str], agent_id: str) -> Optional[Signal]:
        """Atomically claim the oldest open signal of matching type."""
        placeholders = ",".join("?" * len(responds_to))
        timeout_cutoff = (datetime.utcnow() - timedelta(seconds=CLAIM_TIMEOUT_SECONDS)).isoformat()

        with self._connect() as conn:
            conn.execute("BEGIN EXCLUSIVE")
            # Release stale claims first
            conn.execute(
                "UPDATE signals SET claimed_by='', claimed_at='', status='open' "
                "WHERE status='claimed' AND claimed_at < ?",
                (timeout_cutoff,),
            )
            row = conn.execute(
                f"SELECT * FROM signals WHERE type IN ({placeholders}) AND status='open' "
                "ORDER BY created_at ASC LIMIT 1",
                responds_to,
            ).fetchone()
            if row is None:
                return None
            now = datetime.utcnow().isoformat()
            conn.execute(
                "UPDATE signals SET status='claimed', claimed_by=?, claimed_at=? WHERE id=?",
                (agent_id, now, row["id"]),
            )
            return Signal(
                id=row["id"],
                type=row["type"],
                payload=json.loads(row["payload"]),
                from_role=row["from_role"],
                created_at=row["created_at"],
                claimed_by=agent_id,
                status="claimed",
            )

    def unclaim(self, signal_id: str):
        """Release claim, put signal back to open so another agent can take it."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE signals SET status='open', claimed_by='', claimed_at='' WHERE id=?",
                (signal_id,),
            )

    def mark_done(self, signal_id: str):
        with self._connect() as conn:
            conn.execute("UPDATE signals SET status='done' WHERE id=?", (signal_id,))

    def get_context(self, limit: int = 10) -> list[dict]:
        """Return recent done signals as context for agent prompts."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT type, payload, from_role, created_at FROM signals "
                "WHERE status='done' ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"type": r["type"], "from": r["from_role"],
             "at": r["created_at"], "payload": json.loads(r["payload"])}
            for r in rows
        ]

    def get_all_signals(self, limit: int = 30) -> list[dict]:
        """Return all signals regardless of status — used by integrator/judge."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT type, status, payload, from_role, created_at FROM signals "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"type": r["type"], "status": r["status"], "from": r["from_role"],
             "at": r["created_at"], "payload": json.loads(r["payload"])}
            for r in rows
        ]

    def has_signal_of_type(self, signal_type: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM signals WHERE type=? LIMIT 1", (signal_type,)
            ).fetchone()
        return row is not None
