import fcntl
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from models import Signal

CLAIM_TIMEOUT_SECONDS = 300


class Blackboard:
    def __init__(self, path: str):
        self._dir = Path(path) / "signals"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lockfile = Path(path) / ".claim_lock"
        self._lockfile.touch()

    def _load_all(self) -> list[dict]:
        result = []
        for f in self._dir.glob("*.json"):
            try:
                result.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                pass
        return sorted(result, key=lambda s: s["created_at"])

    def _path(self, signal_id: str) -> Path:
        return self._dir / f"{signal_id}.json"

    def write(self, signal: Signal):
        data = {
            "id": signal.id,
            "type": signal.type,
            "payload": signal.payload,
            "from_role": signal.from_role,
            "created_at": signal.created_at,
            "claimed_by": "",
            "claimed_at": "",
            "status": "open",
        }
        self._path(signal.id).write_text(json.dumps(data))

    def claim_next(self, responds_to: list[str], agent_id: str) -> Optional[Signal]:
        timeout_cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=CLAIM_TIMEOUT_SECONDS)
        ).isoformat()

        with open(self._lockfile, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                signals = self._load_all()
                for s in signals:
                    if s["status"] == "claimed" and s["claimed_at"] < timeout_cutoff:
                        s.update(status="open", claimed_by="", claimed_at="")
                        self._path(s["id"]).write_text(json.dumps(s))

                candidates = [
                    s for s in signals
                    if s["type"] in responds_to and s["status"] == "open"
                ]
                if not candidates:
                    return None

                chosen = candidates[0]
                now = datetime.now(timezone.utc).isoformat()
                chosen.update(status="claimed", claimed_by=agent_id, claimed_at=now)
                self._path(chosen["id"]).write_text(json.dumps(chosen))

                return Signal(
                    id=chosen["id"],
                    type=chosen["type"],
                    payload=chosen["payload"],
                    from_role=chosen["from_role"],
                    created_at=chosen["created_at"],
                    claimed_by=agent_id,
                    status="claimed",
                )
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def unclaim(self, signal_id: str):
        p = self._path(signal_id)
        try:
            s = json.loads(p.read_text())
            s.update(status="open", claimed_by="", claimed_at="")
            p.write_text(json.dumps(s))
        except (json.JSONDecodeError, OSError):
            pass

    def mark_done(self, signal_id: str):
        p = self._path(signal_id)
        try:
            s = json.loads(p.read_text())
            s["status"] = "done"
            p.write_text(json.dumps(s))
        except (json.JSONDecodeError, OSError):
            pass

    def get_all_signals(self, limit: int = 30) -> list[dict]:
        return [
            {
                "type": s["type"],
                "status": s["status"],
                "from": s["from_role"],
                "at": s["created_at"],
                "claimed_by": s.get("claimed_by", ""),
                "payload": s["payload"],
            }
            for s in self._load_all()[-limit:]
        ]

    def has_signal_of_type(self, signal_type: str) -> bool:
        return any(s["type"] == signal_type for s in self._load_all())

    def get_last(self, signal_type: str) -> dict | None:
        matches = [s for s in self._load_all() if s["type"] == signal_type]
        return matches[-1] if matches else None
