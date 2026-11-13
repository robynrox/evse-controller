"""Decide when an OCPP handoff qualifies for off-peak import pricing.

The supplier's OCPP handoff alone is not enough.  A slot only counts as an
OCPP-charged slot when the Wallbox actually delivered power to the vehicle
under that handoff.  A short activation can be a schedule handshake or a
momentary command, so require at least a minute of continuous charging in the
slot before flagging it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


#: Continuous charging time needed before a slot is booked at the OCPP rate.
OCPP_MIN_CHARGE_SECONDS = 60.0


def slot_index(when: datetime) -> int:
    """Return the half-hour slot index for a local timestamp."""
    return when.hour * 2 + (1 if when.minute >= 30 else 0)


class OcppChargeTracker:
    """Track continuous OCPP-controlled charging within a slot."""

    def __init__(self, min_seconds: float = OCPP_MIN_CHARGE_SECONDS):
        self.min_seconds = max(0.0, float(min_seconds))
        self._started_at: Optional[datetime] = None
        self._slot: Optional[int] = None

    def reset(self) -> None:
        self._started_at = None
        self._slot = None

    def qualifies(self, now: datetime, *, charging: bool) -> bool:
        """Return True once charging has run long enough in *now*'s slot.

        The timer restarts when charging stops or when the slot changes, so a
        brief charge at the end of one slot cannot qualify the next one.
        """
        if not charging:
            self.reset()
            return False
        slot = slot_index(now)
        if self._started_at is None or self._slot != slot:
            self._started_at = now
            self._slot = slot
            return False
        elapsed = (now - self._started_at).total_seconds()
        return elapsed >= self.min_seconds


def should_flag_ocpp_slot(
    tracker: OcppChargeTracker,
    now: datetime,
    *,
    ocpp_active: bool,
    charging: bool,
) -> bool:
    """Return True when *now*'s slot should be booked at the OCPP rate."""
    if not ocpp_active:
        tracker.reset()
        return False
    return tracker.qualifies(now, charging=charging)
