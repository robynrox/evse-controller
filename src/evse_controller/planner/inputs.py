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
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Optional, Sequence

from evse_controller.energy_sessions import EnergySession
from evse_controller.planner.dp import PlannerSlot

if TYPE_CHECKING:
    from evse_controller.tariffs.source import TariffSource


@dataclass(frozen=True)
class PlannerInput:
    """Everything the DP needs for one run."""

    day: date
    slots: tuple[PlannerSlot, ...]
    initial_soc: float
    target_soc: Optional[float]
    source_name: str
    forecast_days: int = 0
    load_fallback_kw: float = 0.0
    horizon_end_slot: Optional[int] = None
    horizon_end_date: Optional[date] = None
    horizon_limited_by_export: bool = False


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


def uniform_load_prior(
    min_kw: float = 0.0,
    max_kw: float = 0.4,
    step_kw: float = 0.025,
) -> tuple[tuple[float, float], ...]:
    """A flat 0–400 W prior, one sample per 25 W bin midpoint."""
    if max_kw <= min_kw or step_kw <= 0:
        return ()
    samples = []
    value = min_kw
    while value < max_kw - 1e-12:
        upper = min(max_kw, value + step_kw)
        samples.append(((value + upper) / 2.0, 0.0))
        value = upper
    if not samples:
        return ()
    weight = 1.0 / len(samples)
    return tuple((load_kw, weight) for load_kw, _ in samples)


def build_planner_input(
    source: "TariffSource",
    load_profile,
    day: date,
    current_slot: int,
    current_soc: float,
    *,
    target_soc: Optional[float],
    max_charge_current_a: float,
    max_discharge_current_a: float,
    load_fallback_kw: float = 0.0,
    load_prior: Optional[Sequence[tuple[float, float]]] = None,
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
    # Imported lazily: ``tariffs`` imports the live tariff, which imports this
    # module, so a module-level import would be circular when this module is
    # imported first.
    from evse_controller.tariffs.source import SLOTS_PER_DAY

    current_slot = max(0, min(SLOTS_PER_DAY, int(current_slot)))
    dynamic_export = bool(getattr(source, "dynamic_export", False))

    def day_rates(day_to_load: date):
        rates = source.rates_for_day(day_to_load)
        if len(rates) != SLOTS_PER_DAY:
            raise ValueError(
                f"Tariff source returned {len(rates)} rows for "
                f"{day_to_load}, expected {SLOTS_PER_DAY}"
            )
        return rates

    def known_export_end(rates, start_index: int) -> int:
        """First contiguous end of published export prices."""
        for index in range(start_index, SLOTS_PER_DAY):
            if rates[index].export_p is None:
                return index
        return SLOTS_PER_DAY

    # A moving horizon for a dynamic-export tariff ends at the last
    # contiguous slot with a published export price.  When today's prices run
    # to midnight, tomorrow's series is appended if it is already published.
    # Fixed-export tariffs still plan to the end of the current day.
    days_to_plan: list[date] = [day]
    horizon_limited = False
    if dynamic_export:
        today_rates = day_rates(day)
        if known_export_end(today_rates, current_slot) >= SLOTS_PER_DAY:
            tomorrow = day + timedelta(days=1)
            tomorrow_rates = day_rates(tomorrow)
            if tomorrow_rates[0].export_p is not None:
                days_to_plan.append(tomorrow)
            else:
                # Today is fully priced, but tomorrow is not yet published.
                horizon_limited = True

    slots: list[PlannerSlot] = []
    forecast_days = 0
    for day_index, plan_day in enumerate(days_to_plan):
        rates = day_rates(plan_day)
        sessions = list(source.sessions_for_day(plan_day))
        loads = (
            load_profile.for_date(plan_day)
            if load_profile is not None
            else []
        )
        forecast_days = max(
            forecast_days,
            max((item.days for item in loads if item.available), default=0),
        )
        start_index = current_slot if day_index == 0 else 0
        end_slot = SLOTS_PER_DAY
        if dynamic_export:
            end_slot = known_export_end(rates, start_index)
            if end_slot < SLOTS_PER_DAY:
                horizon_limited = True
        if end_slot <= start_index:
            continue

        for index in range(start_index, end_slot):
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
            load_samples: tuple[tuple[float, float], ...] = ()
            load_source = "fallback"
            if index < len(loads):
                stats = loads[index]
                if stats is not None and stats.median_w is not None:
                    load_kw = max(0.0, float(stats.median_w) / 1000.0)
                    load_source = "profile"
                    if stats.samples:
                        load_samples = tuple(
                            (max(0.0, watts) / 1000.0, weight)
                            for watts, weight in stats.samples
                        )
                    else:
                        load_samples = ((load_kw, 1.0),)
            if not load_samples:
                if load_prior:
                    load_samples = tuple(
                        (max(0.0, float(kw)), max(0.0, float(weight)))
                        for kw, weight in load_prior
                    )
                    total_weight = sum(
                        weight for _, weight in load_samples
                    )
                    if total_weight > 0:
                        load_samples = tuple(
                            (kw, weight / total_weight)
                            for kw, weight in load_samples
                        )
                        load_kw = sum(
                            kw * weight for kw, weight in load_samples
                        )
                        load_source = "prior"
                if not load_samples:
                    load_samples = ((load_kw, 1.0),)
                    load_source = "fallback"

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
                    slot=index,
                    import_p=import_p,
                    export_p=export_p,
                    load_kw=load_kw,
                    max_charge_current_a=max_charge_current_a,
                    max_discharge_current_a=max_discharge_current_a,
                    forced=forced,
                    session=session_name,
                    load_samples=load_samples,
                    load_source=load_source,
                    horizon_index=len(slots),
                )
            )

    return PlannerInput(
        day=day,
        slots=tuple(slots),
        initial_soc=float(current_soc),
        target_soc=(
            None if target_soc is None else float(target_soc)
        ),
        source_name=source.name,
        forecast_days=forecast_days,
        load_fallback_kw=float(load_fallback_kw),
        horizon_end_slot=(
            slots[-1].slot if slots else None
        ),
        horizon_end_date=(
            slots[-1].start.date() if slots else None
        ),
        horizon_limited_by_export=horizon_limited,
    )
