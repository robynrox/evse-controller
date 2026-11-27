"""Control-loop stall detection."""

import threading

from evse_controller.control_watchdog import (
    ControlLoopWatchdog,
    thread_stack_dump,
)


class _Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _named_frame():
    return thread_stack_dump()


def test_stack_dump_names_threads_and_frames():
    dump = _named_frame()

    assert "--- thread " in dump
    assert "test_stack_dump_names_threads_and_frames" in dump


def test_watchdog_is_quiet_while_the_loop_beats():
    clock = _Clock()
    watchdog = ControlLoopWatchdog(clock=clock, stall_seconds=120)

    clock.value = 60
    watchdog.beat()
    clock.value = 120

    assert watchdog.check() is None


def test_watchdog_reports_after_the_stall_threshold():
    clock = _Clock()
    watchdog = ControlLoopWatchdog(
        clock=clock, stall_seconds=120, repeat_seconds=300
    )
    watchdog.beat()

    clock.value = 119
    assert watchdog.check() is None

    clock.value = 121
    report = watchdog.check()

    assert report is not None
    assert "CONTROL LOOP STALLED for 121s" in report
    assert "--- thread " in report


def test_watchdog_repeats_only_after_the_repeat_interval():
    clock = _Clock()
    watchdog = ControlLoopWatchdog(
        clock=clock, stall_seconds=120, repeat_seconds=300
    )
    watchdog.beat()

    clock.value = 121
    assert watchdog.check() is not None

    clock.value = 200
    assert watchdog.check() is None

    clock.value = 422
    assert watchdog.check() is not None

    # A fresh heartbeat re-arms the watchdog; an 80 s gap is still quiet.
    watchdog.beat()
    clock.value = 502
    assert watchdog.check() is None


def test_watchdog_thread_is_a_daemon():
    watchdog = ControlLoopWatchdog()

    assert watchdog.daemon is True
    assert watchdog.name == "ControlLoopWatchdog"
    assert isinstance(watchdog, threading.Thread)
