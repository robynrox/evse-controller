"""One-off EV readiness targets.

A target is a requested state of charge by a ready-by time.  Before that time
the planner must reach the target; afterwards the target becomes a discharge
floor until the trip is complete or the vehicle is unplugged.  The store is
deliberately separate from supplier energy sessions: a readiness target is a
user requirement, not a price window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import threading
import uuid

from evse_controller.utils.config import config
from evse_controller.utils.logging_config import error

ACTIVE = "active"
COMPLETE = "complete"
CANCELLED = "cancelled"


@dataclass
class EvReadinessTarget:
    """A requested SoC by a ready-by time."""

    target_soc_percent: float
    ready_by: datetime
    status: str = ACTIVE
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if isinstance(self.ready_by, str):
            self.ready_by = datetime.fromisoformat(self.ready_by)
        if isinstance(self.created_at, str):
            self.created_at = datetime.fromisoformat(self.created_at)
        if isinstance(self.completed_at, str):
            self.completed_at = datetime.fromisoformat(self.completed_at)
        if self.status not in (ACTIVE, COMPLETE, CANCELLED):
            raise ValueError(f"Unknown readiness status: {self.status!r}")
        self.target_soc_percent = float(self.target_soc_percent)
        if not 0.0 < self.target_soc_percent <= 100.0:
            raise ValueError("Target SoC must be between 0 and 100%")

    @property
    def is_active(self) -> bool:
        return self.status == ACTIVE

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "target_soc_percent": self.target_soc_percent,
            "ready_by": self.ready_by.isoformat(),
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "completed_at": (
                None
                if self.completed_at is None
                else self.completed_at.isoformat()
            ),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EvReadinessTarget":
        return cls(
            target_soc_percent=data["target_soc_percent"],
            ready_by=data["ready_by"],
            status=data.get("status", ACTIVE),
            id=data.get("id") or uuid.uuid4().hex[:12],
            created_at=data.get("created_at") or datetime.now(),
            completed_at=data.get("completed_at"),
        )


class EvReadinessStore:
    """Thread-safe, file-backed one-off readiness targets."""

    def __init__(self, path=None) -> None:
        self.path = path or config.EV_READINESS_FILE
        self._lock = threading.Lock()
        self.targets: list[EvReadinessTarget] = []
        self._load()

    def active(self) -> EvReadinessTarget | None:
        """The most recent active target, if any."""
        active = [target for target in self.targets if target.is_active]
        if not active:
            return None
        return max(active, key=lambda target: target.created_at)

    def recent(self, limit: int = 10) -> list[EvReadinessTarget]:
        """Recent targets, newest first."""
        return sorted(
            self.targets,
            key=lambda target: target.created_at,
            reverse=True,
        )[:limit]

    def set_target(
        self, target_soc_percent: float, ready_by: datetime
    ) -> EvReadinessTarget:
        """Create a target, replacing any current active one."""
        target_soc_percent = float(target_soc_percent)
        if not 0.0 < target_soc_percent <= 100.0:
            raise ValueError("Target SoC must be between 0 and 100%")
        if ready_by <= datetime.now():
            raise ValueError("The ready-by time must be in the future")
        with self._lock:
            for existing in self.targets:
                if existing.is_active:
                    existing.status = CANCELLED
                    existing.completed_at = datetime.now()
            target = EvReadinessTarget(
                target_soc_percent=target_soc_percent,
                ready_by=ready_by,
            )
            self.targets.append(target)
            self.targets.sort(key=lambda item: item.created_at)
            self._save()
        return target

    def cancel(self, target_id: str | None = None) -> bool:
        with self._lock:
            target = self._find(target_id)
            if target is None or not target.is_active:
                return False
            target.status = CANCELLED
            target.completed_at = datetime.now()
            self._save()
            return True

    def complete(self, target_id: str | None = None) -> bool:
        with self._lock:
            target = self._find(target_id)
            if target is None or not target.is_active:
                return False
            target.status = COMPLETE
            target.completed_at = datetime.now()
            self._save()
            return True

    def complete_active(self) -> bool:
        return self.complete(None)

    def _find(self, target_id: str | None) -> EvReadinessTarget | None:
        if target_id is None:
            return self.active()
        for target in self.targets:
            if target.id == target_id:
                return target
        return None

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            self.targets = [
                EvReadinessTarget.from_dict(item) for item in data
            ]
            self.targets.sort(key=lambda item: item.created_at)
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            error(f"Error loading EV readiness targets: {exc}")
            self.targets = []

    def _save(self) -> None:
        """Write the targets out (call with the lock held)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    [target.to_dict() for target in self.targets],
                    indent=2,
                )
            )
        except OSError as exc:
            error(f"Error saving EV readiness targets: {exc}")


#: Shared instance used by the web app and the tariff manager.
ev_readiness_store = EvReadinessStore()
