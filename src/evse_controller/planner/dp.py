"""Pure-Python DP planner for the Wallbox.

The planner works on half-hour slots and an integer grid of SoC states.  For
each slot it evaluates three action classes:

* idle;
* charge at an integer commanded current;
* discharge at an integer commanded current.

The AC power and efficiency at each current come from the measured
:class:`BatteryEfficiencyModel`.  A transition is costed from the resulting
AC power, the net house load, the slot's import/export price and an explicit
degradation charge per kWh throughput.

The state grid is deliberately small enough to solve in pure Python.  It is
*successor-indexed* in the sense that the DP table is keyed by the SoC reached
at the end of each slot, and the action that produced each state is retained
for path reconstruction.  The state resolution is configurable; 0.5% keeps a
3 A charge (about 0.45% in half an hour on the measured pack) from collapsing
to a no-op.

The planner is intentionally not wired into the live controller yet.  It
returns a full plan so it can be backtested and explained before it replaces
the heuristic.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from datetime import datetime
from typing import Optional, Sequence

from evse_controller.tariffs.octopus.battery_efficiency import (
    BatteryEfficiencyModel,
    DASHBOARD_SOC_CAPACITY_KWH,
)


SLOT_HOURS = 0.5


@dataclass(frozen=True)
class PlannerSlot:
    """One half-hour input row.

    ``load_kw`` is the net house load *before* the Wallbox: positive is grid
    import, negative is surplus solar/export.  The Wallbox action is added to
    it to get the grid balance.

    ``forced`` is used by supplier sessions: ``idle`` for an EV-away window,
    ``charge_max`` for a free-import window, ``discharge_max`` for a
    grid-support window.  ``None`` leaves the DP free to choose.
    """

    start: datetime
    import_p: float
    export_p: float
    load_kw: float = 0.0
    #: Absolute half-hour slot within the local day (0-47).  The DP may be
    #: given only the remaining horizon, but its actions still refer to the
    #: real slot so history and reports can line them up with the day.
    slot: int = -1
    max_charge_current_a: float = 32.0
    max_discharge_current_a: float = 32.0
    forced: Optional[str] = None
    session: Optional[str] = None


@dataclass(frozen=True)
class PlannerAction:
    """One chosen slot action."""

    slot: int
    start: datetime
    kind: str  # "idle", "charge" or "discharge"
    current_a: float
    ac_power_kw: float
    stored_kwh: float
    soc_before: float
    soc_after: float
    cost_p: float
    session: Optional[str] = None

    @property
    def commanded_current_a(self) -> int:
        """The nearest whole-amp command for the Wallbox.

        ``current_a`` is the continuous optimum from the measured curve.  The
        actuator is integer-current, so the executor should use this rounded
        value and let the next receding-horizon pass correct the small SoC
        difference.
        """
        if self.kind == "idle":
            return 0
        return max(0, int(round(self.current_a)))


@dataclass(frozen=True)
class PlanResult:
    """The complete plan, including the terminal relaxation state."""

    actions: tuple[PlannerAction, ...]
    final_soc: float
    total_cost_p: float
    success: bool
    relaxed: bool = False
    message: str = ""


@dataclass(frozen=True)
class _ActionTemplate:
    kind: str
    current_a: float
    ac_power_kw: float
    stored_kwh: float
    delta_soc: float
    delta_steps: int
    cost_p: float
    session: Optional[str]


class DpPlanner:
    """Dynamic-programming planner for a finite half-hour horizon."""

    def __init__(
        self,
        *,
        efficiency_model: Optional[object] = None,
        capacity_kwh: float = DASHBOARD_SOC_CAPACITY_KWH,
        soc_step_percent: float = 0.5,
        min_current_a: float = 3.0,
        charge_cap_percent: float = 97.0,
        protection_floor_percent: float = 25.0,
        target_soc_percent: float = 50.0,
        degradation_p_per_kwh: float = 0.0,
        horizon_slots: Optional[int] = None,
    ):
        if capacity_kwh <= 0:
            raise ValueError("capacity_kwh must be positive")
        if soc_step_percent <= 0 or soc_step_percent > 100:
            raise ValueError("soc_step_percent must be in (0, 100]")
        if min_current_a <= 0:
            raise ValueError("min_current_a must be positive")

        self.efficiency_model = (
            efficiency_model
            if efficiency_model is not None
            else BatteryEfficiencyModel(capacity_kwh=capacity_kwh)
        )
        self.capacity_kwh = float(capacity_kwh)
        self.soc_step_percent = float(soc_step_percent)
        self.min_current_a = float(min_current_a)
        self.charge_cap_percent = float(charge_cap_percent)
        self.protection_floor_percent = float(protection_floor_percent)
        self.target_soc_percent = float(target_soc_percent)
        self.degradation_p_per_kwh = max(0.0, float(degradation_p_per_kwh))
        self.horizon_slots = (
            None if horizon_slots is None else max(0, int(horizon_slots))
        )

        steps = 100.0 / self.soc_step_percent
        if abs(steps - round(steps)) > 1e-9:
            raise ValueError("soc_step_percent must divide 100 exactly")
        self._state_count = int(round(steps))

    # -- public API -----------------------------------------------------

    def plan(
        self,
        slots: Sequence[PlannerSlot],
        initial_soc_percent: float,
    ) -> PlanResult:
        """Return the minimum-cost action for every slot in *slots*."""
        if not slots:
            return PlanResult(
                actions=(),
                final_soc=float(initial_soc_percent),
                total_cost_p=0.0,
                success=False,
                message="No slots to plan",
            )

        horizon = list(slots)
        if self.horizon_slots is not None:
            horizon = horizon[: self.horizon_slots]
        if not horizon:
            return PlanResult(
                actions=(),
                final_soc=float(initial_soc_percent),
                total_cost_p=0.0,
                success=False,
                message="Empty planning horizon",
            )

        n_slots = len(horizon)
        n_states = self._state_count + 1
        initial = self._clamp_state(
            int(round(initial_soc_percent / self.soc_step_percent))
        )

        # dp[state] = minimum cumulative cost to be in *state* at the start of
        # the current slot.  prev/choice are keyed by the state *reached* at
        # the end of the slot, so reconstruction is a simple walk backwards.
        dp = [float("inf")] * n_states
        dp[initial] = 0.0
        prev: list[list[int]] = [
            [-1] * n_states for _ in range(n_slots)
        ]
        choice: list[list[Optional[_ActionTemplate]]] = [
            [None] * n_states for _ in range(n_slots)
        ]

        for index, slot in enumerate(horizon):
            templates = self._action_templates(slot)
            next_dp = [float("inf")] * n_states
            next_prev = [-1] * n_states
            next_choice: list[Optional[_ActionTemplate]] = [None] * n_states

            for state, base_cost in enumerate(dp):
                if not math.isfinite(base_cost):
                    continue
                soc = state * self.soc_step_percent
                for template in templates:
                    if template.kind == "charge":
                        if soc >= self.charge_cap_percent - 1e-9:
                            continue
                    elif template.kind == "discharge":
                        if soc <= self.protection_floor_percent + 1e-9:
                            continue

                    next_state = self._clamp_state(
                        state + template.delta_steps
                    )
                    next_soc = next_state * self.soc_step_percent

                    # The hard protection floor and charge cap are end-state
                    # constraints as well as action masks.
                    if (
                        template.kind == "discharge"
                        and next_soc < self.protection_floor_percent - 1e-9
                    ):
                        continue
                    if (
                        template.kind == "charge"
                        and next_soc > self.charge_cap_percent + 1e-9
                    ):
                        continue
                    # A transition that rounds to the same state is a no-op;
                    # do not charge for doing nothing.
                    if next_state == state and template.kind != "idle":
                        continue

                    candidate = base_cost + template.cost_p
                    if candidate < next_dp[next_state] - 1e-12:
                        next_dp[next_state] = candidate
                        next_prev[next_state] = state
                        next_choice[next_state] = template

            dp = next_dp
            prev[index] = next_prev
            choice[index] = next_choice

        terminal_state, relaxed = self._choose_terminal(dp)
        if terminal_state is None:
            return PlanResult(
                actions=(),
                final_soc=float(initial_soc_percent),
                total_cost_p=0.0,
                success=False,
                message="No reachable state at the end of the horizon",
            )

        actions = self._reconstruct(
            terminal_state, prev, choice, horizon
        )
        total_cost = dp[terminal_state]
        final_soc = terminal_state * self.soc_step_percent
        return PlanResult(
            actions=tuple(actions),
            final_soc=final_soc,
            total_cost_p=float(total_cost),
            success=not relaxed,
            relaxed=relaxed,
            message=(
                "Optimal plan"
                if not relaxed
                else f"Target {self.target_soc_percent:.1f}% unreachable; "
                "kept the highest reachable SoC"
            ),
        )

    # -- action construction -------------------------------------------

    def _action_templates(self, slot: PlannerSlot) -> list[_ActionTemplate]:
        idle = _ActionTemplate(
            kind="idle",
            current_a=0.0,
            ac_power_kw=0.0,
            stored_kwh=0.0,
            delta_soc=0.0,
            delta_steps=0,
            cost_p=self._grid_cost(slot, 0.0, 0.0),
            session=slot.session,
        )

        forced = (slot.forced or "").lower()
        if forced == "idle":
            return [idle]
        if forced == "charge_max":
            current = self._max_integer_current(slot.max_charge_current_a)
            if current is None:
                return [idle]
            return [self._template_for_current(slot, current, charge=True)]
        if forced == "discharge_max":
            current = self._max_integer_current(
                slot.max_discharge_current_a
            )
            if current is None:
                return [idle]
            return [
                self._template_for_current(slot, current, charge=False)
            ]
        if forced:
            raise ValueError(f"Unknown forced action: {slot.forced}")

        return [idle] + self._delta_templates(slot)

    def _delta_templates(self, slot: PlannerSlot) -> list[_ActionTemplate]:
        """Actions for every reachable successor SoC, not every current.

        The DP is keyed by successor SoC.  For each feasible SoC step we invert
        the measured current → SoC-change curve to find the commanded current
        that produces it.  That keeps the stored energy exact and removes the
        rounding arbitrage a current-indexed grid can create: a 3 A charge and
        a 3 A discharge no longer look like a free cycle because their rounded
        states happened to cancel.
        """
        templates: list[_ActionTemplate] = []
        step = self.soc_step_percent

        charge_curve = self._delta_curve(slot, charge=True)
        if charge_curve:
            min_delta = min(delta for _, delta in charge_curve)
            max_delta = max(delta for _, delta in charge_curve)
            start = int(math.ceil(min_delta / step - 1e-9))
            end = int(math.floor(max_delta / step + 1e-9))
            for steps in range(max(1, start), end + 1):
                delta = steps * step
                current = self._interpolate_current(charge_curve, delta)
                if current is not None:
                    templates.append(
                        self._template_for_delta(slot, delta, current)
                    )

        discharge_curve = self._delta_curve(slot, charge=False)
        if discharge_curve:
            min_delta = min(delta for _, delta in discharge_curve)
            max_delta = max(delta for _, delta in discharge_curve)
            start = int(math.ceil(min_delta / step - 1e-9))
            end = int(math.floor(max_delta / step + 1e-9))
            for steps in range(start, min(-1, end) + 1):
                delta = steps * step
                current = self._interpolate_current(discharge_curve, delta)
                if current is not None:
                    templates.append(
                        self._template_for_delta(slot, delta, current)
                    )
        return templates

    def _delta_curve(
        self, slot: PlannerSlot, *, charge: bool
    ) -> list[tuple[float, float]]:
        """Measured-current points as ``(current, SoC change)``.

        Charge changes are positive and increasing with current; discharge
        changes are negative and decreasing.
        """
        max_current = (
            slot.max_charge_current_a
            if charge
            else slot.max_discharge_current_a
        )
        points: list[tuple[float, float]] = []
        for current in self._currents(max_current):
            if charge:
                ac_kw = float(
                    self.efficiency_model.charge_ac_power_kw(current)
                )
                efficiency = float(
                    self.efficiency_model.charge_efficiency(current)
                )
                delta = (
                    ac_kw
                    * SLOT_HOURS
                    * efficiency
                    / self.capacity_kwh
                    * 100.0
                )
            else:
                ac_kw = float(
                    self.efficiency_model.discharge_ac_power_kw(current)
                )
                efficiency = float(
                    self.efficiency_model.discharge_efficiency(current)
                )
                delta = (
                    -ac_kw
                    * SLOT_HOURS
                    / max(efficiency, 1e-9)
                    / self.capacity_kwh
                    * 100.0
                )
            points.append((float(current), delta))
        return points

    @staticmethod
    def _interpolate_current(
        curve: list[tuple[float, float]], target_delta: float
    ) -> Optional[float]:
        for (left_current, left_delta), (
            right_current,
            right_delta,
        ) in zip(curve, curve[1:]):
            low, high = sorted((left_delta, right_delta))
            if low - 1e-9 <= target_delta <= high + 1e-9:
                if abs(right_delta - left_delta) < 1e-12:
                    return left_current
                fraction = (
                    (target_delta - left_delta)
                    / (right_delta - left_delta)
                )
                return left_current + fraction * (
                    right_current - left_current
                )
        for current, delta in curve:
            if abs(target_delta - delta) < 1e-9:
                return current
        return None

    def _template_for_delta(
        self, slot: PlannerSlot, delta_soc: float, current_a: float
    ) -> _ActionTemplate:
        stored_kwh = abs(delta_soc) / 100.0 * self.capacity_kwh
        if delta_soc > 0:
            efficiency = float(
                self.efficiency_model.charge_efficiency(current_a)
            )
            ac_kw = stored_kwh / (
                SLOT_HOURS * max(efficiency, 1e-9)
            )
            kind = "charge"
            signed_ac = ac_kw
        else:
            efficiency = float(
                self.efficiency_model.discharge_efficiency(current_a)
            )
            ac_kw = stored_kwh * efficiency / SLOT_HOURS
            kind = "discharge"
            signed_ac = -ac_kw
        steps = int(round(delta_soc / self.soc_step_percent))
        return _ActionTemplate(
            kind=kind,
            current_a=float(current_a),
            ac_power_kw=signed_ac,
            stored_kwh=stored_kwh,
            delta_soc=steps * self.soc_step_percent,
            delta_steps=steps,
            cost_p=self._grid_cost(slot, signed_ac, stored_kwh),
            session=slot.session,
        )

    def _template_for_current(
        self, slot: PlannerSlot, current_a: float, *, charge: bool
    ) -> _ActionTemplate:
        """A forced action at a specific integer current."""
        if charge:
            ac_kw = float(
                self.efficiency_model.charge_ac_power_kw(current_a)
            )
            efficiency = float(
                self.efficiency_model.charge_efficiency(current_a)
            )
            stored_kwh = ac_kw * SLOT_HOURS * efficiency
            delta = stored_kwh / self.capacity_kwh * 100.0
            signed_ac = ac_kw
            kind = "charge"
        else:
            ac_kw = float(
                self.efficiency_model.discharge_ac_power_kw(current_a)
            )
            efficiency = float(
                self.efficiency_model.discharge_efficiency(current_a)
            )
            stored_kwh = ac_kw * SLOT_HOURS / max(efficiency, 1e-9)
            delta = -stored_kwh / self.capacity_kwh * 100.0
            signed_ac = -ac_kw
            kind = "discharge"
        steps = int(round(delta / self.soc_step_percent))
        return _ActionTemplate(
            kind=kind,
            current_a=float(current_a),
            ac_power_kw=signed_ac,
            stored_kwh=stored_kwh,
            delta_soc=steps * self.soc_step_percent,
            delta_steps=steps,
            cost_p=self._grid_cost(slot, signed_ac, stored_kwh),
            session=slot.session,
        )

    def _grid_cost(
        self,
        slot: PlannerSlot,
        wallbox_ac_kw: float,
        stored_kwh: float,
    ) -> float:
        net_kw = float(slot.load_kw) + wallbox_ac_kw
        import_kw = max(net_kw, 0.0)
        export_kw = max(-net_kw, 0.0)
        return (
            float(slot.import_p) * import_kw * SLOT_HOURS
            - float(slot.export_p) * export_kw * SLOT_HOURS
            + self.degradation_p_per_kwh * abs(stored_kwh)
        )

    # -- state and terminal helpers ------------------------------------

    def _choose_terminal(
        self, dp: list[float]
    ) -> tuple[Optional[int], bool]:
        target_state = int(
            math.ceil(self.target_soc_percent / self.soc_step_percent - 1e-9)
        )
        target_state = self._clamp_state(target_state)
        reachable = [
            (state, cost)
            for state, cost in enumerate(dp)
            if math.isfinite(cost)
        ]
        if not reachable:
            return None, False
        meeting = [
            (state, cost) for state, cost in reachable if state >= target_state
        ]
        if meeting:
            return min(meeting, key=lambda item: item[1])[0], False
        # Relax explicitly: keep the highest reachable SoC, then the cheapest
        # path to it.  This is preferable to returning no plan.
        highest = max(state for state, _ in reachable)
        best = min(
            (cost for state, cost in reachable if state == highest)
        )
        return highest, True

    def _reconstruct(
        self,
        terminal_state: int,
        prev: list[list[int]],
        choice: list[list[Optional[_ActionTemplate]]],
        slots: Sequence[PlannerSlot],
    ) -> list[PlannerAction]:
        actions: list[PlannerAction] = []
        state = terminal_state
        for index in range(len(slots) - 1, -1, -1):
            template = choice[index][state]
            previous = prev[index][state]
            if template is None or previous < 0:
                return []
            actions.append(
                PlannerAction(
                    slot=(
                        slots[index].slot
                        if slots[index].slot >= 0
                        else index
                    ),
                    start=slots[index].start,
                    kind=template.kind,
                    current_a=template.current_a,
                    ac_power_kw=template.ac_power_kw,
                    stored_kwh=template.stored_kwh,
                    soc_before=previous * self.soc_step_percent,
                    soc_after=state * self.soc_step_percent,
                    cost_p=template.cost_p,
                    session=template.session,
                )
            )
            state = previous
        actions.reverse()
        return actions

    def _currents(self, max_current_a: float) -> list[int]:
        start = int(math.ceil(self.min_current_a - 1e-9))
        end = int(math.floor(float(max_current_a) + 1e-9))
        if end < start:
            return []
        return list(range(start, end + 1))

    def _max_integer_current(
        self, max_current_a: float
    ) -> Optional[float]:
        currents = self._currents(max_current_a)
        return float(currents[-1]) if currents else None

    def _clamp_state(self, state: int) -> int:
        return max(0, min(self._state_count, state))
