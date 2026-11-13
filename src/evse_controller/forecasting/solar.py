"""Capped solar forecast with a learned sun-position shading mask.

The model is deliberately simple and transparent:

``P_dc = kWp × GTI/1000 × performance_ratio × shade(elevation, azimuth)``
``P_ac = min(P_dc, inverter_limit_kw)``

The shading mask is learned from the installation's own measurements.  It can
represent trees, chimneys, nearby buildings and roof geometry without knowing
what those obstructions are.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
import statistics
from typing import Any, Iterable, Mapping, Optional

from evse_controller.solar_position import solar_position


ELEVATION_BIN_DEGREES = 5.0
AZIMUTH_BIN_DEGREES = 15.0


@dataclass(frozen=True)
class SolarSample:
    """One measured hourly solar observation."""

    when: datetime
    gti_wm2: float
    actual_kw: float


@dataclass
class SolarModel:
    """A capped solar model with a learned sun-position mask."""

    kwp: float
    latitude: float
    longitude: float
    performance_ratio: float
    inverter_limit_kw: Optional[float] = None
    shading: dict[tuple[int, int], float] = field(default_factory=dict)
    min_samples: int = 5
    min_gti: float = 100.0
    pr_min_gti: float = 700.0
    samples: int = 0

    def bin_for(
        self, when: datetime
    ) -> Optional[tuple[int, int]]:
        elevation, azimuth = solar_position(
            when, self.latitude, self.longitude
        )
        if elevation <= 0:
            return None
        elevation_bin = int(
            math.floor(elevation / ELEVATION_BIN_DEGREES)
            * ELEVATION_BIN_DEGREES
        )
        azimuth_bin = int(
            math.floor(azimuth / AZIMUTH_BIN_DEGREES)
            * AZIMUTH_BIN_DEGREES
        )
        if azimuth_bin >= 360:
            azimuth_bin = int(360 - AZIMUTH_BIN_DEGREES)
        return elevation_bin, azimuth_bin

    def shading_factor(self, when: datetime) -> float:
        key = self.bin_for(when)
        if key is None:
            return 0.0
        return float(self.shading.get(key, 1.0))

    def predict(self, when: datetime, gti_wm2: float) -> float:
        """Predicted AC solar power in kW."""
        if gti_wm2 is None or gti_wm2 <= 0:
            return 0.0
        dc_kw = (
            self.kwp
            * float(gti_wm2)
            / 1000.0
            * self.performance_ratio
            * self.shading_factor(when)
        )
        if dc_kw <= 0:
            return 0.0
        if self.inverter_limit_kw is not None:
            return min(dc_kw, float(self.inverter_limit_kw))
        return dc_kw

    def to_dict(self) -> dict:
        return {
            "kwp": self.kwp,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "performance_ratio": self.performance_ratio,
            "inverter_limit_kw": self.inverter_limit_kw,
            "shading": {
                f"{elevation},{azimuth}": factor
                for (elevation, azimuth), factor in sorted(
                    self.shading.items()
                )
            },
            "min_samples": self.min_samples,
            "min_gti": self.min_gti,
            "pr_min_gti": self.pr_min_gti,
            "samples": self.samples,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SolarModel":
        shading = {}
        for key, factor in (payload.get("shading") or {}).items():
            try:
                elevation, azimuth = str(key).split(",", 1)
                shading[(int(elevation), int(azimuth))] = float(factor)
            except (TypeError, ValueError):
                continue
        return cls(
            kwp=float(payload["kwp"]),
            latitude=float(payload["latitude"]),
            longitude=float(payload["longitude"]),
            performance_ratio=float(payload["performance_ratio"]),
            inverter_limit_kw=(
                None
                if payload.get("inverter_limit_kw") in (None, 0)
                else float(payload["inverter_limit_kw"])
            ),
            shading=shading,
            min_samples=int(payload.get("min_samples", 5)),
            min_gti=float(payload.get("min_gti", 100.0)),
            pr_min_gti=float(payload.get("pr_min_gti", 700.0)),
            samples=int(payload.get("samples", 0)),
        )


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


def fit_solar_model(
    samples: Iterable[SolarSample],
    *,
    kwp: float,
    latitude: float,
    longitude: float,
    inverter_limit_kw: Optional[float] = None,
    min_samples: int = 5,
    min_gti: float = 100.0,
    pr_min_gti: float = 700.0,
    performance_percentile: float = 0.90,
) -> SolarModel:
    """Fit the performance ratio and the sun-position shading mask."""
    if kwp <= 0:
        raise ValueError("kWp must be positive")
    usable = [
        sample
        for sample in samples
        if sample.gti_wm2 >= min_gti and sample.actual_kw > 0
    ]
    if not usable:
        raise ValueError("No usable solar samples above the GTI threshold")

    def ratios_at(threshold: float) -> list[float]:
        values = []
        for sample in usable:
            if sample.gti_wm2 < threshold:
                continue
            if (
                inverter_limit_kw is not None
                and sample.actual_kw >= float(inverter_limit_kw) * 0.98
            ):
                continue
            dc_kw = kwp * sample.gti_wm2 / 1000.0
            if dc_kw > 0:
                values.append(sample.actual_kw / dc_kw)
        return values

    ratios = ratios_at(max(min_gti, pr_min_gti))
    if not ratios:
        ratios = ratios_at(min_gti)
    if not ratios:
        raise ValueError("No unclipped solar samples to fit")
    performance_ratio = max(
        0.0, min(1.0, _percentile(ratios, performance_percentile))
    )
    if performance_ratio <= 0:
        raise ValueError("Could not estimate a positive performance ratio")

    bins: dict[tuple[int, int], list[float]] = {}
    for sample in usable:
        if (
            inverter_limit_kw is not None
            and sample.actual_kw >= float(inverter_limit_kw) * 0.98
        ):
            continue
        dc_kw = (
            kwp
            * sample.gti_wm2
            / 1000.0
            * performance_ratio
        )
        if dc_kw <= 0:
            continue
        elevation, azimuth = solar_position(
            sample.when, latitude, longitude
        )
        if elevation <= 0:
            continue
        key = (
            int(
                math.floor(elevation / ELEVATION_BIN_DEGREES)
                * ELEVATION_BIN_DEGREES
            ),
            int(
                math.floor(azimuth / AZIMUTH_BIN_DEGREES)
                * AZIMUTH_BIN_DEGREES
            ),
        )
        factor = sample.actual_kw / dc_kw
        bins.setdefault(key, []).append(max(0.0, min(1.0, factor)))

    shading = {}
    for key, values in bins.items():
        median = statistics.median(values)
        count = len(values)
        # Pull sparse bins toward the unshaded value.  A bin with only one
        # sample should not create a permanent hard shadow.
        weight = count / (count + max(1, min_samples))
        shading[key] = median * weight + 1.0 * (1.0 - weight)

    return SolarModel(
        kwp=float(kwp),
        latitude=float(latitude),
        longitude=float(longitude),
        performance_ratio=float(performance_ratio),
        inverter_limit_kw=(
            None
            if inverter_limit_kw in (None, 0)
            else float(inverter_limit_kw)
        ),
        shading=shading,
        min_samples=int(min_samples),
        min_gti=float(min_gti),
        pr_min_gti=float(pr_min_gti),
        samples=len(usable),
    )
