"""Blank optional form fields keep their current setting."""

import pytest

from evse_controller.web.form_values import form_float, form_int


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
