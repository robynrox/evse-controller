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
    #: ``"manual"`` for a user-created one-off target, ``"schedule"`` for a
    #: target materialised by the recurring readiness schedule.
    source: str = "manual"
    schedule_id: str | None = None
    #: Scheduled targets are held until this time, then completed
    #: automatically.  Manual targets have no automatic release.
    release_at: datetime | None = None
    completion_reason: str | None = None
    completed_soc_percent: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.ready_by, str):
            self.ready_by = datetime.fromisoformat(self.ready_by)
        if isinstance(self.created_at, str):
            self.created_at = datetime.fromisoformat(self.created_at)
        if isinstance(self.completed_at, str):
            self.completed_at = datetime.fromisoformat(self.completed_at)
        if isinstance(self.release_at, str):
            self.release_at = datetime.fromisoformat(self.release_at)
        if self.status not in (ACTIVE, COMPLETE, CANCELLED):
            raise ValueError(f"Unknown readiness status: {self.status!r}")
        if self.source not in ("manual", "schedule"):
            raise ValueError(f"Unknown readiness source: {self.source!r}")
        self.target_soc_percent = float(self.target_soc_percent)
        if not 0.0 < self.target_soc_percent <= 100.0:
            raise ValueError("Target SoC must be between 0 and 100%")
        if self.completed_soc_percent is not None:
            self.completed_soc_percent = float(self.completed_soc_percent)

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
            "source": self.source,
            "schedule_id": self.schedule_id,
            "release_at": (
                None
                if self.release_at is None
                else self.release_at.isoformat()
            ),
            "completion_reason": self.completion_reason,
            "completed_soc_percent": self.completed_soc_percent,
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
            source=data.get("source", "manual"),
            schedule_id=data.get("schedule_id"),
            release_at=data.get("release_at"),
            completion_reason=data.get("completion_reason"),
            completed_soc_percent=data.get("completed_soc_percent"),
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
        """Create a manual target, replacing any current active one."""
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
                    existing.completion_reason = "replaced"
            target = EvReadinessTarget(
                target_soc_percent=target_soc_percent,
                ready_by=ready_by,
                source="manual",
            )
            self.targets.append(target)
            self.targets.sort(key=lambda item: item.created_at)
            self._save()
        return target

    def set_scheduled_target(
        self,
        target_soc_percent: float,
        ready_by: datetime,
        release_at: datetime,
        schedule_id: str,
    ) -> EvReadinessTarget | None:
        """Materialise a schedule target without replacing a manual one.

        Returns the existing target when the same occurrence is already
        active, or ``None`` when a manual target owns the readiness slot.
        """
        target_soc_percent = float(target_soc_percent)
        if not 0.0 < target_soc_percent <= 100.0:
            raise ValueError("Target SoC must be between 0 and 100%")
        if release_at < ready_by:
            raise ValueError(
                "The release time must not precede the target time"
            )
        with self._lock:
            active = self.active()
            if active is not None:
                if (
                    active.source == "schedule"
                    and active.schedule_id == schedule_id
                    and active.ready_by == ready_by
                ):
                    return active
                return None
            target = EvReadinessTarget(
                target_soc_percent=target_soc_percent,
                ready_by=ready_by,
                source="schedule",
                schedule_id=schedule_id,
                release_at=release_at,
            )
            self.targets.append(target)
            self.targets.sort(key=lambda item: item.created_at)
            self._save()
            return target

    def cancel(
        self,
        target_id: str | None = None,
        *,
        reason: str | None = None,
    ) -> bool:
        with self._lock:
            target = self._find(target_id)
            if target is None or not target.is_active:
                return False
            target.status = CANCELLED
            target.completed_at = datetime.now()
            target.completion_reason = reason
            self._save()
            return True

    def complete(
        self,
        target_id: str | None = None,
        *,
        reason: str | None = None,
        soc_percent: float | None = None,
    ) -> bool:
        with self._lock:
            target = self._find(target_id)
            if target is None or not target.is_active:
                return False
            target.status = COMPLETE
            target.completed_at = datetime.now()
            target.completion_reason = reason
            if soc_percent is not None:
                target.completed_soc_percent = float(soc_percent)
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
