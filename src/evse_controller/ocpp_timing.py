"""Randomised OCPP switch-in and switch-out times for idle windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import random
from typing import Optional, Sequence


@dataclass(frozen=True)
class OcppSwitchWindow:
    start: datetime
    end: datetime
    switch_in: datetime
    switch_out: datetime

    def active(self, when: datetime) -> bool:
        return self.switch_in <= when < self.switch_out


def switch_window(
    start: datetime,
    end: datetime,
    *,
    randomise: bool = True,
    switch_in_seconds: Sequence[float] = (0.0, 120.0),
    switch_out_seconds: Sequence[float] = (60.0, 180.0),
    rng: Optional[random.Random] = None,
) -> OcppSwitchWindow:
    """Return start/end plus the randomised in and out instants.

    Both delays are independent.  A very short window is clamped so the
    switch-out instant never comes before the switch-in instant.
    """
    if end <= start:
        raise ValueError("an OCPP window must end after it starts")
    if not randomise:
        return OcppSwitchWindow(start, end, start, end)

    generator = rng or random.Random()
    in_low, in_high = (float(value) for value in switch_in_seconds)
    out_low, out_high = (float(value) for value in switch_out_seconds)
    in_delay = generator.uniform(min(in_low, in_high), max(in_low, in_high))
    out_lead = generator.uniform(min(out_low, out_high), max(out_low, out_high))
    switch_in = start + timedelta(seconds=in_delay)
    switch_out = end - timedelta(seconds=out_lead)
    if switch_in >= end:
        switch_in = start
    if switch_out <= switch_in:
        switch_out = end
    return OcppSwitchWindow(start, end, switch_in, switch_out)
