"""Blank optional form fields keep their current setting."""

import pytest

from evse_controller.web.form_values import (
    form_float,
    form_int,
    parse_shelly_monitor,
)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_blank_int_keeps_the_default(value):
    assert form_int(value, 7) == 7


@pytest.mark.parametrize("value", [None, "", "   "])
def test_blank_float_keeps_the_default(value):
    assert form_float(value, 1.5) == 1.5


def test_present_values_are_parsed():
    assert form_int("30", 7) == 30
    assert form_float("12.5", 1.5) == 12.5


def test_invalid_text_still_fails_the_save():
    with pytest.raises(ValueError):
        form_int("thirty", 7)


def test_shelly_monitor_parses_device_and_channel():
    assert parse_shelly_monitor("primary:1") == ("primary", 1)
    assert parse_shelly_monitor("primary:2") == ("primary", 2)
    assert parse_shelly_monitor("secondary:1") == ("secondary", 1)
    assert parse_shelly_monitor("secondary:2") == ("secondary", 2)


@pytest.mark.parametrize(
    "value", [None, "", "primary", "primary:3", "shelly1:1", "other:1"]
)
def test_shelly_monitor_rejects_invalid_or_blank_values(value):
    assert parse_shelly_monitor(value) == ("", None)
