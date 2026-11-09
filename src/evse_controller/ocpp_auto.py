"""Automatic OCPP handoff selection.

The supplier's OCPP schedule is useful periodically even when the heuristic
would otherwise keep control.  This module chooses the cheapest contiguous
block of half-hour slots inside a preferred window, avoids existing sessions,
and enforces a maximum 30-day gap between handoffs.

The selection is pure so it can be tested without the controller or hardware.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Callable, Iterable, Optional

from evse_controller.energy_sessions import (
    OCPP,
    SLOT_MINUTES,
    EnergySession,
    floor_to_slot,
)
from evse_controller.price_tiers import parse_clock

#: The handoff must happen at least this often, even if a user asks for a
#: longer interval.
MAX_GAP_DAYS = 30


def effective_frequency_days(frequency_days: int) -> int:
    """Clamp the configured interval to 1..30 days."""
    try:
        value = int(frequency_days)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(value, MAX_GAP_DAYS))


def _window_end_for(
    start: datetime, window_start_min: int, window_end_min: int
) -> Optional[datetime]:
    """End of the window occurrence containing *start*, or ``None``."""
    if window_start_min == window_end_min:
        return None
    start_min = start.hour * 60 + start.minute

    if window_start_min < window_end_min:
        if not (window_start_min <= start_min < window_end_min):
            return None
        end_day = start.date()
    else:
        # Wraps midnight: the evening part ends tomorrow, the morning part
        # ends today.
        if start_min >= window_start_min:
            end_day = start.date() + timedelta(days=1)
        elif start_min < window_end_min:
            end_day = start.date()
        else:
            return None

    return datetime.combine(end_day, time()) + timedelta(
        minutes=window_end_min
    )


def candidate_starts(
    now: datetime,
    *,
    duration_slots: int,
    window_start: str,
    window_end: str,
    days_ahead: int = 2,
):
    """Yield slot-aligned starts whose whole block fits inside the window."""
    duration_slots = max(1, int(duration_slots))
    duration = timedelta(minutes=SLOT_MINUTES * duration_slots)
    start_min = parse_clock(window_start)
    end_min = parse_clock(window_end)
    if start_min == end_min:
        return

    first = floor_to_slot(now)
    horizon = first + timedelta(days=max(1, int(days_ahead)))
    candidate = first
    while candidate < horizon:
        window_end_dt = _window_end_for(candidate, start_min, end_min)
        if window_end_dt is not None:
            block_end = candidate + duration
            if block_end <= window_end_dt and block_end > now:
                yield candidate
        candidate += timedelta(minutes=SLOT_MINUTES)


def select_ocpp_session(
    now: datetime,
    *,
    duration_slots: int,
    window_start: str,
    window_end: str,
    price_fn: Callable[[datetime], Optional[float]],
    sessions: Iterable[EnergySession],
    days_ahead: int = 2,
) -> Optional[EnergySession]:
    """Choose the cheapest non-overlapping OCPP block, or ``None``."""
    existing = [session for session in sessions if session.enabled]
    duration = timedelta(minutes=SLOT_MINUTES * max(1, int(duration_slots)))
    best = None
    best_price = float("inf")

    for start in candidate_starts(
        now,
        duration_slots=duration_slots,
        window_start=window_start,
        window_end=window_end,
        days_ahead=days_ahead,
    ):
        end = start + duration
        proposed = EnergySession(OCPP, start, end, auto=True)
        if any(proposed.overlaps(session) for session in existing):
            continue

        prices = []
        slot_start = start
        while slot_start < end:
            price = price_fn(slot_start)
            if price is None:
                prices = []
                break
            prices.append(float(price))
            slot_start += timedelta(minutes=SLOT_MINUTES)
        if not prices:
            continue

        average = sum(prices) / len(prices)
        if average < best_price - 1e-9:
            best = proposed
            best_price = average
    return best


def ocpp_due(
    now: datetime,
    sessions: Iterable[EnergySession],
    frequency_days: int,
) -> bool:
    """Whether another OCPP handoff is due.

    An upcoming or active handoff always suppresses a new one.  The interval
    is measured from the most recent handoff's start time, so an overnight
    window that ends after midnight cannot immediately trigger a second
    handoff in the same morning.
    """
    interval = effective_frequency_days(frequency_days)
    ocpp_sessions = [
        session
        for session in sessions
        if session.kind == OCPP and session.enabled
    ]
    if any(session.end > now for session in ocpp_sessions):
        return False
    if not ocpp_sessions:
        return True
    last_start = max(session.start for session in ocpp_sessions)
    return now >= last_start + timedelta(days=interval)


def maybe_schedule_auto_ocpp(
    now: datetime,
    session_store,
    *,
    enabled: bool,
    frequency_days: int,
    duration_slots: int,
    window_start: str,
    window_end: str,
    price_fn: Callable[[datetime], Optional[float]],
    days_ahead: int = 2,
) -> Optional[EnergySession]:
    """Create the next automatic handoff when one is due."""
    if not enabled:
        return None
    if not ocpp_due(now, session_store.sessions, frequency_days):
        return None
    session = select_ocpp_session(
        now,
        duration_slots=duration_slots,
        window_start=window_start,
        window_end=window_end,
        price_fn=price_fn,
        sessions=session_store.sessions,
        days_ahead=days_ahead,
    )
    if session is None:
        return None
    session_store.add(session)
    return session
