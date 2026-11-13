"""Component-based net-grid load forecast for the DP shadow planner."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from evse_controller.forecasting.heatpump import HeatPumpModel
from evse_controller.forecasting.solar import SolarModel


class MissingForecastWeather(ValueError):
    """Raised when a component run has no forecast weather for a slot."""


class ComponentLoadModel:
    """Baseload + heat-pump − solar forecast with residual quantiles."""

    def __init__(
        self,
        *,
        baseload_profile_kw: Mapping[int, float],
        baseload_overall_kw: float,
        solar_model: SolarModel,
        heatpump_model: HeatPumpModel,
        residual_quantiles_kw: Mapping[str, float],
        timezone: str = "Europe/London",
    ):
        self.baseload_profile_kw = {
            int(slot): float(value)
            for slot, value in baseload_profile_kw.items()
        }
        self.baseload_overall_kw = float(baseload_overall_kw)
        self.solar_model = solar_model
        self.heatpump_model = heatpump_model
        self.residual_quantiles_kw = {
            str(key): float(value)
            for key, value in residual_quantiles_kw.items()
        }
        self.timezone = timezone

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ComponentLoadModel":
        return cls(
            baseload_profile_kw=payload.get("baseload_profile_kw") or {},
            baseload_overall_kw=float(
                payload.get("baseload_overall_kw", 0.0)
            ),
            solar_model=SolarModel.from_dict(
                payload.get("solar_model") or {}
            ),
            heatpump_model=HeatPumpModel.from_dict(
                payload.get("heatpump_model") or {}
            ),
            residual_quantiles_kw=(
                payload.get("residual_quantiles_kw") or {}
            ),
            timezone=str(payload.get("timezone", "Europe/London")),
        )

    def _slot(self, when: datetime) -> int:
        local = when.astimezone(ZoneInfo(self.timezone))
        return local.hour * 2 + (1 if local.minute >= 30 else 0)

    def predict(
        self, when: datetime, temperature_c: float, gti_wm2: float
    ) -> float:
        """Median net-grid load in kW (positive import)."""
        slot = self._slot(when)
        baseload = self.baseload_profile_kw.get(
            slot, self.baseload_overall_kw
        )
        solar = self.solar_model.predict(when, gti_wm2)
        heat = (
            self.heatpump_model.predict(
                when, temperature_c, quantile="p50"
            )
            / 1000.0
        )
        median_residual = self.residual_quantiles_kw.get("0.5", 0.0)
        return baseload + heat - solar + median_residual

    def samples(
        self, when: datetime, temperature_c: float, gti_wm2: float
    ) -> tuple[tuple[float, float], ...]:
        """Net-grid load samples for the DP's expected-cost calculation."""
        slot = self._slot(when)
        baseload = self.baseload_profile_kw.get(
            slot, self.baseload_overall_kw
        )
        solar = self.solar_model.predict(when, gti_wm2)
        heat = (
            self.heatpump_model.predict(
                when, temperature_c, quantile="p50"
            )
            / 1000.0
        )
        base = baseload + heat - solar
        values = list(self.residual_quantiles_kw.values()) or [0.0]
        weight = 1.0 / len(values)
        return tuple(
            (base + float(residual), weight) for residual in values
        )


def _aware(when: datetime, zone: ZoneInfo) -> datetime:
    if when.tzinfo is None:
        return when.replace(tzinfo=zone)
    return when


def apply_component_load_to_run(
    run: Mapping[str, Any],
    model: ComponentLoadModel,
    weather: Mapping[str, Mapping[datetime, float]],
) -> dict:
    """Return a copy of *run* with component net-grid load inputs.

    Prices, sessions, follow gates and the initial SoC are left untouched.
    The recorded load is replaced only where historical forecast weather is
    available; a missing slot is an error rather than a synthetic zero.
    """
    zone = ZoneInfo(model.timezone)
    temperature = weather.get("temperature_c") or {}
    gti = weather.get("tilted_irradiance_wm2") or {}
    inputs = []
    for item in run.get("inputs") or []:
        if not isinstance(item, Mapping):
            raise ValueError(
                "Component load source found a non-mapping input row"
            )
        updated = dict(item)
        try:
            when = datetime.fromisoformat(str(item["start"]))
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                "Component load source found an input row without a "
                f"valid start: {item!r}"
            ) from None
        local = _aware(when, zone)
        hour = local.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        temperature_c = temperature.get(hour)
        gti_wm2 = gti.get(hour)
        if temperature_c is None or gti_wm2 is None:
            raise MissingForecastWeather(
                "No historical forecast weather for "
                f"{local.isoformat()}"
            )
        samples = model.samples(
            local, float(temperature_c), float(gti_wm2)
        )
        total_weight = sum(weight for _, weight in samples)
        if total_weight <= 0:
            raise ValueError(
                f"Component forecast has no weight for {local.isoformat()}"
            )
        load_kw = sum(
            kw * weight for kw, weight in samples
        ) / total_weight
        updated["load_kw"] = round(load_kw, 6)
        updated["load_samples"] = [
            [round(kw, 6), weight] for kw, weight in samples
        ]
        updated["load_source"] = "component"
        inputs.append(updated)
    result = dict(run)
    result["inputs"] = inputs
    return result

