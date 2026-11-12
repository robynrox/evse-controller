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

from dataclasses import dataclass, replace
import math
from datetime import datetime
from typing import Optional, Sequence

SLOT_HOURS = 0.5
VOLTAGE_V = 240.0
AGGRESSIVE_DISCHARGE_BIAS_A = 0.5

#: Ranking penalty for ending below the target SoC.  It is not a real cost;
#: it makes the backward policy prefer a feasible target over a cheaper path
#: that misses it, and prefer the highest reachable SoC when the target itself
#: is unreachable — the same behaviour as the forward planner's relaxation.
TARGET_SHORTFALL_PENALTY_P_PER_STEP = 1_000_000.0


def _finite_or_none(value: float) -> Optional[float]:
    return float(value) if math.isfinite(value) else None


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
    #: Time-weighted net house-load samples as ``(load_kw, weight)``.  Empty
    #: means the planner uses ``load_kw`` alone.
    load_samples: tuple[tuple[float, float], ...] = ()
    load_source: str = ""
    #: House-import gates from the heuristic's conservative/aggressive
    #: discharge calculation.  ``None`` disables that follow arc.
    follow_conservative_gate_w: Optional[float] = None
    follow_aggressive_gate_w: Optional[float] = None
    follow_conservative_floor_percent: Optional[float] = None
    follow_aggressive_floor_percent: Optional[float] = None
    #: Unique position in the planning horizon.  Day slots repeat across a
    #: midnight crossing, so reports use this key for multi-day runs.
    horizon_index: int = -1


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
    current_min_a: Optional[float] = None
    current_max_a: Optional[float] = None
    follow: bool = False
    horizon_index: int = -1

    @property
    def commanded_current_a(self) -> Optional[int]:
        """The nearest whole-amp command for the Wallbox.

        ``current_a`` is the continuous optimum from the measured curve.  The
        actuator is integer-current, so the executor should use this rounded
        value and let the next receding-horizon pass correct the small SoC
        difference.
        """
        if self.kind == "idle":
            return 0
        if self.follow:
            return None
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
class PolicyOption:
    """One action considered at a slot and starting SoC."""

    kind: str
    current_a: float
    ac_power_kw: float
    soc_after: float
    immediate_cost_p: float
    future_cost_p: float
    total_cost_p: float
    policy_cost_p: float
    current_min_a: Optional[float] = None
    current_max_a: Optional[float] = None
    follow: bool = False
    feasible: bool = True
    reason: str = ""


@dataclass(frozen=True)
class PolicyCell:
    """The best action and its margin at one slot/SoC point."""

    slot: int
    state_index: int
    soc_percent: float
    action: Optional[PolicyOption]
    cost_to_go_p: Optional[float]
    advantage_p: Optional[float]
    second_best_p: Optional[float]
    target_reachable: bool


@dataclass(frozen=True)
class PolicyResult:
    """A complete optimal policy over a horizon.

    ``cells[slot][state]`` is the best action from that state.  The value
    layers are indexed by slot and then state, with one extra final layer:
    ``energy_values[n][state]`` is zero (the real energy cost ends there) while
    ``policy_values[n][state]`` carries the terminal target penalty.
    ``energy_values`` is the real cost of following the chosen policy; it is
    not the unconstrained minimum, because the policy may pay to meet the
    target.
    """

    slots: tuple[PlannerSlot, ...]
    cells: tuple[tuple[PolicyCell, ...], ...]
    energy_values: tuple[tuple[float, ...], ...]
    policy_values: tuple[tuple[float, ...], ...]
    reachable_values: tuple[tuple[bool, ...], ...]
    terminal_state: int
    relaxed: bool
    initial_soc_percent: float
    actions: tuple[PlannerAction, ...]
    final_soc: float
    total_cost_p: float


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
    current_min_a: Optional[float] = None
    current_max_a: Optional[float] = None
    floor_percent: Optional[float] = None
    follow: bool = False


class DpPlanner:
    """Dynamic-programming planner for a finite half-hour horizon."""

    def __init__(
        self,
        *,
        efficiency_model: Optional[object] = None,
        capacity_kwh: Optional[float] = None,
        soc_step_percent: float = 0.5,
        min_current_a: float = 3.0,
        charge_cap_percent: float = 97.0,
        protection_floor_percent: float = 25.0,
        discharge_floor_percent: Optional[float] = None,
        target_soc_percent: Optional[float] = 50.0,
        degradation_p_per_kwh: float = 0.0,
        terminal_energy_value_p_per_kwh: float = 0.0,
        horizon_slots: Optional[int] = None,
    ):
        if capacity_kwh is None:
            # Imported lazily: the octopus package imports the planner, so a
            # module-level import here would create a cycle.
            from evse_controller.tariffs.octopus.battery_efficiency import (
                DASHBOARD_SOC_CAPACITY_KWH,
            )

            capacity_kwh = DASHBOARD_SOC_CAPACITY_KWH
        capacity_kwh = float(capacity_kwh)
        if capacity_kwh <= 0:
            raise ValueError("capacity_kwh must be positive")
        if soc_step_percent <= 0 or soc_step_percent > 100:
            raise ValueError("soc_step_percent must be in (0, 100]")
        if min_current_a <= 0:
            raise ValueError("min_current_a must be positive")
        if target_soc_percent is not None and not (
            0.0 <= float(target_soc_percent) <= 100.0
        ):
            raise ValueError("target_soc_percent must be in [0, 100] or None")

        if efficiency_model is None:
            from evse_controller.tariffs.octopus.battery_efficiency import (
                BatteryEfficiencyModel,
            )

            efficiency_model = BatteryEfficiencyModel(
                capacity_kwh=capacity_kwh
            )
        self.efficiency_model = efficiency_model
        self.capacity_kwh = float(capacity_kwh)
        self.soc_step_percent = float(soc_step_percent)
        self.min_current_a = float(min_current_a)
        self.charge_cap_percent = float(charge_cap_percent)
        self.protection_floor_percent = float(protection_floor_percent)
        self.discharge_floor_percent = max(
            self.protection_floor_percent,
            self.protection_floor_percent
            if discharge_floor_percent is None
            else float(discharge_floor_percent),
        )
        self.target_soc_percent = (
            None
            if target_soc_percent is None
            else float(target_soc_percent)
        )
        self.degradation_p_per_kwh = max(0.0, float(degradation_p_per_kwh))
        if terminal_energy_value_p_per_kwh < 0:
            raise ValueError(
                "terminal_energy_value_p_per_kwh cannot be negative"
            )
        self.terminal_energy_value_p_per_kwh = float(
            terminal_energy_value_p_per_kwh
        )
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
                for template in self._templates_for_state(templates, state):
                    next_state = self._feasible_next_state(
                        state, template
                    )
                    if next_state is None:
                        continue

                    candidate = base_cost + template.cost_p
                    current_best = next_dp[next_state]
                    # On an exact tie prefer idling in place.  Without this,
                    # a degenerate flat-price day can look like a cheap
                    # discharge/charge cycle because of the order in which
                    # states are visited.
                    idle_tie = (
                        abs(candidate - current_best) <= 1e-9
                        and template.kind == "idle"
                        and state == next_state
                    )
                    if candidate < current_best - 1e-12 or idle_tie:
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

    def policy(
        self,
        slots: Sequence[PlannerSlot],
        initial_soc_percent: float = 0.0,
    ) -> PolicyResult:
        """Return the optimal action for every slot and starting SoC.

        The backward pass is independent of the starting SoC, so one call gives
        the whole policy surface for a run.  ``policy_values`` carries the
        terminal-target penalty used for ranking; ``energy_values`` is the
        real energy cost of following the chosen policy.
        """
        horizon = list(slots)
        if self.horizon_slots is not None:
            horizon = horizon[: self.horizon_slots]
        n_slots = len(horizon)
        n_states = self._state_count + 1

        if n_slots == 0:
            state = self._clamp_state(
                int(round(initial_soc_percent / self.soc_step_percent))
            )
            return PolicyResult(
                slots=(),
                cells=(),
                energy_values=((0.0,) * n_states,),
                policy_values=(
                    tuple(
                        self._terminal_penalty(s)
                        - self._terminal_credit(s)
                        for s in range(n_states)
                    ),
                ),
                reachable_values=(
                    tuple(
                        True
                        if self.target_soc_percent is None
                        else (
                            s * self.soc_step_percent
                            >= self.target_soc_percent - 1e-9
                        )
                        for s in range(n_states)
                    ),
                ),
                terminal_state=state,
                relaxed=(
                    False
                    if self.target_soc_percent is None
                    else (
                        state * self.soc_step_percent
                        < self.target_soc_percent - 1e-9
                    )
                ),
                initial_soc_percent=float(initial_soc_percent),
                actions=(),
                final_soc=state * self.soc_step_percent,
                total_cost_p=0.0,
            )

        target_state = self._target_state()
        energy_next: list[float] = [0.0] * n_states
        policy_next: list[float] = [
            self._terminal_penalty(state) - self._terminal_credit(state)
            for state in range(n_states)
        ]
        reachable_next: list[bool] = [
            state >= target_state for state in range(n_states)
        ]

        energy_layers: list[Optional[tuple[float, ...]]] = [
            None
        ] * (n_slots + 1)
        policy_layers: list[Optional[tuple[float, ...]]] = [
            None
        ] * (n_slots + 1)
        reachable_layers: list[Optional[tuple[bool, ...]]] = [
            None
        ] * (n_slots + 1)
        energy_layers[n_slots] = tuple(energy_next)
        policy_layers[n_slots] = tuple(policy_next)
        reachable_layers[n_slots] = tuple(reachable_next)

        choices: list[Optional[list[Optional[tuple[_ActionTemplate, int]]]]] = [
            None
        ] * n_slots

        for index in range(n_slots - 1, -1, -1):
            templates = self._action_templates(horizon[index])
            energy_cur: list[float] = [float("inf")] * n_states
            policy_cur: list[float] = [float("inf")] * n_states
            reachable_cur: list[bool] = [False] * n_states
            choice_cur: list[Optional[tuple[_ActionTemplate, int]]] = [
                None
            ] * n_states

            for state in range(n_states):
                best_policy = float("inf")
                best_energy = float("inf")
                best_template = None
                best_next = None
                for template in self._templates_for_state(templates, state):
                    next_state = self._feasible_next_state(
                        state, template
                    )
                    if next_state is None:
                        continue
                    policy_cost = (
                        template.cost_p + policy_next[next_state]
                    )
                    energy_cost = (
                        template.cost_p + energy_next[next_state]
                    )
                    if policy_cost < best_policy - 1e-12 or (
                        abs(policy_cost - best_policy) <= 1e-12
                        and energy_cost < best_energy - 1e-12
                    ):
                        best_policy = policy_cost
                        best_energy = energy_cost
                        best_template = template
                        best_next = next_state
                if best_template is None:
                    continue
                policy_cur[state] = best_policy
                energy_cur[state] = best_energy
                reachable_cur[state] = reachable_next[best_next]
                choice_cur[state] = (best_template, best_next)

            energy_next = energy_cur
            policy_next = policy_cur
            reachable_next = reachable_cur
            choices[index] = choice_cur
            energy_layers[index] = tuple(energy_cur)
            policy_layers[index] = tuple(policy_cur)
            reachable_layers[index] = tuple(reachable_cur)

        cells: list[tuple[PolicyCell, ...]] = []
        for index in range(n_slots):
            templates = self._action_templates(horizon[index])
            choice_cur = choices[index] or []
            row = []
            for state in range(n_states):
                entry = choice_cur[state]
                if entry is None:
                    row.append(
                        PolicyCell(
                            slot=index,
                            state_index=state,
                            soc_percent=state * self.soc_step_percent,
                            action=None,
                            cost_to_go_p=_finite_or_none(
                                energy_layers[index][state]  # type: ignore[index]
                            ),
                            advantage_p=None,
                            second_best_p=None,
                            target_reachable=bool(
                                reachable_layers[index][state]  # type: ignore[index]
                            ),
                        )
                    )
                    continue

                template, next_state = entry
                future_energy = energy_layers[index + 1][next_state]  # type: ignore[index]
                future_policy = policy_layers[index + 1][next_state]  # type: ignore[index]
                action = PolicyOption(
                    kind=template.kind,
                    current_a=template.current_a,
                    ac_power_kw=template.ac_power_kw,
                    soc_after=next_state * self.soc_step_percent,
                    immediate_cost_p=template.cost_p,
                    future_cost_p=future_energy,
                    total_cost_p=template.cost_p + future_energy,
                    policy_cost_p=template.cost_p + future_policy,
                    current_min_a=template.current_min_a,
                    current_max_a=template.current_max_a,
                    follow=template.follow,
                )

                mode_best: dict[str, float] = {}
                for candidate in self._templates_for_state(
                    templates, state
                ):
                    candidate_next = self._feasible_next_state(
                        state, candidate
                    )
                    if candidate_next is None:
                        continue
                    policy_cost = (
                        candidate.cost_p
                        + policy_layers[index + 1][candidate_next]  # type: ignore[index]
                    )
                    current = mode_best.get(candidate.kind)
                    if current is None or policy_cost < current:
                        mode_best[candidate.kind] = policy_cost
                mode_costs = sorted(mode_best.values())
                second_best = (
                    mode_costs[1] if len(mode_costs) > 1 else None
                )
                advantage = (
                    second_best - mode_costs[0]
                    if second_best is not None
                    else None
                )

                row.append(
                    PolicyCell(
                        slot=index,
                        state_index=state,
                        soc_percent=state * self.soc_step_percent,
                        action=action,
                        cost_to_go_p=_finite_or_none(
                            energy_layers[index][state]  # type: ignore[index]
                        ),
                        advantage_p=advantage,
                        second_best_p=second_best,
                        target_reachable=bool(
                            reachable_layers[index][state]  # type: ignore[index]
                        ),
                    )
                )
            cells.append(tuple(row))

        initial = self._clamp_state(
            int(round(initial_soc_percent / self.soc_step_percent))
        )
        state = initial
        actions = []
        for index, slot in enumerate(horizon):
            entry = (choices[index] or [])[state]
            if entry is None:
                break
            template, next_state = entry
            actions.append(
                PlannerAction(
                    slot=(
                        slot.slot if slot.slot >= 0 else index
                    ),
                    start=slot.start,
                    kind=template.kind,
                    current_a=template.current_a,
                    ac_power_kw=template.ac_power_kw,
                    stored_kwh=template.stored_kwh,
                    soc_before=state * self.soc_step_percent,
                    soc_after=next_state * self.soc_step_percent,
                    cost_p=template.cost_p,
                    session=template.session,
                    current_min_a=template.current_min_a,
                    current_max_a=template.current_max_a,
                    follow=template.follow,
                    horizon_index=slot.horizon_index,
                )
            )
            state = next_state

        total = energy_layers[0][initial]  # type: ignore[index]
        return PolicyResult(
            slots=tuple(horizon),
            cells=tuple(cells),
            energy_values=tuple(
                layer for layer in energy_layers if layer is not None
            ),
            policy_values=tuple(
                layer for layer in policy_layers if layer is not None
            ),
            reachable_values=tuple(
                layer for layer in reachable_layers if layer is not None
            ),
            terminal_state=state,
            relaxed=not bool(reachable_layers[0][initial]),  # type: ignore[index]
            initial_soc_percent=float(initial_soc_percent),
            actions=tuple(actions),
            final_soc=state * self.soc_step_percent,
            total_cost_p=(
                float(total) if math.isfinite(total) else 0.0
            ),
        )

    def options(
        self,
        policy: PolicyResult,
        slot_index: int,
        state_index: int,
    ) -> tuple[PolicyOption, ...]:
        """Every feasible action at one policy cell, best first."""
        if not (0 <= slot_index < len(policy.slots)):
            return ()
        if not (0 <= state_index <= self._state_count):
            return ()
        templates = self._templates_for_state(
            self._action_templates(policy.slots[slot_index]),
            state_index,
        )
        options = []
        for template in templates:
            next_state = self._feasible_next_state(
                state_index, template
            )
            if next_state is None:
                continue
            future_energy = policy.energy_values[slot_index + 1][
                next_state
            ]
            future_policy = policy.policy_values[slot_index + 1][
                next_state
            ]
            options.append(
                PolicyOption(
                    kind=template.kind,
                    current_a=template.current_a,
                    ac_power_kw=template.ac_power_kw,
                    soc_after=next_state * self.soc_step_percent,
                    immediate_cost_p=template.cost_p,
                    future_cost_p=future_energy,
                    total_cost_p=template.cost_p + future_energy,
                    policy_cost_p=template.cost_p + future_policy,
                    current_min_a=template.current_min_a,
                    current_max_a=template.current_max_a,
                    follow=template.follow,
                )
            )
        options.sort(key=lambda option: option.policy_cost_p)
        return tuple(options)

    def _target_state(self) -> int:
        if self.target_soc_percent is None:
            return 0
        return self._clamp_state(
            int(
                math.ceil(
                    self.target_soc_percent / self.soc_step_percent
                    - 1e-9
                )
            )
        )

    def _terminal_penalty(self, state: int) -> float:
        if self.target_soc_percent is None:
            return 0.0
        target = self._target_state()
        if state >= target:
            return 0.0
        return (
            target - state
        ) * TARGET_SHORTFALL_PENALTY_P_PER_STEP

    def _state_kwh(self, state: int) -> float:
        return (
            state * self.soc_step_percent / 100.0 * self.capacity_kwh
        )

    def _terminal_credit(self, state: int) -> float:
        """Value of energy left in the pack at the end of the horizon."""
        if self.terminal_energy_value_p_per_kwh <= 0:
            return 0.0
        return self.terminal_energy_value_p_per_kwh * self._state_kwh(state)

    def _feasible_next_state(
        self, state: int, template: _ActionTemplate
    ) -> Optional[int]:
        """Apply the action masks and end-state bounds to one transition."""
        soc = state * self.soc_step_percent
        if (
            template.kind == "charge"
            and soc >= self.charge_cap_percent - 1e-9
        ):
            return None
        if (
            template.kind == "discharge"
            and soc <= self.protection_floor_percent + 1e-9
        ):
            return None

        next_state = self._clamp_state(state + template.delta_steps)
        next_soc = next_state * self.soc_step_percent
        if template.kind == "discharge":
            action_floor = (
                self.discharge_floor_percent
                if template.floor_percent is None
                else float(template.floor_percent)
            )
            if next_soc < action_floor - 1e-9:
                return None
        elif template.kind in (
            "follow_house_conservative",
            "follow_house_aggressive",
            "follow_bidirectional_conservative",
            "follow_bidirectional_aggressive",
        ):
            action_floor = self.discharge_floor_percent
            if template.floor_percent is not None:
                action_floor = max(
                    action_floor, float(template.floor_percent)
                )
            if next_soc < action_floor - 1e-9:
                return None
        if (
            template.kind == "charge"
            and next_soc > self.charge_cap_percent + 1e-9
        ):
            return None
        if template.follow:
            if template.delta_steps > 0 and (
                next_soc > self.charge_cap_percent + 1e-9
            ):
                return None
        if (
            next_state == state
            and template.kind != "idle"
            and not template.follow
        ):
            return None
        return next_state

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
            template = self._template_for_current(
                slot, current, charge=False
            )
            # Supplier grid-support sessions may use the hard protection
            # floor; ordinary fixed discharge stops at the export floor.
            return [
                replace(
                    template,
                    floor_percent=self.protection_floor_percent,
                )
            ]
        if forced:
            raise ValueError(f"Unknown forced action: {slot.forced}")

        templates = [idle] + self._integer_templates(slot)
        if slot.follow_conservative_gate_w is not None:
            template = self._follow_house_template(
                slot,
                gate_w=slot.follow_conservative_gate_w,
                aggressive=False,
                floor_percent=slot.follow_conservative_floor_percent,
                kind="follow_house_conservative",
            )
            if template is not None:
                templates.append(template)
        if slot.follow_aggressive_gate_w is not None:
            template = self._follow_house_template(
                slot,
                gate_w=slot.follow_aggressive_gate_w,
                aggressive=True,
                floor_percent=slot.follow_aggressive_floor_percent,
                kind="follow_house_aggressive",
            )
            if template is not None:
                templates.append(template)
        for kind, aggressive in (
            ("follow_bidirectional_conservative", False),
            ("follow_bidirectional_aggressive", True),
        ):
            template = self._follow_bidirectional_template(
                slot,
                aggressive=aggressive,
                kind=kind,
            )
            if template is not None:
                templates.append(template)
        solar = self._follow_solar_template(slot)
        if solar is not None:
            templates.append(solar)
        return templates

    def _templates_for_state(
        self,
        templates: list[_ActionTemplate],
        state: int,
    ) -> list[_ActionTemplate]:
        """Drop unidirectional follows when a bidirectional one is feasible.

        A feasible bidirectional arc already covers charging from surplus and
        discharging above the house gate, and is at least as valuable when both
        directions occur in the slot.  The one-direction arcs are fallbacks
        for SoCs where the bidirectional arc cannot reach its floor or charge
        cap.
        """
        feasible_bidirectional = {
            template.kind
            for template in templates
            if template.kind.startswith("follow_bidirectional")
            and self._feasible_next_state(state, template) is not None
        }
        if not feasible_bidirectional:
            return templates

        filtered = []
        for template in templates:
            if (
                template.kind == "follow_solar"
                and feasible_bidirectional
            ):
                continue
            if (
                template.kind == "follow_house_conservative"
                and "follow_bidirectional_conservative"
                in feasible_bidirectional
            ):
                continue
            if (
                template.kind == "follow_house_aggressive"
                and "follow_bidirectional_aggressive"
                in feasible_bidirectional
            ):
                continue
            filtered.append(template)
        return filtered

    def _follow_house_template(
        self,
        slot: PlannerSlot,
        *,
        gate_w: float,
        aggressive: bool,
        floor_percent: Optional[float],
        kind: str,
    ) -> Optional[_ActionTemplate]:
        samples = slot.load_samples or ((slot.load_kw, 1.0),)
        if gate_w is None or gate_w >= 1e8:
            return None
        minimum = int(math.ceil(self.min_current_a - 1e-9))
        maximum = int(math.floor(slot.max_discharge_current_a + 1e-9))
        if maximum < minimum:
            return None

        expected_cost = 0.0
        net_stored = 0.0
        gross_stored = 0.0
        signed_ac = 0.0
        currents = []
        for load_kw, weight in samples:
            load_w = max(0.0, float(load_kw) * 1000.0)
            if load_w <= 0 or load_w + 1e-9 < gate_w:
                expected_cost += weight * self._grid_energy_cost_at(
                    slot, load_kw, 0.0
                )
                continue
            raw = load_w / VOLTAGE_V
            if aggressive:
                raw += AGGRESSIVE_DISCHARGE_BIAS_A
                current = int(round(raw))
            else:
                current = int(math.floor(raw))
            current = max(minimum, min(maximum, current))
            ac_kw = float(
                self.efficiency_model.discharge_ac_power_kw(current)
            )
            efficiency = float(
                self.efficiency_model.discharge_efficiency(current)
            )
            stored = ac_kw * SLOT_HOURS / max(efficiency, 1e-9)
            expected_cost += weight * self._grid_energy_cost_at(
                slot, load_kw, -ac_kw
            )
            net_stored += weight * stored
            gross_stored += weight * stored
            signed_ac -= weight * ac_kw
            currents.append(current)

        if not currents:
            return None
        delta_soc = -net_stored / self.capacity_kwh * 100.0
        steps = int(round(delta_soc / self.soc_step_percent))
        return _ActionTemplate(
            kind=kind,
            current_a=sum(currents) / len(currents),
            ac_power_kw=signed_ac,
            stored_kwh=net_stored,
            delta_soc=steps * self.soc_step_percent,
            delta_steps=steps,
            cost_p=(
                expected_cost
                + self.degradation_p_per_kwh * gross_stored
            ),
            session=slot.session,
            current_min_a=float(min(currents)),
            current_max_a=float(max(currents)),
            floor_percent=floor_percent,
            follow=True,
        )

    def _follow_bidirectional_template(
        self,
        slot: PlannerSlot,
        *,
        aggressive: bool,
        kind: str,
    ) -> Optional[_ActionTemplate]:
        """One arc that follows both solar surplus and house demand.

        Each load sample is handled independently: charge when it has a
        usable surplus, discharge when it reaches the house gate, otherwise
        idle.  The samples are then folded into one expected transition, so
        this adds a single arc rather than enumerating charge/discharge
        current pairs inside the slot.
        """
        samples = slot.load_samples or ((slot.load_kw, 1.0),)
        minimum = int(math.ceil(self.min_current_a - 1e-9))
        max_charge = int(math.floor(slot.max_charge_current_a + 1e-9))
        max_discharge = int(math.floor(slot.max_discharge_current_a + 1e-9))
        gate_w = (
            slot.follow_aggressive_gate_w
            if aggressive
            else slot.follow_conservative_gate_w
        )
        floor_percent = (
            slot.follow_aggressive_floor_percent
            if aggressive
            else slot.follow_conservative_floor_percent
        )

        expected_cost = 0.0
        net_stored = 0.0
        gross_stored = 0.0
        signed_ac = 0.0
        currents: list[int] = []
        charged = False
        discharged = False

        for load_kw, weight in samples:
            load_kw = float(load_kw)
            if load_kw < 0:
                surplus_w = -load_kw * 1000.0
                current = int(math.floor(surplus_w / VOLTAGE_V))
                if current < minimum or max_charge < minimum:
                    expected_cost += weight * self._grid_energy_cost_at(
                        slot, load_kw, 0.0
                    )
                    continue
                current = min(max_charge, current)
                ac_kw = float(
                    self.efficiency_model.charge_ac_power_kw(current)
                )
                efficiency = float(
                    self.efficiency_model.charge_efficiency(current)
                )
                stored = ac_kw * SLOT_HOURS * efficiency
                expected_cost += weight * self._grid_energy_cost_at(
                    slot, load_kw, ac_kw
                )
                net_stored += weight * stored
                gross_stored += weight * stored
                signed_ac += weight * ac_kw
                currents.append(current)
                charged = True
                continue

            load_w = max(0.0, load_kw * 1000.0)
            if (
                gate_w is None
                or gate_w >= 1e8
                or load_w + 1e-9 < gate_w
                or max_discharge < minimum
            ):
                expected_cost += weight * self._grid_energy_cost_at(
                    slot, load_kw, 0.0
                )
                continue

            raw = load_w / VOLTAGE_V
            if aggressive:
                raw += AGGRESSIVE_DISCHARGE_BIAS_A
                current = int(round(raw))
            else:
                current = int(math.floor(raw))
            current = max(minimum, min(max_discharge, current))
            ac_kw = float(
                self.efficiency_model.discharge_ac_power_kw(current)
            )
            efficiency = float(
                self.efficiency_model.discharge_efficiency(current)
            )
            stored = ac_kw * SLOT_HOURS / max(efficiency, 1e-9)
            expected_cost += weight * self._grid_energy_cost_at(
                slot, load_kw, -ac_kw
            )
            net_stored -= weight * stored
            gross_stored += weight * stored
            signed_ac -= weight * ac_kw
            currents.append(-current)
            discharged = True

        if not (charged and discharged):
            return None

        delta_soc = net_stored / self.capacity_kwh * 100.0
        steps = int(round(delta_soc / self.soc_step_percent))
        return _ActionTemplate(
            kind=kind,
            current_a=sum(currents) / len(currents),
            ac_power_kw=signed_ac,
            stored_kwh=net_stored,
            delta_soc=steps * self.soc_step_percent,
            delta_steps=steps,
            cost_p=(
                expected_cost
                + self.degradation_p_per_kwh * gross_stored
            ),
            session=slot.session,
            current_min_a=float(min(currents)),
            current_max_a=float(max(currents)),
            floor_percent=floor_percent,
            follow=True,
        )

    def _follow_solar_template(
        self, slot: PlannerSlot
    ) -> Optional[_ActionTemplate]:
        samples = slot.load_samples or ((slot.load_kw, 1.0),)
        minimum = int(math.ceil(self.min_current_a - 1e-9))
        maximum = int(math.floor(slot.max_charge_current_a + 1e-9))
        if maximum < minimum:
            return None

        expected_cost = 0.0
        net_stored = 0.0
        gross_stored = 0.0
        signed_ac = 0.0
        currents = []
        for load_kw, weight in samples:
            if load_kw >= 0:
                expected_cost += weight * self._grid_energy_cost_at(
                    slot, load_kw, 0.0
                )
                continue
            surplus_w = -float(load_kw) * 1000.0
            current = int(math.floor(surplus_w / VOLTAGE_V))
            if current < minimum:
                expected_cost += weight * self._grid_energy_cost_at(
                    slot, load_kw, 0.0
                )
                continue
            current = min(maximum, current)
            ac_kw = float(
                self.efficiency_model.charge_ac_power_kw(current)
            )
            efficiency = float(
                self.efficiency_model.charge_efficiency(current)
            )
            stored = ac_kw * SLOT_HOURS * efficiency
            expected_cost += weight * self._grid_energy_cost_at(
                slot, load_kw, ac_kw
            )
            net_stored += weight * stored
            gross_stored += weight * stored
            signed_ac += weight * ac_kw
            currents.append(current)

        if not currents:
            return None
        delta_soc = net_stored / self.capacity_kwh * 100.0
        steps = int(round(delta_soc / self.soc_step_percent))
        return _ActionTemplate(
            kind="follow_solar",
            current_a=sum(currents) / len(currents),
            ac_power_kw=signed_ac,
            stored_kwh=net_stored,
            delta_soc=steps * self.soc_step_percent,
            delta_steps=steps,
            cost_p=(
                expected_cost
                + self.degradation_p_per_kwh * gross_stored
            ),
            session=slot.session,
            current_min_a=float(min(currents)),
            current_max_a=float(max(currents)),
            follow=True,
        )

    def _integer_templates(
        self, slot: PlannerSlot
    ) -> list[_ActionTemplate]:
        """Every commandable integer current in each direction.

        The Wallbox commands integer amps, so the planner must not invent
        fractional currents.  The measured current → SoC curve is still used
        for the efficiency and AC power at each integer point; the SoC step is
        rounded to the state grid, exactly as the executor sees it.
        """
        templates: list[_ActionTemplate] = []
        for current in self._currents(slot.max_charge_current_a):
            templates.append(
                self._template_for_current(
                    slot, float(current), charge=True
                )
            )
        for current in self._currents(slot.max_discharge_current_a):
            templates.append(
                self._template_for_current(
                    slot, float(current), charge=False
                )
            )
        return templates

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

    @staticmethod
    def _grid_energy_cost_at(
        slot: PlannerSlot,
        load_kw: float,
        wallbox_ac_kw: float,
    ) -> float:
        net_kw = float(load_kw) + wallbox_ac_kw
        import_kw = max(net_kw, 0.0)
        export_kw = max(-net_kw, 0.0)
        return (
            float(slot.import_p) * import_kw * SLOT_HOURS
            - float(slot.export_p) * export_kw * SLOT_HOURS
        )

    def _grid_cost(
        self,
        slot: PlannerSlot,
        wallbox_ac_kw: float,
        stored_kwh: float,
    ) -> float:
        samples = slot.load_samples or ((slot.load_kw, 1.0),)
        expected = sum(
            weight
            * self._grid_energy_cost_at(slot, load_kw, wallbox_ac_kw)
            for load_kw, weight in samples
        )
        return (
            expected
            + self.degradation_p_per_kwh * abs(stored_kwh)
        )

    # -- state and terminal helpers ------------------------------------

    def _choose_terminal(
        self, dp: list[float]
    ) -> tuple[Optional[int], bool]:
        reachable = [
            (state, cost)
            for state, cost in enumerate(dp)
            if math.isfinite(cost)
        ]
        if not reachable:
            return None, False
        if self.target_soc_percent is None:
            # No fixed end-of-day SoC target: choose the state with the best
            # cost after crediting energy left in the pack.  The credit is a
            # continuation value, so the plan does not stop charging merely
            # because the horizon has ended.
            return min(
                reachable,
                key=lambda item: item[1] - self._terminal_credit(item[0]),
            )[0], False
        target_state = self._clamp_state(
            int(
                math.ceil(
                    self.target_soc_percent / self.soc_step_percent
                    - 1e-9
                )
            )
        )
        meeting = [
            (state, cost) for state, cost in reachable if state >= target_state
        ]
        if meeting:
            return min(
                meeting,
                key=lambda item: item[1] - self._terminal_credit(item[0]),
            )[0], False
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
                    current_min_a=template.current_min_a,
                    current_max_a=template.current_max_a,
                    follow=template.follow,
                    horizon_index=slots[index].horizon_index,
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
