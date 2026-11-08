"""Execution modes and the temporary override that owns the Wallbox.

The configured tariff keeps running while an override drives the hardware.
These types are deliberately free of hardware imports so the ownership rules
can be unit-tested without starting the controller, MQTT or OCPP threads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ExecState(Enum):
    SMART = 1
    CHARGE = 2
    DISCHARGE = 3
    PAUSE = 4
    FIXED = 5
    SOLAR = 6
    POWER_HOME = 7
    BALANCE = 8
    PAUSE_UNTIL_DISCONNECT = 9
    FREERUN = 10
    OCPP = 11
    SOC_PROBE = 12


#: Human-readable labels for the dashboard.  These modes are temporary
#: overrides, not tariff modes, so the text says who owns the Wallbox.
OVERRIDE_LABELS = {
    ExecState.PAUSE: "Pause",
    ExecState.CHARGE: "Charge at maximum",
    ExecState.DISCHARGE: "Discharge at maximum",
    ExecState.FIXED: "Fixed current",
    ExecState.SOLAR: "Solar only",
    ExecState.POWER_HOME: "Power home",
    ExecState.BALANCE: "Bidirectional",
    ExecState.PAUSE_UNTIL_DISCONNECT: (
        "Remove plug — waiting for car to disconnect"
    ),
    ExecState.FREERUN: "Freerun",
    ExecState.OCPP: "OCPP handoff",
    ExecState.SOC_PROBE: "Checking battery level — 3 A probe",
}


def describe_override(
    override: ControlOverride,
    tariff_name: str = "no tariff",
) -> str:
    """Dashboard text for an active override."""
    label = OVERRIDE_LABELS.get(override.mode, override.mode.name.title())
    if override.mode == ExecState.FIXED and override.fixed_current is not None:
        label = f"Fixed current {override.fixed_current} A"
    return f"{label} (override; {tariff_name} observing)"


def soc_probe_complete(
    start_soc: Optional[float],
    current_soc: Optional[float],
    elapsed_seconds: float,
    timeout_seconds: float = 120.0,
) -> bool:
    """Whether the post-reconnect 3 A probe has learned a new SoC.

    It ends early when a previously unknown SoC becomes valid, or when a
    valid SoC changes.  If the pack reports the same value throughout, the
    timeout returns control to the tariff rather than charging indefinitely.
    """
    if elapsed_seconds >= timeout_seconds:
        return True
    if current_soc is None or current_soc < 0:
        return False
    if start_soc is None or start_soc < 0:
        return True
    return float(current_soc) != float(start_soc)


@dataclass
class ControlOverride:
    """A temporary owner of the Wallbox, while the tariff keeps observing."""

    mode: ExecState
    reason: str
    source: str = "dashboard"
    fixed_current: Optional[int] = None
    created_at: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None

    def is_active(self, now: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return True
        return (now or datetime.now()) < self.expires_at

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.name,
            "reason": self.reason,
            "source": self.source,
            "fixed_current": self.fixed_current,
            "created_at": self.created_at.isoformat(),
            "expires_at": (
                self.expires_at.isoformat()
                if self.expires_at is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Optional["ControlOverride"]:
        try:
            mode = ExecState[data["mode"]]
        except (KeyError, TypeError):
            return None
        expires_at = data.get("expires_at")
        try:
            expires = (
                datetime.fromisoformat(expires_at)
                if expires_at
                else None
            )
        except (TypeError, ValueError):
            expires = None
        return cls(
            mode=mode,
            reason=str(data.get("reason") or "override"),
            source=str(data.get("source") or "dashboard"),
            fixed_current=data.get("fixed_current"),
            expires_at=expires,
        )


def override_release_reason(
    override: Optional[ControlOverride],
    *,
    now: Optional[datetime] = None,
    vehicle_disconnected: bool = False,
) -> Optional[str]:
    """Why an override should hand the Wallbox back to the tariff, if at all.

    A timed override expires.  Pause-until-disconnect releases when the cable
    leaves.  Other overrides stay until the user explicitly chooses SMART or
    switches tariff, so they must not be released merely because the vehicle
    is unplugged.
    """
    if override is None:
        return None
    if not override.is_active(now):
        return "expired"
    if (
        override.mode == ExecState.PAUSE_UNTIL_DISCONNECT
        and vehicle_disconnected
    ):
        return "vehicle disconnected"
    return None
