import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCK_TIMEOUT_SECONDS = 600


class FileLockManager:
    def __init__(self, path: str):
        self._dir = Path(path) / "locks"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        safe = name.replace("/", "_").replace("\\", "_").replace(":", "_")
        return self._dir / f"{safe}.json"

    def _is_active(self, data: dict) -> bool:
        try:
            locked_at = datetime.fromisoformat(data["locked_at"])
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=LOCK_TIMEOUT_SECONDS)
            return locked_at > cutoff
        except (KeyError, ValueError):
            return False

    def acquire(self, name: str, agent_id: str) -> bool:
        p = self._path(name)
        if p.exists():
            try:
                data = json.loads(p.read_text())
                if self._is_active(data) and data.get("agent_id") != agent_id:
                    return False
            except (json.JSONDecodeError, OSError):
                pass
        p.write_text(json.dumps({
            "agent_id": agent_id,
            "locked_at": datetime.now(timezone.utc).isoformat(),
        }))
        return True

    def release_all(self, agent_id: str):
        for f in self._dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("agent_id") == agent_id:
                    f.unlink(missing_ok=True)
            except (json.JSONDecodeError, OSError):
                pass

    def get_all_locks(self) -> dict[str, str]:
        result = {}
        for f in self._dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if self._is_active(data):
                    result[f.stem] = data["agent_id"]
            except (json.JSONDecodeError, OSError):
                pass
        return result
