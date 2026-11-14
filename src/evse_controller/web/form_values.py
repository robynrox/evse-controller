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


def parse_shelly_monitor(value):
    """Parse a combined ``device:channel`` monitor selection.

    Returns ``(device, channel)`` or ``("", None)`` for a blank or invalid
    selection.  The internal device keys stay ``primary``/``secondary`` so
    existing history and configuration remain valid; the UI calls them
    Shelly 1 and Shelly 2.
    """
    if not value:
        return "", None
    device, _, channel = str(value).partition(":")
    if device not in ("primary", "secondary"):
        return "", None
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        return "", None
    if channel not in (1, 2):
        return "", None
    return device, channel
