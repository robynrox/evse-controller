"""Per-slot heat-pump forecast with a learned temperature response.

The model is intentionally transparent:

- each half-hour slot learns its own daily profile, which captures regular
  domestic-hot-water cycles and thermostat/occupancy behaviour;
- within each slot, outdoor temperature bins refine the response;
- sparse temperature bins fall back to the slot profile, and then to the
  overall profile;
- P50 and P90 are stored so the planner can use expected and conservative
  values.

It does not assume a particular heat-pump configuration.  A system that runs
like a boiler, or one with different DHW timing, simply learns a different
profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
import statistics
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo


DEFAULT_TEMPERATURE_BIN_C = 2.0


@dataclass(frozen=True)
class HeatPumpSample:
    """One measured half-hour heat-pump observation."""

    when: datetime
    temperature_c: float
    power_w: float


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(
        0,
        min(
            len(ordered) - 1,
            int(round(fraction * (len(ordered) - 1))),
        ),
    )
    return ordered[index]


def _profile(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50": statistics.median(values),
        "p90": _percentile(values, 0.90),
    }


@dataclass
class HeatPumpModel:
    """A learned per-slot heat-pump power profile."""

    timezone: str = "Europe/London"
    temperature_bin_c: float = DEFAULT_TEMPERATURE_BIN_C
    min_samples: int = 5
    bins: dict[tuple[int, int], dict] = field(default_factory=dict)
    slot_profiles: dict[int, dict] = field(default_factory=dict)
    overall: dict = field(
        default_factory=lambda: {"n": 0, "p50": 0.0, "p90": 0.0}
    )
    samples: int = 0

    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception:
            return ZoneInfo("UTC")

    def slot_for(self, when: datetime) -> int:
        local = when.astimezone(self._zone())
        return local.hour * 2 + (1 if local.minute >= 30 else 0)

    def temperature_bin(self, temperature_c: float) -> int:
        return int(
            math.floor(float(temperature_c) / self.temperature_bin_c)
            * self.temperature_bin_c
        )

    def predict(
        self,
        when: datetime,
        temperature_c: float,
        *,
        quantile: str = "p50",
    ) -> float:
        """Predicted heat-pump power in watts."""
        key = (self.slot_for(when), self.temperature_bin(temperature_c))
        exact = self.bins.get(key)
        slot = self.slot_profiles.get(self.slot_for(when))
        if slot is None:
            base = float(self.overall.get(quantile, 0.0))
        else:
            base = float(slot.get(quantile, 0.0))
        if exact is not None and exact.get("n", 0) > 0:
            count = int(exact.get("n", 0))
            # Once a temperature bin has enough samples, trust it fully.
            # Sparse bins are blended toward the stable slot profile.
            weight = min(1.0, count / max(1, self.min_samples))
            base = (
                weight * float(exact.get(quantile, base))
                + (1.0 - weight) * base
            )
        return max(0.0, base)

    def to_dict(self) -> dict:
        return {
            "timezone": self.timezone,
            "temperature_bin_c": self.temperature_bin_c,
            "min_samples": self.min_samples,
            "bins": {
                f"{slot},{temperature}": profile
                for (slot, temperature), profile in sorted(
                    self.bins.items()
                )
            },
            "slot_profiles": {
                str(slot): profile
                for slot, profile in sorted(self.slot_profiles.items())
            },
            "overall": self.overall,
            "samples": self.samples,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "HeatPumpModel":
        bins = {}
        for key, profile in (payload.get("bins") or {}).items():
            try:
                slot, temperature = str(key).split(",", 1)
                bins[(int(slot), int(temperature))] = {
                    "n": int(profile.get("n", 0)),
                    "p50": float(profile.get("p50", 0.0)),
                    "p90": float(profile.get("p90", 0.0)),
                }
            except (TypeError, ValueError):
                continue
        slot_profiles = {}
        for key, profile in (payload.get("slot_profiles") or {}).items():
            try:
                slot_profiles[int(key)] = {
                    "n": int(profile.get("n", 0)),
                    "p50": float(profile.get("p50", 0.0)),
                    "p90": float(profile.get("p90", 0.0)),
                }
            except (TypeError, ValueError):
                continue
        return cls(
            timezone=str(payload.get("timezone", "Europe/London")),
            temperature_bin_c=float(
                payload.get("temperature_bin_c", DEFAULT_TEMPERATURE_BIN_C)
            ),
            min_samples=int(payload.get("min_samples", 5)),
            bins=bins,
            slot_profiles=slot_profiles,
            overall={
                "n": int((payload.get("overall") or {}).get("n", 0)),
                "p50": float(
                    (payload.get("overall") or {}).get("p50", 0.0)
                ),
                "p90": float(
                    (payload.get("overall") or {}).get("p90", 0.0)
                ),
            },
            samples=int(payload.get("samples", 0)),
        )


def fit_heatpump_model(
    samples: list[HeatPumpSample],
    *,
    timezone: str = "Europe/London",
    temperature_bin_c: float = DEFAULT_TEMPERATURE_BIN_C,
    min_samples: int = 5,
) -> HeatPumpModel:
    """Fit the per-slot temperature response."""
    if not samples:
        raise ValueError("No heat-pump samples to fit")
    if temperature_bin_c <= 0:
        raise ValueError("temperature_bin_c must be positive")
    model = HeatPumpModel(
        timezone=timezone,
        temperature_bin_c=float(temperature_bin_c),
        min_samples=max(1, int(min_samples)),
    )
    bin_values: dict[tuple[int, int], list[float]] = {}
    slot_values: dict[int, list[float]] = {}
    all_values: list[float] = []
    for sample in samples:
        slot = model.slot_for(sample.when)
        temperature = model.temperature_bin(sample.temperature_c)
        power = max(0.0, float(sample.power_w))
        bin_values.setdefault((slot, temperature), []).append(power)
        slot_values.setdefault(slot, []).append(power)
        all_values.append(power)
    model.bins = {
        key: _profile(values) for key, values in bin_values.items()
    }
    model.slot_profiles = {
        slot: _profile(values) for slot, values in slot_values.items()
    }
    model.overall = _profile(all_values)
    model.samples = len(samples)
    return model
