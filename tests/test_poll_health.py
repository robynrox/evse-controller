"""Rolling poll-success windows used by the dashboard link-health row."""

from evse_controller.poll_health import PollHealth


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _health():
    clock = FakeClock()
    return PollHealth(clock=clock), clock


def test_no_polls_yet_is_unknown_not_zero():
    health, _ = _health()

    snapshot = health.snapshot()

    assert snapshot["windows"]["60"]["pct"] is None
    assert snapshot["windows"]["60"]["total"] == 0
    assert snapshot["last_ok_seconds"] is None


def test_all_successes_report_one_hundred_percent():
    health, _ = _health()
    for _ in range(10):
        health.record(True)

    snapshot = health.snapshot()

    assert snapshot["windows"]["60"]["success"] == 10
    assert snapshot["windows"]["60"]["total"] == 10
    assert snapshot["windows"]["60"]["pct"] == 100.0


def test_mixed_results_report_the_window_rate():
    health, _ = _health()
    for ok in (True, True, False, True):
        health.record(ok)

    snapshot = health.snapshot()

    assert snapshot["windows"]["60"]["pct"] == 75.0
    assert snapshot["consecutive_failures"] == 0


def test_windows_prune_independently():
    health, clock = _health()
    health.record(True)
    clock.advance(61)
    health.record(False)
    clock.advance(1)

    snapshot = health.snapshot()

    assert snapshot["windows"]["60"]["total"] == 1
    assert snapshot["windows"]["60"]["pct"] == 0.0
    assert snapshot["windows"]["300"]["total"] == 2
    assert snapshot["windows"]["300"]["pct"] == 50.0


def test_last_success_age_and_consecutive_failures():
    health, clock = _health()
    health.record(True)
    clock.advance(5)
    health.record(False)
    clock.advance(2)
    health.record(False)

    snapshot = health.snapshot()

    assert snapshot["last_ok_seconds"] == 7.0
    assert snapshot["consecutive_failures"] == 2


def test_a_success_resets_the_failure_streak():
    health, _ = _health()
    health.record(False)
    health.record(False)
    health.record(True)

    assert health.snapshot()["consecutive_failures"] == 0


class _Device:
    def __init__(self):
        self.poll_health = PollHealth()


class _Controller:
    def __init__(self):
        self.pmon = _Device()
        self.pmon2 = _Device()
        self.evse = _Device()


def test_payload_includes_primary_and_configured_secondary():
    from evse_controller.poll_health import poll_health_payload

    payload = poll_health_payload(_Controller(), "primary", "secondary")

    assert [entry["key"] for entry in payload] == [
        "shelly_primary",
        "shelly_secondary",
        "wallbox",
    ]
    assert all(entry["configured"] for entry in payload)


def test_payload_hides_an_unconfigured_secondary():
    from evse_controller.poll_health import poll_health_payload

    payload = poll_health_payload(_Controller(), "primary", "")

    assert [entry["key"] for entry in payload] == [
        "shelly_primary",
        "wallbox",
    ]


def test_payload_labels_the_simulator():
    from evse_controller.poll_health import poll_health_payload

    controller = _Controller()

    class SimulatedWallboxThread:
        def __init__(self):
            self.poll_health = PollHealth()

    controller.evse = SimulatedWallboxThread()
    payload = poll_health_payload(controller, "primary", "")

    assert payload[-1]["label"] == "Wallbox (sim)"
