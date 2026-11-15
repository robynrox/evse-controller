"""Compare fixed, pure-follow and floor-plus-follow discharge arcs.

This is an offline experiment, not part of the live control path.  It builds
synthetic days (and replays any recorded planner runs it can find) and compares
the DP optimum with and without the experimental ``follow_hybrid_floors`` arcs.

Run with::

    PYTHONPATH=src poetry run python -m evse_controller.tools.hybrid_arc_experiment

The final summary reports the number of cases, how many used a hybrid arc,
improved/unchanged/worse counts, total baseline/hybrid/saving and the best
case.  ``--quiet`` prints only the summaries, ``--recorded-only`` skips the
synthetic cases and ``--synthetic-only`` skips the recorded runs.
``--realised`` also compares recorded plans against the measured daily house
load; that is a load-only simulation, not a full battery/SoC model.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Iterable, Optional

from evse_controller.planner.dp import DpPlanner, PlannerSlot
from evse_controller.daily_report import daily_log
from evse_controller.tariffs.octopus.battery_efficiency import (
    BatteryEfficiencyModel,
)
from evse_controller.utils.config import config

VOLTAGE_V = 240.0
MAX_DISCHARGE_A = 16.0
STORED_HURDLE_P = 12.0


@dataclass(frozen=True)
class CaseResult:
    name: str
    baseline_p: float
    hybrid_p: float
    saving_p: float
    floors: tuple[float, ...]
    baseline_actions: str
    hybrid_actions: str
    hybrid_used: bool
    realised_baseline_p: Optional[float] = None
    realised_hybrid_p: Optional[float] = None
    realised_saving_p: Optional[float] = None
    realised_slots: int = 0


def _planner() -> DpPlanner:
    return DpPlanner(
        efficiency_model=BatteryEfficiencyModel(),
        capacity_kwh=48.3,
        soc_step_percent=0.1,
        min_current_a=3.0,
        charge_cap_percent=97.0,
        protection_floor_percent=25.0,
        discharge_floor_percent=40.0,
        target_soc_percent=None,
    )


def _conservative_gate(
    import_p: float, export_p: float, floor_a: float
) -> Optional[float]:
    """Closed-form break-even for a discharge floor, without the 3 A floor."""
    if STORED_HURDLE_P >= import_p:
        return None
    if export_p >= STORED_HURDLE_P:
        return 0.0
    floor_w = floor_a * VOLTAGE_V
    return max(
        0.0,
        floor_w * (STORED_HURDLE_P - export_p) / (import_p - export_p),
    )


def _aggressive_gate(
    import_p: float, export_p: float, floor_a: float
) -> Optional[float]:
    """Scan the house load for the first economic floor-plus-follow start."""
    model = BatteryEfficiencyModel()
    for load_w in range(0, int(MAX_DISCHARGE_A * VOLTAGE_V) + 1):
        current = max(
            floor_a,
            round(load_w / VOLTAGE_V + 0.5),
        )
        current = max(floor_a, min(current, MAX_DISCHARGE_A))
        output_w = current * VOLTAGE_V
        efficiency = model.discharge_efficiency(current)
        revenue_p = (
            min(load_w, output_w) * import_p
            + max(0.0, output_w - load_w) * export_p
        ) / 1000.0
        cost_p = (
            output_w / 1000.0
        ) * (STORED_HURDLE_P / max(efficiency, 1e-9))
        if revenue_p >= cost_p:
            return float(load_w)
    return None


def _gates_for(
    import_p: float, export_p: float, floor_a: float
) -> tuple[Optional[float], Optional[float]]:
    return (
        _conservative_gate(import_p, export_p, floor_a),
        _aggressive_gate(import_p, export_p, floor_a),
    )


def _slot(
    index: int,
    *,
    import_p: float,
    export_p: float,
    load_samples: tuple[tuple[float, float], ...],
    pure_follow: bool = True,
) -> PlannerSlot:
    cons_gate, aggr_gate = _gates_for(import_p, export_p, 3.0)
    return PlannerSlot(
        start=datetime(2026, 11, 15) + timedelta(minutes=30 * index),
        slot=index,
        import_p=import_p,
        export_p=export_p,
        load_kw=sum(kw * weight for kw, weight in load_samples),
        load_samples=load_samples,
        load_source="synthetic",
        max_charge_current_a=16.0,
        max_discharge_current_a=MAX_DISCHARGE_A,
        follow_conservative_gate_w=cons_gate if pure_follow else None,
        follow_aggressive_gate_w=aggr_gate if pure_follow else None,
        follow_conservative_floor_percent=40.0,
        follow_aggressive_floor_percent=50.0,
    )


def _with_hybrid_floors(
    slots: Iterable[PlannerSlot], floors: Iterable[float]
) -> list[PlannerSlot]:
    result = []
    for slot in slots:
        entries = []
        for floor_a in floors:
            cons_gate, aggr_gate = _gates_for(
                slot.import_p, slot.export_p, floor_a
            )
            entries.append((float(floor_a), cons_gate, aggr_gate))
        result.append(
            replace(slot, follow_hybrid_floors=tuple(entries))
        )
    return result


def _candidate_floors(actions) -> list[float]:
    """The fixed discharge currents the baseline actually chose."""
    floors = {
        float(round(action.current_a))
        for action in actions
        if action.kind == "discharge"
        and not action.follow
        and action.current_a >= 3.0
    }
    expanded = set(floors)
    for floor_a in floors:
        for neighbour in (floor_a - 1.0, floor_a + 1.0):
            if 3.0 <= neighbour <= MAX_DISCHARGE_A:
                expanded.add(neighbour)
    return sorted(expanded)


def _describe(actions) -> str:
    parts = []
    for action in actions:
        if action.kind == "idle":
            parts.append("idle")
        elif action.kind == "discharge":
            parts.append(f"discharge {action.current_a:.0f}A")
        elif action.kind.startswith("follow_house_floor"):
            parts.append(
                f"{action.kind} floor {action.floor_current_a:.0f}A "
                f"{action.current_min_a:.0f}-{action.current_max_a:.0f}A"
            )
        elif action.kind.startswith("follow_house"):
            parts.append(
                f"{action.kind} "
                f"{action.current_min_a:.0f}-{action.current_max_a:.0f}A"
            )
        else:
            parts.append(f"{action.kind} {action.current_a:.0f}A")
    return ", ".join(parts)


def _number(value) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _signed_kw(energy: dict) -> Optional[float]:
    in_kwh = _number(energy.get("in"))
    out_kwh = _number(energy.get("out"))
    if in_kwh is None and out_kwh is None:
        return None
    return ((in_kwh or 0.0) - (out_kwh or 0.0)) / 0.5


def _house_kw(slot: dict) -> Optional[float]:
    energy = slot.get("energy") or {}
    grid = _signed_kw(energy.get("grid") or {})
    if grid is None:
        return None
    evse = _signed_kw(energy.get("evse") or {}) or 0.0
    return grid - evse


def _actual_wallbox_kw(
    slot: PlannerSlot, action, house_kw: float
) -> float:
    """Approximate the live actuator for one plan action.

    This is deliberately a simple load-follow simulation: it uses the actual
    house load, the recorded gate and the Wallbox current limits.  It does not
    model voltage-dependent efficiency or SoC limits, so it is a realised
    *load* comparison, not a full battery simulation.
    """
    kind = action.kind
    if kind == "idle":
        return 0.0
    if kind in ("charge", "discharge"):
        return float(action.ac_power_kw or 0.0)
    if kind.startswith("follow_house"):
        aggressive = kind.endswith("aggressive")
        if "floor" in kind:
            floor = action.floor_current_a
            gate = None
            if floor is not None:
                for floor_a, cons, aggr in slot.follow_hybrid_floors:
                    if abs(float(floor_a) - float(floor)) < 1e-9:
                        gate = aggr if aggressive else cons
                        break
        else:
            floor = action.current_min_a or 3.0
            gate = (
                slot.follow_aggressive_gate_w
                if aggressive
                else slot.follow_conservative_gate_w
            )
        if floor is None or gate is None:
            return 0.0
        house_w = max(0.0, house_kw * 1000.0)
        if house_w < float(gate):
            return 0.0
        bias = 0.5 if aggressive else -0.5
        current = round(house_w / VOLTAGE_V + bias)
        current = max(
            float(floor),
            min(current, float(slot.max_discharge_current_a)),
        )
        return -current * VOLTAGE_V / 1000.0
    if kind == "follow_solar":
        if house_kw >= 0:
            return 0.0
        floor = float(action.current_min_a or 3.0)
        surplus_w = -house_kw * 1000.0
        if surplus_w < floor * VOLTAGE_V:
            return 0.0
        current = round(surplus_w / VOLTAGE_V - 0.5)
        current = max(
            floor, min(current, float(slot.max_charge_current_a))
        )
        return current * VOLTAGE_V / 1000.0
    if kind.startswith("follow_bidirectional"):
        aggressive = kind.endswith("aggressive")
        if house_kw < 0:
            floor = 3.0
            surplus_w = -house_kw * 1000.0
            if surplus_w < floor * VOLTAGE_V:
                return 0.0
            current = round(surplus_w / VOLTAGE_V - 0.5)
            current = max(
                floor, min(current, float(slot.max_charge_current_a))
            )
            return current * VOLTAGE_V / 1000.0
        gate = (
            slot.follow_aggressive_gate_w
            if aggressive
            else slot.follow_conservative_gate_w
        )
        if gate is None:
            return 0.0
        house_w = max(0.0, house_kw * 1000.0)
        if house_w < float(gate):
            return 0.0
        bias = 0.5 if aggressive else -0.5
        current = round(house_w / VOLTAGE_V + bias)
        current = max(
            3.0, min(current, float(slot.max_discharge_current_a))
        )
        return -current * VOLTAGE_V / 1000.0
    return float(action.ac_power_kw or 0.0)


def _realised_cost(
    day, slots: list[PlannerSlot], actions
) -> tuple[Optional[float], int]:
    snapshot = daily_log.snapshot_for(day)
    daily_slots = snapshot.get("slots") or []
    if not daily_slots:
        return None, 0
    actions_by_key = {
        (action.start.date(), action.slot): action
        for action in actions
    }
    total_p = 0.0
    used = 0
    for slot in slots:
        if slot.start.date() != day:
            continue
        action = actions_by_key.get((slot.start.date(), slot.slot))
        if action is None:
            continue
        daily = (
            daily_slots[slot.slot]
            if 0 <= slot.slot < len(daily_slots)
            else {}
        )
        house_kw = _house_kw(daily)
        prices = daily.get("prices") or {}
        import_p = _number(prices.get("import_p"))
        export_p = _number(prices.get("export_p"))
        if house_kw is None or import_p is None or export_p is None:
            continue
        wallbox_kw = _actual_wallbox_kw(slot, action, house_kw)
        net_kw = house_kw + wallbox_kw
        total_p += (
            max(net_kw, 0.0) * import_p
            - max(-net_kw, 0.0) * export_p
        ) * 0.5
        used += 1
    if used == 0:
        return None, 0
    return total_p, used


def _run_case(
    name: str,
    slots: list[PlannerSlot],
    *,
    initial_soc: float,
    floors: Optional[Iterable[float]] = None,
    quiet: bool = False,
    realised_day=None,
) -> CaseResult:
    planner = _planner()
    baseline = planner.plan(slots, initial_soc)
    candidate = (
        list(floors)
        if floors is not None
        else _candidate_floors(baseline.actions)
    )
    default_floors = [5.0, 8.0, 11.0, 14.0]
    if not candidate:
        candidate = default_floors
    elif not any(floor < 16.0 for floor in candidate):
        candidate = sorted(set(candidate) | set(default_floors))
    hybrid_slots = _with_hybrid_floors(slots, candidate)
    hybrid = _planner().plan(hybrid_slots, initial_soc)
    delta = baseline.total_cost_p - hybrid.total_cost_p
    realised_baseline = None
    realised_hybrid = None
    realised_saving = None
    realised_slots = 0
    if realised_day is not None:
        realised_baseline, baseline_slots = _realised_cost(
            realised_day, slots, baseline.actions
        )
        realised_hybrid, hybrid_slots_used = _realised_cost(
            realised_day, hybrid_slots, hybrid.actions
        )
        if (
            realised_baseline is not None
            and realised_hybrid is not None
            and baseline_slots == hybrid_slots_used
        ):
            realised_saving = realised_baseline - realised_hybrid
            realised_slots = baseline_slots
        else:
            realised_baseline = None
            realised_hybrid = None
    result = CaseResult(
        name=name,
        baseline_p=baseline.total_cost_p,
        hybrid_p=hybrid.total_cost_p,
        saving_p=delta,
        floors=tuple(float(floor) for floor in candidate),
        baseline_actions=_describe(baseline.actions),
        hybrid_actions=_describe(hybrid.actions),
        hybrid_used=any(
            action.kind.startswith("follow_house_floor")
            for action in hybrid.actions
        ),
        realised_baseline_p=realised_baseline,
        realised_hybrid_p=realised_hybrid,
        realised_saving_p=realised_saving,
        realised_slots=realised_slots,
    )
    if not quiet:
        print(f"\n{name}")
        print(f"  floors: {', '.join(f'{f:.0f}A' for f in result.floors)}")
        print(
            f"  baseline: {result.baseline_p:8.3f}p  "
            f"{result.baseline_actions}"
        )
        print(
            f"  hybrid:   {result.hybrid_p:8.3f}p  "
            f"{result.hybrid_actions}"
        )
        print(f"  saving:   {result.saving_p:8.3f}p")
        if result.realised_saving_p is not None:
            print(
                f"  realised: {result.realised_baseline_p:8.3f}p -> "
                f"{result.realised_hybrid_p:8.3f}p, saving "
                f"{result.realised_saving_p:8.3f}p "
                f"({result.realised_slots} slots)"
            )
    return result


def _synthetic_cases(quiet: bool = False) -> list[CaseResult]:
    results: list[CaseResult] = []

    def run(name, slots, *, initial_soc, floors=None):
        results.append(
            _run_case(
                name,
                slots,
                initial_soc=initial_soc,
                floors=floors,
                quiet=quiet,
            )
        )

    constant = [_slot(i, import_p=25.0, export_p=10.0, load_samples=((2.64, 1.0),)) for i in range(6)]
    spike = [_slot(i, import_p=25.0, export_p=10.0, load_samples=((1.2, 0.5), (3.84, 0.5))) for i in range(6)]
    mixed = [_slot(i, import_p=25.0, export_p=10.0, load_samples=((0.96, 0.25), (1.92, 0.25), (2.88, 0.25), (3.84, 0.25))) for i in range(6)]
    low_high_export = [_slot(i, import_p=10.0, export_p=30.0, load_samples=((0.48, 1.0),)) for i in range(6)]
    variable = [
        _slot(i, import_p=20.0 + 3.0 * (i % 3), export_p=8.0 + 4.0 * (i % 4), load_samples=((0.96 + 0.48 * (i % 4), 1.0),))
        for i in range(12)
    ]

    run("constant 11 A house load", constant, initial_soc=60.0, floors=[11.0])
    run("spike 5 A / 16 A", spike, initial_soc=60.0, floors=[11.0])
    run("mixed 4 / 8 / 12 / 16 A", mixed, initial_soc=60.0, floors=[11.0])
    run("low load, high export", low_high_export, initial_soc=60.0, floors=[11.0])
    run("variable day", variable, initial_soc=60.0)

    fixed_spike = [
        _slot(
            i,
            import_p=25.0,
            export_p=10.0,
            load_samples=((1.2, 0.5), (3.84, 0.5)),
            pure_follow=False,
        )
        for i in range(6)
    ]
    fixed_mixed = [
        _slot(
            i,
            import_p=25.0,
            export_p=10.0,
            load_samples=((0.96, 0.25), (1.92, 0.25), (2.88, 0.25), (3.84, 0.25)),
            pure_follow=False,
        )
        for i in range(6)
    ]
    fixed_variable = [
        _slot(
            i,
            import_p=25.0,
            export_p=10.0,
            load_samples=((0.48, 0.4), (1.92, 0.3), (3.84, 0.3)),
            pure_follow=False,
        )
        for i in range(6)
    ]
    run("fixed-only spike 5 A / 16 A", fixed_spike, initial_soc=60.0, floors=[11.0])
    run("fixed-only mixed 4 / 8 / 12 / 16 A", fixed_mixed, initial_soc=60.0, floors=[11.0])
    run("fixed-only low / medium / high", fixed_variable, initial_soc=60.0, floors=[11.0])
    run(
        "fixed-only mixed, floor sweep 6-16 A",
        fixed_mixed,
        initial_soc=60.0,
        floors=range(6, 17),
    )
    run(
        "fixed-only mixed, near floor (45%)",
        fixed_mixed,
        initial_soc=45.0,
        floors=[11.0],
    )
    run(
        "fixed-only mixed, near cap (90%)",
        fixed_mixed,
        initial_soc=90.0,
        floors=[11.0],
    )
    run(
        "fixed-only mixed, below floor (30%)",
        fixed_mixed,
        initial_soc=30.0,
        floors=[11.0],
    )
    return results


def _historical_cases(
    quiet: bool = False, realised: bool = False
) -> list[CaseResult]:
    results: list[CaseResult] = []
    history_dir = Path(config.PLANNER_HISTORY_DIR)
    paths = sorted(history_dir.glob("*.runs.jsonl"))
    if not paths:
        print("\nNo recorded planner runs found.")
        return results
    for path in paths:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            run = json.loads(line)
            rows = run.get("inputs") or []
            if not rows:
                continue
            slots = []
            for item in rows:
                try:
                    start = datetime.fromisoformat(item["start"])
                except (KeyError, TypeError, ValueError):
                    continue
                load_samples = tuple(
                    (float(kw), float(weight))
                    for kw, weight in (item.get("load_samples") or [])
                )
                if not load_samples:
                    load_samples = ((float(item.get("load_kw") or 0.0), 1.0),)
                slots.append(
                    PlannerSlot(
                        start=start,
                        slot=int(item.get("slot", -1)),
                        import_p=float(item.get("import_p") or 0.0),
                        export_p=float(item.get("export_p") or 0.0),
                        load_kw=float(item.get("load_kw") or 0.0),
                        load_samples=load_samples,
                        load_source=str(item.get("load_source") or ""),
                        max_charge_current_a=float(
                            config.WALLBOX_MAX_CHARGE_CURRENT
                        ),
                        max_discharge_current_a=float(
                            config.WALLBOX_MAX_DISCHARGE_CURRENT
                        ),
                        forced=item.get("forced"),
                        session=item.get("session"),
                        follow_conservative_gate_w=item.get(
                            "follow_conservative_gate_w"
                        ),
                        follow_aggressive_gate_w=item.get(
                            "follow_aggressive_gate_w"
                        ),
                        follow_conservative_floor_percent=item.get(
                            "follow_conservative_floor_percent"
                        ),
                        follow_aggressive_floor_percent=item.get(
                            "follow_aggressive_floor_percent"
                        ),
                    )
                )
            if not slots:
                continue
            initial_soc = float(
                run.get("estimate_soc") or run.get("reported_soc") or 50.0
            )
            day_value = run.get("day") or path.stem
            try:
                realised_day = datetime.fromisoformat(
                    str(day_value)
                ).date()
            except (TypeError, ValueError):
                realised_day = None
            results.append(
                _run_case(
                    f"recorded {path.stem} slot {run.get('slot')}",
                    slots,
                    initial_soc=initial_soc,
                    quiet=quiet,
                    realised_day=realised_day if realised else None,
                )
            )
    return results


def _summary(label: str, results: list[CaseResult]) -> None:
    if not results:
        return
    improved = [result for result in results if result.saving_p > 1e-9]
    unchanged = [
        result for result in results if abs(result.saving_p) <= 1e-9
    ]
    worse = [result for result in results if result.saving_p < -1e-9]
    total_baseline = sum(result.baseline_p for result in results)
    total_hybrid = sum(result.hybrid_p for result in results)
    total_saving = total_baseline - total_hybrid
    print(f"\n{label}: {len(results)} cases")
    print(f"  hybrid used: {sum(1 for r in results if r.hybrid_used)}")
    print(
        f"  improved: {len(improved)}  unchanged: {len(unchanged)}  "
        f"worse: {len(worse)}"
    )
    print(f"  total baseline: {total_baseline:.3f}p")
    print(f"  total hybrid:   {total_hybrid:.3f}p")
    print(f"  total saving:   {total_saving:.3f}p")
    if improved:
        best = max(improved, key=lambda result: result.saving_p)
        print(
            f"  best: {best.name} ({best.saving_p:.3f}p), "
            f"average improved {total_saving / len(improved):.3f}p"
        )

    realised = [
        result
        for result in results
        if result.realised_saving_p is not None
    ]
    if not realised:
        return
    improved = [
        result for result in realised if result.realised_saving_p > 1e-9
    ]
    unchanged = [
        result for result in realised if abs(result.realised_saving_p) <= 1e-9
    ]
    worse = [
        result for result in realised if result.realised_saving_p < -1e-9
    ]
    total_baseline = sum(
        result.realised_baseline_p or 0.0 for result in realised
    )
    total_hybrid = sum(
        result.realised_hybrid_p or 0.0 for result in realised
    )
    total_saving = total_baseline - total_hybrid
    print(f"  realised cases: {len(realised)}")
    print(
        f"  realised improved: {len(improved)}  unchanged: {len(unchanged)}  "
        f"worse: {len(worse)}"
    )
    print(f"  realised baseline: {total_baseline:.3f}p")
    print(f"  realised hybrid:   {total_hybrid:.3f}p")
    print(f"  realised saving:   {total_saving:.3f}p")
    if improved:
        best = max(
            improved, key=lambda result: result.realised_saving_p or 0.0
        )
        print(
            f"  realised best: {best.name} "
            f"({best.realised_saving_p:.3f}p), average improved "
            f"{total_saving / len(improved):.3f}p"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare fixed, pure-follow and floor-plus-follow discharge arcs."
        )
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print the final summaries, not every case.",
    )
    parser.add_argument(
        "--recorded-only",
        action="store_true",
        help="Skip the synthetic cases.",
    )
    parser.add_argument(
        "--synthetic-only",
        action="store_true",
        help="Skip the recorded planner runs.",
    )
    parser.add_argument(
        "--realised",
        action="store_true",
        help=(
            "Also compare the plans against the measured daily house load. "
            "This is a load-only simulation, not a full battery model."
        ),
    )
    args = parser.parse_args()

    print("Hybrid floor-plus-follow discharge experiment")
    print(
        f"stored hurdle {STORED_HURDLE_P:.1f}p/kWh, "
        f"max discharge {MAX_DISCHARGE_A:.0f}A"
    )
    synthetic = (
        [] if args.recorded_only else _synthetic_cases(args.quiet)
    )
    recorded = (
        []
        if args.synthetic_only
        else _historical_cases(args.quiet, args.realised)
    )
    _summary("Synthetic cases", synthetic)
    _summary("Recorded runs", recorded)


if __name__ == "__main__":
    main()
