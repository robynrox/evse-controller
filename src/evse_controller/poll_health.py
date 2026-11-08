"""Rolling success rates for device poll loops.

A poll is one complete attempt to read a device, not one TCP retry inside it.
The tracker keeps a bounded history for the usual 1 minute, 5 minute and
1 hour windows and is safe to read from the Flask thread while the device
thread records results.
"""

from __future__ import annotations

from collections import deque
import threading
import time

DEFAULT_WINDOWS = (60, 300, 3600)


class PollHealth:
    """Success/failure history for one device's poll loop."""

    def __init__(self, windows=DEFAULT_WINDOWS, clock=time.monotonic):
        self._windows = tuple(windows)
        self._clock = clock
        self._events: deque[tuple[float, bool]] = deque()
        self._last_ok: float | None = None
        self._last_attempt: float | None = None
        self._consecutive_failures = 0
        self._lock = threading.Lock()

    def record(self, ok: bool) -> None:
        """Record one completed poll."""
        now = self._clock()
        with self._lock:
            self._events.append((now, bool(ok)))
            self._last_attempt = now
            if ok:
                self._last_ok = now
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
            self._prune(now)

    def _prune(self, now: float) -> None:
        cutoff = now - max(self._windows)
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def snapshot(self) -> dict:
        """Return JSON-friendly rates and ages for the dashboard."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            events = tuple(self._events)
            last_ok = self._last_ok
            last_attempt = self._last_attempt
            consecutive_failures = self._consecutive_failures

        windows = {}
        for window in self._windows:
            start = now - window
            sample = [ok for timestamp, ok in events if timestamp >= start]
            total = len(sample)
            successes = sum(1 for ok in sample if ok)
            windows[str(window)] = {
                "success": successes,
                "total": total,
                "pct": (
                    None if total == 0 else round(100.0 * successes / total, 1)
                ),
            }

        return {
            "windows": windows,
            "last_ok_seconds": (
                None if last_ok is None else round(now - last_ok, 1)
            ),
            "last_attempt_seconds": (
                None if last_attempt is None else round(now - last_attempt, 1)
            ),
            "consecutive_failures": consecutive_failures,
        }


def health_entry(key, label, device, configured=True):
    """One dashboard row for a device's rolling poll success."""
    health = getattr(device, "poll_health", None)
    entry = {
        "key": key,
        "label": label,
        "configured": bool(configured),
        "windows": {},
        "last_ok_seconds": None,
        "last_attempt_seconds": None,
        "consecutive_failures": None,
    }
    if health is None:
        return entry
    entry.update(health.snapshot())
    return entry


def poll_health_payload(controller, primary_url, secondary_url):
    """Shelly and Wallbox poll health for the status panel.

    Only configured devices are included, so an absent secondary Shelly does
    not look like a permanently failed link.
    """
    devices = []
    primary = getattr(controller, "pmon", None)
    if primary is not None:
        devices.append(
            health_entry(
                "shelly_primary",
                "Shelly EM 1",
                primary,
                configured=bool(primary_url),
            )
        )

    secondary = getattr(controller, "pmon2", None)
    if secondary is not None and secondary_url:
        devices.append(
            health_entry(
                "shelly_secondary",
                "Shelly EM 2",
                secondary,
                configured=True,
            )
        )

    wallbox = getattr(controller, "evse", None)
    if wallbox is not None and getattr(wallbox, "poll_health", None) is not None:
        label = (
            "Wallbox (sim)"
            if type(wallbox).__name__.startswith("Simulated")
            else "Wallbox"
        )
        devices.append(
            health_entry("wallbox", label, wallbox, configured=True)
        )
    return devices
