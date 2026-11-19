"""Recurring EV readiness schedule.

A schedule is a list of events.  Each event selects weekdays, a target time,
a target SoC and a hold period.  The scheduler materialises a normal one-off
readiness target for the next occurrence, so the planner, policy map and
dashboard do not need a second control path.

The target time is when the car should be ready.  The hold period starts at
that time, not when the SoC target is reached: the target remains a floor
until ``target_time + hold`` and is then completed automatically.  That is
the safety behaviour for a user who forgets to clear a target before a
holiday.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import json
import threading
import uuid

from evse_controller.ev_readiness import (
    EvReadinessStore,
    EvReadinessTarget,
    ev_readiness_store,
)
from evse_controller.utils.config import config
from evse_controller.utils.logging_config import error

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
HOLD_STEP_MINUTES = 30
DEFAULT_LEAD = timedelta(hours=24)


def format_days(days: list[int] | tuple[int, ...]) -> str:
    """A compact weekday summary, e.g. ``Mon–Thu``."""
    values = sorted({int(day) for day in days})
    if not values:
        return "No days"
    if len(values) == 7:
        return "Every day"
    groups: list[tuple[int, int]] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        groups.append((start, previous))
        start = previous = value
    groups.append((start, previous))
    parts = []
    for first, last in groups:
        if first == last:
            parts.append(DAY_NAMES[first])
        else:
            parts.append(f"{DAY_NAMES[first]}–{DAY_NAMES[last]}")
    return ", ".join(parts)


def format_hold(minutes: int) -> str:
    minutes = int(minutes)
    if minutes <= 0:
        return "No hold"
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours} h {remainder} m"
    if hours:
        return f"{hours} h"
    return f"{remainder} m"


def format_release(event: "RecurringReadinessEvent") -> str:
    """The time the floor is released, with a next-day marker if needed."""
    start = datetime.combine(date(2000, 1, 1), event.ready_time)
    release = start + timedelta(minutes=event.hold_minutes)
    text = release.strftime("%H:%M")
    if release.date() > start.date():
        text += " (+1 day)"
    return text


def time_options() -> list[str]:
    """Every selectable target time, on the hour or half hour."""
    return [
        f"{hour:02d}:{minute:02d}"
        for hour in range(24)
        for minute in (0, 30)
    ]


def hold_options() -> list[int]:
    """Selectable hold periods, in 30-minute steps up to 24 hours."""
    return list(range(0, 24 * 60 + 1, HOLD_STEP_MINUTES))


@dataclass
class RecurringReadinessEvent:
    """One weekly readiness event."""

    days: list[int]
    ready_time: time
    target_soc_percent: float
    hold_minutes: int = 0
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self) -> None:
        if isinstance(self.ready_time, str):
            self.ready_time = datetime.strptime(
                self.ready_time, "%H:%M"
            ).time()
        if isinstance(self.created_at, str):
            self.created_at = datetime.fromisoformat(self.created_at)
        if isinstance(self.updated_at, str):
            self.updated_at = datetime.fromisoformat(self.updated_at)
        self.days = sorted({int(day) for day in self.days})
        if not self.days:
            raise ValueError("Choose at least one day")
        if any(day < 0 or day > 6 for day in self.days):
            raise ValueError("Days must be between Monday and Sunday")
        if (
            self.ready_time.minute not in (0, 30)
            or self.ready_time.second
            or self.ready_time.microsecond
        ):
            raise ValueError(
                "The target time must be on the hour or half hour"
            )
        self.target_soc_percent = float(self.target_soc_percent)
        if not 0.0 < self.target_soc_percent <= 100.0:
            raise ValueError("Target SoC must be between 0 and 100%")
        self.hold_minutes = int(self.hold_minutes)
        if (
            self.hold_minutes < 0
            or self.hold_minutes > 24 * 60
            or self.hold_minutes % HOLD_STEP_MINUTES
        ):
            raise ValueError(
                "Hold time must be a 30-minute interval up to 24 hours"
            )
        self.enabled = bool(self.enabled)

    @property
    def ready_label(self) -> str:
        return self.ready_time.strftime("%H:%M")

    @property
    def days_label(self) -> str:
        return format_days(self.days)

    @property
    def hold_label(self) -> str:
        return format_hold(self.hold_minutes)

    @property
    def release_label(self) -> str:
        return format_release(self)

    def ready_by_on(self, day: date) -> datetime:
        return datetime.combine(day, self.ready_time)

    def release_on(self, day: date) -> datetime:
        return self.ready_by_on(day) + timedelta(
            minutes=self.hold_minutes
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "days": list(self.days),
            "ready_time": self.ready_time.strftime("%H:%M"),
            "target_soc_percent": self.target_soc_percent,
            "hold_minutes": self.hold_minutes,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RecurringReadinessEvent":
        return cls(
            days=data["days"],
            ready_time=data["ready_time"],
            target_soc_percent=data["target_soc_percent"],
            hold_minutes=data.get("hold_minutes", 0),
            enabled=data.get("enabled", True),
            id=data.get("id") or uuid.uuid4().hex[:12],
            created_at=data.get("created_at") or datetime.now(),
            updated_at=data.get("updated_at") or datetime.now(),
        )


class EvReadinessScheduleStore:
    """Thread-safe, file-backed weekly readiness events."""

    def __init__(self, path=None) -> None:
        self.path = path or config.EV_READINESS_SCHEDULE_FILE
        self._lock = threading.Lock()
        self.events: list[RecurringReadinessEvent] = []
        self.paused_until: date | None = None
        self.skipped: set[str] = set()
        self._load()

    def list(self) -> list[RecurringReadinessEvent]:
        return sorted(
            self.events,
            key=lambda event: (
                event.ready_time,
                event.days,
                event.id,
            ),
        )

    def get(self, event_id: str | None) -> RecurringReadinessEvent | None:
        for event in self.events:
            if event.id == event_id:
                return event
        return None

    def add(
        self, event: RecurringReadinessEvent
    ) -> RecurringReadinessEvent:
        with self._lock:
            self.events.append(event)
            self._save()
        return event

    def update(self, event: RecurringReadinessEvent) -> bool:
        with self._lock:
            for index, existing in enumerate(self.events):
                if existing.id == event.id:
                    event.updated_at = datetime.now()
                    self.events[index] = event
                    self._save()
                    return True
        return False

    def delete(self, event_id: str) -> bool:
        with self._lock:
            for index, event in enumerate(self.events):
                if event.id == event_id:
                    del self.events[index]
                    self._save()
                    return True
        return False

    def set_enabled(self, event_id: str, enabled: bool) -> bool:
        with self._lock:
            event = self.get(event_id)
            if event is None:
                return False
            event.enabled = bool(enabled)
            event.updated_at = datetime.now()
            self._save()
            return True

    def set_paused_until(self, paused_until: date | None) -> None:
        with self._lock:
            self.paused_until = paused_until
            self._save()

    def skip_occurrence(
        self, schedule_id: str, occurrence_date: date
    ) -> None:
        """Suppress one occurrence after the user cancels it."""
        with self._lock:
            self.skipped.add(
                self._skip_key(schedule_id, occurrence_date)
            )
            self._prune_skipped()
            self._save()

    def is_skipped(
        self, schedule_id: str, occurrence_date: date
    ) -> bool:
        return (
            self._skip_key(schedule_id, occurrence_date)
            in self.skipped
        )

    @staticmethod
    def _skip_key(schedule_id: str, occurrence_date: date) -> str:
        return f"{schedule_id}:{occurrence_date.isoformat()}"

    def _prune_skipped(self) -> None:
        """Keep the skip list bounded without depending on the wall clock."""
        if len(self.skipped) <= 500:
            return
        self.skipped = set(sorted(self.skipped)[-500:])

    def is_paused(self, now: datetime | None = None) -> bool:
        if self.paused_until is None:
            return False
        today = (now or datetime.now()).date()
        return today <= self.paused_until

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, list):
                raw_events = data
                raw_pause = None
                raw_skipped = []
            elif isinstance(data, dict):
                raw_events = data.get("events") or []
                raw_pause = data.get("paused_until")
                raw_skipped = data.get("skipped") or []
            else:
                raise ValueError("Unexpected schedule store format")
            self.events = [
                RecurringReadinessEvent.from_dict(item)
                for item in raw_events
            ]
            self.paused_until = (
                date.fromisoformat(str(raw_pause))
                if raw_pause
                else None
            )
            self.skipped = {
                str(key) for key in raw_skipped if key
            }
            self._prune_skipped()
        except (
            json.JSONDecodeError,
            KeyError,
            ValueError,
            TypeError,
        ) as exc:
            error(f"Error loading EV readiness schedule: {exc}")
            self.events = []
            self.paused_until = None

    def _save(self) -> None:
        """Write the events out (call with the lock held)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "paused_until": (
                            None
                            if self.paused_until is None
                            else self.paused_until.isoformat()
                        ),
                        "events": [
                            event.to_dict()
                            for event in self.events
                        ],
                        "skipped": sorted(self.skipped),
                    },
                    indent=2,
                )
            )
        except OSError as exc:
            error(f"Error saving EV readiness schedule: {exc}")


class EvReadinessScheduler:
    """Materialise and release scheduled readiness targets."""

    def __init__(
        self,
        target_store: EvReadinessStore | None = None,
        schedule_store: EvReadinessScheduleStore | None = None,
        *,
        lead: timedelta = DEFAULT_LEAD,
    ) -> None:
        self.target_store = target_store or ev_readiness_store
        self.schedule_store = (
            schedule_store or ev_readiness_schedule_store
        )
        self.lead = lead

    def tick(
        self,
        now: datetime | None = None,
        *,
        current_soc: float | None = None,
    ) -> EvReadinessTarget | None:
        """Release expired targets and materialise the next occurrence."""
        now = now or datetime.now()
        active = self.target_store.active()

        # A scheduled target always ends at its fixed release time, even if
        # the car reached the SoC target early.
        if (
            active is not None
            and active.source == "schedule"
            and active.release_at is not None
            and now >= active.release_at
        ):
            self.target_store.complete(
                active.id,
                reason="released",
                soc_percent=current_soc,
            )
            active = None

        # Disabling, deleting or pausing the event must release a target it
        # already materialised.
        if active is not None and active.source == "schedule":
            event = self.schedule_store.get(active.schedule_id)
            if event is None:
                self.target_store.cancel(
                    active.id, reason="schedule deleted"
                )
                active = None
            elif not event.enabled:
                self.target_store.cancel(
                    active.id, reason="schedule disabled"
                )
                active = None
            elif self.schedule_store.is_paused(now):
                self.target_store.cancel(
                    active.id, reason="schedule paused"
                )
                active = None

        if self.schedule_store.is_paused(now):
            return None

        if active is not None:
            return active

        candidate = self._next_candidate(now)
        if candidate is None:
            return None
        event, ready_by, release_at = candidate
        return self.target_store.set_scheduled_target(
            target_soc_percent=event.target_soc_percent,
            ready_by=ready_by,
            release_at=release_at,
            schedule_id=event.id,
        )

    def _next_candidate(
        self, now: datetime
    ) -> tuple[RecurringReadinessEvent, datetime, datetime] | None:
        lead_end = now + self.lead
        candidates = []
        for event in list(self.schedule_store.events):
            if not event.enabled:
                continue
            candidate = self._event_candidate(event, now, lead_end)
            if candidate is not None:
                candidates.append(candidate)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: (
                item[1],
                item[2],
                item[0].id,
            ),
        )

    def _event_candidate(
        self,
        event: RecurringReadinessEvent,
        now: datetime,
        lead_end: datetime,
    ) -> tuple[RecurringReadinessEvent, datetime, datetime] | None:
        for offset in range(0, self.lead.days + 2):
            day = now.date() + timedelta(days=offset)
            if day.weekday() not in event.days:
                continue
            if self.schedule_store.is_skipped(event.id, day):
                continue
            ready_by = event.ready_by_on(day)
            release_at = event.release_on(day)
            if release_at <= now:
                # This occurrence has already finished; try the next one.
                continue
            if ready_by > lead_end:
                # Later days are even further away, so this event has no
                # occurrence inside the lead window yet.
                return None
            return event, ready_by, release_at
        return None


#: Shared instances used by the control loop and the web app.
ev_readiness_schedule_store = EvReadinessScheduleStore()
ev_readiness_scheduler = EvReadinessScheduler(
    ev_readiness_store, ev_readiness_schedule_store
)
