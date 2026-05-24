from dataclasses import dataclass, field
from datetime import datetime, timezone
import uuid


@dataclass
class Signal:
    type: str
    payload: dict
    from_role: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    claimed_by: str = ""
    status: str = "open"
