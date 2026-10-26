"""Pure analysis helpers for Wallbox / EVSE efficiency data.

This module contains no database or command-line code so that it can be
exercised by unit tests without InfluxDB, pandas or a live charger.  The
command-line wrapper lives in :mod:`evse_controller.tools.analyse_efficiency`.

The data model mirrors the fields written by ``EvseController``:

``ac_power``
    Signed AC power in watts at the Wallbox boundary
    (positive = charging/importing, negative = discharging/exporting).
``ac_voltage``
    AC voltage in volts.
``dc_voltage``
    DC voltage in volts at the charger output.
``dc_current``
    Signed DC current in amps at the charger output.
``batteryChargeLevel``
    Reported state of charge in percent, or ``-1`` when unknown.
``evseTargetCurrent``
    Signed EVSE current setpoint in amps.

Segmentation deliberately uses the *setpoint* as well as the direction: a
change of setpoint means the converter has moved to a new operating point and
lumping the two together would smear the efficiency measurement across
incompatible currents.

Efficiency is calculated from integrated energy, not from the ratio of mean
powers, because integrating is slightly more robust to dropped samples::

    charge:    eta = dc_energy / ac_energy
    discharge: eta = |ac_energy| / |dc_energy|

The loss is the difference between the magnitudes of the AC and DC power at
the charger boundary::

    charge:    loss = ac_power - dc_power
    discharge: loss = |dc_power| - |ac_power|

This is converter + cabling + contactor loss at the charger, not the battery's
internal I^2R loss (which is not visible at the Wallbox DC measurement).

Note that the earlier manual LeafSpy measurements were taken with the car
powered up, so they include the vehicle's 12 V DC-DC converter and any pumps or
fans.  Those are real losses for a planner, but they are not converter losses
and they bias the manual table down, especially at low current.  The Wallbox
data used here measures a different boundary; the SoC-based cross-check is the
closest available estimate of true AC-to-stored efficiency.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

DIRECTION_CHARGE = "charge"
DIRECTION_DISCHARGE = "discharge"
DIRECTIONS = (DIRECTION_CHARGE, DIRECTION_DISCHARGE)

# Physical plausibility bounds used when filtering steady-state windows.
MIN_PLAUSIBLE_EFFICIENCY = 0.2
MAX_PLAUSIBLE_EFFICIENCY = 1.02

SCHEMA_VERSION = 1


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _stdev(values: Sequence[float]) -> float | None:
    return statistics.stdev(values) if len(values) > 1 else None


def _weighted_mean(pairs: Iterable[tuple[float, float]]) -> float | None:
    """Return sum(weight * value) / sum(weight), or None if no weight."""
    total = 0.0
    weighted = 0.0
    for value, weight in pairs:
        if weight <= 0:
            continue
        total += weight
        weighted += value * weight
    return weighted / total if total else None


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ChargerSample:
    """One observation from the InfluxDB ``measurement`` series."""

    time: datetime
    ac_power_w: float | None = None
    ac_voltage_v: float | None = None
    dc_voltage_v: float | None = None
    dc_current_a: float | None = None
    soc: float | None = None
    target_current_a: float | None = None

    @property
    def dc_power_w(self) -> float | None:
        if self.dc_voltage_v is None or self.dc_current_a is None:
            return None
        return self.dc_voltage_v * self.dc_current_a

    @property
    def is_complete(self) -> bool:
        """True when the three fields needed for an efficiency point exist."""
        return (
            self.ac_power_w is not None
            and self.dc_voltage_v is not None
            and self.dc_current_a is not None
        )

    @property
    def has_soc(self) -> bool:
        return self.soc is not None and self.soc >= 0


def classify_direction(ac_power_w: float | None, deadband_w: float = 50.0) -> str | None:
    """Classify a sample as charge, discharge or idle.

    The deadband prevents 1 V / 0.1 A register noise around zero from being
    labelled as an active session.
    """
    if ac_power_w is None:
        return None
    if ac_power_w > deadband_w:
        return DIRECTION_CHARGE
    if ac_power_w < -deadband_w:
        return DIRECTION_DISCHARGE
    return None


def segment_samples(
    samples: Sequence[ChargerSample],
    *,
    max_gap_s: float = 10.0,
    deadband_w: float = 50.0,
    target_change_a: float = 0.5,
) -> list[list[ChargerSample]]:
    """Split samples into runs of steady direction and EVSE setpoint.

    A new segment starts when the measurement becomes idle/incomplete, when
    the direction changes, when the setpoint changes by more than
    ``target_change_a``, or when the gap between samples exceeds
    ``max_gap_s`` (e.g. the controller was restarted or InfluxDB missed data).
    """
    segments: list[list[ChargerSample]] = []
    current: list[ChargerSample] = []
    last_time: datetime | None = None
    last_direction: str | None = None
    last_target: float | None = None

    def flush() -> None:
        nonlocal current, last_time, last_direction, last_target
        if current:
            segments.append(current)
        current = []
        last_time = None
        last_direction = None
        last_target = None

    for sample in samples:
        direction = classify_direction(sample.ac_power_w, deadband_w)
        if direction is None or not sample.is_complete:
            flush()
            continue

        if current and last_time is not None:
            gap = (sample.time - last_time).total_seconds()
            target_changed = (
                sample.target_current_a is not None
                and last_target is not None
                and abs(sample.target_current_a - last_target) > target_change_a
            )
            if gap < 0 or gap > max_gap_s or direction != last_direction or target_changed:
                flush()

        current.append(sample)
        last_time = sample.time
        last_direction = direction
        last_target = sample.target_current_a

    flush()
    return segments


def _integrate_power(
    samples: Sequence[ChargerSample],
    accessor: Callable[[ChargerSample], float | None],
) -> float:
    """Trapezoidal integration of a power series, returning W.s."""
    total = 0.0
    for previous, current in zip(samples, samples[1:]):
        previous_value = accessor(previous)
        current_value = accessor(current)
        if previous_value is None or current_value is None:
            continue
        dt = (current.time - previous.time).total_seconds()
        if dt <= 0:
            continue
        total += 0.5 * (previous_value + current_value) * dt
    return total


@dataclass
class Window:
    """A steady sub-interval of a segment, used as one efficiency observation."""

    start: datetime
    end: datetime
    direction: str
    duration_s: float
    n_samples: int
    mean_ac_power_w: float
    mean_dc_power_w: float
    mean_ac_voltage_v: float | None
    mean_dc_voltage_v: float
    mean_abs_dc_current_a: float
    mean_soc: float | None
    mean_target_current_a: float | None
    ac_energy_wh: float
    dc_energy_wh: float
    ac_power_cv: float

    @property
    def efficiency(self) -> float | None:
        ac = self.ac_energy_wh if self.direction == DIRECTION_CHARGE else abs(self.ac_energy_wh)
        dc = self.dc_energy_wh if self.direction == DIRECTION_CHARGE else abs(self.dc_energy_wh)
        if ac <= 0 or dc <= 0:
            return None
        return dc / ac if self.direction == DIRECTION_CHARGE else ac / dc

    @property
    def loss_kw(self) -> float:
        if self.direction == DIRECTION_CHARGE:
            return (abs(self.mean_ac_power_w) - abs(self.mean_dc_power_w)) / 1000.0
        return (abs(self.mean_dc_power_w) - abs(self.mean_ac_power_w)) / 1000.0

    @property
    def dc_power_kw(self) -> float:
        return abs(self.mean_dc_power_w) / 1000.0

    @property
    def ac_power_kw(self) -> float:
        return abs(self.mean_ac_power_w) / 1000.0

    @property
    def ac_current_a(self) -> float | None:
        if not self.mean_ac_voltage_v or self.mean_ac_voltage_v <= 0:
            return None
        return abs(self.mean_ac_power_w) / self.mean_ac_voltage_v

    @property
    def representative_current_a(self) -> float:
        """Current used to bin the observation.

        Prefer the commanded setpoint because that is the variable the planner
        controls.  Fall back to measured AC current, then DC current.
        """
        if self.mean_target_current_a is not None and abs(self.mean_target_current_a) > 0.05:
            return abs(self.mean_target_current_a)
        if self.ac_current_a is not None:
            return self.ac_current_a
        return self.mean_abs_dc_current_a

    def to_row(self) -> dict[str, Any]:
        return {
            "start": _iso(self.start),
            "end": _iso(self.end),
            "direction": self.direction,
            "duration_s": _round(self.duration_s, 1),
            "n_samples": self.n_samples,
            "mean_ac_power_w": _round(self.mean_ac_power_w, 2),
            "mean_dc_power_w": _round(self.mean_dc_power_w, 2),
            "mean_ac_voltage_v": _round(self.mean_ac_voltage_v, 2),
            "mean_dc_voltage_v": _round(self.mean_dc_voltage_v, 3),
            "mean_abs_dc_current_a": _round(self.mean_abs_dc_current_a, 3),
            "mean_ac_current_a": _round(self.ac_current_a, 3),
            "mean_soc": _round(self.mean_soc, 2),
            "mean_target_current_a": _round(self.mean_target_current_a, 2),
            "efficiency": _round(self.efficiency),
            "loss_kw": _round(self.loss_kw),
            "ac_energy_wh": _round(self.ac_energy_wh, 3),
            "dc_energy_wh": _round(self.dc_energy_wh, 3),
            "ac_power_cv": _round(self.ac_power_cv),
        }


def _make_window(samples: Sequence[ChargerSample]) -> Window:
    ac_values = [s.ac_power_w for s in samples if s.ac_power_w is not None]
    dc_values = [s.dc_power_w for s in samples if s.dc_power_w is not None]
    socs = [s.soc for s in samples if s.has_soc]
    targets = [s.target_current_a for s in samples if s.target_current_a is not None]
    ac_voltages = [s.ac_voltage_v for s in samples if s.ac_voltage_v is not None]
    dc_voltages = [s.dc_voltage_v for s in samples if s.dc_voltage_v is not None]
    dc_currents = [abs(s.dc_current_a) for s in samples if s.dc_current_a is not None]

    direction = classify_direction(statistics.fmean(ac_values)) or DIRECTION_CHARGE
    duration = (samples[-1].time - samples[0].time).total_seconds()
    mean_ac = statistics.fmean(ac_values)
    mean_dc = statistics.fmean(dc_values)
    if mean_ac == 0:
        ac_cv = 0.0
    elif len(ac_values) > 1:
        ac_cv = statistics.pstdev(ac_values) / abs(mean_ac)
    else:
        ac_cv = 0.0

    return Window(
        start=samples[0].time,
        end=samples[-1].time,
        direction=direction,
        duration_s=duration,
        n_samples=len(samples),
        mean_ac_power_w=mean_ac,
        mean_dc_power_w=mean_dc,
        mean_ac_voltage_v=_mean(ac_voltages),
        mean_dc_voltage_v=statistics.fmean(dc_voltages),
        mean_abs_dc_current_a=statistics.fmean(dc_currents),
        mean_soc=_mean(socs),
        mean_target_current_a=_mean(targets),
        ac_energy_wh=_integrate_power(samples, lambda s: s.ac_power_w) / 3600.0,
        dc_energy_wh=_integrate_power(samples, lambda s: s.dc_power_w) / 3600.0,
        ac_power_cv=ac_cv,
    )


def build_windows(
    segments: Sequence[Sequence[ChargerSample]],
    *,
    window_seconds: float = 60.0,
    min_samples: int = 3,
    min_coverage: float = 0.5,
) -> list[Window]:
    """Divide each segment into fixed-length windows and summarise them.

    Windowing is what makes a long charge at one setpoint useful: it produces
    many observations across the SoC range instead of one averaged point.
    Partial windows shorter than ``min_coverage * window_seconds`` are dropped
    so their statistics are not dominated by a handful of samples.
    """
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")

    windows: list[Window] = []
    for segment in segments:
        if len(segment) < min_samples:
            continue
        origin = segment[0].time
        buckets: dict[int, list[ChargerSample]] = {}
        for sample in segment:
            index = int((sample.time - origin).total_seconds() // window_seconds)
            buckets.setdefault(index, []).append(sample)
        for index in sorted(buckets):
            bucket = buckets[index]
            if len(bucket) < min_samples:
                continue
            span = (bucket[-1].time - bucket[0].time).total_seconds()
            if span < min_coverage * window_seconds:
                continue
            windows.append(_make_window(bucket))
    return windows


def window_rejection_reason(
    window: Window,
    *,
    min_active_power_w: float = 100.0,
    min_current_a: float = 0.5,
    max_power_cv: float = 0.2,
    min_voltage_v: float = 50.0,
    max_voltage_v: float = 600.0,
) -> str | None:
    """Return why a window is unsuitable for steady-state analysis, or None."""
    if window.duration_s <= 0:
        return "zero_duration"
    if window.n_samples < 3:
        return "too_few_samples"
    if abs(window.mean_ac_power_w) < min_active_power_w:
        return "below_active_power"
    if window.mean_abs_dc_current_a < min_current_a:
        return "below_min_current"
    if not min_voltage_v <= window.mean_dc_voltage_v <= max_voltage_v:
        return "implausible_voltage"
    efficiency = window.efficiency
    if efficiency is None:
        return "no_efficiency"
    if not MIN_PLAUSIBLE_EFFICIENCY <= efficiency <= MAX_PLAUSIBLE_EFFICIENCY:
        return "implausible_efficiency"
    if window.loss_kw < 0:
        return "negative_loss"
    if window.ac_power_cv > max_power_cv:
        return "unsteady_power"
    return None


def filter_windows(
    windows: Sequence[Window],
    *,
    min_active_power_w: float = 100.0,
    min_current_a: float = 0.5,
    max_power_cv: float = 0.2,
    min_voltage_v: float = 50.0,
    max_voltage_v: float = 600.0,
) -> tuple[list[Window], dict[str, int]]:
    """Return accepted windows and a count of rejection reasons."""
    accepted: list[Window] = []
    rejected: Counter[str] = Counter()
    for window in windows:
        reason = window_rejection_reason(
            window,
            min_active_power_w=min_active_power_w,
            min_current_a=min_current_a,
            max_power_cv=max_power_cv,
            min_voltage_v=min_voltage_v,
            max_voltage_v=max_voltage_v,
        )
        if reason is None:
            accepted.append(window)
        else:
            rejected[reason] += 1
    return accepted, dict(sorted(rejected.items()))


def summarise_windows(windows: Sequence[Window]) -> dict[str, Any] | None:
    """Aggregate a group of same-direction windows."""
    if not windows:
        return None

    direction = windows[0].direction
    duration = sum(w.duration_s for w in windows)
    ac_energy = sum(abs(w.ac_energy_wh) for w in windows)
    dc_energy = sum(abs(w.dc_energy_wh) for w in windows)
    etas = [w.efficiency for w in windows if w.efficiency is not None]
    losses = [w.loss_kw for w in windows]

    if direction == DIRECTION_CHARGE:
        efficiency = dc_energy / ac_energy if ac_energy > 0 else None
    else:
        efficiency = ac_energy / dc_energy if dc_energy > 0 else None

    def weighted(attribute: str) -> float | None:
        pairs = []
        for window in windows:
            value = getattr(window, attribute)
            if value is not None:
                pairs.append((float(value), window.duration_s))
        return _weighted_mean(pairs)

    efficiency_stdev = _stdev(etas)
    loss_stdev = _stdev(losses)

    return {
        "direction": direction,
        "n_windows": len(windows),
        "duration_s": _round(duration, 1),
        "ac_energy_kwh": _round(ac_energy / 1000.0),
        "dc_energy_kwh": _round(dc_energy / 1000.0),
        "efficiency": _round(efficiency),
        "efficiency_mean": _round(_mean(etas)),
        "efficiency_median": _round(_median(etas)),
        "efficiency_stdev": _round(efficiency_stdev),
        "loss_kw_mean": _round(_mean(losses)),
        "loss_kw_median": _round(_median(losses)),
        "loss_kw_stdev": _round(loss_stdev),
        "mean_ac_power_kw": _round(weighted("ac_power_kw")),
        "mean_dc_power_kw": _round(weighted("dc_power_kw")),
        "mean_abs_dc_current_a": _round(weighted("mean_abs_dc_current_a")),
        "mean_ac_current_a": _round(weighted("ac_current_a")),
        "mean_dc_voltage_v": _round(weighted("mean_dc_voltage_v")),
        "mean_soc": _round(weighted("mean_soc"), 2),
    }


def bin_windows(
    windows: Sequence[Window],
    key: Callable[[Window], float | None],
    bin_size: float,
    *,
    min_windows: int = 1,
) -> list[dict[str, Any]]:
    """Group windows into fixed-width bins of ``key`` and summarise each."""
    if bin_size <= 0:
        raise ValueError("bin_size must be positive")

    groups: dict[float, list[Window]] = {}
    for window in windows:
        value = key(window)
        if value is None:
            continue
        low = round(math.floor(value / bin_size) * bin_size, 6)
        groups.setdefault(low, []).append(window)

    rows: list[dict[str, Any]] = []
    for low in sorted(groups):
        group = groups[low]
        if len(group) < min_windows:
            continue
        row = summarise_windows(group)
        if row is None:
            continue
        representative = _weighted_mean([(key(w), w.duration_s) for w in group if key(w) is not None])
        row.update(
            {
                "bin_low": low,
                "bin_high": round(low + bin_size, 6),
                "bin_center": round(low + bin_size / 2.0, 6),
                "key_mean": _round(representative, 6),
            }
        )
        rows.append(row)
    return rows


def group_by_direction(windows: Sequence[Window]) -> dict[str, list[Window]]:
    return {
        direction: [w for w in windows if w.direction == direction]
        for direction in DIRECTIONS
    }


def bin_by_current(
    windows: Sequence[Window],
    bin_size: float = 1.0,
) -> dict[str, list[dict[str, Any]]]:
    groups = group_by_direction(windows)
    result = {}
    for direction, group in groups.items():
        rows = bin_windows(group, lambda w: w.representative_current_a, bin_size)
        for row in rows:
            row["mean_current_a"] = row.pop("key_mean")
        result[direction] = rows
    return result


def bin_by_power(
    windows: Sequence[Window],
    bin_size_kw: float = 0.25,
) -> dict[str, list[dict[str, Any]]]:
    groups = group_by_direction(windows)
    return {
        direction: bin_windows(group, lambda w: w.dc_power_kw, bin_size_kw)
        for direction, group in groups.items()
    }


def bin_by_soc(
    windows: Sequence[Window],
    bin_size_percent: float = 5.0,
) -> dict[str, list[dict[str, Any]]]:
    groups = group_by_direction(windows)
    return {
        direction: bin_windows(group, lambda w: w.mean_soc, bin_size_percent)
        for direction, group in groups.items()
    }


def fit_loss_model(
    windows: Sequence[Window],
    direction: str,
    *,
    x_kind: str = "power",
) -> dict[str, Any] | None:
    """Weighted least-squares fit of ``loss_kw = a + b * x``.

    ``x_kind="power"`` uses ``|P_dc|^2`` in kW^2 (the physical I^2R form when
    voltage is roughly constant).  ``x_kind="current"`` uses ``I_dc^2`` in A^2.

    The slope's t-statistic is reported so callers can decide whether the
    power-dependent term is significant or whether a constant offset is an
    adequate model.
    """
    if x_kind not in ("power", "current"):
        raise ValueError("x_kind must be 'power' or 'current'")

    rows: list[tuple[float, float, float]] = []
    for window in windows:
        if window.direction != direction:
            continue
        efficiency = window.efficiency
        if efficiency is None:
            continue
        if x_kind == "power":
            x = window.dc_power_kw**2
        else:
            x = window.mean_abs_dc_current_a**2
        if window.duration_s <= 0:
            continue
        rows.append((x, window.loss_kw, window.duration_s))

    if len(rows) < 3:
        return None

    weight_sum = sum(weight for _, _, weight in rows)
    if weight_sum <= 0:
        return None
    x_bar = sum(weight * x for x, _, weight in rows) / weight_sum
    y_bar = sum(weight * y for _, y, weight in rows) / weight_sum
    sxx = sum(weight * (x - x_bar) ** 2 for x, _, weight in rows)
    if sxx <= 0:
        return None
    sxy = sum(weight * (x - x_bar) * (y - y_bar) for x, y, weight in rows)

    slope = sxy / sxx
    intercept = y_bar - slope * x_bar

    residuals = [y - (intercept + slope * x) for x, y, _ in rows]
    ss_res = sum(weight * residual**2 for residual, (_, _, weight) in zip(residuals, rows))
    ss_tot = sum(weight * (y - y_bar) ** 2 for _, y, weight in rows)
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    rmse = math.sqrt(ss_res / weight_sum)

    dof = len(rows) - 2
    slope_stderr = None
    t_statistic = None
    if dof > 0:
        sigma_squared = ss_res / dof
        slope_stderr = math.sqrt(sigma_squared / sxx)
        if slope_stderr > 0:
            t_statistic = slope / slope_stderr

    return {
        "direction": direction,
        "x_kind": x_kind,
        "n_windows": len(rows),
        "intercept_kw": _round(intercept),
        "slope": _round(slope, 9),
        "slope_units": "kW per kW^2" if x_kind == "power" else "kW per A^2",
        "slope_stderr": _round(slope_stderr, 9),
        "slope_t_statistic": _round(t_statistic, 3),
        "r_squared": _round(r_squared, 4),
        "rmse_kw": _round(rmse, 6),
        "mean_loss_kw": _round(y_bar),
        "mean_x": _round(x_bar, 9),
    }


def round_trip_table(
    by_current: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Combine per-current charge/discharge efficiencies into round-trip rows."""
    charge = {row["bin_low"]: row for row in by_current.get(DIRECTION_CHARGE, [])}
    discharge = {row["bin_low"]: row for row in by_current.get(DIRECTION_DISCHARGE, [])}

    rows: list[dict[str, Any]] = []
    for low in sorted(set(charge) & set(discharge)):
        charge_row = charge[low]
        discharge_row = discharge[low]
        charge_eff = charge_row.get("efficiency")
        discharge_eff = discharge_row.get("efficiency")
        round_trip = None
        if charge_eff is not None and discharge_eff is not None:
            round_trip = charge_eff * discharge_eff
        rows.append(
            {
                "current_a": _representative_current(charge_row, discharge_row, low),
                "charge_efficiency": charge_eff,
                "discharge_efficiency": discharge_eff,
                "round_trip_efficiency": _round(round_trip),
                "charge_n_windows": charge_row["n_windows"],
                "discharge_n_windows": discharge_row["n_windows"],
            }
        )
    return rows


def _representative_current(
    charge_row: dict[str, Any] | None,
    discharge_row: dict[str, Any] | None,
    low: float,
) -> float:
    values = [
        row["mean_current_a"]
        for row in (charge_row, discharge_row)
        if row is not None and row.get("mean_current_a") is not None
    ]
    if values:
        return round(statistics.fmean(values), 2)
    return round(low + 0.5, 2)


def model_table(by_current: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Build a flat current -> efficiency table suitable for the DP/planner."""
    charge = {row["bin_low"]: row for row in by_current.get(DIRECTION_CHARGE, [])}
    discharge = {row["bin_low"]: row for row in by_current.get(DIRECTION_DISCHARGE, [])}

    rows: list[dict[str, Any]] = []
    for low in sorted(set(charge) | set(discharge)):
        charge_row = charge.get(low)
        discharge_row = discharge.get(low)
        charge_eff = charge_row.get("efficiency") if charge_row else None
        discharge_eff = discharge_row.get("efficiency") if discharge_row else None
        round_trip = (
            charge_eff * discharge_eff
            if charge_eff is not None and discharge_eff is not None
            else None
        )
        rows.append(
            {
                "current_a": _representative_current(charge_row, discharge_row, low),
                "charge_efficiency": charge_eff,
                "charge_loss_kw": charge_row.get("loss_kw_mean") if charge_row else None,
                "charge_n_windows": charge_row["n_windows"] if charge_row else 0,
                "discharge_efficiency": discharge_eff,
                "discharge_loss_kw": discharge_row.get("loss_kw_mean") if discharge_row else None,
                "discharge_n_windows": discharge_row["n_windows"] if discharge_row else 0,
                "round_trip_efficiency": _round(round_trip),
            }
        )
    return rows


def estimate_standby(
    samples: Sequence[ChargerSample],
    *,
    deadband_w: float = 50.0,
) -> dict[str, Any] | None:
    """Estimate idle/standby AC draw (EVSE electronics, not the inverter)."""
    values = [
        sample.ac_power_w
        for sample in samples
        if sample.ac_power_w is not None and abs(sample.ac_power_w) < deadband_w
    ]
    if not values:
        return None

    non_zero = [value for value in values if abs(value) > 0.5]
    return {
        "n_samples": len(values),
        "mean_w": _round(_mean(values), 2),
        "median_w": _round(_median(values), 2),
        "min_w": _round(min(values), 2),
        "max_w": _round(max(values), 2),
        "n_nonzero": len(non_zero),
        "mean_nonzero_w": _round(_mean(non_zero), 2),
        "note": (
            "Idle samples only; duration is not inferred from sample count because "
            "sampling may have been downsampled or interrupted. Includes unplugged "
            "periods if the charger reports a small non-zero draw while idle."
        ),
    }


def estimate_soc_efficiency(
    segments: Sequence[Sequence[ChargerSample]],
    *,
    battery_capacity_kwh: float,
    min_soc_delta: float = 3.0,
    min_duration_s: float = 300.0,
    current_bin_a: float = 1.0,
) -> dict[str, Any] | None:
    """Estimate efficiency from SoC change over long steady segments.

    This is inherently fuzzy: SoC is reported in 1% steps, which on a 50 kWh
    pack is 0.5 kWh per step, and the BMS may taper or balance.  Results are
    reported separately from the AC<->DC measurement so the two are not
    confused.

    The "implied capacity" is DC energy throughput divided by the SoC change.
    It includes charger DC-side energy, so it is not a pure pack-capacity
    estimate, but it shows whether the assumed capacity is in the right
    ballpark.
    """
    if battery_capacity_kwh <= 0:
        raise ValueError("battery_capacity_kwh must be positive")

    records: list[dict[str, Any]] = []
    for segment in segments:
        if len(segment) < 3:
            continue
        valid = [sample for sample in segment if sample.has_soc]
        if len(valid) < 2:
            continue
        first, last = valid[0], valid[-1]
        duration = (last.time - first.time).total_seconds()
        if duration < min_duration_s:
            continue

        slice_ = [sample for sample in segment if first.time <= sample.time <= last.time]
        if len(slice_) < 3:
            continue

        soc_delta = last.soc - first.soc
        if abs(soc_delta) < min_soc_delta:
            continue

        direction = classify_direction(statistics.fmean([s.ac_power_w for s in slice_]))
        if direction is None:
            continue
        if direction == DIRECTION_CHARGE and soc_delta <= 0:
            continue
        if direction == DIRECTION_DISCHARGE and soc_delta >= 0:
            continue

        ac_energy_wh = abs(_integrate_power(slice_, lambda s: s.ac_power_w) / 3600.0)
        dc_energy_wh = abs(_integrate_power(slice_, lambda s: s.dc_power_w) / 3600.0)
        if ac_energy_wh <= 0 or dc_energy_wh <= 0:
            continue

        stored_energy_wh = abs(soc_delta) / 100.0 * battery_capacity_kwh * 1000.0
        if direction == DIRECTION_CHARGE:
            soc_efficiency = stored_energy_wh / ac_energy_wh
        else:
            soc_efficiency = ac_energy_wh / stored_energy_wh
        if not MIN_PLAUSIBLE_EFFICIENCY <= soc_efficiency <= MAX_PLAUSIBLE_EFFICIENCY:
            continue

        ac_dc_efficiency = (
            dc_energy_wh / ac_energy_wh if direction == DIRECTION_CHARGE else ac_energy_wh / dc_energy_wh
        )
        targets = [abs(s.target_current_a) for s in slice_ if s.target_current_a is not None]
        ac_currents = [
            abs(s.ac_power_w) / s.ac_voltage_v
            for s in slice_
            if s.ac_power_w is not None and s.ac_voltage_v
        ]
        current = _mean(targets) or _mean(ac_currents) or 0.0

        records.append(
            {
                "direction": direction,
                "current_a": abs(current),
                "soc_start": first.soc,
                "soc_end": last.soc,
                "soc_delta": soc_delta,
                "duration_s": duration,
                "ac_energy_kwh": ac_energy_wh / 1000.0,
                "dc_energy_kwh": dc_energy_wh / 1000.0,
                "soc_efficiency": soc_efficiency,
                "ac_dc_efficiency": ac_dc_efficiency,
                "implied_capacity_kwh": dc_energy_wh / (abs(soc_delta) / 100.0) / 1000.0,
            }
        )

    if not records:
        return {
            "capacity_kwh_assumed": battery_capacity_kwh,
            "min_soc_delta": min_soc_delta,
            "min_duration_s": min_duration_s,
            "n_segments": 0,
            "by_current": {DIRECTION_CHARGE: [], DIRECTION_DISCHARGE: []},
            "note": "No segments met the SoC-delta and duration thresholds.",
        }

    def summarise(subset: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
        if not subset:
            return None
        return {
            "n_segments": len(subset),
            "efficiency": _round(_median([r["soc_efficiency"] for r in subset])),
            "efficiency_mean": _round(_mean([r["soc_efficiency"] for r in subset])),
            "ac_dc_efficiency": _round(_median([r["ac_dc_efficiency"] for r in subset])),
            "implied_capacity_kwh": _round(_median([r["implied_capacity_kwh"] for r in subset])),
            "mean_current_a": _round(_mean([r["current_a"] for r in subset]), 3),
            "mean_abs_soc_delta": _round(_mean([abs(r["soc_delta"]) for r in subset]), 2),
        }

    by_current: dict[str, list[dict[str, Any]]] = {}
    for direction in DIRECTIONS:
        subset = [r for r in records if r["direction"] == direction]
        groups: dict[float, list[dict[str, Any]]] = {}
        for record in subset:
            low = round(math.floor(record["current_a"] / current_bin_a) * current_bin_a, 6)
            groups.setdefault(low, []).append(record)
        rows = []
        for low in sorted(groups):
            row = summarise(groups[low])
            if row is not None:
                row.update({"bin_low": low, "bin_high": round(low + current_bin_a, 6)})
                rows.append(row)
        by_current[direction] = rows

    return {
        "capacity_kwh_assumed": battery_capacity_kwh,
        "min_soc_delta": min_soc_delta,
        "min_duration_s": min_duration_s,
        "n_segments": len(records),
        "by_current": by_current,
        "note": (
            "SoC-based efficiency assumes the configured usable capacity and that "
            "reported SoC changes are proportional to stored energy. Treat it as a "
            "cross-check on the AC<->DC result, not as a precise measurement."
        ),
    }


@dataclass
class AnalysisOptions:
    """Tunable thresholds for :func:`analyse`."""

    window_seconds: float = 60.0
    min_coverage: float = 0.5
    max_gap_s: float = 10.0
    deadband_w: float = 50.0
    target_change_a: float = 0.5
    min_current_a: float = 0.5
    min_active_power_w: float = 100.0
    max_power_cv: float = 0.2
    current_bin_a: float = 1.0
    power_bin_kw: float = 0.25
    soc_bin_percent: float = 5.0
    min_soc_delta: float = 3.0
    min_soc_segment_duration_s: float = 300.0
    battery_capacity_kwh: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def analyse(
    samples: Sequence[ChargerSample],
    options: AnalysisOptions | None = None,
    *,
    source: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[Window]]:
    """Run the full analysis and return ``(report, accepted_windows)``."""
    options = options or AnalysisOptions()
    ordered = sorted(samples, key=lambda sample: sample.time)

    segments = segment_samples(
        ordered,
        max_gap_s=options.max_gap_s,
        deadband_w=options.deadband_w,
        target_change_a=options.target_change_a,
    )
    candidate_windows = build_windows(
        segments,
        window_seconds=options.window_seconds,
        min_coverage=options.min_coverage,
    )
    accepted, rejected = filter_windows(
        candidate_windows,
        min_active_power_w=options.min_active_power_w,
        min_current_a=options.min_current_a,
        max_power_cv=options.max_power_cv,
    )

    by_current = bin_by_current(accepted, options.current_bin_a)
    by_power = bin_by_power(accepted, options.power_bin_kw)
    by_soc = bin_by_soc(accepted, options.soc_bin_percent)

    loss_fits = {}
    for direction in DIRECTIONS:
        loss_fits[direction] = {
            "power": fit_loss_model(accepted, direction, x_kind="power"),
            "current": fit_loss_model(accepted, direction, x_kind="current"),
        }

    groups = group_by_direction(accepted)
    overall = {}
    for direction in DIRECTIONS:
        overall[direction] = summarise_windows(groups[direction])

    soc_based = None
    if options.battery_capacity_kwh is not None:
        soc_based = estimate_soc_efficiency(
            segments,
            battery_capacity_kwh=options.battery_capacity_kwh,
            min_soc_delta=options.min_soc_delta,
            min_duration_s=options.min_soc_segment_duration_s,
            current_bin_a=options.current_bin_a,
        )

    first_time = ordered[0].time if ordered else None
    last_time = ordered[-1].time if ordered else None
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "source": source or {},
        "options": options.to_dict(),
        "summary": {
            "n_samples": len(ordered),
            "first_sample": _iso(first_time),
            "last_sample": _iso(last_time),
            "n_segments": len(segments),
            "n_candidate_windows": len(candidate_windows),
            "n_windows": len(accepted),
            "n_windows_charge": len(groups[DIRECTION_CHARGE]),
            "n_windows_discharge": len(groups[DIRECTION_DISCHARGE]),
            "rejected_windows": rejected,
            "charge": overall[DIRECTION_CHARGE],
            "discharge": overall[DIRECTION_DISCHARGE],
        },
        "standby": estimate_standby(ordered, deadband_w=options.deadband_w),
        "by_current": by_current,
        "by_dc_power": by_power,
        "by_soc": by_soc,
        "loss_fit": loss_fits,
        "round_trip": round_trip_table(by_current),
        "model_table": model_table(by_current),
        "soc_based": soc_based,
    }
    return report, accepted


def _write_csv(path: Path, rows: Sequence[dict[str, Any]], fieldnames: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = []
        seen = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_outputs(
    report: dict[str, Any],
    windows: Sequence[Window],
    output_dir: str | Path,
    *,
    write_csv: bool = True,
) -> dict[str, Path]:
    """Write the JSON report and optional CSV detail files."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    json_path = output / "efficiency_analysis.json"
    json_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    paths["json"] = json_path

    if not write_csv:
        return paths

    windows_rows = [window.to_row() for window in windows]
    windows_path = output / "windows.csv"
    _write_csv_windows(windows_path, windows_rows)
    paths["windows"] = windows_path

    by_current_rows: list[dict[str, Any]] = []
    for direction in DIRECTIONS:
        for row in report["by_current"].get(direction, []):
            by_current_rows.append({"direction": direction, **row})
    current_path = output / "by_current.csv"
    _write_csv(current_path, by_current_rows)
    paths["by_current"] = current_path

    by_soc_rows: list[dict[str, Any]] = []
    for direction in DIRECTIONS:
        for row in report["by_soc"].get(direction, []):
            by_soc_rows.append({"direction": direction, **row})
    soc_path = output / "by_soc.csv"
    _write_csv(soc_path, by_soc_rows)
    paths["by_soc"] = soc_path

    model_path = output / "model_table.csv"
    _write_csv(model_path, report.get("model_table", []))
    paths["model_table"] = model_path

    return paths


_WINDOW_FIELDS = [
    "start",
    "end",
    "direction",
    "duration_s",
    "n_samples",
    "mean_ac_power_w",
    "mean_dc_power_w",
    "mean_ac_voltage_v",
    "mean_dc_voltage_v",
    "mean_abs_dc_current_a",
    "mean_ac_current_a",
    "mean_soc",
    "mean_target_current_a",
    "efficiency",
    "loss_kw",
    "ac_energy_wh",
    "dc_energy_wh",
    "ac_power_cv",
]


def _write_csv_windows(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    _write_csv(path, rows, fieldnames=_WINDOW_FIELDS)
