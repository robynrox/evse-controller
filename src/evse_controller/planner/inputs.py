"""Build the DP's slot rows from the normalised tariff and forecast layers.

The planner deliberately does not import a tariff class.  It consumes:

* ``TariffSource.rates_for_day`` — 48 import/export rows in p/kWh;
* ``TariffSource.sessions_for_day`` — free-import, grid-support and EV-away
  windows;
* ``LoadProfile.for_date`` — per-slot house load, or ``None`` when the history
  is too thin;
* the site's Wallbox current limits.

That keeps the optimiser independent of the supplier and of the control
heuristic, which is the contract in ``reports/smart-tariff/SPEC.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from evse_controller.energy_sessions import EnergySession
from evse_controller.planner.dp import PlannerSlot
from evse_controller.tariffs.source import SLOTS_PER_DAY, SlotRate, TariffSource


@dataclass(frozen=True)
class PlannerInput:
    """Everything the DP needs for one run."""

    day: date
    slots: tuple[PlannerSlot, ...]
    initial_soc: float
    target_soc: float
    source_name: str
    forecast_days: int = 0


def _session_for(slot_start: datetime, sessions: list[EnergySession]):
    for session in sessions:
        if session.enabled and session.start <= slot_start < session.end:
            return session
    return None


def _price(
    value: Optional[float],
    *,
    fallback: Optional[float],
    label: str,
    slot: int,
) -> float:
    if value is not None:
        return float(value)
    if fallback is not None:
        return float(fallback)
    raise ValueError(f"No {label} price for slot {slot}")


def build_planner_input(
    source: TariffSource,
    load_profile,
    day: date,
    current_slot: int,
    current_soc: float,
    *,
    target_soc: float,
    max_charge_current_a: float,
    max_discharge_current_a: float,
    load_fallback_kw: float = 0.0,
    unknown_import_p: Optional[float] = None,
    unknown_export_p: Optional[float] = 0.0,
) -> PlannerInput:
    """Build the horizon from *current_slot* to the end of the local day.

    ``unknown_export_p`` defaults to 0p, matching the existing Agile control
    fallback for a slot whose export rate is not yet published.  Import should
    normally be known; pass ``unknown_import_p`` only if the caller has a
    deliberate terminal value.  An unknown import with no fallback is an
    error, because treating it as free would be dangerous.
    """
    current_slot = max(0, min(SLOTS_PER_DAY, int(current_slot)))
    rates: list[SlotRate] = source.rates_for_day(day)
    if len(rates) != SLOTS_PER_DAY:
        raise ValueError(
            f"Tariff source returned {len(rates)} rows, expected "
            f"{SLOTS_PER_DAY}"
        )
    sessions = list(source.sessions_for_day(day))
    loads = load_profile.for_date(day) if load_profile is not None else []
    forecast_days = max(
        (item.days for item in loads if item.available),
        default=0,
    )

    slots: list[PlannerSlot] = []
    for index in range(current_slot, SLOTS_PER_DAY):
        rate = rates[index]
        import_p = _price(
            rate.import_p,
            fallback=unknown_import_p,
            label="import",
            slot=index,
        )
        export_p = _price(
            rate.export_p,
            fallback=unknown_export_p,
            label="export",
            slot=index,
        )
        load_kw = load_fallback_kw
        if index < len(loads):
            stats = loads[index]
            if stats is not None and stats.median_w is not None:
                load_kw = max(0.0, float(stats.median_w) / 1000.0)

        session = _session_for(rate.start, sessions)
        forced = None
        session_name = None
        if session is not None:
            session_name = session.kind
            if session.kind == "free_import":
                import_p = 0.0
            elif session.kind == "grid_support":
                forced = "discharge_max"
            elif session.kind == "ev_away":
                forced = "idle"

        slots.append(
            PlannerSlot(
                start=rate.start,
                import_p=import_p,
                export_p=export_p,
                load_kw=load_kw,
                max_charge_current_a=max_charge_current_a,
                max_discharge_current_a=max_discharge_current_a,
                forced=forced,
                session=session_name,
            )
        )

    return PlannerInput(
        day=day,
        slots=tuple(slots),
        initial_soc=float(current_soc),
        target_soc=float(target_soc),
        source_name=source.name,
        forecast_days=forecast_days,
    )
