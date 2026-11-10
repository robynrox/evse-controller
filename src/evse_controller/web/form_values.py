"""Tolerant coercion for optional numeric fields in HTML forms.

Browsers submit a present-but-empty field as ``""`` rather than omitting it.
A blank field means "leave this setting alone", not "fail the whole save".
"""

from __future__ import annotations


def form_float(value, default):
    if value is None or str(value).strip() == "":
        return default
    return float(value)


def form_int(value, default):
    if value is None or str(value).strip() == "":
        return default
    return int(value)
