"""Backtest a shadow DP plan against what actually happened.

The shadow planner records the plan in force for each slot.  This module
replays one plan against the measured house load and frozen prices from the
daily energy report, so the planner's cost is comparable with the heuristic's
actual cost over the same slots.

The replay is deliberately first-order:

* the house load is reconstructed as ``grid − EVSE`` from measured energy;
* the plan's AC power replaces the EVSE term, then grid import/export is
  priced with the frozen slot prices;
* battery constraints are not re-solved, because the plan already respected
  them at the time it was made.

It is a sanity check on the planner's model, not a claim that the plan could
have been executed exactly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from evse_controller.daily_report import daily_log
from evse_controller.planner.history import planner_history


SLOT_HOURS = 0.5
SLOTS_PER_DAY = 48


def _number(value) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _signed_kw(energy: dict) -> Optional[float]:
    """Signed average kW from an in/out energy pair, or None if unmeasured."""
    in_kwh = _number(energy.get("in"))
    out_kwh = _number(energy.get("out"))
    if in_kwh is None and out_kwh is None:
        return None
    return ((in_kwh or 0.0) - (out_kwh or 0.0)) / SLOT_HOURS


def _house_kw(slot: dict) -> Optional[float]:
    energy = slot.get("energy") or {}
    grid = _signed_kw(energy.get("grid") or {})
    if grid is None:
        return None
    evse = _signed_kw(energy.get("evse") or {}) or 0.0
    return grid - evse


def _actual_kind(slot: dict) -> Optional[str]:
    energy = (slot.get("energy") or {}).get("evse") or {}
    if energy.get("in") is None and energy.get("out") is None:
        return None
    charged = _number(energy.get("in")) or 0.0
    discharged = _number(energy.get("out")) or 0.0
    if charged > 0.005 and discharged > 0.005:
        return "mixed"
    if charged > 0.005:
        return "charged"
    if discharged > 0.005:
        return "discharged"
    return "idle"


def _planned_kind(kind: Optional[str]) -> str:
    if kind == "charge":
        return "charged"
    if kind == "discharge":
        return "discharged"
    return "idle"


@dataclass(frozen=True)
class SlotOutcome:
    slot: int
    planned_kind: Optional[str]
    planned_ac_kw: Optional[float]
    actual_kind: Optional[str]
    house_kw: Optional[float]
    dp_import_kwh: Optional[float]
    dp_export_kwh: Optional[float]
    dp_cost_gbp: Optional[float]
    actual_cost_gbp: Optional[float]
    planned_soc: Optional[float]
    estimated_soc: Optional[float]


@dataclass(frozen=True)
class DayBacktest:
    day: date
    start_slot: int
    run_ts: Optional[str]
    solver_version: Optional[str]
    solver_mode: Optional[str]
    linked_slots: int
    skipped_slots: int
    dp_cost_gbp: Optional[float]
    actual_cost_gbp: Optional[float]
    action_matches: int
    action_slots: int
    soc_pairs: int
    soc_abs_error_sum: float
    soc_error_sum: float
    rows: tuple[SlotOutcome, ...]

    @property
    def delta_gbp(self) -> Optional[float]:
        if self.dp_cost_gbp is None or self.actual_cost_gbp is None:
            return None
        return self.actual_cost_gbp - self.dp_cost_gbp

    @property
    def soc_mae(self) -> Optional[float]:
        if self.soc_pairs == 0:
            return None
        return self.soc_abs_error_sum / self.soc_pairs

    @property
    def soc_bias(self) -> Optional[float]:
        if self.soc_pairs == 0:
            return None
        return self.soc_error_sum / self.soc_pairs

    @property
    def action_match_rate(self) -> Optional[float]:
        if self.action_slots == 0:
            return None
        return self.action_matches / self.action_slots


def day_backtest_to_dict(result: DayBacktest) -> dict:
    """Serialise one day's result for a persisted backtest job."""
    return {
        "day": result.day.isoformat(),
        "start_slot": result.start_slot,
        "run_ts": result.run_ts,
        "solver_version": result.solver_version,
        "solver_mode": result.solver_mode,
        "linked_slots": result.linked_slots,
        "skipped_slots": result.skipped_slots,
        "dp_cost_gbp": result.dp_cost_gbp,
        "actual_cost_gbp": result.actual_cost_gbp,
        "delta_gbp": result.delta_gbp,
        "action_matches": result.action_matches,
        "action_slots": result.action_slots,
        "soc_pairs": result.soc_pairs,
        "soc_abs_error_sum": result.soc_abs_error_sum,
        "soc_error_sum": result.soc_error_sum,
        "soc_mae": result.soc_mae,
        "soc_bias": result.soc_bias,
        "action_match_rate": result.action_match_rate,
        "rows": [asdict(row) for row in result.rows],
    }


def simulate_run(
    day: date,
    run: dict,
    snapshot: dict,
    soc_samples: Optional[list[dict]] = None,
    disconnected_slots: Optional[set[int]] = None,
) -> DayBacktest:
    """Replay one plan against the measured day."""
    actions = {}
    for action in run.get("actions") or []:
        if not isinstance(action.get("slot"), int):
            continue
        try:
            action_day = datetime.fromisoformat(
                str(action.get("start"))
            ).date()
        except (TypeError, ValueError):
            action_day = None
        if action_day is not None and action_day != day:
            continue
        actions[action["slot"]] = action
    start_slot = int(run.get("slot", 0))
    slots = snapshot.get("slots") or []
    samples = {}
    for sample in soc_samples or []:
        slot = sample.get("slot")
        if isinstance(slot, int):
            samples[slot] = _number(sample.get("estimate"))

    rows = []
    dp_total = 0.0
    any_dp = False
    actual_total = 0.0
    any_actual = False
    matches = 0
    action_slots = 0
    soc_pairs = 0
    soc_abs = 0.0
    soc_sum = 0.0
    skipped = 0

    for slot in range(start_slot, SLOTS_PER_DAY):
        action = actions.get(slot)
        daily = slots[slot] if slot < len(slots) else {}
        house_kw = _house_kw(daily)
        prices = daily.get("prices") or {}
        import_p = _number(prices.get("import_p"))
        export_p = _number(prices.get("export_p"))

        dp_cost = None
        dp_import = None
        dp_export = None
        if (
            action is not None
            and house_kw is not None
            and import_p is not None
            and export_p is not None
        ):
            plan_kw = _number(action.get("ac_power_kw")) or 0.0
            if disconnected_slots and slot in disconnected_slots:
                # The car was not connected, so no plan action could run.
                plan_kw = 0.0
            net_kw = house_kw + plan_kw
            dp_import = max(net_kw, 0.0) * SLOT_HOURS
            dp_export = max(-net_kw, 0.0) * SLOT_HOURS
            dp_cost = (
                dp_import * import_p / 100.0
                - dp_export * export_p / 100.0
            )
            dp_total += dp_cost
            any_dp = True

        money = daily.get("money") or {}
        actual_cost = _number(money.get("net_cost_gbp"))
        if actual_cost is None:
            grid = (daily.get("energy") or {}).get("grid") or {}
            grid_in = _number(grid.get("in"))
            grid_out = _number(grid.get("out"))
            if (
                grid_in is not None
                and grid_out is not None
                and import_p is not None
                and export_p is not None
            ):
                actual_cost = (
                    grid_in * import_p / 100.0
                    - grid_out * export_p / 100.0
                )
        if actual_cost is not None:
            actual_total += actual_cost
            any_actual = True

        actual_kind = _actual_kind(daily)
        planned_kind = _planned_kind((action or {}).get("kind"))
        if action is not None and actual_kind is not None:
            action_slots += 1
            if planned_kind == actual_kind:
                matches += 1

        planned_soc = (
            _number(action.get("soc_before"))
            if action is not None
            else None
        )
        estimated_soc = samples.get(slot)
        if planned_soc is not None and estimated_soc is not None:
            soc_pairs += 1
            error = planned_soc - estimated_soc
            soc_sum += error
            soc_abs += abs(error)

        if action is None or dp_cost is None or actual_cost is None:
            skipped += 1

        rows.append(
            SlotOutcome(
                slot=slot,
                planned_kind=(action or {}).get("kind"),
                planned_ac_kw=(
                    _number(action.get("ac_power_kw"))
                    if action is not None
                    else None
                ),
                actual_kind=actual_kind,
                house_kw=house_kw,
                dp_import_kwh=dp_import,
                dp_export_kwh=dp_export,
                dp_cost_gbp=dp_cost,
                actual_cost_gbp=actual_cost,
                planned_soc=planned_soc,
                estimated_soc=estimated_soc,
            )
        )

    return DayBacktest(
        day=day,
        start_slot=start_slot,
        run_ts=run.get("ts"),
        solver_version=run.get("solver_version"),
        solver_mode=run.get("solver_mode"),
        linked_slots=SLOTS_PER_DAY - start_slot,
        skipped_slots=skipped,
        dp_cost_gbp=dp_total if any_dp else None,
        actual_cost_gbp=actual_total if any_actual else None,
        action_matches=matches,
        action_slots=action_slots,
        soc_pairs=soc_pairs,
        soc_abs_error_sum=soc_abs,
        soc_error_sum=soc_sum,
        rows=tuple(rows),
    )


def first_run(runs: list[dict]) -> Optional[dict]:
    usable = [run for run in runs if run.get("actions")]
    if not usable:
        return None
    return min(
        usable,
        key=lambda run: (
            int(run.get("slot") or 0),
            str(run.get("ts") or ""),
        ),
    )


def backtest_days(
    history=planner_history,
    log=daily_log,
    days: Optional[int] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> list[DayBacktest]:
    """Backtest the first usable plan on each day in the requested range."""
    available = history.available_days()
    if not available:
        return []
    if end is None:
        end = max(available)
    if start is None:
        if days is None:
            days = 30
        start = end - timedelta(days=max(0, days - 1))

    day = start
    results = []
    while day <= end:
        runs = history.runs_for_day(day)
        run = first_run(runs)
        if run is not None:
            results.append(
                simulate_run(
                    day,
                    run,
                    log.snapshot_for(day),
                    history.soc_samples_for_day(day),
                )
            )
        day += timedelta(days=1)
    return results


def summarise(results: list[DayBacktest]) -> dict:
    """Aggregate the per-day backtests.

    The headline cost totals use only days where both the DP replay and the
    actual cost exist, so the delta compares the same slots.
    """
    paired = [
        row
        for row in results
        if row.dp_cost_gbp is not None
        and row.actual_cost_gbp is not None
    ]
    dp_total = sum(row.dp_cost_gbp for row in paired)
    actual_total = sum(row.actual_cost_gbp for row in paired)
    soc_pairs = sum(row.soc_pairs for row in results)
    soc_abs = sum(row.soc_abs_error_sum for row in results)
    soc_sum = sum(row.soc_error_sum for row in results)
    action_slots = sum(row.action_slots for row in results)
    matches = sum(row.action_matches for row in results)
    return {
        "days": len(results),
        "paired_days": len(paired),
        "days_with_dp_cost": sum(
            1 for row in results if row.dp_cost_gbp is not None
        ),
        "days_with_actual_cost": sum(
            1 for row in results if row.actual_cost_gbp is not None
        ),
        "dp_cost_gbp": dp_total if paired else None,
        "actual_cost_gbp": actual_total if paired else None,
        "delta_gbp": (
            actual_total - dp_total if paired else None
        ),
        "soc_pairs": soc_pairs,
        "soc_mae": soc_abs / soc_pairs if soc_pairs else None,
        "soc_bias": soc_sum / soc_pairs if soc_pairs else None,
        "action_slots": action_slots,
        "action_match_rate": (
            matches / action_slots if action_slots else None
        ),
        "solver_versions": sorted(
            {row.solver_version or "legacy" for row in results}
        ),
    }
