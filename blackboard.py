import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from models import Signal

CLAIM_TIMEOUT_SECONDS = 300


class Blackboard:
    def __init__(self, path: str):
        self._dir = Path(path) / "signals"
        self._dir.mkdir(parents=True, exist_ok=True)

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

    def _claim_path(self, signal_id: str) -> Path:
        return self._dir / f"{signal_id}.claim"

    def _try_atomic_claim(self, signal_id: str, agent_id: str) -> bool:
        claim_path = self._claim_path(signal_id)
        try:
            fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, agent_id.encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            # Check if stale
            try:
                mtime = claim_path.stat().st_mtime
                age = datetime.now().timestamp() - mtime
                if age > CLAIM_TIMEOUT_SECONDS:
                    claim_path.unlink(missing_ok=True)
                    # Retry once after removing stale claim
                    try:
                        fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        try:
                            os.write(fd, agent_id.encode())
                        finally:
                            os.close(fd)
                        return True
                    except FileExistsError:
                        return False
            except OSError:
                pass
            return False

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
        signals = self._load_all()
        candidates = [
            s for s in signals
            if s["type"] in responds_to and s["status"] in ("open", "claimed")
        ]

        for s in candidates:
            signal_id = s["id"]

            # Skip non-open unless we can reclaim a stale claimed signal
            if s["status"] == "claimed":
                claim_path = self._claim_path(signal_id)
                try:
                    mtime = claim_path.stat().st_mtime
                    age = datetime.now().timestamp() - mtime
                    if age <= CLAIM_TIMEOUT_SECONDS:
                        continue
                    # Stale — fall through to attempt atomic claim
                except OSError:
                    # No claim file but status=claimed: stale state, try to reclaim
                    pass

            if not self._try_atomic_claim(signal_id, agent_id):
                continue

            # We own the claim file — update JSON status
            now = datetime.now(timezone.utc).isoformat()
            try:
                current = json.loads(self._path(signal_id).read_text())
            except (json.JSONDecodeError, OSError):
                self._claim_path(signal_id).unlink(missing_ok=True)
                continue

            # Another agent may have already marked it done/claimed
            if current["status"] not in ("open", "claimed"):
                self._claim_path(signal_id).unlink(missing_ok=True)
                continue

            current.update(status="claimed", claimed_by=agent_id, claimed_at=now)
            self._path(signal_id).write_text(json.dumps(current))

            return Signal(
                id=signal_id,
                type=current["type"],
                payload=current["payload"],
                from_role=current["from_role"],
                created_at=current["created_at"],
                claimed_by=agent_id,
                status="claimed",
            )

        return None

    def unclaim(self, signal_id: str):
        p = self._path(signal_id)
        try:
            s = json.loads(p.read_text())
            s.update(status="open", claimed_by="", claimed_at="")
            p.write_text(json.dumps(s))
        except (json.JSONDecodeError, OSError):
            pass
        self._claim_path(signal_id).unlink(missing_ok=True)

    def mark_done(self, signal_id: str):
        p = self._path(signal_id)
        try:
            s = json.loads(p.read_text())
            s["status"] = "done"
            p.write_text(json.dumps(s))
        except (json.JSONDecodeError, OSError):
            pass
        self._claim_path(signal_id).unlink(missing_ok=True)

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
