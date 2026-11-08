"""Whole-slot energy sessions, EV availability and scheduled control modes.

Six kinds of window are supported, all expressed in neutral terms rather than
one supplier's marketing names:

``free_import``
    The supplier makes import free for the window (a "Happy Hour").  The tariff
    must treat the import price as 0p so the ledger books the energy at what it
    actually cost.

``grid_support``
    The supplier asks for reduced grid demand (a "Saving Session").  For a
    battery-backed installation the useful response is to export at maximum
    for the window.  The reward for doing so is not a price the tariff can see,
    so it is deliberately not modelled as one.

``ev_away``
    The user expects the EV to be disconnected for the window.  This is a
    planning assumption, not a control override: the 23:30 projection leaves
    those slots out because the pack will not be available, but if the EV is
    still connected the normal heuristic keeps running and may charge or
    discharge it.

``home_only``
    Cover the house load from the pack, conservatively, without planning an
    export.  This is a temporary control override; the tariff keeps observing.

``bidirectional``
    Allow load-following charge and discharge.  The tariff keeps observing.

``solar_only``
    Charge only from solar surplus, with no grid import.  The tariff keeps
    observing.

``pause``
    Hold the Wallbox dormant.  The tariff keeps observing.

``ocpp``
    Hand the Wallbox to the supplier's OCPP schedule.  The tariff keeps
    observing so history and pricing continue.

Control windows are temporary overrides, not scheduled tariff switches.  A
manual dashboard command still outranks them.

Windows are quantised to the half-hour tariff slots: ``start`` is floored and
``end`` is ceiled to a slot boundary, and ``end`` is exclusive.  Utilities bill
and pay on half-hour settlement periods, so anything finer would be a fiction.

The module owns a process-wide :data:`session_store` so the web UI and the
tariff manager see the same windows without threading a reference through the
tariff construction path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_type, datetime, time, timedelta
import json
import threading
import uuid

from evse_controller.utils.config import config
from evse_controller.utils.logging_config import error

FREE_IMPORT = "free_import"
GRID_SUPPORT = "grid_support"
EV_AWAY = "ev_away"
HOME_ONLY = "home_only"
BIDIRECTIONAL = "bidirectional"
SOLAR_ONLY = "solar_only"
PAUSE = "pause"
OCPP = "ocpp"

#: Kinds that temporarily own the Wallbox.  The tariff keeps observing.
CONTROL_SESSION_KINDS = frozenset(
    {HOME_ONLY, BIDIRECTIONAL, SOLAR_ONLY, PAUSE, OCPP}
)

#: Display labels.  Kept out of the stored data so the wording can change (or
#: be translated) without migrating the file.
SESSION_LABELS = {
    FREE_IMPORT: "Free Energy Session",
    GRID_SUPPORT: "Save Energy Session",
    EV_AWAY: "EV Away",
    HOME_ONLY: "Home only",
    BIDIRECTIONAL: "Bidirectional",
    SOLAR_ONLY: "Solar only",
    PAUSE: "Pause",
    OCPP: "OCPP handoff",
}

SLOT_MINUTES = 30
SLOTS_PER_DAY = 48


def floor_to_slot(when: datetime) -> datetime:
    """Start of the half-hour slot containing *when*."""
    minute = (when.minute // SLOT_MINUTES) * SLOT_MINUTES
    return when.replace(minute=minute, second=0, microsecond=0)


def ceil_to_slot(when: datetime) -> datetime:
    """First slot boundary at or after *when* (exclusive end convention)."""
    floored = floor_to_slot(when)
    return floored if when == floored else floored + timedelta(
        minutes=SLOT_MINUTES
    )


def session_from_slots(
    kind: str,
    start_slot,
    end_slot,
    day: date_type | None = None,
    now: datetime | None = None,
) -> "EnergySession":
    """Build a session from half-hour slot indices.

    ``start_slot`` is inclusive and ``end_slot`` is exclusive, so selecting the
    last slot of the day gives ``end_slot == 48`` and an end at midnight.
    Validation messages are written for the person using the web form.
    """
    now = now or datetime.now()
    day = day or now.date()

    if kind not in SESSION_LABELS:
        raise ValueError("Choose a session type")

    try:
        first = int(start_slot)
        last = int(end_slot)
    except (TypeError, ValueError):
        raise ValueError("Select one or more half-hour slots") from None

    if not (0 <= first < last <= SLOTS_PER_DAY):
        raise ValueError("Selected slots are out of range")

    midnight = datetime.combine(day, time())
    start = midnight + timedelta(minutes=SLOT_MINUTES * first)
    end = midnight + timedelta(minutes=SLOT_MINUTES * last)
    if end <= now:
        raise ValueError("That window has already ended")
    return EnergySession(kind, start, end)


@dataclass
class EnergySession:
    """A supplier-requested or EV-availability window, snapped to slots."""

    kind: str
    start: datetime
    end: datetime
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def __post_init__(self) -> None:
        if self.kind not in SESSION_LABELS:
            raise ValueError(f"Unknown session kind: {self.kind!r}")
        if isinstance(self.start, str):
            self.start = datetime.fromisoformat(self.start)
        if isinstance(self.end, str):
            self.end = datetime.fromisoformat(self.end)
        self.start = floor_to_slot(self.start)
        self.end = ceil_to_slot(self.end)
        if self.end == self.start:
            raise ValueError("A session must cover at least one slot")
        if self.end < self.start:
            # An end clock-time earlier than the start means the window runs
            # past midnight, matching how the scheduler reads overnight
            # windows.
            self.end += timedelta(days=1)

    @property
    def label(self) -> str:
        return SESSION_LABELS[self.kind]

    def covers(self, when: datetime) -> bool:
        return self.enabled and self.start <= when < self.end

    def slot_indices(self, day: date_type) -> list[int]:
        """Half-hour slots of *day* covered by this session."""
        midnight = datetime.combine(day, time())
        return [
            slot
            for slot in range(SLOTS_PER_DAY)
            if self.covers(midnight + timedelta(minutes=SLOT_MINUTES * slot))
        ]

    def overlaps(self, other: "EnergySession") -> bool:
        return self.start < other.end and other.start < self.end

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EnergySession":
        return cls(
            kind=data["kind"],
            start=data["start"],
            end=data["end"],
            enabled=data.get("enabled", True),
            id=data.get("id") or uuid.uuid4().hex[:12],
        )


class SessionStore:
    """Thread-safe, file-backed collection of energy sessions."""

    def __init__(self, path=None) -> None:
        self.path = path or config.SESSIONS_FILE
        self._lock = threading.Lock()
        self.sessions: list[EnergySession] = []
        self._load()

    # -- queries ---------------------------------------------------------

    def active(self, when: datetime) -> EnergySession | None:
        """The enabled session covering *when*, if any."""
        for session in self.sessions:
            if session.covers(when):
                return session
        return None

    def control_session(self, when: datetime) -> EnergySession | None:
        """The enabled control session covering *when*, if any.

        Supplier price windows and EV-availability windows are not control
        sessions: they change prices or the plan, not who owns the Wallbox.
        """
        session = self.active(when)
        if session is not None and session.kind in CONTROL_SESSION_KINDS:
            return session
        return None

    def upcoming(self, when: datetime) -> list[EnergySession]:
        """Sessions that have not finished yet, earliest first."""
        return sorted(
            (session for session in self.sessions if session.end > when),
            key=lambda session: session.start,
        )

    def slots_for_kind(self, day: date_type, kind: str) -> set[int]:
        """Slots of *day* covered by enabled sessions of *kind*."""
        slots: set[int] = set()
        for session in self.sessions:
            if session.kind == kind and session.enabled:
                slots.update(session.slot_indices(day))
        return slots

    def free_import_slots(self, day: date_type) -> set[int]:
        """Slots of *day* whose import price is zero."""
        return self.slots_for_kind(day, FREE_IMPORT)

    def away_slots(self, day: date_type) -> set[int]:
        """Slots of *day* during which the EV is planned to be away."""
        return self.slots_for_kind(day, EV_AWAY)

    def is_free_import(self, when: datetime) -> bool:
        session = self.active(when)
        return session is not None and session.kind == FREE_IMPORT

    # -- mutation --------------------------------------------------------

    def add(self, session: EnergySession) -> EnergySession:
        with self._lock:
            for existing in self.sessions:
                if existing.overlaps(session):
                    raise ValueError(
                        f"{session.label} overlaps the existing "
                        f"{existing.label} at "
                        f"{existing.start:%Y-%m-%d %H:%M}"
                    )
            self.sessions.append(session)
            self.sessions.sort(key=lambda item: item.start)
            self._save()
        return session

    def remove(self, session_id: str) -> bool:
        with self._lock:
            for session in self.sessions:
                if session.id == session_id:
                    self.sessions.remove(session)
                    self._save()
                    return True
        return False

    def set_enabled(self, session_id: str, enabled: bool) -> bool:
        with self._lock:
            for session in self.sessions:
                if session.id == session_id:
                    session.enabled = bool(enabled)
                    self._save()
                    return True
        return False

    def clear(self) -> None:
        with self._lock:
            self.sessions = []
            self._save()

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            self.sessions = [
                EnergySession.from_dict(item) for item in data
            ]
            self.sessions.sort(key=lambda session: session.start)
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            error(f"Error loading energy sessions: {exc}")
            self.sessions = []

    def _save(self) -> None:
        """Write the sessions out (call with the lock held)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    [session.to_dict() for session in self.sessions],
                    indent=2,
                )
            )
        except OSError as exc:
            error(f"Error saving energy sessions: {exc}")


#: Shared instance used by both the web app and the tariff manager.
session_store = SessionStore()
