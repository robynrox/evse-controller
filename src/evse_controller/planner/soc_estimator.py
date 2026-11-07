"""Continuous SoC estimate for the planner.

The Wallbox reports integer SoC, often in steps of 2–3%, and the BMS value is
not a precise instantaneous measurement.  The planner needs a continuous,
model-based estimate that stays close to those reports without chasing every
quantisation step.

The estimator is a scalar Kalman filter:

* between reports, it integrates the actual AC power through the measured
  charge/discharge efficiency and the effective dashboard capacity;
* each new report is treated as a noisy measurement with a configurable
  standard deviation;
* a single correction is capped, so a large BMS step cannot yank the plan;
* the variance is carried forward, giving callers a lower bound for discharge
  decisions and an upper bound for charge decisions.

The state is intentionally small enough to persist as a few JSON numbers and
to remain explainable in the dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class SocEstimate:
    """The persistent state of the estimator."""

    soc_percent: float
    variance: float
    last_reported_soc: Optional[float] = None
    last_update: Optional[datetime] = None
    last_observation: Optional[datetime] = None
    last_innovation: Optional[float] = None
    observations: int = 0


class SocEstimator:
    """Scalar Kalman filter for the dashboard SoC.

    The defaults treat a report as roughly ±1.5% and let the model drift by
    about 1.5% per hour.  That makes the estimate responsive to a real 3% BMS
    step over a few reports, while still smoothing the usual quantisation.
    """

    def __init__(
        self,
        initial_soc_percent: float,
        capacity_kwh: float,
        *,
        measurement_sigma_percent: float = 1.5,
        process_sigma_percent_per_hour: float = 1.5,
        max_correction_percent: float = 2.5,
        initial_variance: Optional[float] = None,
        at: Optional[datetime] = None,
    ):
        if capacity_kwh <= 0:
            raise ValueError("capacity_kwh must be positive")
        if measurement_sigma_percent <= 0:
            raise ValueError("measurement_sigma_percent must be positive")
        if process_sigma_percent_per_hour < 0:
            raise ValueError(
                "process_sigma_percent_per_hour cannot be negative"
            )
        if max_correction_percent <= 0:
            raise ValueError("max_correction_percent must be positive")

        self.capacity_kwh = float(capacity_kwh)
        self.measurement_sigma_percent = float(
            measurement_sigma_percent
        )
        self.process_sigma_percent_per_hour = float(
            process_sigma_percent_per_hour
        )
        self.max_correction_percent = float(max_correction_percent)
        variance = (
            float(initial_variance)
            if initial_variance is not None
            else self.measurement_sigma_percent ** 2
        )
        self._state = SocEstimate(
            soc_percent=self._clamp(float(initial_soc_percent)),
            variance=max(0.0, variance),
            last_update=at,
        )

    # -- state ---------------------------------------------------------

    @property
    def soc_percent(self) -> float:
        return self._state.soc_percent

    @property
    def variance(self) -> float:
        return self._state.variance

    @property
    def sigma_percent(self) -> float:
        return max(0.0, self._state.variance) ** 0.5

    @property
    def state(self) -> SocEstimate:
        return self._state

    def lower_bound(self, sigmas: float = 1.0) -> float:
        """Conservative SoC for discharge decisions."""
        return self._clamp(
            self._state.soc_percent - sigmas * self.sigma_percent
        )

    def upper_bound(self, sigmas: float = 1.0) -> float:
        """Conservative SoC for charge decisions."""
        return self._clamp(
            self._state.soc_percent + sigmas * self.sigma_percent
        )

    # -- model propagation --------------------------------------------

    def update_from_stored_energy(
        self,
        stored_kwh: float,
        *,
        dt_hours: float = 0.5,
        at: Optional[datetime] = None,
    ) -> None:
        """Advance the estimate by the stored energy moved through the pack.

        ``stored_kwh`` is positive when charging and negative when
        discharging.  The caller is responsible for applying the measured
        efficiency at the actual current.
        """
        if dt_hours < 0:
            raise ValueError("dt_hours cannot be negative")
        delta_soc = (
            float(stored_kwh) / self.capacity_kwh * 100.0
        )
        self._state.soc_percent = self._clamp(
            self._state.soc_percent + delta_soc
        )
        self._state.variance += (
            self.process_sigma_percent_per_hour ** 2
        ) * dt_hours
        self._state.last_update = at or datetime.now()

    def update_from_ac_power(
        self,
        ac_power_kw: float,
        dt_hours: float,
        *,
        charge_efficiency: float,
        discharge_efficiency: float,
        at: Optional[datetime] = None,
    ) -> None:
        """Advance from the Wallbox AC power over ``dt_hours``.

        Positive AC power is charging; negative is discharging.  The
        efficiencies are the measured AC↔pack values at the commanded
        current.
        """
        if ac_power_kw >= 0:
            stored_kwh = (
                ac_power_kw
                * dt_hours
                * max(0.0, float(charge_efficiency))
            )
        else:
            stored_kwh = (
                ac_power_kw
                * dt_hours
                / max(float(discharge_efficiency), 1e-9)
            )
        self.update_from_stored_energy(
            stored_kwh, dt_hours=dt_hours, at=at
        )

    # -- measurement ---------------------------------------------------

    def observe(
        self,
        reported_soc_percent: float,
        *,
        at: Optional[datetime] = None,
        measurement_sigma_percent: Optional[float] = None,
    ) -> float:
        """Correct the estimate with a Wallbox SoC report.

        Returns the correction that was applied (percentage points).  The
        correction is capped so a single coarse BMS step does not dominate.
        Repeated reports in the same direction will still converge.
        """
        reported = self._clamp(float(reported_soc_percent))
        sigma = (
            self.measurement_sigma_percent
            if measurement_sigma_percent is None
            else max(1e-6, float(measurement_sigma_percent))
        )
        measurement_variance = sigma ** 2
        prior_variance = max(self._state.variance, 1e-9)
        gain = prior_variance / (prior_variance + measurement_variance)
        innovation = reported - self._state.soc_percent
        correction = gain * innovation
        correction = max(
            -self.max_correction_percent,
            min(self.max_correction_percent, correction),
        )

        self._state.soc_percent = self._clamp(
            self._state.soc_percent + correction
        )
        # If the correction was capped, reduce the variance by the effective
        # gain rather than the nominal Kalman gain.  This keeps the filter
        # honest about how much information it actually used.
        effective_gain = (
            correction / innovation
            if abs(innovation) > 1e-9
            else gain
        )
        self._state.variance = max(
            1e-9,
            prior_variance * (1.0 - max(0.0, min(1.0, effective_gain))),
        )
        self._state.last_reported_soc = reported
        self._state.last_observation = at or datetime.now()
        self._state.last_innovation = innovation
        self._state.observations += 1
        return correction

    # -- persistence ---------------------------------------------------

    def as_dict(self) -> dict:
        return {
            "soc_percent": self._state.soc_percent,
            "variance": self._state.variance,
            "last_reported_soc": self._state.last_reported_soc,
            "last_update": (
                self._state.last_update.isoformat()
                if self._state.last_update is not None
                else None
            ),
            "last_observation": (
                self._state.last_observation.isoformat()
                if self._state.last_observation is not None
                else None
            ),
            "last_innovation": self._state.last_innovation,
            "observations": self._state.observations,
        }

    @classmethod
    def from_dict(
        cls,
        payload: dict,
        capacity_kwh: float,
        **kwargs,
    ) -> "SocEstimator":
        estimator = cls(
            initial_soc_percent=float(payload.get("soc_percent", 50.0)),
            capacity_kwh=capacity_kwh,
            initial_variance=float(payload.get("variance", 0.0)),
            **kwargs,
        )
        estimator._state.last_reported_soc = payload.get(
            "last_reported_soc"
        )
        estimator._state.last_innovation = payload.get(
            "last_innovation"
        )
        estimator._state.observations = int(
            payload.get("observations", 0)
        )
        for key, attribute in (
            ("last_update", "last_update"),
            ("last_observation", "last_observation"),
        ):
            text = payload.get(key)
            if text:
                try:
                    setattr(
                        estimator._state,
                        attribute,
                        datetime.fromisoformat(text),
                    )
                except ValueError:
                    pass
        return estimator

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(100.0, float(value)))
