"""Randomised OCPP switch windows."""

from datetime import datetime, timedelta
import random

import pytest

from evse_controller.ocpp_timing import switch_window


START = datetime(2026, 11, 25, 12, 0)
END = START + timedelta(minutes=30)


def test_switch_window_without_randomisation_uses_the_boundaries():
    window = switch_window(START, END, randomise=False)

    assert window.switch_in == START
    assert window.switch_out == END


def test_switch_window_respects_the_delay_ranges():
    window = switch_window(START, END, rng=random.Random(7))

    assert START <= window.switch_in <= START + timedelta(seconds=120)
    assert END - timedelta(seconds=180) <= window.switch_out <= END
    assert window.switch_in < window.switch_out


def test_switch_window_clamps_a_very_short_window():
    window = switch_window(
        START,
        START + timedelta(seconds=30),
        rng=random.Random(1),
    )

    assert window.switch_out == START + timedelta(seconds=30)
    assert window.switch_in <= window.switch_out


def test_switch_window_rejects_an_empty_window():
    with pytest.raises(ValueError):
        switch_window(START, START)
