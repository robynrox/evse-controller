"""Detect and report a stalled controller loop.

The control loop is a single thread.  If one call blocks forever (a lock held
by a thread that is itself stuck, an I/O call that never returns, or an
accidental unbounded loop), the process still looks healthy: the web server
keeps answering and the hardware threads keep polling, but no plan is
refreshed and every queued override is ignored until someone restarts it.

This module keeps that failure from being silent.  The loop calls
:meth:`ControlLoopWatchdog.beat` on every pass; a daemon thread dumps every
live thread's stack when the loop makes no progress for a configured time.
The stack dump goes to the normal logger, so it lands in the rotated log file
next to the control messages.
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import Callable, Mapping, Optional

#: How long the loop may make no progress before stacks are dumped.
STALL_SECONDS = 120.0
#: Repeat the dump while the stall persists, but not on every check.
STALL_REPEAT_SECONDS = 300.0
#: How often the watchdog checks the heartbeat.
CHECK_INTERVAL_SECONDS = 5.0


def thread_stack_dump(
    frames: Optional[Mapping[int, object]] = None,
    names: Optional[Mapping[int, str]] = None,
) -> str:
    """Return a human-readable stack for every live thread."""
    if frames is None:
        frames = sys._current_frames()
    if names is None:
        names = {
            thread.ident: thread.name
            for thread in threading.enumerate()
            if thread.ident is not None
        }
    parts = []
    for ident, frame in frames.items():
        name = names.get(ident, f"thread-{ident}")
        parts.append(f"--- thread {name} ({ident}) ---")
        parts.append("".join(traceback.format_stack(frame)).rstrip())
    return "\n".join(parts)


class ControlLoopWatchdog(threading.Thread):
    """Daemon thread that reports a control loop which stops beating."""

    def __init__(
        self,
        *,
        stall_seconds: float = STALL_SECONDS,
        repeat_seconds: float = STALL_REPEAT_SECONDS,
        interval: float = CHECK_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        report: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(name="ControlLoopWatchdog", daemon=True)
        self.stall_seconds = max(1.0, float(stall_seconds))
        self.repeat_seconds = max(self.stall_seconds, float(repeat_seconds))
        self.interval = max(0.1, float(interval))
        self.clock = clock
        self.report = report or (lambda text: print(text, file=sys.stderr))
        self._stop = threading.Event()
        self._last_beat = clock()
        self._last_report_at: Optional[float] = None

    # -- heartbeat -------------------------------------------------------

    def beat(self) -> None:
        """Mark that the loop completed a pass."""
        self._last_beat = self.clock()
        self._last_report_at = None

    def stalled_for(self) -> float:
        """Seconds since the last heartbeat."""
        return max(0.0, self.clock() - self._last_beat)

    # -- lifecycle -------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def check(self) -> Optional[str]:
        """Return a stall report when one is due, else ``None``.

        Separate from :meth:`run` so the threshold and repeat behaviour can be
        tested without real sleeping.
        """
        stalled = self.stalled_for()
        if stalled < self.stall_seconds:
            self._last_report_at = None
            return None
        if (
            self._last_report_at is not None
            and stalled - self._last_report_at < self.repeat_seconds
        ):
            return None
        self._last_report_at = stalled
        return (
            "CONTROL LOOP STALLED for "
            f"{stalled:.0f}s without a tick; thread stacks follow:\n"
            + thread_stack_dump()
        )

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            text = self.check()
            if text:
                self.report(text)
