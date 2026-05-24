import sqlite3
from datetime import datetime, timedelta


LOCK_TIMEOUT_SECONDS = 600


class FileLockManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_locks (
                    file_path  TEXT PRIMARY KEY,
                    agent_id   TEXT NOT NULL,
                    locked_at  TEXT NOT NULL
                )
            """)

    def acquire(self, file_path: str, agent_id: str) -> bool:
        """Try to acquire a write lock on file_path. Returns True if successful."""
        timeout_cutoff = (datetime.utcnow() - timedelta(seconds=LOCK_TIMEOUT_SECONDS)).isoformat()
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN EXCLUSIVE")
            # Release stale locks
            conn.execute("DELETE FROM file_locks WHERE locked_at < ?", (timeout_cutoff,))
            row = conn.execute(
                "SELECT agent_id FROM file_locks WHERE file_path=?", (file_path,)
            ).fetchone()
            if row is not None and row["agent_id"] != agent_id:
                return False
            conn.execute(
                "INSERT OR REPLACE INTO file_locks (file_path, agent_id, locked_at) VALUES (?,?,?)",
                (file_path, agent_id, now),
            )
        return True

    def release_all(self, agent_id: str):
        with self._connect() as conn:
            conn.execute("DELETE FROM file_locks WHERE agent_id=?", (agent_id,))

    def locked_by(self, file_path: str) -> str | None:
        """Return agent_id holding the lock, or None."""
        timeout_cutoff = (datetime.utcnow() - timedelta(seconds=LOCK_TIMEOUT_SECONDS)).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT agent_id FROM file_locks WHERE file_path=? AND locked_at >= ?",
                (file_path, timeout_cutoff),
            ).fetchone()
        return row["agent_id"] if row else None

    def get_all_locks(self) -> dict[str, str]:
        """Return {file_path: agent_id} for all active locks."""
        timeout_cutoff = (datetime.utcnow() - timedelta(seconds=LOCK_TIMEOUT_SECONDS)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT file_path, agent_id FROM file_locks WHERE locked_at >= ?",
                (timeout_cutoff,),
            ).fetchall()
        return {r["file_path"]: r["agent_id"] for r in rows}
