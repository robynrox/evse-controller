from ..base import Tariff
from .agile_rates import fetch_day_rates
from .battery_efficiency import BatteryEfficiencyModel, DASHBOARD_SOC_CAPACITY_KWH
from evse_controller.drivers.EvseController import ControlState
from evse_controller.utils.config import config
from evse_controller.energy_sessions import (
    FREE_IMPORT,
    GRID_SUPPORT,
    SLOT_MINUTES,
    session_store,
)
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.utils.logging_config import debug, info, warning, error
import threading
import queue
import math
import copy
from collections import deque
from typing import Optional
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
import html as html_module


@dataclass
class StorageDecision:
    """Structured result of the three-tier store/export evaluation.

    ``_should_use_bidirectional_mode()`` keeps returning a bool for existing
    callers; this object carries the reasoning the dashboard needs without
    recomputing the decision.
    """

    use_bidirectional: bool
    tier: str
    reason: str
    current_rate_p: float
    future_rate_p: Optional[float] = None
    target_slot: Optional[int] = None
    round_trip: Optional[float] = None
    efficiency_label: str = ""


@dataclass
class DecisionSnapshot:
    """Dashboard-facing record of the last control decision.

    Written by the control-loop thread in ``get_control_state()`` and
    ``set_home_demand_levels()``, read by Flask when it renders the dashboard.
    A single reference assignment is atomic in CPython, and the dashboard
    tolerates a snapshot that is up to one control cycle (~20 s) old.
    """

    updated_at: datetime
    mode: str
    reason: str
    detail: str = ""
    soc_percent: int = -1
    target_soc_percent: Optional[float] = None
    projected_soc_at_2330: Optional[float] = None
    planned_export_slots: Optional[list] = None
    planned_slot_actions: Optional[dict] = None
    current_rate_p: Optional[float] = None
    target_rate_p: Optional[float] = None
    target_slot: Optional[int] = None
    harvest_current_a: Optional[float] = None
    surplus_w: Optional[float] = None
    efficiency_pct: Optional[float] = None
    efficiency_label: str = ""
    min_gain_p: Optional[float] = None
    charge_min_a: Optional[float] = None
    charge_max_a: Optional[float] = None
    discharge_min_a: Optional[float] = None
    discharge_max_a: Optional[float] = None
    discharge_aggressive: bool = True
    charge_activation_w: Optional[float] = None
    discharge_activation_w: Optional[float] = None
    # Extra context for the "why this decision?" page.  Defaults keep the
    # snapshot useful for branches that do not need them.
    decision_tier: str = ""
    store_decision: bool = False
    shortfall: bool = False
    import_rate_p: Optional[float] = None
    house_floor_soc: Optional[float] = None
    charge_cap_percent: Optional[float] = None
    stored_cost_p: Optional[float] = None
    voltage_v: Optional[float] = None
    # Ledger at decision time.  Captured automatically by _record_decision so
    # the explanation page can show what produced stored_cost_p.
    ledger_dc_kwh: Optional[float] = None
    ledger_cost_gbp: Optional[float] = None
    ledger_stored_cost_p: Optional[float] = None
    ledger_delivered_cost_p: Optional[float] = None
    ledger_basis: str = ""
    ledger_seeded: bool = False
    ledger_saved_today_gbp: Optional[float] = None


class IOctGoWithAgileOutgoingTariff(Tariff):
    """Intelligent Octopus Go tariff with Agile Outgoing export display.

    This tariff combines:
    - Intelligent Octopus Go import (cheap rate 23:30-05:30)
    - Agile Outgoing export rate display on dashboard
    - Intelligent bidirectional load-following for solar charging

    The dashboard shows a 48-cell visual strip of today's Agile Outgoing
    export rates, allowing users to see peak export periods at a glance.

    The tariff intelligently chooses between:
    - LOAD_FOLLOW_DISCHARGE: Export when current rates are favorable
    - LOAD_FOLLOW_BIDIRECTIONAL: Store solar energy based on three-tier logic:

    **Energy Shortfall Mode:**

    Before applying the storage/export logic, the tariff projects the SoC at
    23:30 slot by slot.  Ordinary slots lose
    NON_EXPORT_SLOT_SOC_LOSS_PERCENT, free-import session slots gain the
    full-charge rate, grid-support session slots lose the bulk-export rate and
    planned EV-away slots are left out entirely.
    The bulk-export planner normally uses MIN_AGILE_DISCHARGE_SOC as its
    floor.  High-value slots whose price beats offsetting the house load may
    spend the reserve down to the absolute minimum instead.  The
    aggressive/conservative house-load decision uses a lower floor,
    MIN_AGILE_DISCHARGE_SOC less HOUSE_LOAD_MARGIN_PERCENT (default 5%,
    clamped above the 25% protection reserve).  That lets the controller stay
    aggressive after a bulk export has consumed the planned headroom, instead
    of switching to conservative the moment the projection reaches the target.

    If the projection is below that house floor, the day is treated as an
    energy shortfall.  High-value exports already planned are still protected;
    everything else switches to a conservative bidirectional mode:

    - High-value export slots may still be planned down to the absolute
      minimum, because their sale value beats covering the house at 3 A.
    - While such a slot is still ahead, house discharge is paused so the pack
      is not spent before the export.  Surplus solar may still charge it.
    - With no high-value export left, discharge covers most of the house load
      but deliberately leaves a small import (activation floored at the 3 A
      minimum, -0.5 A bias) so it does not intentionally export. This replaces
      the fixed SOC_THRESHOLD_FOR_STRATEGY approach in the standard IOCTGO
      tariff.
    - The shortfall state is recalculated whenever the export plan is
      recalculated (slot boundaries, significant SoC changes, and rate
      refreshes), keeping the plan and discharge mode consistent.

    **Three-Tier Storage Decision:**

    Tier 1 - Storage Floor (< STORAGE_FLOOR_THRESHOLD_P p/kWh):
      Always store - rate is too trivial to justify exporting

    Tier 2 - Self-Use Value (STORAGE_FLOOR_THRESHOLD_P p-SELF_USE_VALUE_THRESHOLD_P p/kWh, low SoC):
      Store for self-consumption when SoC is below threshold
      (avoids importing at the peak rate later; the effective value is the
      peak import price less the minimum profit margin, times the measured
      harvest -> self-use round trip, i.e. charge at the actual solar current
      and discharge at ~3 A)

    Tier 3 - Future Export Optimization (all other cases):
      Store if: (best_future_rate - margin) × solar round trip > current_rate
      Export otherwise

    Decision summary:
        if current_rate < STORAGE_FLOOR_THRESHOLD_P:
            STORE (rate too trivial)
        elif current_rate < self_use_value and SoC < threshold:
            STORE (self-use value)
        elif (future_rate - margin) × solar_round_trip > current_rate:
            STORE (better future value)
        else:
            EXPORT now

    **Missing rate data is 0p, not a mode.**  If Octopus has not supplied
    prices, every current and future export price is treated as 0p.  The tier
    logic then stores (Tier 1) and the export planner finds no profitable
    slots.  There is no separate "no rates" branch, and stale prices from a
    previous day are ignored.

    **The solar current is not fixed at 3 A.**  In bidirectional mode the
    load-following loop charges at whatever current the solar surplus supports
    (from the Wallbox minimum up to the configured maximum).  The efficiency
    therefore depends on the surplus, and the same store/export comparison must
    use the current that will actually be used:

    - 3 A harvest -> export at the configured current: 0.580 × 0.943 = 0.547
    - 6 A harvest -> export: 0.778 × 0.943 = 0.734
    - 12 A harvest -> export: 0.880 × 0.943 = 0.830
    - grid charge at max -> export at max: 0.883 × 0.943 = 0.833
    - 3 A harvest -> self-use discharge at 3 A: 0.580 × 0.734 = 0.426

    Using 3 A for every solar decision is pessimistic on a sunny day: it can
    export when storing at 6–12 A would have paid.  The tariff estimates the
    current from the live grid surplus, rounds up to a measured point, and
    uses that for the threshold.  Rounding up is deliberate: efficiency
    improves with current, so rounding down understates the round trip and can
    reject a profitable store.  A grid charge for arbitrage still runs at the
    maximum current, and a self-use discharge is assumed to run at the Wallbox
    minimum (~3 A, which is enough to cover the house base load).

    Attributes:
        time_of_use (dict): IOCTGO time periods and rates
        agile_rates (list): Today's Agile Outgoing rates (p/kWh)
        agile_rates_fetched_at (datetime): When rates were last fetched
        STORAGE_FLOOR_THRESHOLD_P (float): Rate below which always store (=off peak import price)
        SELF_USE_VALUE_THRESHOLD_P (float): Effective self-use value at the minimum
            harvest current (peak import price times the 3 A/3 A round trip).
            The live decision uses the actual harvest current instead.
        BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL (float): Solar-harvest
            round trip at the minimum current (3 A charge → export current
            discharge); a floor/reference value, not the live threshold
        BATTERY_ROUND_TRIP_EFFICIENCY (float): Grid-charge → export round trip
        STORED_TO_AC_EFFICIENCY (float): Pack → AC efficiency at export current
        SOC_CAPACITY_KWH (float): Effective kWh per 100% dashboard SoC
        EFFICIENCY_MODEL (BatteryEfficiencyModel): Measured current-dependent model
        SOLAR_CHARGE_REFERENCE_CURRENT (float): Minimum harvest current, used
            when the solar surplus is unknown
        SELF_USE_DISCHARGE_REFERENCE_CURRENT (float): Discharge current assumed
            for self-consumption (~3 A, the house base load)
        EXPORT_REFERENCE_CURRENT (float): Discharge current used for export
        MIN_PROFIT_MARGIN_P_PER_KWH (float): Minimum profit margin, p/kWh
            delivered, required before cycling the battery or exporting
    """

    # Agile Outgoing tariff codes by region
    AGILE_TARIFF_CODES = {
        'A': 'AGILE-OUTGOING-19-05-13',
        'B': 'AGILE-OUTGOING-19-05-13',
        'C': 'AGILE-OUTGOING-19-05-13',
        'D': 'AGILE-OUTGOING-19-05-13',
        'E': 'AGILE-OUTGOING-19-05-13',
        'F': 'AGILE-OUTGOING-19-05-13',
        'G': 'AGILE-OUTGOING-19-05-13',
        'H': 'AGILE-OUTGOING-19-05-13',
        'J': 'AGILE-OUTGOING-19-05-13',
        'K': 'AGILE-OUTGOING-19-05-13',  # Southern Wales (default)
        'L': 'AGILE-OUTGOING-19-05-13',
        'M': 'AGILE-OUTGOING-19-05-13',
        'N': 'AGILE-OUTGOING-19-05-13',
        'P': 'AGILE-OUTGOING-19-05-13',
    }

    # SoC at or below which the pack is never discharged.
    BATTERY_PROTECTION_RESERVE_SOC = 25

    # Activation power that no house load will reach.  Used when even the
    # minimum discharge output cannot repay the stored energy, so a discharge
    # must never start rather than exporting at a loss.
    NEVER_DISCHARGE_ACTIVATION_W = 1_000_000.0

    # Window used to estimate the recent rate of change of "saved today".
    SAVINGS_RATE_WINDOW_SECONDS = 120

    # Smallest ledger that is trusted as a cost basis.  A few watt-seconds of
    # meter noise must not price the whole pack; below this the fallback cost
    # is used instead.
    MIN_LEDGER_BASIS_KWH = 0.05

    # Version of the on-disk state.  Bump when a field changes meaning so a
    # future loader can migrate rather than silently misread old data.
    STATE_VERSION = 1

    # Test hook: point persistence at a temporary file.  Production uses
    # config.IOCTGO_AGILE_STATE_FILE.
    STATE_PATH = None

    # Ledger settling after a direction change.  The Wallbox powers up or down
    # before it reaches the commanded current and draws roughly 200 W while it
    # does so.  These are the delays already used by the Wallbox thread
    # (``start_charging`` and ``large_change``); ignore the ledger for the same
    # time so that transient is not booked at the import price.  Current
    # changes within an active direction are not delayed.
    LEDGER_STARTUP_SETTLE_SECONDS = 21.9
    LEDGER_SHUTDOWN_SETTLE_SECONDS = 10.9
    LEDGER_DIRECTION_DEADBAND_W = 50.0

    # Load-following discharge biases.  Positive exports the excess, negative
    # deliberately leaves a small import.
    AGGRESSIVE_DISCHARGE_BIAS_A = 0.5
    CONSERVATIVE_DISCHARGE_BIAS_A = -0.5

    # Styles for the compact decision line.  Kept separate from the rate-strip
    # CSS so the line is styled even while the rates are still loading.
    DECISION_LINE_CSS = """
    <style>
    .ioctgo-decision-line {
        display: flex;
        flex-wrap: wrap;
        align-items: baseline;
        gap: 2px 10px;
        font-size: 12px;
        line-height: 1.35;
        color: #444;
        margin: 8px 0 0;
        padding: 0 2px;
    }
    .ioctgo-decision-line .ioctgo-mode {
        color: #111;
        font-weight: 600;
        white-space: nowrap;
    }
    .ioctgo-decision-line .ioctgo-reason { color: #555; }
    .ioctgo-decision-line .ioctgo-metric {
        color: #666;
        white-space: nowrap;
    }
    .ioctgo-decision-line .ioctgo-saved {
        color: #2e7d32;
        font-weight: 600;
        white-space: nowrap;
    }
    .ioctgo-decision-line .ioctgo-lost {
        color: #c62828;
        font-weight: 600;
        white-space: nowrap;
    }
    .ioctgo-decision-line .ioctgo-rate {
        color: #777;
        font-weight: 400;
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
    }
    .ioctgo-decision-line .ioctgo-explain {
        color: #0b5cad;
        font-weight: 600;
        text-decoration: none;
        white-space: nowrap;
    }
    .ioctgo-decision-line .ioctgo-explain:hover { text-decoration: underline; }
    @media (max-width: 700px) {
        .ioctgo-decision-line { font-size: 11px; gap: 2px 8px; }
    }
    </style>
    """

    def __init__(self, command_queue: Optional[queue.Queue] = None, battery_capacity_kwh=None,
                 bulk_discharge_start_time=None, bulk_discharge_end_time=None, enable_bulk_discharge=None,
                 state_path=None):
        """Initialize tariff with IOCTGO logic and Agile Outgoing rate fetching."""
        super().__init__(command_queue=command_queue)

        # Where the ledger and today's saving are kept between runs.  Tests
        # pass a temporary file; production uses config.IOCTGO_AGILE_STATE_FILE.
        self._state_path_override = Path(state_path) if state_path else None

        # Supplier-requested half-hour windows.  The shared store means the web
        # UI can add one and this running tariff sees it immediately.  Tests
        # replace the attribute with an isolated store.
        self.session_store = session_store
        
        # Measured, current-dependent AC <-> pack model.  See
        # reports/efficiency/REVIEW.md and battery_efficiency.py.
        self.EFFICIENCY_MODEL = BatteryEfficiencyModel()
        self.SOC_CAPACITY_KWH = DASHBOARD_SOC_CAPACITY_KWH

        # Minimum profit margin, p/kWh delivered.  The old blanket 0.50
        # constant carried an implicit wear margin; the measured model is pure
        # efficiency, so make the hurdle explicit instead of hiding it in the
        # efficiency numbers.  It covers marginal wear plus unmodelled
        # actuation/forecast overheads and is deliberately a policy value, not
        # a degradation measurement.  The DP will use the same value as a
        # provision in its arc cost.
        self.MIN_PROFIT_MARGIN_P_PER_KWH = (
            config.IOCTGO_MIN_PROFIT_MARGIN_P_PER_KWH
        )

        # Reference currents.  The solar harvest current is not fixed: in
        # bidirectional mode the load-following loop charges at whatever the
        # surplus supports, from the Wallbox minimum up to the configured
        # maximum.  SOLAR_CHARGE_REFERENCE_CURRENT is the fallback when the
        # surplus is unknown, and the worst-case value for logging.  A grid
        # charge for arbitrage runs at the maximum.  A self-use discharge is
        # assumed to run at the Wallbox minimum (~3 A), because that is what
        # covers the house base load.
        #
        # Use the configured currents directly: the model clamps efficiency at
        # the last measured point (16 A) but extrapolates AC power above it, so
        # a non-derated 32 A unit plans proportionally more export than the
        # derated test unit.  If your Wallbox is physically derated, set
        # WALLBOX_MAX_CHARGE_CURRENT / WALLBOX_MAX_DISCHARGE_CURRENT to the
        # derated value (e.g. 16 A).
        self.SOLAR_CHARGE_REFERENCE_CURRENT = config.WALLBOX_MIN_CHARGE_CURRENT
        self.GRID_CHARGE_REFERENCE_CURRENT = config.WALLBOX_MAX_CHARGE_CURRENT
        self.EXPORT_REFERENCE_CURRENT = config.WALLBOX_MAX_DISCHARGE_CURRENT

        # Discharge to cover the house load runs at the minimum current in
        # normal operation (~3 A, ~0.72 kW).  Using the export current here
        # would over-value self-use storage by ~28% at 3 A.
        self.SELF_USE_DISCHARGE_REFERENCE_CURRENT = max(
            config.WALLBOX_MIN_DISCHARGE_CURRENT,
            config.WALLBOX_MIN_CHARGE_CURRENT,
        )

        # Worst-case solar round trip, at the minimum harvest current.  The
        # live decision uses the surplus-derived current instead; this is the
        # floor used when the surplus is unknown and for logging.
        self.BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL = (
            self.solar_export_round_trip()
        )

        # Storage decision thresholds - TODO: move to config.yaml
        # Below this rate, always store solar energy regardless of other factors
        # This prevents exporting at trivial rates when energy may be needed later
        self.STORAGE_FLOOR_THRESHOLD_P = config.IMPORT_LOW

        # Self-use value threshold at the minimum harvest current.  The live
        # decision recomputes this from the actual harvest current.
        self.SELF_USE_VALUE_THRESHOLD_P = self.self_use_value_threshold_p()
        
        self.time_of_use = {
            "low":  {"start": "23:30", "end": "05:30", "import_rate": config.IMPORT_LOW / 100, "export_rate": config.EXPORT_MID},
            "high": {"start": "05:30", "end": "23:30", "import_rate": config.IMPORT_HIGH / 100, "export_rate": config.EXPORT_MID}
        }
        
        # Agile Outgoing rate storage
        self.agile_rates = []  # List of {start, end, rate} dicts
        self.agile_rates_fetched_at = None
        self._rates_lock = threading.Lock()
        
        # Get region from config
        self.region = getattr(config, 'OCTOPUS_REGION', 'K')
        self.tariff_code = self.AGILE_TARIFF_CODES.get(self.region, 'AGILE-OUTGOING-19-05-13')
        
        # === IOCTGO CONFIGURABLE PARAMETERS ===
        self.BATTERY_CAPACITY_KWH = battery_capacity_kwh if battery_capacity_kwh is not None else config.IOCTGO_BATTERY_CAPACITY_KWH
        self.MAX_CHARGE_CURRENT = config.WALLBOX_MAX_CHARGE_CURRENT
        self.MAX_DISCHARGE_CURRENT = config.WALLBOX_MAX_DISCHARGE_CURRENT
        self.ENABLE_BULK_DISCHARGE = enable_bulk_discharge if enable_bulk_discharge is not None else config.IOCTGO_ENABLE_BULK_DISCHARGE
        
        bulk_discharge_start_time = bulk_discharge_start_time if bulk_discharge_start_time is not None else config.IOCTGO_BULK_DISCHARGE_START_TIME
        self.BULK_DISCHARGE_START_TIME_STR = bulk_discharge_start_time
        self.BULK_DISCHARGE_START_TIME = self._time_to_minutes(bulk_discharge_start_time)
        
        bulk_discharge_end_time = bulk_discharge_end_time if bulk_discharge_end_time is not None else config.IOCTGO_BULK_DISCHARGE_END_TIME
        self.BULK_DISCHARGE_END_TIME_STR = bulk_discharge_end_time
        self.BULK_DISCHARGE_END_TIME = self._time_to_minutes(bulk_discharge_end_time)
        
        self.TARGET_SOC_AT_BULK_DISCHARGE_END = config.IOCTGO_TARGET_SOC_AT_BULK_DISCHARGE_END
        self.MIN_DISCHARGE_CURRENT = config.WALLBOX_MIN_DISCHARGE_CURRENT
        self.GRID_IMPORT_THRESHOLD_HIGH_SOC = config.IOCTGO_GRID_IMPORT_THRESHOLD_HIGH_SOC
        self.GRID_IMPORT_THRESHOLD_LOW_SOC = config.IOCTGO_GRID_IMPORT_THRESHOLD_LOW_SOC
        
        # OCPP parameters
        self.SMART_OCPP_OPERATION = config.IOCTGO_SMART_OCPP_OPERATION
        self._state_lock = threading.Lock()
        self.OCPP_ENABLE_TIME_STR = config.IOCTGO_OCPP_ENABLE_TIME
        self.OCPP_DISABLE_TIME_STR = config.IOCTGO_OCPP_DISABLE_TIME
        self.OCPP_ENABLE_TIME = self._time_to_minutes(config.IOCTGO_OCPP_ENABLE_TIME)
        self.OCPP_DISABLE_TIME = self._time_to_minutes(config.IOCTGO_OCPP_DISABLE_TIME)
        self.OCPP_ENABLE_SOC_THRESHOLD = config.IOCTGO_OCPP_ENABLE_SOC_THRESHOLD
        self.OCPP_DISABLE_SOC_THRESHOLD = config.IOCTGO_OCPP_DISABLE_SOC_THRESHOLD
        
        # Export optimization parameters
        # The configured maximum is the Wallbox nameplate (7.2 kW by default,
        # or the derated value if the unit is derated).  If both are present,
        # use the lower of the configured cap and the model's AC power at the
        # export current, so a 16 A unit plans 3.534 kW and a 32 A unit plans
        # ~7.07 kW without exceeding a user-imposed export cap.
        self.EXPORT_POWER_KW = min(
            config.MAX_EXPORT_POWER_KW,
            self.EFFICIENCY_MODEL.discharge_ac_power_kw(self.EXPORT_REFERENCE_CURRENT),
        )
        # Grid charge -> export round trip (~0.833; efficiency is flat above
        # the measured 16 A point).
        self.BATTERY_ROUND_TRIP_EFFICIENCY = (
            self.EFFICIENCY_MODEL.round_trip_efficiency(
                self.GRID_CHARGE_REFERENCE_CURRENT,
                self.EXPORT_REFERENCE_CURRENT,
            )
        )
        # Pack -> AC efficiency at the export current, ~0.943.  Used to turn
        # an AC export energy into the stored energy (and SoC drop) it costs.
        self.STORED_TO_AC_EFFICIENCY = self.EFFICIENCY_MODEL.discharge_efficiency(
            self.EXPORT_REFERENCE_CURRENT
        )

        # SoC change percentages per slot (configured by user).  A zero
        # export loss or full-charge gain means "calculate from the measured
        # power model"; the non-export loss is always an explicit rate.
        self.EXPORT_SLOT_SOC_LOSS_PERCENT = config.IOCTGO_EXPORT_SLOT_SOC_LOSS_PERCENT
        self.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = config.IOCTGO_NON_EXPORT_SLOT_SOC_LOSS_PERCENT
        self.FULL_CHARGE_SLOT_SOC_GAIN_PERCENT = (
            config.IOCTGO_FULL_CHARGE_SLOT_SOC_GAIN_PERCENT
        )
        
        # Planned export slots (calculated dynamically)
        self._planned_export_slots = []
        # Per-slot stage-1 actions behind the plan: export, conservative or
        # idle.  The 23:30 projection and the explanation table replay this
        # map, so they cannot drift from the planner's own feasibility check.
        self._planned_slot_actions = {}
        # Break-even house-load gate for each planned aggressive slot.  The
        # plan is conditional on the live load reaching this threshold, so
        # the explanation shows it instead of promising a full slot of export.
        self._aggressive_slot_gates = {}
        self._exported_slots = []  # Track slots where export actually occurred

        # Rate fetching state.  _last_fetch_date is read by
        # _have_complete_rates_for_date() before the first successful fetch,
        # so it must exist from construction rather than only after a fetch
        # that stored new data.
        self._rate_fetch_timer = None
        self._last_fetch_attempt = None
        self._last_fetch_date = None
        
        # Cache for export plan calculation
        self._last_plan_soc = -1
        self._last_plan_slot = -1
        self._plan_cache = []
        self._prev_battery_level = -1

        # House-load discharge policy, derived from the projected 23:30 SoC:
        # ``aggressive`` above the house floor, ``conservative`` between the
        # house floor and the absolute minimum, and ``inhibited`` below the
        # absolute minimum.  Updated whenever the export plan is recalculated
        # so the projection and the configured demand levels stay in step.
        self._energy_shortfall = False
        self._discharge_policy = "aggressive"
        self._discharge_inhibited = False
        self._projected_soc_at_2330 = None

        # Dashboard snapshot of the last control decision.  Written by the
        # control loop; the dashboard only reads it.
        self._decision_snapshot = None
        # Last minute whose decision summary was written at INFO, so the log
        # gets one readable line per minute rather than one per control cycle.
        self._last_info_log_minute = None

        # Ledger of stored energy and what it cost, so a discharge can be
        # priced against the import it avoids.  Energy is carried in DC-side
        # kWh: AC input is multiplied by the measured charge efficiency on the
        # way in, and discharge AC output is divided by the measured discharge
        # efficiency on the way out, so the round-trip loss is charged to the
        # energy that actually comes back out.  The ledger survives midnight;
        # only the daily total resets.
        self._bank_dc_kwh = 0.0
        self._bank_cost_gbp = 0.0
        # Whether the charge already in the pack has been valued at the
        # fallback cost.  Without this, the first tiny sample booked after a
        # restart would become the entire cost basis for the pack.
        self._bank_seeded = False

        # Last measured EVSE direction (+1 charging, -1 discharging, 0 idle)
        # and the end of any settling window after a direction change.
        self._ledger_direction = 0
        self._ledger_settle_until = None

        # Estimated saving realised today by discharging stored energy, in
        # GBP, reset at local midnight.
        self._savings_day = None
        self._saved_today_gbp = 0.0
        # The same saving split by local half-hour slot.  It exists so the
        # daily report can attribute the saving to the slot it happened in;
        # sum(_saved_by_slot_gbp) always equals _saved_today_gbp.
        self._saved_by_slot_gbp = [0.0] * 48
        self._savings_last_time = None
        self._savings_last_baseline_w = 0.0
        self._savings_last_evse_w = 0.0
        # Recent (time, saved-today) samples, used to show the rate at which
        # the daily total is moving.  Only the control thread mutates this.
        self._savings_rate_history = deque(maxlen=240)
        self._savings_rate_gbp_per_hour = 0.0

        # Bring back the ledger, today's saving and the export markers from a
        # previous run.  Interval tracking is deliberately not restored: the
        # first sample after a switch or restart establishes a new baseline,
        # so the time spent away cannot be booked as if it had just happened.
        self._load_persisted_state()

        # Ensure OCPP is off
        from evse_controller.drivers.evse.ocpp_manager import ocpp_manager
        ocpp_manager.initialize()
        ocpp_manager.set_state(False)

        # Fetch rates at startup (async). Keep this last so the fetch thread
        # cannot run before the instance attributes it needs are initialised.
        self._fetch_rates_async()

    def _time_to_minutes(self, time_str: str) -> int:
        """Convert time string in HH:MM format to minutes since midnight."""
        hours, minutes = map(int, time_str.split(":"))
        return hours * 60 + minutes

    def absolute_min_soc_percent(self) -> float:
        """Hard lower SoC limit for discharge and export.

        The configured value can raise the 25% physical protection reserve
        (for example to 40% in winter), but it cannot lower it.  A value above
        100 is clamped to 100.
        """
        try:
            configured = float(config.IOCTGO_ABSOLUTE_MIN_SOC_PERCENT)
        except (TypeError, ValueError):
            configured = float(self.BATTERY_PROTECTION_RESERVE_SOC)
        return max(
            float(self.BATTERY_PROTECTION_RESERVE_SOC),
            min(100.0, configured),
        )

    def _house_load_floor_soc(self) -> float:
        """SoC floor for the aggressive/conservative house-load decision.

        Above this floor the pack may cover the house aggressively.  Between
        it and :meth:`absolute_min_soc_percent` it discharges conservatively,
        and below the absolute minimum it stops discharging altogether.  The
        floor is the normal bulk target less the configured house-load
        flexibility, raised if necessary to the absolute minimum and never
        below the physical protection reserve.

        Bulk export planning normally stops at MIN_AGILE_DISCHARGE_SOC,
        raised to the absolute minimum if that is higher.  High-value export
        slots that beat offsetting the house may spend below that target, down
        to the absolute minimum; the flexibility margin itself only applies to
        the house-load choice.
        """
        try:
            margin = float(config.HOUSE_LOAD_MARGIN_PERCENT)
        except (TypeError, ValueError):
            margin = 5.0
        margin = max(0.0, margin)
        return max(
            self.BATTERY_PROTECTION_RESERVE_SOC + 1,
            config.MIN_AGILE_DISCHARGE_SOC - margin,
            self.absolute_min_soc_percent(),
        )

    def self_use_discharge_efficiency(self) -> float:
        """Pack-to-AC efficiency at the ~3 A house-load reference."""
        return self.EFFICIENCY_MODEL.discharge_efficiency(
            self.SELF_USE_DISCHARGE_REFERENCE_CURRENT
        )

    def export_discharge_efficiency(self) -> float:
        """Pack-to-AC efficiency at the maximum export current."""
        return self.EFFICIENCY_MODEL.discharge_efficiency(
            self.EXPORT_REFERENCE_CURRENT
        )

    def export_house_break_even_p(
        self, import_price_p: float | None = None
    ) -> float:
        """Export price at which bulk export beats offsetting house load.

        The charge efficiency is sunk for energy already in the pack, so only
        the two discharge efficiencies matter::

            break_even = eta_house / eta_export * import_price

        At the measured 3 A (73.4%) and 16 A (94.3%) points this is 77.8% of
        the import price.  No profit margin is added: both options are ways of
        using the same stored energy, and the controller has already decided
        to use it rather than leave it in the pack.
        """
        export_eff = self.export_discharge_efficiency()
        if export_eff <= 0:
            return float("inf")
        price = (
            config.IMPORT_HIGH
            if import_price_p is None
            else import_price_p
        )
        return self.self_use_discharge_efficiency() / export_eff * float(price)

    def _export_priority_slots(
        self, current_slot: int | None = None
    ) -> set[int]:
        """Future slots where bulk export beats offsetting house load.

        A slot qualifies when its export price is at or above
        :meth:`export_house_break_even_p` for the import price in force, and
        it is not already marked as free-import, grid-support or EV-away.
        Off-peak slots are excluded because the car is charging then.

        These slots get first claim on the pack and may spend the house
        reserve down to the absolute minimum, because their export value beats
        offsetting the house load.  Ordinary profitable slots still stop at
        MIN_AGILE_DISCHARGE_SOC.
        """
        if current_slot is None:
            now = self.get_current_datetime()
            current_slot = now.hour * 2 + (1 if now.minute >= 30 else 0)
        marked = (
            self._free_import_slots_today()
            | self._grid_support_slots_today()
            | self._away_slots_today()
        )
        qualifying = set()
        for rate_data in self._today_rates():
            slot = rate_data['start'].hour * 2 + (
                1 if rate_data['start'].minute == 30 else 0
            )
            if slot < current_slot or slot >= 47 or slot in marked:
                continue
            if self.is_off_peak(slot * 30):
                continue
            break_even = self.export_house_break_even_p(
                self._current_import_rate_p(slot * 30)
            )
            if rate_data['rate'] >= break_even:
                qualifying.add(slot)
        return qualifying

    def _pending_priority_export_slots(
        self, current_slot: int
    ) -> list[int]:
        """Planned high-value export slots that are still ahead.

        These are the slots the planner has deliberately kept energy for, so
        the house load must not spend that energy first.  A slot is included
        only when it is strictly after the current one: during the export slot
        itself ``get_control_state()`` already commands the discharge.
        """
        priority = self._export_priority_slots(current_slot)
        return sorted(
            slot
            for slot in self._planned_export_slots
            if slot > current_slot and slot in priority
        )

    def _reserve_slots_for_priority_export(
        self, current_slot: int, day_minute: int
    ) -> list[int]:
        """Planned priority slots that beat covering the house right now.

        Pausing discharge is only justified when the best planned export price
        clears the current house-offset break-even.  If it does not, covering
        the house now is the better use of the stored energy and conservative
        discharge remains correct.
        """
        pending = self._pending_priority_export_slots(current_slot)
        if not pending:
            return []
        best_rate_p = max(
            self._current_export_rate_p(slot) for slot in pending
        )
        break_even_p = self.export_house_break_even_p(
            self._current_import_rate_p(day_minute)
        )
        return pending if best_rate_p >= break_even_p else []

    def _export_slot_soc_loss_percent(self) -> float:
        """SoC lost by one half-hour of bulk export.

        A configured value wins; zero means "derive it from the measured
        export power, efficiency and usable capacity".
        """
        if self.EXPORT_SLOT_SOC_LOSS_PERCENT > 0:
            return self.EXPORT_SLOT_SOC_LOSS_PERCENT
        return (
            self.EXPORT_POWER_KW / self.STORED_TO_AC_EFFICIENCY
            * 0.5 / self.SOC_CAPACITY_KWH
        ) * 100

    def _aggressive_slot_soc_loss_percent(self) -> float:
        """SoC lost by one half-hour of aggressive load-following discharge.

        Conservative coverage models the measured 3 A house-drain point.
        Aggressive mode adds the positive discharge bias, so it covers the
        house and exports the small surplus.  Scale the measured conservative
        loss by the extra current rather than inventing a separate figure.
        """
        base = max(0.0, float(self.NON_EXPORT_SLOT_SOC_LOSS_PERCENT))
        bias_a = max(0.0, float(self.AGGRESSIVE_DISCHARGE_BIAS_A))
        reference_a = max(1.0, float(self.MIN_DISCHARGE_CURRENT))
        return base * (1.0 + bias_a / reference_a)

    def _aggressive_extra_soc_loss_percent(self) -> float:
        """Extra SoC spent by aggressive discharge over conservative coverage."""
        return max(
            0.0,
            self._aggressive_slot_soc_loss_percent()
            - max(0.0, float(self.NON_EXPORT_SLOT_SOC_LOSS_PERCENT)),
        )

    def _aggressive_export_kwh(self) -> float:
        """AC energy exported by one aggressive slot, from its extra SoC.

        The extra SoC is the modelled pack energy; multiplying by the
        measured pack-to-AC efficiency gives the energy sold.  This keeps the
        slot's revenue and its SoC budget on the same model.
        """
        return (
            self._aggressive_extra_soc_loss_percent()
            / 100.0
            * self.SOC_CAPACITY_KWH
            * self.STORED_TO_AC_EFFICIENCY
        )

    def _full_charge_slot_soc_gain_percent(self) -> float:
        """SoC gained by one half-hour of sustained full-rate charging.

        A free-import session charges at the Wallbox maximum, so the default
        uses the measured AC charge power and AC→pack efficiency.  A
        configured value overrides the model when the user has measured the
        real rise per slot.
        """
        if self.FULL_CHARGE_SLOT_SOC_GAIN_PERCENT > 0:
            return self.FULL_CHARGE_SLOT_SOC_GAIN_PERCENT
        charge_power_kw = self.EFFICIENCY_MODEL.charge_ac_power_kw(
            self.GRID_CHARGE_REFERENCE_CURRENT
        )
        stored_kw = charge_power_kw * self.EFFICIENCY_MODEL.charge_efficiency(
            self.GRID_CHARGE_REFERENCE_CURRENT
        )
        return stored_kw * 0.5 / self.SOC_CAPACITY_KWH * 100

    def _slot_soc_delta(
        self,
        slot: int,
        projected_soc: float,
        *,
        free_import_slots: set[int],
        grid_support_slots: set[int],
        away_slots: set[int],
        planned_export_slots: set[int],
        charge_cap: float,
        full_charge_gain: float,
        export_loss: float,
        non_export_loss: float,
        slot_actions: dict[int, str] | None = None,
    ) -> tuple[float, str]:
        """Return the SoC change and the reason for one planning slot.

        This is the single per-slot model behind both the 23:30 projection and
        the explanation page's half-hour table, so the two cannot disagree.
        The returned kind is one of ``away``, ``free_import``,
        ``grid_support``, ``planned_export``, ``load_follow`` or ``idle``.

        ``slot_actions`` maps a slot to ``export``, ``conservative`` or
        ``idle``.  When it is supplied, an ordinary slot not named in the map
        is idle: the stage-1 planner starts from a no-discretionary-load
        baseline.  When it is omitted, ordinary slots keep the historical
        load-follow loss and ``export_slots`` alone selects exports.
        """
        if slot in away_slots:
            return 0.0, "away"
        if slot in free_import_slots:
            if projected_soc < charge_cap:
                return (
                    min(charge_cap, projected_soc + full_charge_gain)
                    - projected_soc,
                    "free_import",
                )
            return 0.0, "free_import"
        if slot in grid_support_slots:
            return -export_loss, "grid_support"
        if slot in planned_export_slots:
            return -export_loss, "planned_export"
        if slot_actions is not None:
            action = slot_actions.get(slot)
            if action == "export":
                return -export_loss, "planned_export"
            if action == "aggressive":
                return -self._aggressive_slot_soc_loss_percent(), "aggressive"
            if action == "conservative":
                return -non_export_loss, "load_follow"
            return 0.0, "idle"
        return -non_export_loss, "load_follow"

    def _soc_projection_path(
        self,
        current_soc_percent: float,
        current_slot_idx: int,
        *,
        export_slots: set[int] | None = None,
        slot_actions: dict[int, str] | None = None,
    ) -> list[tuple[int, float, str]]:
        """Return ``(slot, SoC after the slot, kind)`` for the rest of today.

        The path starts at ``current_slot_idx`` and stops before slot 47
        (23:30).  It is shared by the 23:30 projection, the running-minimum
        floor check and the explanation table, so all three use exactly the
        same per-slot model.
        """
        free_import_slots = self._free_import_slots_today()
        grid_support_slots = self._grid_support_slots_today()
        away_slots = self._away_slots_today()
        planned_export_slots = set(export_slots or ())
        non_export_loss = self.NON_EXPORT_SLOT_SOC_LOSS_PERCENT
        export_loss = self._export_slot_soc_loss_percent()
        full_charge_gain = self._full_charge_slot_soc_gain_percent()
        charge_cap = self.charge_cap_percent()

        projected = current_soc_percent
        path: list[tuple[int, float, str]] = []
        for slot in range(current_slot_idx, 47):
            delta, kind = self._slot_soc_delta(
                slot,
                projected,
                free_import_slots=free_import_slots,
                grid_support_slots=grid_support_slots,
                away_slots=away_slots,
                planned_export_slots=planned_export_slots,
                charge_cap=charge_cap,
                full_charge_gain=full_charge_gain,
                export_loss=export_loss,
                non_export_loss=non_export_loss,
                slot_actions=slot_actions,
            )
            projected += delta
            path.append((slot, projected, kind))
        return path

    def _project_soc_at_2330(
        self,
        current_soc_percent: float,
        current_slot_idx: int,
        *,
        export_slots: set[int] | None = None,
        slot_actions: dict[int, str] | None = None,
    ) -> float:
        """Project the SoC at 23:30, including session, away and export slots.

        Every ordinary slot keeps the load-follow loss unless ``slot_actions``
        supplies an explicit action map.  In that case an unlisted ordinary
        slot is idle, which is the stage-1 planning baseline.  A free-import
        slot charges at full rate, a grid-support slot exports at full rate,
        and an EV-away slot is skipped entirely.  The free-import gain stops
        at the effective charge cap, matching what the controller will do.
        """
        path = self._soc_projection_path(
            current_soc_percent,
            current_slot_idx,
            export_slots=export_slots,
            slot_actions=slot_actions,
        )
        return path[-1][1] if path else current_soc_percent

    def _minimum_projected_soc(
        self,
        current_soc_percent: float,
        current_slot_idx: int,
        *,
        export_slots: set[int] | None = None,
        slot_actions: dict[int, str] | None = None,
    ) -> float:
        """Lowest SoC reached between now and 23:30, including the start.

        The export floor applies to the running minimum, not only to the final
        projection, so a later free-import charge cannot hide an earlier dip
        below the floor.
        """
        path = self._soc_projection_path(
            current_soc_percent,
            current_slot_idx,
            export_slots=export_slots,
            slot_actions=slot_actions,
        )
        minimum = current_soc_percent
        for _, soc, _ in path:
            minimum = min(minimum, soc)
        return minimum

    def _apply_discharge_policy(self, projected_soc: float) -> str:
        """Choose the house-load discharge policy from the 23:30 projection.

        Three bands, outermost first:

        * below ``absolute_min_soc_percent``: fully inhibited, no discharge;
        * below the house floor: conservative, cover most of the house load;
        * otherwise: aggressive load-following.

        Returns the policy name and updates the flags read by
        ``get_control_state()`` and ``set_home_demand_levels()``.
        """
        floor_soc = self._house_load_floor_soc()
        absolute_floor = self.absolute_min_soc_percent()
        # The planner accepts a plan whose running minimum lands exactly on
        # the floor.  Floating-point arithmetic can leave it a few ulps below
        # (for example 39.99999999999999 for 40%), so use the same tolerance
        # here; otherwise a correctly planned day is reported as inhibited.
        if projected_soc < absolute_floor - 1e-9:
            policy = "inhibited"
        elif projected_soc < floor_soc:
            policy = "conservative"
        else:
            policy = "aggressive"

        self._projected_soc_at_2330 = projected_soc
        self._discharge_policy = policy
        self._discharge_inhibited = policy == "inhibited"
        self._energy_shortfall = policy != "aggressive"

        debug(
            f"IOCTGO_AGILEOUT: Projected SoC at 23:30={projected_soc:.1f}% "
            f"(target {config.MIN_AGILE_DISCHARGE_SOC}%, house floor "
            f"{floor_soc:.1f}%, absolute floor {absolute_floor:.1f}%), "
            f"discharge policy={policy}"
        )
        return policy

    def _update_energy_shortfall(self, current_soc_percent: float) -> bool:
        """Update and return whether the day is projected to end in shortfall.

        The projection uses the same session-aware helper as
        ``calculate_export_plan()``, plus the currently planned export slots:
        ordinary slots lose NON_EXPORT_SLOT_SOC_LOSS_PERCENT, free-import slots
        gain the full-charge rate, grid-support and planned export slots lose
        the export rate, and planned EV-away slots are skipped.

        The returned flag is True for both conservative and inhibited modes.
        Use ``_discharge_inhibited`` to distinguish the hard floor.

        Args:
            current_soc_percent: Current battery state of charge (%), or -1
                if unknown.

        Returns:
            True if the projected SoC at 23:30 is below the house-load floor.
        """
        if current_soc_percent < 0:
            self._energy_shortfall = False
            self._discharge_policy = "aggressive"
            self._discharge_inhibited = False
            self._projected_soc_at_2330 = None
            return False

        now = self.get_current_datetime()
        current_slot_idx = now.hour * 2 + (1 if now.minute >= 30 else 0)
        # Replay the committed action map when there is one, so the projection
        # includes the conservative and idle slots the planner chose.  Without
        # a plan, keep the historical load-follow baseline.
        projection_kwargs = (
            {"slot_actions": dict(self._planned_slot_actions)}
            if self._planned_slot_actions
            else {"export_slots": set(self._planned_export_slots)}
        )
        projected_soc_at_2330 = self._project_soc_at_2330(
            current_soc_percent,
            current_slot_idx,
            **projection_kwargs,
        )
        self._apply_discharge_policy(projected_soc_at_2330)
        return self._energy_shortfall

    def calculate_export_plan(self, current_soc_percent: float) -> list:
        """Cherry-pick exports, then spend what is left on house load.

        The planner no longer treats the ~3 A load-follow drain as a mandatory
        baseline in every ordinary slot.  Instead it:

        1. Projects the day with **no discretionary house load** (ordinary
           slots idle, supplier sessions still applied) and takes the running
           minimum as the available budget.
        2. Spends that budget on the highest-priced export-priority slots
           first, at the full export cost per slot, down to the absolute
           minimum.  A slot is accepted only when the running minimum over the
           whole horizon stays at or above the floor.
        3. Offers ordinary profitable slots only the headroom above the bulk
           target, again highest price first.
        4. Uses whatever budget remains for conservative house coverage in
           the remaining slots, earliest first.  Slots that no longer fit are
           left idle so the house imports instead of spending reserved energy.

        Grid-support slots already export at maximum regardless of the quoted
        price.  Their drain is part of the session simulation and they are
        excluded from the candidate list, so the same slot is never charged
        for export twice.  Planned EV-away and free-import slots are excluded
        for the same reason.

        Args:
            current_soc_percent: Current battery state of charge (%)

        Returns:
            List of slot indices (0-47) planned for export
        """
        if current_soc_percent < 0:
            self._planned_slot_actions = {}
            self._aggressive_slot_gates = {}
            self._update_energy_shortfall(current_soc_percent)
            return []

        bulk_target_soc = max(
            config.MIN_AGILE_DISCHARGE_SOC,
            self.absolute_min_soc_percent(),
        )
        absolute_floor_soc = self.absolute_min_soc_percent()

        export_slot_soc_loss = self._export_slot_soc_loss_percent()
        non_export_slot_soc_loss = self.NON_EXPORT_SLOT_SOC_LOSS_PERCENT

        debug(
            f"IOCTGO_AGILEOUT: Export slot loss={export_slot_soc_loss:.2f}%, "
            f"conservative slot loss={non_export_slot_soc_loss:.2f}%"
        )
        info(
            f"IOCTGO_AGILEOUT: Planning export - SoC {current_soc_percent}%, "
            f"bulk target {bulk_target_soc}%, absolute minimum "
            f"{absolute_floor_soc}%, export loss "
            f"{export_slot_soc_loss:.1f}%/slot, conservative loss "
            f"{non_export_slot_soc_loss:.1f}%/slot"
        )

        # Get current time to determine slot range
        now = self.get_current_datetime()
        current_slot_idx = now.hour * 2 + (1 if now.minute >= 30 else 0)

        # Slot 47 starts at 23:30, so slots remaining = 47 - current_slot
        # e.g. at 21:33 (slot 43): 47 - 43 = 4 slots (43, 44, 45, 46)
        if 47 - current_slot_idx <= 0:
            debug("IOCTGO_AGILEOUT: No slots remaining today")
            self._planned_slot_actions = {}
            self._aggressive_slot_gates = {}
            self._apply_discharge_policy(current_soc_percent)
            return []

        # Phase 1: the no-discretionary-load baseline.  Ordinary slots are
        # idle, so the only changes come from supplier sessions.  The running
        # minimum is the real budget: a later free-import charge must not
        # sanction an earlier dip below the floor.
        baseline_path = self._soc_projection_path(
            current_soc_percent,
            current_slot_idx,
            slot_actions={},
        )
        baseline_projection = (
            baseline_path[-1][1] if baseline_path else current_soc_percent
        )
        baseline_minimum = min(
            [current_soc_percent] + [soc for _, soc, _ in baseline_path]
        )
        priority_headroom_soc = baseline_minimum - absolute_floor_soc
        target_headroom_soc = baseline_minimum - bulk_target_soc
        debug(
            "IOCTGO_AGILEOUT: Session-aware baseline 23:30="
            f"{baseline_projection:.1f}%, running minimum="
            f"{baseline_minimum:.1f}%, headroom above bulk target="
            f"{target_headroom_soc:.1f}%, above absolute minimum="
            f"{priority_headroom_soc:.1f}%"
        )

        actions: dict[int, str] = {}
        planned_slots: list[int] = []

        def feasible(floor_soc: float) -> bool:
            """True when the proposed action map never crosses *floor_soc*."""
            return (
                self._minimum_projected_soc(
                    current_soc_percent,
                    current_slot_idx,
                    slot_actions=actions,
                )
                >= floor_soc - 1e-9
            )

        # Calculate minimum profitable rate.  With no rate data this is just a
        # threshold that no (0p) slot can meet, so the plan is empty.
        min_export_rate_p = self.export_min_rate_p()
        info(f"IOCTGO_AGILEOUT: Min export rate {min_export_rate_p:.1f}p/kWh")

        # Candidate slots: future, not owned by a supplier session, not
        # off-peak charging, and at or above the export minimum.
        free_import_slots = self._free_import_slots_today()
        grid_support_slots = self._grid_support_slots_today()
        away_slots = self._away_slots_today()
        slot_rates: list[tuple[int, float]] = []
        for rate_data in self._today_rates():
            hour = rate_data['start'].hour
            slot_idx = hour * 2 + (1 if rate_data['start'].minute == 30 else 0)

            if slot_idx in away_slots:
                debug(f"IOCTGO_AGILEOUT: Slot {slot_idx} skipped - EV planned away")
                continue
            if slot_idx in free_import_slots:
                debug(f"IOCTGO_AGILEOUT: Slot {slot_idx} reserved for a free-import session")
                continue
            if slot_idx in grid_support_slots:
                debug(f"IOCTGO_AGILEOUT: Slot {slot_idx} already exports for grid support")
                continue
            if slot_idx < current_slot_idx:
                continue
            if slot_idx >= 47:
                continue
            if self.is_off_peak(slot_idx * 30):
                debug(f"IOCTGO_AGILEOUT: Slot {slot_idx} skipped - off-peak charging")
                continue

            rate_p = rate_data['rate']  # Already in p/kWh
            if rate_p >= min_export_rate_p:
                slot_rates.append((slot_idx, rate_p))
                debug(
                    f"IOCTGO_AGILEOUT: Slot {slot_idx} "
                    f"({hour:02d}:{rate_data['start'].minute:02d}) @ "
                    f"{rate_p:.1f}p (profitable)"
                )

        debug(f"IOCTGO_AGILEOUT: Found {len(slot_rates)} profitable slots")

        priority_slots = self._export_priority_slots(current_slot_idx)
        priority_rates = sorted(
            (item for item in slot_rates if item[0] in priority_slots),
            key=lambda item: item[1],
            reverse=True,
        )
        other_rates = sorted(
            (item for item in slot_rates if item[0] not in priority_slots),
            key=lambda item: item[1],
            reverse=True,
        )
        if priority_rates:
            info(
                f"IOCTGO_AGILEOUT: {len(priority_rates)} export-priority "
                "slot(s) beat the house-offset break-even"
            )

        # Phase 2a: priority exports.  They get first claim on the pack and
        # may spend it down to the hard floor.  All exports carry the same
        # full per-slot cost, so price order is also value order.
        for slot_idx, rate in priority_rates:
            actions[slot_idx] = "export"
            if feasible(absolute_floor_soc):
                planned_slots.append(slot_idx)
                debug(
                    f"IOCTGO_AGILEOUT: Selected priority slot {slot_idx} @ "
                    f"{rate:.1f}p, running minimum "
                    f"{self._minimum_projected_soc(current_soc_percent, current_slot_idx, slot_actions=actions):.1f}%"
                )
            else:
                del actions[slot_idx]
                debug(
                    f"IOCTGO_AGILEOUT: Skipping priority slot {slot_idx} @ "
                    f"{rate:.1f}p - it would take the running minimum below "
                    f"the {absolute_floor_soc:.0f}% absolute minimum"
                )

        # Phase 2b: remaining priority slots can still take the small extra
        # export that aggressive load-following produces.  The house is
        # covered in both cases, so the marginal value is the slot's export
        # price; this lets a high-value slot contribute even when a full
        # export slot would not fit the remaining budget.  Ordinary slots do
        # not get this treatment: below the house-offset break-even, spending
        # the extra SoC on another conservative slot is worth more.
        aggressive_count = 0
        aggressive_loss = self._aggressive_slot_soc_loss_percent()
        aggressive_gates: dict[int, dict] = {}
        for slot_idx, rate in priority_rates:
            if slot_idx in actions:
                continue
            gate = self._aggressive_slot_gate(
                slot_idx,
                planned_slots={
                    slot
                    for slot, action in actions.items()
                    if action in ("export", "aggressive")
                }
                | set(planned_slots),
            )
            if (
                gate["activation_w"]
                >= self.NEVER_DISCHARGE_ACTIVATION_W
            ):
                debug(
                    f"IOCTGO_AGILEOUT: Skipping aggressive slot {slot_idx} @ "
                    f"{rate:.1f}p - the forced output cannot repay stored "
                    f"energy (hurdle {gate['hurdle_p']:.1f}p)"
                )
                continue
            actions[slot_idx] = "aggressive"
            if feasible(absolute_floor_soc):
                aggressive_count += 1
                aggressive_gates[slot_idx] = gate
                debug(
                    f"IOCTGO_AGILEOUT: Selected aggressive slot {slot_idx} @ "
                    f"{rate:.1f}p, cost {aggressive_loss:.3f}%, starts at "
                    f"{gate['activation_w']:.0f} W house import"
                )
            else:
                del actions[slot_idx]
                debug(
                    f"IOCTGO_AGILEOUT: Skipping aggressive slot {slot_idx} @ "
                    f"{rate:.1f}p - it would take the running minimum below "
                    f"the {absolute_floor_soc:.0f}% absolute minimum"
                )

        # Phase 2c: ordinary exports may only use the headroom above the bulk
        # target.  Their lower price makes them the next best use of energy.
        for slot_idx, rate in other_rates:
            actions[slot_idx] = "export"
            if feasible(bulk_target_soc):
                planned_slots.append(slot_idx)
                debug(
                    f"IOCTGO_AGILEOUT: Selected ordinary slot {slot_idx} @ "
                    f"{rate:.1f}p above the {bulk_target_soc:.0f}% bulk target"
                )
            else:
                del actions[slot_idx]
                debug(
                    f"IOCTGO_AGILEOUT: Skipping ordinary slot {slot_idx} @ "
                    f"{rate:.1f}p - no headroom above the "
                    f"{bulk_target_soc:.0f}% bulk target"
                )

        # Phase 2d: spend the remaining budget on conservative house coverage,
        # earliest first.  Slots that no longer fit stay idle and the house
        # imports instead of eating energy reserved for the exports.
        conservative_count = 0
        for slot_idx in range(current_slot_idx, 47):
            if slot_idx in free_import_slots or slot_idx in grid_support_slots:
                continue
            if slot_idx in away_slots or slot_idx in actions:
                continue
            if self.is_off_peak(slot_idx * 30):
                continue
            actions[slot_idx] = "conservative"
            if feasible(absolute_floor_soc):
                conservative_count += 1
            else:
                actions[slot_idx] = "idle"

        planned_slots.sort()
        self._planned_slot_actions = dict(actions)
        self._aggressive_slot_gates = dict(aggressive_gates)

        debug(
            f"IOCTGO_AGILEOUT: Final plan: {len(planned_slots)} export "
            f"slot(s), {aggressive_count} aggressive slot(s), "
            f"{conservative_count} conservative slot(s): {planned_slots}"
        )
        info(
            f"IOCTGO_AGILEOUT: Export plan - {len(planned_slots)} export "
            f"slot(s), {aggressive_count} aggressive house+export slot(s), "
            f"{conservative_count} conservative house slot(s)"
        )

        # Refresh the three-band house-load policy with the full action map.
        # The projection now includes both the export drain and the
        # conservative coverage, so the house mode cannot be over-optimistic.
        final_projection = self._project_soc_at_2330(
            current_soc_percent,
            current_slot_idx,
            slot_actions=actions,
        )
        self._apply_discharge_policy(final_projection)

        return planned_slots

    def _today_rates(self) -> list:
        """Rate entries for today, or an empty list if none are available.

        An empty list deliberately means every export price is **0p** rather
        than a special "no data" state: the normal tier logic then stores
        instead of exporting, and the export planner finds no profitable
        slots.  Stale entries from a previous day are excluded by date, so a
        failed fetch cannot silently reuse yesterday's prices.
        """
        today = self.get_current_datetime().date()
        return [rate for rate in self.agile_rates if rate['start'].date() == today]

    def _current_export_rate_p(self, current_slot: int) -> float:
        """Export price for *current_slot*, or 0p if it is not known."""
        for rate_data in self._today_rates():
            hour = rate_data['start'].hour
            slot_idx = hour * 2 + (1 if rate_data['start'].minute == 30 else 0)
            if slot_idx == current_slot:
                return rate_data['rate']
        return 0.0

    def export_min_rate_p(self) -> float:
        """Minimum export price covering cheap import and the profit margin.

        Cost per kWh delivered to the grid is ``IMPORT_LOW / round_trip``
        (grid charge at max, export at max) plus the minimum profit margin.
        """
        return (
            config.IMPORT_LOW / self.BATTERY_ROUND_TRIP_EFFICIENCY
            + self.MIN_PROFIT_MARGIN_P_PER_KWH
        )

    def _get_best_unused_export_rate(self, current_slot: int) -> float:
        """Find the best export rate among slots not selected for export.

        This is used to determine whether bidirectional load-following is more
        beneficial than exporting at the current slot's rate.

        Args:
            current_slot: Current slot index (0-47)

        Returns:
            Best export rate (p/kWh) from unused slots, or 0 if none available
        """
        _, best_rate = self._get_best_unused_export_slot_rate(current_slot)
        return best_rate

    def _get_best_unused_export_slot_rate(
        self, current_slot: int
    ) -> tuple[Optional[int], float]:
        """Best unused export slot and its rate, or ``(None, 0.0)``.

        The slot is the *first* slot carrying the best rate, so the dashboard
        can say which half-hour the decision is aiming at.
        """
        # Build list of (slot_index, rate) for all slots that:
        # - Are in the future (or current slot)
        # - Are NOT in the planned export slots
        # - Are NOT reserved for a free-import session
        # - Are NOT planned EV-away slots
        # - Are before slot 47 (23:30-00:00, not available for export)
        unused_slot_rates = []
        free_import_slots = self._free_import_slots_today()
        away_slots = self._away_slots_today()

        for rate_data in self._today_rates():
            hour = rate_data['start'].hour
            slot_idx = hour * 2 + (1 if rate_data['start'].minute == 30 else 0)
            
            # Skip past slots
            if slot_idx < current_slot:
                continue

            # The car is not planned to be available for export.
            if slot_idx in away_slots:
                continue

            # A free-import slot cannot also be an export target.
            if slot_idx in free_import_slots:
                continue
            
            # Skip slot 47 (23:30-00:00)
            if slot_idx >= 47:
                continue
            
            # Skip slots we're already exporting in
            if slot_idx in self._planned_export_slots:
                continue
            
            rate_p = rate_data['rate']
            unused_slot_rates.append((slot_idx, rate_p))
        
        if not unused_slot_rates:
            return None, 0.0
        
        # Return the highest rate among unused slots (first slot on a tie).
        best_rate = max(r[1] for r in unused_slot_rates)
        best_slot = next(slot for slot, rate in unused_slot_rates if rate == best_rate)
        debug(f"IOCTGO_AGILEOUT: Best unused export rate: {best_rate:.1f}p/kWh")
        return best_slot, best_rate

    def _best_planned_export_slot_rate(
        self, current_slot: int
    ) -> tuple[Optional[int], float]:
        """Highest-rate planned export slot still to come, or ``(None, 0.0)``.

        A planned slot's revenue is already committed to the energy in the
        pack: the plan spends the pack's spare SoC on it.  A marginal kWh
        stored now is therefore sold in the best *unused* slot, which is what
        the storage decision compares against.

        This helper exists only to explain that.  Without it a no-store
        decision can look wrong: the best rate of the day is visible in the
        plan, yet the surplus is exported now, and the reason said "no future
        slot beats the current one".  The truthful reason is that the good
        slot is already taken.
        """
        planned = [
            (slot, self._current_export_rate_p(slot))
            for slot in self._planned_export_slots
            if current_slot <= slot < 47
        ]
        if not planned:
            return None, 0.0
        return max(planned, key=lambda item: item[1])

    def minimum_round_trip_saving_p(
        self, charge_price_p: float, round_trip_efficiency: float
    ) -> float:
        """Efficiency-adjusted break-even spread over the input price.

        This is not the displayed ``min gain``: the configured profit margin
        is the spread the policy requires, and it is what the dashboard and
        log report.  This helper answers a different question - how much the
        input price itself must rise to earn that margin *after* the
        round-trip loss.  A round trip turns 1 kWh of input into
        ``round_trip_efficiency`` kWh of output, so the break-even input price
        is ``charge_price / efficiency + margin`` and the required spread is::

            charge_price / efficiency + margin - charge_price

        For grid import at ``IMPORT_LOW`` and the ~0.833 grid round trip this
        is ~2.3p/kWh on the input price.  For free solar the input price is
        zero, so it collapses to the margin itself (~0.3p/kWh).  The tooltip
        uses these figures to show the loss component alongside the policy
        margin; control decisions apply the margin directly to the future
        price and never call this helper.
        """
        if round_trip_efficiency <= 0:
            return 0.0
        return (
            charge_price_p / round_trip_efficiency
            + self.MIN_PROFIT_MARGIN_P_PER_KWH
            - charge_price_p
        )

    def minimum_round_trip_saving_solar_p(self) -> float:
        """Minimum saving for free solar -> export, i.e. just the margin."""
        return self.minimum_round_trip_saving_p(
            0.0, self.solar_export_round_trip()
        )

    def minimum_round_trip_saving_grid_p(self) -> float:
        """Minimum saving for cheap grid import -> export."""
        return self.minimum_round_trip_saving_p(
            config.IMPORT_LOW, self.BATTERY_ROUND_TRIP_EFFICIENCY
        )

    def charge_cap_percent(self) -> float:
        """Effective charge ceiling: the configured cap, hard-limited to 97%.

        Charging to 100% is hard on the pack and leaves no room for
        regenerative energy.  The config can lower the ceiling (for example
        ``max_charge_percent: 90``), but it cannot raise it above 97%.
        """
        return min(config.MAX_CHARGE_PERCENT, 97)

    def solar_export_round_trip(self, harvest_current_a: float | None = None) -> float:
        """Round trip for solar harvest -> later export.

        The harvest current is the current the load-following loop will use,
        which depends on the solar surplus.  It is not fixed at 3 A: a 6 A
        harvest is 0.778 x 0.943 = 0.734, versus 0.547 at 3 A.
        """
        current = (
            self.SOLAR_CHARGE_REFERENCE_CURRENT
            if harvest_current_a is None
            else harvest_current_a
        )
        return self.EFFICIENCY_MODEL.round_trip_efficiency(
            current, self.EXPORT_REFERENCE_CURRENT
        )

    def self_use_value_threshold_p(
        self,
        harvest_current_a: float | None = None,
    ) -> float:
        """Value of solar stored for self-consumption, in p/kWh.

        Charge at the harvest current and discharge at ~3 A to cover the house
        base load, then compare the avoided peak import with the export rate
        forgone now.  At a 3 A harvest this is (30p - 0.3p margin) x 0.580 x
        0.734 = 12.6p; at 6 A it is (30p - 0.3p) x 0.778 x 0.734 = 17.0p.
        Using the export current here (0.943 instead of 0.734) would overstate
        the value by ~28%.

        The minimum profit margin is subtracted from the avoided import price
        before the round trip, because the margin is required per kWh
        delivered to the house.
        """
        current = (
            self.SOLAR_CHARGE_REFERENCE_CURRENT
            if harvest_current_a is None
            else harvest_current_a
        )
        round_trip = self.EFFICIENCY_MODEL.round_trip_efficiency(
            current, self.SELF_USE_DISCHARGE_REFERENCE_CURRENT
        )
        return (
            config.IMPORT_HIGH - self.MIN_PROFIT_MARGIN_P_PER_KWH
        ) * round_trip

    def _minimum_harvest_charge_current_a(
        self,
        export_rate_p: float,
    ) -> Optional[float]:
        """Smallest measured charge current worth taking instead of exporting.

        The self-use branch is not filling the pack for a later sale; it is
        deciding whether to keep surplus solar for the house or sell it now at
        ``export_rate_p``.  Keeping it is worth it when the value delivered to
        the house, ``(IMPORT_HIGH - margin) × charge_eff(c) × discharge_eff``,
        beats the export price forgone.  Efficiency rises with current, so that
        condition is monotonic in ``c``: below some point the round-trip loss
        is too large, above it the solar is better kept.

        Returns the first measured point that clears the hurdle, or ``None``
        when even the best measured current does not.  ``None`` means the
        surplus should be exported now; the caller must then close the charge
        range rather than inherit a stale one.

        This is a *control* floor.  ``_estimate_solar_harvest_current()`` keeps
        the 3 A measured point because that is the efficiency at which a small
        charge would run, not the minimum worth bothering with.
        """
        for point in self.EFFICIENCY_MODEL.points:
            if (
                self.self_use_value_threshold_p(point.current_a)
                > export_rate_p
            ):
                return point.current_a
        return None

    def _best_self_use_value_p(self) -> tuple[float, float]:
        """Best self-use value over the measured points and its current.

        Used for the dashboard explanation when no charge current clears the
        current export price.
        """
        best = max(
            self.EFFICIENCY_MODEL.points,
            key=lambda point: self.self_use_value_threshold_p(point.current_a),
        )
        return (
            self.self_use_value_threshold_p(best.current_a),
            best.current_a,
        )

    def _minimum_store_harvest_current_a(
        self,
        future_rate_p: float,
        export_rate_p: float,
    ) -> Optional[float]:
        """Smallest measured current whose later sale beats exporting now.

        This mirrors the Tier 3 store test, so crossing it makes the
        controller switch from self-use to storing.  The discharge is costed
        at the export reference current, because the stored energy is sold to
        the grid rather than used at the house base load.
        """
        for point in self.EFFICIENCY_MODEL.points:
            adjusted = (
                future_rate_p - self.MIN_PROFIT_MARGIN_P_PER_KWH
            ) * self.solar_export_round_trip(point.current_a)
            if adjusted > export_rate_p:
                return point.current_a
        return None

    def _effective_charge_floor_a(
        self,
        export_rate_p: float,
        future_rate_p: Optional[float],
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Charge onset for the no-store branch, and its two components.

        Charging becomes worthwhile when *either* keeping the solar for the
        house (self-use) or storing it for a later sale (Tier 3) beats the
        export price now.  The onset is therefore the lower of the two
        break-even currents.  Returning the components lets the dashboard
        explain which one is responsible.

        A store break-even below the current harvest cannot occur here: Tier 3
        would already have chosen to store, so this branch would not run.
        """
        self_use_floor = self._minimum_harvest_charge_current_a(export_rate_p)
        store_floor = (
            self._minimum_store_harvest_current_a(
                future_rate_p, export_rate_p
            )
            if future_rate_p is not None and future_rate_p > 0
            else None
        )
        candidates = [
            floor for floor in (self_use_floor, store_floor)
            if floor is not None
        ]
        return (
            min(candidates) if candidates else None,
            self_use_floor,
            store_floor,
        )

    def _solar_charge_floor_for_slot(
        self, current_slot: int
    ) -> tuple[float, float]:
        """Charge onset and activation for conservative/idle solar harvesting.

        Conservative, reserve and absolute-floor states stop discharge but
        still have to decide whether surplus solar is worth keeping.  They use
        the same break-even test as the normal self-use branch: the solar must
        be worth more kept for the house or a later sale than exported now.
        A zero floor means no measured current clears the live export price,
        so the charge range is closed and the surplus is sold instead.

        This replaces the old unconditional 3 A harvest, which could keep a
        trickle of solar at a poor round-trip efficiency while a high export
        price was available now.
        """
        export_rate_p = self._current_export_rate_p(current_slot)
        _, future_rate_p = self._get_best_unused_export_slot_rate(current_slot)
        floor_a, _, _ = self._effective_charge_floor_a(
            export_rate_p, future_rate_p
        )
        if floor_a is None:
            return 0.0, 0.0
        return float(floor_a), self._charge_activation_w_for_current(floor_a)

    @staticmethod
    def _charge_activation_w_for_current(current_a: float) -> float:
        """Surplus power at which charging at ``current_a`` should start.

        ``LOAD_FOLLOW_BIDIRECTIONAL`` treats the charge range minimum as a
        floor: if charging is active at all, a smaller surplus is topped up
        from the grid to reach it.  That is fine for the inherited 3 A floor
        (at most ~120 W of bias rounding), but a calculated 9 A floor would
        turn a 3 A surplus into a 9 A charge and import 6 A at the peak price.
        Those imported amps were never part of the self-use calculation, which
        prices the input at the export value it displaces.

        Gate on the floor's AC power instead, so the surplus must support the
        floor before charging starts.  The only import left is the half-amp
        bias rounding at the boundary, matching the existing 3 A/720 W
        behaviour.  A floor of zero disables charging entirely.
        """
        return 0.0 if current_a <= 0 else float(current_a) * 240.0

    def _solar_charge_floor_text(
        self,
        *,
        at_cap: bool = False,
        charge_min_a: float | None = None,
        activation_w: float | None = None,
    ) -> str:
        """One sentence describing what idle/conservative modes do with solar.

        Idle and conservative modes stop discharge but do not automatically
        accept every trickle of solar.  The charge path is armed at the
        break-even current for the live prices, or closed when exporting now
        is worth more.  The text is shared by the dashboard mode line, the
        explanation page and the export-plan rows so they cannot disagree.
        """
        cap = self.charge_cap_percent()
        if at_cap:
            return (
                f"The pack is at its {cap:.0f}% charge cap, so surplus solar "
                "is exported."
            )
        if charge_min_a is None:
            charge_min_a = float(self.SOLAR_CHARGE_REFERENCE_CURRENT)
        if activation_w is None:
            activation_w = self._charge_activation_w_for_current(charge_min_a)
        if charge_min_a <= 0:
            return (
                "No measured solar current is worth keeping at the current "
                "export price, so the surplus is sold instead of charging."
            )
        return (
            f"Below the {cap:.0f}% charge cap, surplus solar charges at "
            f"≥{charge_min_a:.0f} A (≈{activation_w:.0f} W), up to "
            f"{self.MAX_CHARGE_CURRENT:.0f} A."
        )

    def _read_power_snapshot(self) -> Optional[dict]:
        """Read the latest grid/EVSE channels, or ``None`` if unavailable.

        ``getHomeWatts()`` is ``ch1 - ch2`` (grid minus EVSE), so the house
        load without the EVSE is the baseline a no-storage system would show
        on the grid.  Its negative is the surplus available to the car.

        ``current_grid_power`` has already been normalised by
        :class:`EvseController` so that ch1 is the configured grid channel and
        ch2 the configured EVSE channel, whichever Shelly and channel those
        happen to be.
        """
        controller = getattr(self, "evseController", None)
        power = getattr(controller, "current_grid_power", None) if controller else None
        if power is None:
            return None

        try:
            grid_w = float(power.ch1Watts)
            evse_w = float(power.ch2Watts)
            baseline_w = grid_w - evse_w
            voltage = float(power.voltage) if power.voltage and power.voltage > 0 else 240.0
            soc = int(getattr(power, "soc", -1))
        except (AttributeError, TypeError, ValueError):
            return None

        return {
            "grid_w": grid_w,
            "evse_w": evse_w,
            "baseline_w": baseline_w,
            "surplus_w": -baseline_w,
            "voltage": voltage,
            "soc": soc,
        }

    def _bank_fallback_cost_per_dc_kwh(self) -> float:
        """Cost basis for stored energy with no recorded purchase price.

        At process start the pack may already hold charge bought before the
        ledger existed.  Rather than reporting free energy, assume it was
        bought at the off-peak rate, which is the most expensive way this
        system normally charges.  That keeps the saving conservative.
        """
        charge_efficiency = self.EFFICIENCY_MODEL.charge_efficiency(
            self.GRID_CHARGE_REFERENCE_CURRENT
        )
        return (config.IMPORT_LOW / 100.0) / max(charge_efficiency, 1e-6)

    def _stored_cost_per_dc_kwh_p(self) -> float:
        """Average purchase cost of the energy in the pack, in p/kWh DC.

        This is the price paid per kWh actually stored in the pack (or the
        Agile export value forgone when it was stored).  The discharge
        threshold uses ``_delivered_cost_per_kwh_p()`` instead, which divides
        this by the measured discharge efficiency.  When the ledger has no
        basis (fresh process, or energy that predates it) this uses the
        off-peak purchase price, which keeps the estimate conservative.  A
        ledger smaller than ``MIN_LEDGER_BASIS_KWH`` is treated as noise
        rather than a price basis.
        """
        if self._bank_dc_kwh >= self.MIN_LEDGER_BASIS_KWH:
            return (self._bank_cost_gbp / self._bank_dc_kwh) * 100.0
        return self._bank_fallback_cost_per_dc_kwh() * 100.0

    def _delivered_cost_per_kwh_p(self, current_a: float | None = None) -> float:
        """Ledger cost per kWh delivered from the pack, in p/kWh.

        The delivered cost depends on the discharge current: the pack loses a
        larger share at low current, so the same stored energy costs more per
        kWh delivered at 3 A than at maximum current.  ``current_a`` defaults
        to ``MIN_DISCHARGE_CURRENT`` because the activation threshold is a
        decision to start at the minimum output.  Callers that describe a
        discharge at another current should pass it explicitly.
        """
        if current_a is None:
            current_a = float(self.MIN_DISCHARGE_CURRENT)
        discharge_efficiency = self.EFFICIENCY_MODEL.discharge_efficiency(
            current_a
        )
        return self._stored_cost_per_dc_kwh_p() / max(discharge_efficiency, 1e-6)

    def _delivered_cost_range_per_kwh_p(self) -> tuple[float, float]:
        """Lowest and highest delivered cost across the discharge range.

        The low end is at maximum discharge current (least loss after the
        pack); the high end is at the 3 A minimum (most loss).
        """
        min_current = self.MIN_DISCHARGE_CURRENT
        max_current = max(min_current, config.WALLBOX_MAX_DISCHARGE_CURRENT)
        return (
            self._delivered_cost_per_kwh_p(max_current),
            self._delivered_cost_per_kwh_p(min_current),
        )

    def _seed_bank_from_soc(self, soc_percent) -> None:
        """Value the charge already in the pack when the ledger is fresh.

        The ledger only sees energy that passes through the EVSE while the
        process is running.  After a restart the pack can hold a full charge
        bought before the ledger existed; without a seed, the first tiny
        sample booked afterwards would become the entire cost basis and a
        momentary positive reading at a high import price could disable
        discharge.  Value the existing DC energy at the same off-peak
        fallback used for unknown energy, so later purchases blend with it.
        """
        if self._bank_seeded:
            return
        try:
            soc = float(soc_percent)
        except (TypeError, ValueError):
            return
        if soc <= 0:
            # No usable reading yet: -1 means the measurement failed, and the
            # placeholder Power starts at 0.  Try again on a later cycle.
            return

        existing_dc_kwh = (soc / 100.0) * self.SOC_CAPACITY_KWH
        if existing_dc_kwh > 0:
            self._bank_dc_kwh += existing_dc_kwh
            self._bank_cost_gbp += (
                existing_dc_kwh * self._bank_fallback_cost_per_dc_kwh()
            )
        self._bank_seeded = True

    def _state_file_path(self) -> Optional[Path]:
        """Where this tariff's persistent state lives, or None to disable it."""
        if self._state_path_override is not None:
            return self._state_path_override
        if self.STATE_PATH is not None:
            return Path(self.STATE_PATH)
        return config.IOCTGO_AGILE_STATE_FILE

    def _persisted_state(self) -> dict:
        """The small amount of state worth carrying across a restart."""
        savings_day = self._savings_day
        dc_kwh = float(self._bank_dc_kwh)
        cost_gbp = float(self._bank_cost_gbp)
        saved_today = float(self._saved_today_gbp)
        if not math.isfinite(dc_kwh) or dc_kwh < 0:
            dc_kwh = 0.0
        if not math.isfinite(cost_gbp) or cost_gbp < 0:
            cost_gbp = 0.0
        if not math.isfinite(saved_today):
            saved_today = 0.0
        exported_slots = []
        for slot in self._exported_slots:
            try:
                value = int(slot)
            except (TypeError, ValueError):
                continue
            if 0 <= value < 48:
                exported_slots.append(value)
        saved_by_slot = []
        for value in self._saved_by_slot_gbp[:48]:
            try:
                number = float(value)
            except (TypeError, ValueError):
                number = 0.0
            if not math.isfinite(number):
                number = 0.0
            saved_by_slot.append(number)
        saved_by_slot.extend([0.0] * (48 - len(saved_by_slot)))
        total_by_slot = sum(saved_by_slot)
        if saved_today == 0.0:
            saved_by_slot = [0.0] * 48
        elif not math.isclose(total_by_slot, saved_today, abs_tol=1e-9):
            # Keep the persisted invariant even if the array was damaged or
            # came from an older file.  Attribute the residual to the last
            # slot that already holds a saving, or to the current slot.
            target = next(
                (i for i in range(47, -1, -1) if saved_by_slot[i] != 0.0),
                None,
            )
            if target is None:
                now = self.get_current_datetime()
                target = now.hour * 2 + (1 if now.minute >= 30 else 0)
            saved_by_slot[target] += saved_today - total_by_slot
        return {
            "version": self.STATE_VERSION,
            "saved_at": self.get_current_datetime().isoformat(),
            "ledger": {
                "dc_kwh": dc_kwh,
                "cost_gbp": cost_gbp,
                "seeded": bool(self._bank_seeded),
            },
            "savings": {
                "day": savings_day.isoformat() if savings_day else None,
                "saved_today_gbp": saved_today,
                "saved_by_slot_gbp": saved_by_slot,
            },
            "exported_slots": sorted(set(exported_slots)),
        }

    def save_state(self) -> bool:
        """Write the ledger and daily saving to disk.

        Called when the tariff is switched away from and on a clean shutdown.
        A failed write must never take the controller down, so errors are
        logged and the in-memory state is left untouched.
        """
        path = self._state_file_path()
        if path is None:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                json.dumps(self._persisted_state(), indent=2) + "\n"
            )
            temporary.replace(path)
            debug(
                f"IOCTGO_AGILEOUT: Saved tariff state to {path} "
                f"(ledger {self._bank_dc_kwh:.2f} kWh, "
                f"saved today £{self._saved_today_gbp:.4f})"
            )
            return True
        except (OSError, TypeError, ValueError) as exc:
            error(f"IOCTGO_AGILEOUT: Failed to save tariff state: {exc}")
            return False

    def _load_persisted_state(self) -> None:
        """Restore the ledger and today's saving, if a valid file exists.

        Only the ledger day's saving is kept.  A file from an earlier day
        restores the ledger (which deliberately carries over midnight) but
        starts today's saving at zero, matching ``_accumulate_daily_savings``.
        """
        path = self._state_file_path()
        if path is None or not path.exists():
            return

        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            warning(
                f"IOCTGO_AGILEOUT: Ignoring unreadable tariff state at "
                f"{path}: {exc}"
            )
            return
        if not isinstance(data, dict):
            warning(
                f"IOCTGO_AGILEOUT: Ignoring tariff state at {path}: "
                "not a JSON object"
            )
            return

        ledger = data.get("ledger")
        if not isinstance(ledger, dict):
            ledger = {}
        ledger_usable = True
        try:
            dc_kwh = float(ledger.get("dc_kwh", 0.0))
            cost_gbp = float(ledger.get("cost_gbp", 0.0))
        except (TypeError, ValueError):
            warning(
                f"IOCTGO_AGILEOUT: Ignoring invalid ledger in {path}; "
                "using the off-peak fallback"
            )
            dc_kwh = 0.0
            cost_gbp = 0.0
            ledger_usable = False
        if not math.isfinite(dc_kwh) or dc_kwh < 0:
            dc_kwh = 0.0
            ledger_usable = False
        if not math.isfinite(cost_gbp) or cost_gbp < 0:
            cost_gbp = 0.0
            ledger_usable = False

        self._bank_dc_kwh = dc_kwh
        self._bank_cost_gbp = cost_gbp
        # An unusable ledger must not claim the pack has already been valued,
        # otherwise the startup SoC seed would be skipped and the fallback
        # price would never be applied.
        self._bank_seeded = bool(
            ledger.get("seeded", False)
        ) and ledger_usable

        today = self.get_current_datetime().date()
        savings = data.get("savings")
        if not isinstance(savings, dict):
            savings = {}
        saved_day = None
        raw_day = savings.get("day")
        if isinstance(raw_day, str):
            try:
                saved_day = date.fromisoformat(raw_day)
            except ValueError:
                saved_day = None
        try:
            saved_today = float(savings.get("saved_today_gbp", 0.0))
        except (TypeError, ValueError):
            saved_today = 0.0
        if not math.isfinite(saved_today):
            saved_today = 0.0

        same_day = saved_day == today
        self._savings_day = today
        self._saved_today_gbp = saved_today if same_day else 0.0

        self._saved_by_slot_gbp = [0.0] * 48
        if same_day and self._saved_today_gbp != 0.0:
            raw_by_slot = savings.get("saved_by_slot_gbp")
            restored_by_slot = []
            if isinstance(raw_by_slot, (list, tuple)) and len(raw_by_slot) == 48:
                for raw_value in raw_by_slot:
                    try:
                        value = float(raw_value)
                    except (TypeError, ValueError):
                        value = 0.0
                    restored_by_slot.append(value if math.isfinite(value) else 0.0)
            if restored_by_slot and math.isclose(
                sum(restored_by_slot), self._saved_today_gbp, abs_tol=1e-9
            ):
                self._saved_by_slot_gbp = restored_by_slot
            else:
                # An older state file (or a damaged array) has a daily total
                # but no trustworthy split.  Keep the invariant that the
                # slots sum to the total by putting the remainder in the most
                # recent slot we can identify.
                slot = 0
                saved_at = data.get("saved_at")
                if isinstance(saved_at, str):
                    try:
                        stamp = datetime.fromisoformat(saved_at)
                    except ValueError:
                        stamp = None
                    if stamp is not None and stamp.date() == today:
                        slot = stamp.hour * 2 + (1 if stamp.minute >= 30 else 0)
                self._saved_by_slot_gbp[min(slot, 47)] = self._saved_today_gbp

        restored_slots = []
        if same_day:
            raw_slots = data.get("exported_slots")
            if not isinstance(raw_slots, (list, tuple, set)):
                raw_slots = []
            for raw_slot in raw_slots:
                try:
                    slot = int(raw_slot)
                except (TypeError, ValueError):
                    continue
                if 0 <= slot < 48:
                    restored_slots.append(slot)
        self._exported_slots = sorted(set(restored_slots))

        # The gap while the tariff was inactive must not be charged to the
        # first control cycle as if it had just happened.
        self._savings_last_time = None
        self._savings_last_baseline_w = 0.0
        self._savings_last_evse_w = 0.0
        self._savings_rate_history.clear()
        self._savings_rate_gbp_per_hour = 0.0
        self._ledger_direction = 0
        self._ledger_settle_until = None

        info(
            f"IOCTGO_AGILEOUT: Restored tariff state from {path} "
            f"(ledger {self._bank_dc_kwh:.2f} kWh, "
            f"saved today £{self._saved_today_gbp:.4f})"
        )

    @classmethod
    def _ledger_direction_for(cls, evse_w: float) -> int:
        """Classify the EVSE power flow, ignoring near-zero idle noise."""
        if evse_w > cls.LEDGER_DIRECTION_DEADBAND_W:
            return 1
        if evse_w < -cls.LEDGER_DIRECTION_DEADBAND_W:
            return -1
        return 0

    def _update_ledger_settle(self, now: datetime, direction: int) -> None:
        """Start a settling window when the power flow changes direction.

        The Wallbox draws power while it starts or stops, and that draw has no
        useful price.  A transition from idle or to idle opens a window for
        the corresponding delay from ``wallbox_thread.py``.  A transition
        inside an existing window is the startup transient itself (for
        example the brief import before a discharge begins), so it updates the
        direction without extending the window.  Magnitude changes within one
        direction do not open a window at all.
        """
        previous = self._ledger_direction
        if direction == previous:
            return
        self._ledger_direction = direction

        if self._ledger_settle_until is not None and now < self._ledger_settle_until:
            return

        if previous == 0:
            seconds = self.LEDGER_STARTUP_SETTLE_SECONDS
            label = "startup"
        elif direction == 0:
            seconds = self.LEDGER_SHUTDOWN_SETTLE_SECONDS
            label = "shutdown"
        else:
            seconds = self.LEDGER_STARTUP_SETTLE_SECONDS
            label = "reversal"

        self._ledger_settle_until = now + timedelta(seconds=seconds)
        debug(
            f"IOCTGO_AGILEOUT: Ledger settling for {seconds:.1f}s after "
            f"EVSE {label}"
        )

    def _best_alternative_export_rate(
        self,
        current_slot: int,
        *,
        planned_slots: list[int] | set[int] | None = None,
    ) -> float:
        """Best price a marginal stored kWh could still earn elsewhere.

        Planned export slots are already committed to the energy in the pack,
        so the small extra SoC that aggressive load-following would spend
        cannot displace them.  The alternative is therefore the best future
        slot that the plan has *not* claimed.  When no such slot exists the
        marginal energy has no better sale, so the alternative is 0p and the
        ledger cost sets the hurdle.

        The plan is current information, not a forecast: it is rebuilt from
        today's known Agile prices and the live SoC.
        """
        if planned_slots is None:
            actions = self._planned_slot_actions
            if actions:
                planned = {
                    slot
                    for slot, action in actions.items()
                    if action in ("export", "aggressive")
                }
            else:
                planned = set(self._planned_export_slots)
        else:
            planned = set(planned_slots)

        free_import_slots = self._free_import_slots_today()
        grid_support_slots = self._grid_support_slots_today()
        away_slots = self._away_slots_today()
        min_export_rate_p = self.export_min_rate_p()
        best = 0.0
        for rate_data in self._today_rates():
            slot = rate_data['start'].hour * 2 + (
                1 if rate_data['start'].minute == 30 else 0
            )
            if slot <= current_slot or slot >= 47:
                continue
            if slot in planned:
                continue
            if slot in free_import_slots or slot in away_slots:
                continue
            if slot in grid_support_slots:
                continue
            if self.is_off_peak(slot * SLOT_MINUTES):
                continue
            rate_p = float(rate_data['rate'])
            if rate_p < min_export_rate_p:
                continue
            best = max(best, rate_p)
        return best

    def _aggressive_energy_dc_cost_p(self, alternative_rate_p: float) -> float:
        """Value of one stored DC kWh: ledger cost or export alternative.

        The alternative is converted from p/kWh AC to p/kWh DC with the
        measured export efficiency, so the gate compares like with like.
        """
        alternative_value_p = (
            max(0.0, float(alternative_rate_p))
            * self.export_discharge_efficiency()
        )
        return max(
            self._stored_cost_per_dc_kwh_p(),
            alternative_value_p,
        )

    def _aggressive_activation_scan(
        self,
        export_rate_p: float,
        *,
        import_rate_p: float,
        alternative_rate_p: float,
        voltage: float | None,
    ) -> tuple[float, float, float]:
        """Scan house load for the first economic aggressive discharge.

        The Wallbox current is quantised to whole amps and the +0.5 A bias
        means the actual current rises with the load.  Discharge efficiency
        improves with current, so a single break-even evaluated at 3 A can
        miss a load that only pays at 4 A or 5 A.  This scan evaluates the
        integer current the Wallbox would actually choose at each load.

        A stored DC kWh is valued at the higher of its ledger cost and the
        best alternative export price, then converted to a delivered cost at
        the current being evaluated and increased by the profit margin.

        Returns ``(activation_w, hurdle_p, current_a)``.  ``hurdle_p`` is the
        delivered cost at the activation current, or at the minimum current
        when no load is economic.
        """
        volts = 240.0 if voltage is None or voltage <= 0 else float(voltage)
        min_current = max(1.0, float(self.MIN_DISCHARGE_CURRENT))
        max_current = max(
            min_current, float(config.WALLBOX_MAX_DISCHARGE_CURRENT)
        )
        energy_dc_p = self._aggressive_energy_dc_cost_p(alternative_rate_p)
        margin_p = self.MIN_PROFIT_MARGIN_P_PER_KWH
        min_hurdle_p = 0.0
        max_load_w = int(math.floor(max_current * volts))

        for load_w in range(0, max_load_w + 1):
            biased_current = (
                load_w / volts + self.AGGRESSIVE_DISCHARGE_BIAS_A
            )
            current_a = float(
                round(
                    max(
                        min_current,
                        min(biased_current, max_current),
                    )
                )
            )
            current_a = max(min_current, min(current_a, max_current))
            output_w = current_a * volts
            efficiency = self.EFFICIENCY_MODEL.discharge_efficiency(
                current_a
            )
            hurdle_p = energy_dc_p / max(efficiency, 1e-6) + margin_p
            if load_w == 0:
                min_hurdle_p = hurdle_p

            revenue_p = (
                min(load_w, output_w) * import_rate_p
                + max(0.0, output_w - load_w) * export_rate_p
            )
            if revenue_p >= output_w * hurdle_p:
                return float(load_w), hurdle_p, current_a

        return (
            float(self.NEVER_DISCHARGE_ACTIVATION_W),
            min_hurdle_p,
            min_current,
        )

    def discharge_activation_threshold_w(
        self,
        export_rate_p: float,
        *,
        import_rate_p: float | None = None,
        aggressive: bool = True,
        voltage: float | None = None,
        alternative_rate_p: float | None = None,
    ) -> float:
        """Smallest house import at which discharging still makes money.

        A load-following discharge cannot follow a small house load: the
        Wallbox cannot go below ``MIN_DISCHARGE_CURRENT`` (~720 W).  Starting
        for a 30 W base load therefore sends the other ~690 W to the grid at
        the prevailing export price, which can cost more than the import it
        avoids.  This threshold is the break-even load.

        **Aggressive mode** scans the load and evaluates the integer current
        the Wallbox would actually pick at each point.  A stored kWh is valued
        at the higher of its ledger cost and the best *uncommitted* export
        price, so energy is not spent now if a better unreserved slot could
        need it.  The scan naturally covers the 4 A/5 A case where higher
        discharge efficiency makes a load economic that 3 A could not repay.

        **Conservative mode** deliberately never exports at activation, so it
        keeps the closed-form break-even evaluated at the minimum output and
        floors the result at ``P_min``.  One hour at ``P_min`` serving a load
        ``L < P_min`` earns ``L * P_import + (P_min - L) * P_export`` and
        costs ``P_min * H`` with the ledger's delivered cost plus margin::

            G* = P_min * (H - P_export) / (P_import - P_export)

        * ``H >= P_import``: even a load equal to ``P_min`` cannot repay the
          stored energy, so never start (the export planner handles any
          profitable export separately).
        * ``P_export >= H``: the forced export alone clears the hurdle, so
          any import justifies a start (0 W).

        Args:
            export_rate_p: Export price that the forced excess would earn.
            import_rate_p: Import price the discharge avoids.  Defaults to
                the peak rate, because discharge decisions happen outside
                off-peak charging.
            aggressive: True for the +0.5 A bias strategy, False for the
                -0.5 A conservative strategy.
            voltage: Live grid voltage in volts.  Defaults to 240 V.
            alternative_rate_p: Best alternative export price for the stored
                energy in an uncommitted slot.  Defaults to 0p, meaning the
                marginal energy has no better sale than the ledger value.
        """
        import_p = (
            config.IMPORT_HIGH if import_rate_p is None else import_rate_p
        )
        volts = 240.0 if voltage is None or voltage <= 0 else voltage

        if aggressive:
            threshold_w, _, _ = self._aggressive_activation_scan(
                export_rate_p,
                import_rate_p=import_p,
                alternative_rate_p=(
                    0.0
                    if alternative_rate_p is None
                    else alternative_rate_p
                ),
                voltage=volts,
            )
            return threshold_w

        # Conservative mode: never intentionally export at activation.
        p_min_w = max(0.0, self.MIN_DISCHARGE_CURRENT) * volts
        hurdle_p = (
            self._delivered_cost_per_kwh_p()
            + self.MIN_PROFIT_MARGIN_P_PER_KWH
        )

        if hurdle_p >= import_p:
            return self.NEVER_DISCHARGE_ACTIVATION_W
        if export_rate_p >= hurdle_p:
            return 0.0

        spread_p = import_p - export_rate_p
        if p_min_w <= 0 or spread_p <= 0:
            return self.NEVER_DISCHARGE_ACTIVATION_W

        clamped_break_even_w = p_min_w * (hurdle_p - export_rate_p) / spread_p
        return round(max(clamped_break_even_w, p_min_w))

    def _slot_start_datetime(self, day_minute: int) -> datetime:
        """Start of the half-hour slot containing *day_minute* today."""
        midnight = datetime.combine(self.get_current_datetime().date(), time())
        return midnight + timedelta(minutes=SLOT_MINUTES * (day_minute // SLOT_MINUTES))

    def _session_at(self, when: datetime):
        """Session covering *when*, or ``None`` when no store is configured."""
        store = getattr(self, "session_store", None)
        return store.active(when) if store is not None else None

    def _free_import_slots_today(self) -> set[int]:
        """Today's slots whose import price is 0p under a free session."""
        store = getattr(self, "session_store", None)
        if store is None:
            return set()
        return store.free_import_slots(self.get_current_datetime().date())

    def _grid_support_slots_today(self) -> set[int]:
        """Today's slots covered by a grid-support session."""
        store = getattr(self, "session_store", None)
        if store is None:
            return set()
        return store.slots_for_kind(
            self.get_current_datetime().date(), GRID_SUPPORT
        )

    def _away_slots_today(self) -> set[int]:
        """Today's slots the EV is planned to be away for."""
        store = getattr(self, "session_store", None)
        if store is None:
            return set()
        return store.away_slots(self.get_current_datetime().date())

    def _current_import_rate_p(self, day_minute: int) -> float:
        """Import price in force at *day_minute*, in p/kWh.

        A ``free_import`` session overrides the normal high/low band to 0p.
        The ledger and the decision logic both call this, so the booked cost
        and the control decision cannot disagree about a free window.
        """
        session = self._session_at(self._slot_start_datetime(day_minute))
        if session is not None and session.kind == FREE_IMPORT:
            return 0.0
        return (
            config.IMPORT_LOW
            if self.is_off_peak(day_minute)
            else config.IMPORT_HIGH
        )

    def _current_discharge_activation_w(
        self, current_slot: int, day_minute: int, *, aggressive: bool
    ) -> float:
        """Discharge activation for the live prices and stored-energy cost.

        Aggressive load-following values the stored energy at its best
        uncommitted alternative use, so a better unreserved export slot raises
        the gate.  Conservative coverage keeps the ledger's delivered-cost
        hurdle.

        A planned aggressive slot reuses the gate the planner recorded, so the
        threshold the plan explains and the threshold the Wallbox receives
        cannot drift apart inside the slot.
        """
        if aggressive:
            gate = self._aggressive_slot_gates.get(current_slot)
            if gate is not None:
                return gate["activation_w"]
        power = self._read_power_snapshot()
        return self.discharge_activation_threshold_w(
            self._current_export_rate_p(current_slot),
            import_rate_p=self._current_import_rate_p(day_minute),
            aggressive=aggressive,
            voltage=power["voltage"] if power else None,
            alternative_rate_p=(
                self._best_alternative_export_rate(current_slot)
                if aggressive
                else None
            ),
        )

    @classmethod
    def _activation_note(cls, activation_w: float) -> str:
        """One-sentence explanation of a discharge activation threshold."""
        if activation_w >= cls.NEVER_DISCHARGE_ACTIVATION_W:
            return (
                "will not run: even the lowest output cannot repay the stored "
                "energy's best alternative use"
            )
        if activation_w <= 0:
            return (
                "starts on any import: export covers the stored-energy hurdle"
            )
        return (
            f"starts at {activation_w:.0f} W import: below that the "
            "forced output would export more than it saves"
        )

    @classmethod
    def _activation_explanation(cls, activation_w: float) -> str:
        """Short dashboard sentence explaining the aggressive trigger."""
        if activation_w >= cls.NEVER_DISCHARGE_ACTIVATION_W:
            return (
                "Discharge is disabled because stored energy is worth more in "
                "its best alternative use than the import it avoids."
            )
        if activation_w <= 0:
            return (
                "Any import starts discharge because the export alone clears "
                "the stored cost."
            )
        return (
            f"Discharge starts at ≥{activation_w:.0f} W house import; below "
            "that the forced output would export more than it saves."
        )

    def _aggressive_slot_gate(
        self,
        slot: int,
        *,
        planned_slots: list[int] | set[int] | None = None,
    ) -> dict:
        """Break-even house load for aggressive load-following in *slot*.

        The planner does not forecast the house load, so an aggressive slot
        is a *conditional* action: the live controller only starts the
        discharge once the house import reaches this break-even.  Evaluating
        the gate at the slot's own prices keeps the plan and the control loop
        from disagreeing about whether the slot is worthwhile.

        The stored energy is valued at the higher of its ledger cost and the
        best *uncommitted* export price.  Planned sales are already committed
        to the pack, so they are not an alternative for the small extra SoC
        this slot would spend; an unplanned higher-priced slot is, because
        spending now could put it out of reach.  The scan evaluates the
        integer current the Wallbox would actually choose, so the 4 A/5 A
        efficiency step is included.  The export price and the live voltage
        come from the same helpers the control loop uses.
        """
        power = self._read_power_snapshot()
        voltage = power["voltage"] if power else None
        export_rate_p = self._current_export_rate_p(slot)
        import_rate_p = self._current_import_rate_p(slot * SLOT_MINUTES)
        alternative_rate_p = self._best_alternative_export_rate(
            slot, planned_slots=planned_slots
        )
        (
            activation_w,
            hurdle_p,
            activation_current_a,
        ) = self._aggressive_activation_scan(
            export_rate_p,
            import_rate_p=import_rate_p,
            alternative_rate_p=alternative_rate_p,
            voltage=voltage,
        )
        return {
            "slot": slot,
            "activation_w": activation_w,
            "hurdle_p": hurdle_p,
            "activation_current_a": activation_current_a,
            "alternative_rate_p": alternative_rate_p,
            "export_rate_p": export_rate_p,
            "import_rate_p": import_rate_p,
            "voltage_v": voltage,
            "note": self._activation_note(activation_w),
            "explanation": self._activation_explanation(activation_w),
        }

    def _record_savings_rate(self, now: datetime) -> None:
        """Update the recent rate of change of the saved-today total.

        A short window keeps the figure responsive while smoothing the
        control-cycle noise.  The dashboard reads only the resulting float,
        so the deque is confined to the control thread.
        """
        history = self._savings_rate_history
        history.append((now, self._saved_today_gbp))
        cutoff = now - timedelta(seconds=self.SAVINGS_RATE_WINDOW_SECONDS)
        while len(history) > 1 and history[0][0] < cutoff:
            history.popleft()

        if len(history) < 2:
            self._savings_rate_gbp_per_hour = 0.0
            return

        start_time, start_value = history[0]
        elapsed_hours = (now - start_time).total_seconds() / 3600.0
        if elapsed_hours <= 0:
            self._savings_rate_gbp_per_hour = 0.0
            return

        self._savings_rate_gbp_per_hour = (
            self._saved_today_gbp - start_value
        ) / elapsed_hours

    def _accumulate_daily_savings(self) -> None:
        """Accumulate the money saved today by cycling stored energy.

        Each interval is priced against what would have happened without the
        EVSE/battery:

        * **Charging** puts energy into the ledger at the price paid.  Grid
          energy costs the current import rate; surplus solar is capped at the
          live surplus and costed at the current Agile export price, because
          exporting it was the alternative.  No saving is booked yet.
        * **Discharging** releases the ledger.  Energy that offsets house
          import earns the current import rate; anything beyond the house load
          earns the Agile export rate.  The saving is that avoided value minus
          the ledger cost of the energy released.

        So an off-peak charge at 7.62p followed by a daytime discharge that
        displaces 32.26p import books ``32.26p − 7.62p / η_rt`` per kWh
        delivered.  The round-trip loss is included because the ledger holds
        DC-side energy and the discharge is divided by its measured discharge
        efficiency.  Resets the daily total at midnight; the ledger persists,
        so energy bought yesterday is priced correctly when it is used today.

        This is a counterfactual cash-flow measure, not a capital-cost one.
        Solar is costed at the export revenue it displaced, so at a 0p export
        price it enters free and a later 0p export books nothing.  A negative
        figure therefore means either bought energy was sold for less than it
        cost, or stored solar was sold for less than the export it displaced —
        forgone revenue, not a running cost.
        """
        now = self.get_current_datetime()
        today = now.date()
        if self._savings_day != today:
            self._savings_day = today
            self._saved_today_gbp = 0.0
            self._saved_by_slot_gbp = [0.0] * 48
            self._savings_last_time = None
            self._savings_last_baseline_w = 0.0
            self._savings_last_evse_w = 0.0
            self._savings_rate_history.clear()
            self._savings_rate_gbp_per_hour = 0.0

        power = self._read_power_snapshot()
        if power is None:
            self._record_savings_rate(now)
            return

        # Value the charge that predates this process before booking anything,
        # so a small sample cannot become the whole cost basis.
        self._seed_bank_from_soc(power.get("soc"))

        direction = self._ledger_direction_for(power["evse_w"])
        self._update_ledger_settle(now, direction)

        if self._savings_last_time is None:
            self._savings_last_time = now
            self._savings_last_baseline_w = power["baseline_w"]
            self._savings_last_evse_w = power["evse_w"]
            self._record_savings_rate(now)
            return

        elapsed = (now - self._savings_last_time).total_seconds()
        if elapsed <= 0:
            return

        # The Wallbox is still powering up or down: discard this interval
        # rather than booking its ~200 W transient at the import price.  Keep
        # the baseline current so the first post-settle interval is correct.
        if (
            self._ledger_settle_until is not None
            and now < self._ledger_settle_until
        ):
            self._savings_last_time = now
            self._savings_last_baseline_w = power["baseline_w"]
            self._savings_last_evse_w = power["evse_w"]
            self._record_savings_rate(now)
            return

        # Cap a long gap (process suspended, clock jump) so a single sample
        # cannot invent a large saving.
        hours = min(elapsed, 300.0) / 3600.0

        day_minute = now.hour * 60 + now.minute
        import_rate_p = self._current_import_rate_p(day_minute)
        current_slot = now.hour * 2 + (1 if now.minute >= 30 else 0)
        export_rate_p = self._current_export_rate_p(current_slot)

        # Trapezoidal average over the sample interval.
        baseline_w = 0.5 * (self._savings_last_baseline_w + power["baseline_w"])
        evse_w = 0.5 * (self._savings_last_evse_w + power["evse_w"])
        voltage = power["voltage"]

        energy_kwh = abs(evse_w) / 1000.0 * hours
        if energy_kwh > 0:
            current_a = abs(evse_w) / voltage
            if evse_w > 0:
                # Charging: split between free surplus and paid import.
                charge_efficiency = self.EFFICIENCY_MODEL.charge_efficiency(
                    current_a
                )
                surplus_w = max(0.0, -baseline_w)
                solar_kwh = min(energy_kwh, surplus_w / 1000.0 * hours)
                grid_kwh = energy_kwh - solar_kwh
                self._bank_dc_kwh += energy_kwh * charge_efficiency
                self._bank_cost_gbp += (
                    grid_kwh * import_rate_p / 100.0
                    + solar_kwh * export_rate_p / 100.0
                )
            else:
                # Discharging: release the ledger and book the avoided cost.
                discharge_efficiency = self.EFFICIENCY_MODEL.discharge_efficiency(
                    current_a
                )
                drain_dc = energy_kwh / max(discharge_efficiency, 1e-6)

                if self._bank_dc_kwh > 0:
                    cost_per_dc = self._bank_cost_gbp / self._bank_dc_kwh
                else:
                    cost_per_dc = self._bank_fallback_cost_per_dc_kwh()
                from_bank = min(drain_dc, self._bank_dc_kwh)
                bank_cost_used = from_bank * cost_per_dc
                released_cost = bank_cost_used + (
                    drain_dc - from_bank
                ) * self._bank_fallback_cost_per_dc_kwh()

                house_kwh = min(
                    energy_kwh, max(0.0, baseline_w) / 1000.0 * hours
                )
                export_kwh = max(0.0, energy_kwh - house_kwh)
                avoided = (
                    house_kwh * import_rate_p / 100.0
                    + export_kwh * export_rate_p / 100.0
                )

                self._saved_today_gbp += avoided - released_cost
                self._saved_by_slot_gbp[current_slot] += avoided - released_cost
                self._bank_dc_kwh = max(0.0, self._bank_dc_kwh - from_bank)
                self._bank_cost_gbp = max(
                    0.0, self._bank_cost_gbp - bank_cost_used
                )

        self._savings_last_time = now
        self._savings_last_baseline_w = power["baseline_w"]
        self._savings_last_evse_w = power["evse_w"]
        self._record_savings_rate(now)

    def saved_for_slot(self, day: date, slot: int) -> Optional[float]:
        """Return the saving booked in a local half-hour slot.

        ``0.0`` means the slot was monitored and nothing was saved; ``None``
        means the tariff has no data for that day.  The daily report uses
        ``None`` itself when no suitable tariff was active.
        """
        if not (0 <= slot < 48):
            return None
        if self._savings_day is None:
            # Nothing has been accumulated yet.  The tariff is still active,
            # so today's slots are a genuine zero rather than "unknown".
            return (
                0.0
                if day == self.get_current_datetime().date()
                else None
            )
        # Compare with the day the array belongs to, not the wall clock: the
        # report closes the previous day's last slot on the first sample after
        # midnight, which is still before the tariff resets its array.
        if day != self._savings_day:
            return None
        return float(self._saved_by_slot_gbp[slot])

    def _estimate_solar_harvest_current(self) -> float:
        """Estimate the charge current the load-following loop will use.

        ``EvseController.getHomeWatts()`` is the house load net of PV,
        *excluding* the EVSE, so its negative is the surplus available to the
        car.  Mirror the controller's calculation (``P / V + bias``, clamped to
        the configured charge range) and then round *up* to a measured
        efficiency point.

        Efficiency improves with current, so rounding down understates the
        round trip and can reject a store that would have paid.  The loop can
        start at the surplus current, so rounding up reflects the charge it
        will actually run rather than the worst plausible one.  A fractional
        surplus may import the difference up to the next measured point, which
        is small next to the 3 A floor.

        Falls back to the minimum current when there is no recent measurement,
        so the decision is never better than the old 3 A assumption.  This is
        an instantaneous estimate, re-evaluated each control cycle; a PV
        forecast would be better, and the DP replaces this heuristic.
        """
        power_snapshot = self._read_power_snapshot()
        if power_snapshot is None:
            return self.SOLAR_CHARGE_REFERENCE_CURRENT

        surplus_w = power_snapshot["surplus_w"]
        voltage = power_snapshot["voltage"]

        controller = getattr(self, "evseController", None)
        bias = getattr(controller, "chargeCurrentBias", -0.5)
        amps = surplus_w / voltage + bias
        amps = max(
            self.SOLAR_CHARGE_REFERENCE_CURRENT,
            min(amps, self.MAX_CHARGE_CURRENT),
        )
        estimate = self.EFFICIENCY_MODEL.measured_current_at_or_above(amps)
        source = (
            f"surplus {surplus_w:.0f} W"
            if surplus_w > 0
            else f"import {-surplus_w:.0f} W (no surplus)"
        )
        floor_note = (
            " (minimum)"
            if estimate <= self.SOLAR_CHARGE_REFERENCE_CURRENT
            else ""
        )
        debug(
            f"IOCTGO_AGILEOUT: Solar {source} at {voltage:.0f} V -> "
            f"harvest estimate {estimate:.0f} A{floor_note}"
        )
        return estimate

    @staticmethod
    def _slot_range_label(slot_idx: int) -> str:
        """Human-readable half-hour range for a slot index, e.g. 16:30–17:00."""
        start_minute = slot_idx * 30
        end_minute = start_minute + 30
        return (
            f"{start_minute // 60 % 24:02d}:{start_minute % 60:02d}"
            f"–{end_minute // 60 % 24:02d}:{end_minute % 60:02d}"
        )

    def _evaluate_storage_decision(
        self,
        current_slot: int,
        current_export_rate_p: float,
        soc_percent: float,
        harvest_current_a: float | None = None,
    ) -> StorageDecision:
        """Evaluate the three-tier store/export decision.

        This is the single source of truth for the decision.  The bool-returning
        ``_should_use_bidirectional_mode()`` wrapper preserves the old API; the
        dashboard reads the structured result through ``get_control_state()``.

        The solar round trip depends on the harvest current.  ``harvest_current_a``
        should be the current the load-following loop will actually use (from
        the live surplus); ``None`` falls back to the minimum current, which is
        the conservative/cold-start case.

        Missing rate data is not a special case: callers pass 0p when no
        current/future export price is known, and the normal tier logic then
        stores rather than exporting.
        """
        # Calculate remaining slots until 23:30 (slot 47)
        # This is used for dynamic SoC threshold calculation in Tier 2
        slots_remaining = max(0, 47 - current_slot)

        solar_round_trip = self.solar_export_round_trip(harvest_current_a)
        self_use_value_p = self.self_use_value_threshold_p(harvest_current_a)
        harvest_current = (
            self.SOLAR_CHARGE_REFERENCE_CURRENT
            if harvest_current_a is None
            else harvest_current_a
        )

        # === TIER 1: Storage Floor ===
        # Below this rate, always store - rate is too trivial to export.
        # With no rate data the rate is 0p, so this also covers the "Octopus
        # has not given us prices" case without a separate branch.
        if current_export_rate_p < self.STORAGE_FLOOR_THRESHOLD_P:
            debug(
                f"IOCTGO_AGILEOUT: STORE (Tier 1): rate {current_export_rate_p:.1f}p "
                f"< floor {self.STORAGE_FLOOR_THRESHOLD_P:.1f}p"
            )
            return StorageDecision(
                use_bidirectional=True,
                tier="storage floor",
                reason=(
                    f"export {current_export_rate_p:.1f}p is below the "
                    f"{self.STORAGE_FLOOR_THRESHOLD_P:.1f}p floor — storing solar"
                ),
                current_rate_p=current_export_rate_p,
                round_trip=solar_round_trip,
                efficiency_label="round trip",
            )

        # Unknown SoC - fall back to export optimization only
        if soc_percent < 0:
            target_slot, best_unused_rate = self._get_best_unused_export_slot_rate(
                current_slot
            )
            if best_unused_rate <= 0:
                return StorageDecision(
                    use_bidirectional=False,
                    tier="future export",
                    reason="SoC unknown and no future export prices — covering the house",
                    current_rate_p=current_export_rate_p,
                    round_trip=solar_round_trip,
                    efficiency_label="round trip",
                )
            net_future_rate = (
                best_unused_rate - self.MIN_PROFIT_MARGIN_P_PER_KWH
            )
            adjusted_future_rate = net_future_rate * solar_round_trip
            use_bidirectional = adjusted_future_rate > current_export_rate_p
            return StorageDecision(
                use_bidirectional=use_bidirectional,
                tier="future export",
                reason=(
                    f"(future {best_unused_rate:.1f}p − "
                    f"{self.MIN_PROFIT_MARGIN_P_PER_KWH:.1f}p margin) × "
                    f"{solar_round_trip:.0%} = {adjusted_future_rate:.1f}p"
                    f" {'>' if use_bidirectional else '≤'} "
                    f"{current_export_rate_p:.1f}p now"
                ),
                current_rate_p=current_export_rate_p,
                future_rate_p=best_unused_rate,
                target_slot=target_slot,
                round_trip=solar_round_trip,
                efficiency_label="round trip",
            )

        # === TIER 2: Self-Use Value ===
        # When rate is below self-use threshold and SoC is low, store for self-consumption
        if current_export_rate_p < self_use_value_p:
            # Calculate dynamic SoC threshold based on remaining slots until 23:30
            # This attempts to "get back on the curve" without needing grid import
            # Dynamic threshold = MIN_AGILE_DISCHARGE_SOC + (remaining_slots × NON_EXPORT_SLOT_SOC_LOSS_PERCENT)
            min_soc_percent = config.MIN_AGILE_DISCHARGE_SOC
            dynamic_soc_threshold = min_soc_percent + (slots_remaining * self.NON_EXPORT_SLOT_SOC_LOSS_PERCENT)

            if soc_percent < dynamic_soc_threshold:
                debug(f"IOCTGO_AGILEOUT: STORE (Tier 2): rate {current_export_rate_p:.1f}p < self-use value "
                      f"{self_use_value_p:.1f}p, SoC {soc_percent}% < dynamic threshold "
                      f"{dynamic_soc_threshold:.1f}% (min {min_soc_percent}% + {slots_remaining} slots × {self.NON_EXPORT_SLOT_SOC_LOSS_PERCENT:.1f}%)")
                self_use_round_trip = self.EFFICIENCY_MODEL.round_trip_efficiency(
                    harvest_current, self.SELF_USE_DISCHARGE_REFERENCE_CURRENT
                )
                return StorageDecision(
                    use_bidirectional=True,
                    tier="self-use",
                    reason=(
                        f"export {current_export_rate_p:.1f}p < self-use "
                        f"{self_use_value_p:.1f}p and SoC {soc_percent:.0f}% < "
                        f"{dynamic_soc_threshold:.0f}% curve — storing for the house"
                    ),
                    current_rate_p=current_export_rate_p,
                    round_trip=self_use_round_trip,
                    efficiency_label="solar → house",
                )
            # SoC is adequate, fall through to Tier 3

        # === TIER 3: Future Export Optimization ===
        # Get the best rate among slots we're NOT exporting in
        target_slot, best_unused_rate = self._get_best_unused_export_slot_rate(
            current_slot
        )

        if best_unused_rate <= 0:
            debug(f"IOCTGO_AGILEOUT: EXPORT (Tier 3): no better future rates available")
            return StorageDecision(
                use_bidirectional=False,
                tier="future export",
                reason="no future export price beats the current one — covering the house",
                current_rate_p=current_export_rate_p,
                round_trip=solar_round_trip,
                efficiency_label="round trip",
            )

        # Apply the margin first, then the round trip at the actual harvest
        # current. 1 kWh of solar input yields `solar_round_trip` kWh later,
        # each of which must clear (future export price - margin).
        net_future_rate = best_unused_rate - self.MIN_PROFIT_MARGIN_P_PER_KWH
        adjusted_future_rate = net_future_rate * solar_round_trip

        should_use_bidirectional = adjusted_future_rate > current_export_rate_p

        slot_label = (
            self._slot_range_label(target_slot)
            if target_slot is not None
            else "later"
        )
        if should_use_bidirectional:
            debug(f"IOCTGO_AGILEOUT: STORE (Tier 3): current={current_export_rate_p:.1f}p, "
                  f"future={best_unused_rate:.1f}p - margin {self.MIN_PROFIT_MARGIN_P_PER_KWH:.1f}p "
                  f"= {net_future_rate:.1f}p × {solar_round_trip:.0%} = "
                  f"{adjusted_future_rate:.1f}p")
            reason = (
                f"future {best_unused_rate:.1f}p at {slot_label} "
                f"− {self.MIN_PROFIT_MARGIN_P_PER_KWH:.1f}p margin = "
                f"{net_future_rate:.1f}p × {solar_round_trip:.0%} = "
                f"{adjusted_future_rate:.1f}p > "
                f"{current_export_rate_p:.1f}p now — storing solar"
            )
        else:
            debug(f"IOCTGO_AGILEOUT: EXPORT (Tier 3): current={current_export_rate_p:.1f}p >= "
                  f"future={best_unused_rate:.1f}p - margin {self.MIN_PROFIT_MARGIN_P_PER_KWH:.1f}p "
                  f"= {net_future_rate:.1f}p × {solar_round_trip:.0%} = "
                  f"{adjusted_future_rate:.1f}p")
            committed_slot, committed_rate = self._best_planned_export_slot_rate(
                current_slot
            )
            if committed_slot is not None and committed_rate > best_unused_rate:
                # Name the slot that is *better* but already committed, so the
                # dashboard does not imply the day has no higher price left.
                reason = (
                    f"{committed_rate:.1f}p at "
                    f"{self._slot_range_label(committed_slot)} is already "
                    "committed to the pack — the best unused slot is "
                    f"{best_unused_rate:.1f}p, which yields "
                    f"{adjusted_future_rate:.1f}p after the "
                    f"{solar_round_trip:.0%} round trip, below "
                    f"{current_export_rate_p:.1f}p now"
                )
            elif best_unused_rate > current_export_rate_p:
                # A higher slot exists and is not committed: say what it is
                # actually worth rather than claiming no slot beats the
                # current price.  At 3 A the round trip is poor enough that a
                # 30.5p slot can still be worth less than the 19.9p on offer.
                reason = (
                    f"future {best_unused_rate:.1f}p at {slot_label} "
                    f"− {self.MIN_PROFIT_MARGIN_P_PER_KWH:.1f}p margin = "
                    f"{net_future_rate:.1f}p × {solar_round_trip:.0%} = "
                    f"{adjusted_future_rate:.1f}p ≤ "
                    f"{current_export_rate_p:.1f}p now — covering the house"
                )
            else:
                reason = (
                    f"no future slot beats {current_export_rate_p:.1f}p now after "
                    f"{solar_round_trip:.0%} round trip — covering the house"
                )

        return StorageDecision(
            use_bidirectional=should_use_bidirectional,
            tier="future export",
            reason=reason,
            current_rate_p=current_export_rate_p,
            future_rate_p=best_unused_rate,
            target_slot=target_slot,
            round_trip=solar_round_trip,
            efficiency_label="round trip",
        )

    def _should_use_bidirectional_mode(
        self,
        current_slot: int,
        current_export_rate_p: float,
        soc_percent: float,
        harvest_current_a: float | None = None,
    ) -> bool:
        """Return whether bidirectional load-following is better than export.

        Delegates to :meth:`_evaluate_storage_decision`; the bool API is kept
        for existing callers and tests.
        """
        return self._evaluate_storage_decision(
            current_slot,
            current_export_rate_p,
            soc_percent,
            harvest_current_a=harvest_current_a,
        ).use_bidirectional

    def _fetch_rates_async(self):
        """Fetch Agile Outgoing rates asynchronously (non-blocking).
        
        Called at startup and then scheduled every minute until complete data
        is received for the current day. This handles:
        - Delayed rate releases from Octopus (typically ~16:00 but can be later)
        - Transient API errors
        - Partial data availability
        - DST changes (46/48/50 slots)
        """
        def fetch_thread():
            try:
                now = datetime.now()
                today = now.date()
                
                # Check if we already have complete data for today
                with self._rates_lock:
                    if self._have_complete_rates_for_date(today):
                        debug(f"IOCTGO_AGILEOUT: Already have complete rates for {today}, not fetching")
                        return
                
                rates = self._fetch_rates_from_api()
                
                with self._rates_lock:
                    if not rates:
                        # API returned no data - log and retry
                        debug(f"IOCTGO_AGILEOUT: No rates returned from API, will retry")
                        self._last_fetch_attempt = now
                        self._schedule_next_fetch()
                        return
                    
                    # Check if this is new data (different date or more slots than before)
                    last_fetch_date = getattr(self, '_last_fetch_date', None)
                    existing_count = len(self.agile_rates) if self.agile_rates else 0
                    new_count = len(rates)
                    
                    # Only update if: new day, or more slots than before
                    if last_fetch_date != today or new_count > existing_count:
                        self.agile_rates = rates
                        self.agile_rates_fetched_at = now
                        self._last_fetch_date = today
                        self._last_fetch_attempt = now

                        # Clear exported slots when a new day begins
                        if last_fetch_date != today:
                            self._exported_slots = []
                            debug(f"IOCTGO_AGILEOUT: New day detected ({today}), cleared exported slots")

                        info(f"IOCTGO_AGILEOUT: Fetched {new_count} Agile Outgoing rates for {today} (region {self.region})")
                        
                        # Check if we have complete data
                        if self._have_complete_rates_for_date(today):
                            info(f"IOCTGO_AGILEOUT: Have complete rates for {today} ({new_count} slots)")
                            # Cancel any scheduled fetches
                            if self._rate_fetch_timer:
                                self._rate_fetch_timer.cancel()
                                self._rate_fetch_timer = None
                        else:
                            # Still waiting for more data
                            debug(f"IOCTGO_AGILEOUT: Incomplete data - have {new_count} slots, will retry")
                            self._schedule_next_fetch()
                        
                        # Trigger export plan recalculation with current SoC
                        # This ensures the plan uses the new rates
                        try:
                            from evse_controller.drivers.evse.async_interface import EvseThreadInterface
                            evse = EvseThreadInterface.get_instance()
                            state = evse.get_state()
                            if state and state.battery_level >= 0:
                                # Recalculate plan with new rates
                                self._planned_export_slots = self.calculate_export_plan(state.battery_level)
                                self._last_plan_soc = state.battery_level
                                self._last_plan_slot = (now.hour * 60 + now.minute) // 30
                                self._plan_cache = self._planned_export_slots.copy()
                                if self._planned_export_slots:
                                    info(f"IOCTGO_AGILEOUT: Export plan recalculated with new rates - {len(self._planned_export_slots)} slots planned")
                        except Exception as e:
                            debug(f"IOCTGO_AGILEOUT: Could not recalculate export plan after rate fetch: {e}")
                    else:
                        # Same day, same or fewer slots - keep waiting
                        debug(f"IOCTGO_AGILEOUT: Rates unchanged for {today} ({new_count} slots), will retry")
                        self._schedule_next_fetch()
                        
            except Exception as e:
                error(f"IOCTGO_AGILEOUT: Failed to fetch Agile rates: {e}")
                # On error, schedule retry
                with self._rates_lock:
                    self._schedule_next_fetch()

        thread = threading.Thread(target=fetch_thread, daemon=True)
        thread.start()
    
    def _have_complete_rates_for_date(self, date) -> bool:
        """Check if we have complete rate data for the given date.
        
        Only checks slots from 05:30 onwards, which:
        - Avoids DST ambiguity (transitions at 02:00 don't affect these slots)
        - Focuses on the export-relevant period (Agile rates 05:30-23:30)
        - Before 16:00: Need slots 05:30-22:30 (slots 11-45)
        - After 16:00: Need slots 05:30-23:30 (slots 11-47)
        
        Args:
            date: datetime.date to check
            
        Returns:
            True if we have the required slots for effective export planning
        """
        if not self.agile_rates or self._last_fetch_date != date:
            return False
        
        now = datetime.now()
        current_hour = now.hour
        
        # Build a set of available slot start times for quick lookup
        # Only consider slots from 05:30 onwards (slot 11) to avoid DST issues
        available_slots = set()
        for rate_data in self.agile_rates:
            hour = rate_data['start'].hour
            # Skip slots before 05:30 (not relevant for export, and DST-ambiguous)
            if hour < 5:
                continue
            slot_idx = hour * 2 + (1 if rate_data['start'].minute == 30 else 0)
            available_slots.add(slot_idx)
        
        # Slot indices (from 05:30 onwards, DST-safe):
        # 05:30 = slot 11, 22:30 = slot 45, 23:00 = slot 46, 23:30 = slot 47
        
        # Before 16:00: Need slots 11-45 (05:30 through 22:30)
        # After 16:00: Need slots 11-47 (05:30 through 23:30)
        if current_hour < 16:
            required_min_slot = 11  # 05:30
            required_max_slot = 45  # 22:30
            info_text = "slots 05:30-22:30 (pre-16:00)"
        else:
            required_min_slot = 11  # 05:30
            required_max_slot = 47  # 23:30
            info_text = "slots 05:30-23:30 (post-16:00)"
        
        # Check if we have all required slots
        has_required = all(slot in available_slots for slot in range(required_min_slot, required_max_slot + 1))
        
        if has_required:
            debug(f"IOCTGO_AGILEOUT: Have {info_text} - {len(available_slots)} slots available (05:30+)")
        
        return has_required
    
    def _schedule_next_fetch(self):
        """Schedule the next rate fetch attempt in 60 seconds.
        
        Must be called with self._rates_lock held.
        """
        # Cancel any existing timer
        if self._rate_fetch_timer:
            self._rate_fetch_timer.cancel()
        
        # Schedule new fetch in 60 seconds
        self._rate_fetch_timer = threading.Timer(60.0, self._fetch_rates_async)
        self._rate_fetch_timer.daemon = True
        self._rate_fetch_timer.start()
        debug(f"IOCTGO_AGILEOUT: Scheduled next fetch attempt in 60 seconds")

    def _fetch_rates_from_api(self) -> list:
        """Fetch Agile Outgoing rates from Octopus API.
        
        Returns:
            List of dicts with 'start', 'end', 'rate' keys
        """
        today = datetime.now()
        day = today.date()
        rates = fetch_day_rates(
            self.region, day, tariff_code=self.tariff_code
        )
        if rates:
            # Publish to the shared price book so the daily report can still
            # price a slot after this tariff has been stopped.
            from evse_controller.slot_prices import price_book

            price_book.update_rates(day, rates)
        return rates

    def _record_decision(
        self,
        *,
        mode: str,
        reason: str,
        soc_percent: int,
        detail: str = "",
        target_soc_percent: Optional[float] = None,
        projected_soc_at_2330: Optional[float] = None,
        current_rate_p: Optional[float] = None,
        target_rate_p: Optional[float] = None,
        target_slot: Optional[int] = None,
        harvest_current_a: Optional[float] = None,
        surplus_w: Optional[float] = None,
        efficiency_pct: Optional[float] = None,
        efficiency_label: str = "",
        min_gain_p: Optional[float] = None,
        charge_min_a: Optional[float] = None,
        charge_max_a: Optional[float] = None,
        charge_activation_w: Optional[float] = None,
        discharge_min_a: Optional[float] = None,
        discharge_max_a: Optional[float] = None,
        discharge_aggressive: bool = True,
        discharge_activation_w: Optional[float] = None,
        decision_tier: str = "",
        store_decision: bool = False,
        shortfall: bool = False,
        import_rate_p: Optional[float] = None,
        house_floor_soc: Optional[float] = None,
        charge_cap_percent: Optional[float] = None,
        stored_cost_p: Optional[float] = None,
        voltage_v: Optional[float] = None,
    ) -> DecisionSnapshot:
        """Record the decision the dashboard should display."""
        ledger_stored_cost_p = self._stored_cost_per_dc_kwh_p()
        snapshot = DecisionSnapshot(
            updated_at=self.get_current_datetime(),
            mode=mode,
            reason=reason,
            detail=detail,
            soc_percent=soc_percent,
            target_soc_percent=target_soc_percent,
            projected_soc_at_2330=projected_soc_at_2330,
            planned_export_slots=list(self._planned_export_slots),
            planned_slot_actions=(
                dict(self._planned_slot_actions)
                if self._planned_slot_actions
                else None
            ),
            current_rate_p=current_rate_p,
            target_rate_p=target_rate_p,
            target_slot=target_slot,
            harvest_current_a=harvest_current_a,
            surplus_w=surplus_w,
            efficiency_pct=efficiency_pct,
            efficiency_label=efficiency_label,
            min_gain_p=min_gain_p,
            charge_min_a=charge_min_a,
            charge_max_a=charge_max_a,
            charge_activation_w=charge_activation_w,
            discharge_min_a=discharge_min_a,
            discharge_max_a=discharge_max_a,
            discharge_aggressive=discharge_aggressive,
            discharge_activation_w=discharge_activation_w,
            decision_tier=decision_tier,
            store_decision=store_decision,
            shortfall=shortfall,
            import_rate_p=import_rate_p,
            house_floor_soc=house_floor_soc,
            charge_cap_percent=charge_cap_percent,
            stored_cost_p=stored_cost_p,
            voltage_v=voltage_v,
            ledger_dc_kwh=float(self._bank_dc_kwh),
            ledger_cost_gbp=float(self._bank_cost_gbp),
            ledger_stored_cost_p=ledger_stored_cost_p,
            ledger_delivered_cost_p=(
                stored_cost_p
                if stored_cost_p is not None
                else self._delivered_cost_per_kwh_p()
            ),
            ledger_basis=self._stored_cost_basis(),
            ledger_seeded=bool(self._bank_seeded),
            ledger_saved_today_gbp=float(self._saved_today_gbp),
        )
        self._decision_snapshot = snapshot
        self._log_decision_summary(snapshot)
        return snapshot

    def _update_snapshot_ranges(
        self,
        *,
        charge_min_a: Optional[float] = None,
        charge_max_a: Optional[float] = None,
        charge_activation_w: Optional[float] = None,
        discharge_min_a: Optional[float] = None,
        discharge_max_a: Optional[float] = None,
        discharge_aggressive: Optional[bool] = None,
        discharge_activation_w: Optional[float] = None,
    ) -> None:
        """Fill in the actual EVSE ranges after ``set_home_demand_levels()``.

        ``get_control_state()`` records the intent; these values are what the
        Wallbox really receives.  Load-follow states override the min/max
        returned by ``get_control_state()``, so the dashboard must use these.
        """
        snapshot = self._decision_snapshot
        if snapshot is None:
            return
        if charge_min_a is not None:
            snapshot.charge_min_a = charge_min_a
        if charge_max_a is not None:
            snapshot.charge_max_a = charge_max_a
        if charge_activation_w is not None:
            snapshot.charge_activation_w = charge_activation_w
        if discharge_min_a is not None:
            snapshot.discharge_min_a = discharge_min_a
        if discharge_max_a is not None:
            snapshot.discharge_max_a = discharge_max_a
        if discharge_aggressive is not None:
            snapshot.discharge_aggressive = discharge_aggressive
        if discharge_activation_w is not None:
            snapshot.discharge_activation_w = discharge_activation_w

    def _format_saved_today(self) -> str:
        """Format the running saving with an explicit sign.

        Small totals keep four decimal places so a few pence of progress is
        visible between dashboard refreshes; larger totals stay at pennies.
        """
        value = self._saved_today_gbp
        decimals = 4 if abs(value) < 10 else 2
        return f"{'-' if value < 0 else ''}£{abs(value):.{decimals}f}"

    def _format_savings_rate(self) -> str:
        """Recent rate of change of the saved-today total, or an empty string.

        The rate is hidden while the total is effectively flat, so the
        indicator only appears when there is something to watch.
        """
        rate_p_per_hour = self._savings_rate_gbp_per_hour * 100.0
        if abs(rate_p_per_hour) < 0.05:
            return ""
        arrow = "↑" if rate_p_per_hour > 0 else "↓"
        return f"{arrow}{abs(rate_p_per_hour):.1f}p/h"

    def _stored_cost_basis(self) -> str:
        """Where the stored-energy cost comes from, for the dashboard tooltip."""
        if self._bank_dc_kwh >= self.MIN_LEDGER_BASIS_KWH:
            return f"ledger of {self._bank_dc_kwh:.1f} kWh"
        return "off-peak fallback while the ledger is empty"

    def _activation_label(self, activation_w: float) -> Optional[str]:
        """Compact dashboard label for a discharge activation threshold.

        A zero threshold is omitted because the aggressive reason already says
        that any import starts discharge.
        """
        if activation_w >= self.NEVER_DISCHARGE_ACTIVATION_W:
            return "discharge off"
        if activation_w <= 0:
            return None
        return f"discharge ≥{activation_w:.0f} W"

    def _format_stored_cost(self) -> str:
        """Stored energy cost in the pack and across the discharge range."""
        low_p, high_p = self._delivered_cost_range_per_kwh_p()
        if high_p - low_p < 0.05:
            delivered = f"{high_p:.1f}p delivered"
        else:
            delivered = f"{low_p:.1f}–{high_p:.1f}p delivered"
        return (
            f"stored {self._stored_cost_per_dc_kwh_p():.1f}p/kWh "
            f"({delivered})"
        )

    def _decision_summary_text(self, snapshot) -> str:
        """Plain-text version of the dashboard decision line, for the log."""
        parts = [snapshot.mode]
        if snapshot.reason:
            parts.append(snapshot.reason)

        if snapshot.efficiency_pct is not None:
            label = snapshot.efficiency_label or "efficiency"
            parts.append(f"η {snapshot.efficiency_pct:.0f}% {label}")

        if snapshot.min_gain_p is not None:
            parts.append(f"min gain {snapshot.min_gain_p:.1f}p/kWh")

        if snapshot.harvest_current_a is not None:
            parts.append(f"harvest {snapshot.harvest_current_a:.0f} A")

        if snapshot.discharge_activation_w is not None:
            activation_label = self._activation_label(
                snapshot.discharge_activation_w
            )
            if activation_label is not None:
                parts.append(activation_label)

        parts.append(self._format_stored_cost())
        saved = self._format_saved_today()
        rate = self._format_savings_rate()
        if rate:
            saved = f"{saved} {rate}"
        parts.append(f"saved today {saved}")
        return " | ".join(parts)

    def _log_decision_summary(self, snapshot) -> None:
        """Write the decision line once per minute at INFO."""
        now = self.get_current_datetime()
        minute_key = (now.date(), now.hour, now.minute)
        if self._last_info_log_minute == minute_key:
            return
        self._last_info_log_minute = minute_key
        info(f"IOCTGO_AGILEOUT: Decision: {self._decision_summary_text(snapshot)}")

    def _mode_line_html(self) -> str:
        """One-line mode/reason summary, wrapping on narrow screens."""
        snapshot = self._decision_snapshot
        if snapshot is None:
            return self.DECISION_LINE_CSS + (
                '<div class="ioctgo-decision-line">'
                '<span class="ioctgo-metric">Waiting for the first control cycle…</span>'
                "</div>"
            )
        age_seconds = (
            self.get_current_datetime() - snapshot.updated_at
        ).total_seconds()
        if age_seconds > 120:
            return self.DECISION_LINE_CSS + (
                '<div class="ioctgo-decision-line">'
                '<span class="ioctgo-metric">Waiting for the next control '
                "cycle…</span>"
                "</div>"
            )

        parts = [
            f'<span class="ioctgo-mode">{html_module.escape(snapshot.mode)}</span>'
        ]
        if snapshot.reason:
            parts.append(
                f'<span class="ioctgo-reason">{html_module.escape(snapshot.reason)}</span>'
            )

        if snapshot.efficiency_pct is not None:
            label = html_module.escape(snapshot.efficiency_label or "efficiency")
            worst_case = self.EFFICIENCY_MODEL.round_trip_efficiency(
                config.WALLBOX_MIN_CHARGE_CURRENT,
                config.WALLBOX_MIN_DISCHARGE_CURRENT,
            )
            title = html_module.escape(
                f"Measured {label} efficiency used for this decision. "
                f"The model varies with current; the {config.WALLBOX_MIN_CHARGE_CURRENT:.0f} A"
                f"/{config.WALLBOX_MIN_DISCHARGE_CURRENT:.0f} A worst case is "
                f"{worst_case:.0%}.",
                quote=True,
            )
            parts.append(
                f'<span class="ioctgo-metric" title="{title}">'
                f"η {snapshot.efficiency_pct:.0f}% {label}</span>"
            )

        if snapshot.min_gain_p is not None:
            title = html_module.escape(
                "Minimum profit required per kWh delivered: "
                f"{snapshot.min_gain_p:.1f}p/kWh. This is the configured "
                "policy spread, applied to the future price before the "
                "round-trip efficiency, so a later sale must beat the "
                "current price by at least this much. It is not the "
                "efficiency-adjusted break-even spread: covering the "
                "round-trip loss and earning this margin needs "
                f"{self.minimum_round_trip_saving_grid_p():.1f}p/kWh on the "
                f"input price for grid charging at {config.IMPORT_LOW:.1f}p, "
                f"and {self.minimum_round_trip_saving_solar_p():.1f}p/kWh "
                "for free solar.",
                quote=True,
            )
            parts.append(
                f'<span class="ioctgo-metric" title="{title}">'
                f"min gain {snapshot.min_gain_p:.1f}p/kWh</span>"
            )

        if snapshot.discharge_activation_w is not None:
            activation_label = self._activation_label(
                snapshot.discharge_activation_w
            )
            if activation_label is not None:
                title = html_module.escape(
                    "The house import at which discharging starts. A "
                    "load-following discharge cannot go below the Wallbox "
                    f"minimum ({config.WALLBOX_MIN_DISCHARGE_CURRENT:.0f} A, "
                    f"~{config.WALLBOX_MIN_DISCHARGE_CURRENT * 240:.0f} W), so "
                    "starting for a smaller load would export the difference at "
                    "the current Agile price. The threshold is the break-even "
                    "point: at the "
                    f"{config.WALLBOX_MIN_DISCHARGE_CURRENT:.0f} A minimum, "
                    f"stored energy costs "
                    f"{self._delivered_cost_per_kwh_p():.1f}p/kWh delivered "
                    f"plus a {self.MIN_PROFIT_MARGIN_P_PER_KWH:.1f}p "
                    "margin. It rises when stored energy was expensive and falls "
                    "when the export price it displaces is high.",
                    quote=True,
                )
                parts.append(
                    f'<span class="ioctgo-metric" title="{title}">'
                    f"{activation_label}</span>"
                )

        if snapshot.charge_min_a is not None:
            charge_floor_a = float(snapshot.charge_min_a)
            if charge_floor_a <= 0:
                charge_label = "solar off"
                charge_title = html_module.escape(
                    "No surplus solar is accepted: the pack is at its charge "
                    "cap, this slot is committed to export, or selling the "
                    "surplus now is worth more than storing it.",
                    quote=True,
                )
            else:
                activation_w = snapshot.charge_activation_w
                if activation_w is None:
                    activation_w = self._charge_activation_w_for_current(
                        charge_floor_a
                    )
                max_a = snapshot.charge_max_a
                max_text = (
                    f", up to {float(max_a):.0f} A" if max_a else ""
                )
                charge_label = (
                    f"solar ≥{charge_floor_a:.0f} A "
                    f"(≈{activation_w:.0f} W)"
                )
                charge_title = html_module.escape(
                    "Surplus solar starts charging at "
                    f"{charge_floor_a:.0f} A (≈{activation_w:.0f} W)"
                    f"{max_text}. Below that the surplus is exported or left "
                    "unused; the car only takes what the roof provides.",
                    quote=True,
                )
            parts.append(
                f'<span class="ioctgo-metric" title="{charge_title}">'
                f"{charge_label}</span>"
            )

        low_p, high_p = self._delivered_cost_range_per_kwh_p()
        stored_title = html_module.escape(
            "Average cost of the energy in the pack: "
            f"{self._stored_cost_per_dc_kwh_p():.1f}p/kWh in the pack. "
            "Discharging it costs "
            f"{low_p:.1f}–{high_p:.1f}p/kWh delivered, depending on the "
            "discharge current: the 3 A minimum is the upper end, maximum "
            "current the lower end. The in-pack figure is the import cost paid "
            "plus any Agile export value forgone when the energy was stored, "
            "divided by the measured charge efficiency. The delivered figure "
            "divides that by the measured discharge efficiency. The activation "
            "threshold uses the 3 A minimum because a load-following discharge "
            "starts at the minimum output. Energy already in the pack when the "
            "controller starts is valued at the off-peak replacement price, "
            "not zero, so a restart does not make it look free. The threshold "
            "uses the delivered figure plus the "
            f"{self.MIN_PROFIT_MARGIN_P_PER_KWH:.1f}p/kWh margin. Basis: "
            f"{self._stored_cost_basis()}.",
            quote=True,
        )
        parts.append(
            f'<span class="ioctgo-metric" title="{stored_title}">'
            f"{self._format_stored_cost()}</span>"
        )

        saved = self._format_saved_today()
        rate = self._format_savings_rate()
        saved_class = "ioctgo-saved" if self._saved_today_gbp >= 0 else "ioctgo-lost"
        title = html_module.escape(
            "Money saved today versus not cycling the battery. Each discharge "
            "earns the import price it avoids (or the Agile export price for "
            "energy sent to the grid), less what that energy cost to store, "
            "including round-trip losses. Solar is costed at the Agile export "
            "price it displaced when stored, so a negative figure means bought "
            "energy was sold for less than it cost, or stored solar was sold "
            "for less than the export it displaced (forgone revenue, not a "
            "running cost). A charge is only counted when the energy is used. "
            "Resets at midnight. The arrow shows the recent rate of change "
            "over the last couple of minutes and is hidden while the total "
            "is flat.",
            quote=True,
        )
        rate_html = (
            f'<span class="ioctgo-rate"> {rate}</span>' if rate else ""
        )
        parts.append(
            f'<span class="{saved_class}" title="{title}">'
            f"saved today {saved}{rate_html}</span>"
        )
        parts.append(
            '<a class="ioctgo-explain" href="/decision" '
            'title="Open the frozen calculation behind this decision">'
            "Explanation →</a>"
        )

        detail_attr = ""
        if snapshot.detail:
            detail_attr = f' title="{html_module.escape(snapshot.detail, quote=True)}"'
        return self.DECISION_LINE_CSS + (
            f'<div class="ioctgo-decision-line"{detail_attr}>'
            + "".join(parts)
            + "</div>"
        )

    def _ledger_explanation(self, snap: DecisionSnapshot) -> dict:
        """Explain the stored-energy ledger that priced this decision.

        The snapshot captures the ledger at the decision instant, so the
        figures here match ``stored_cost_p``.  Older snapshots fall back to the
        live ledger and say so.
        """
        timing = "decision time" if snap.ledger_dc_kwh is not None else "page load"
        dc_kwh = (
            snap.ledger_dc_kwh
            if snap.ledger_dc_kwh is not None
            else float(self._bank_dc_kwh)
        )
        cost_gbp = (
            snap.ledger_cost_gbp
            if snap.ledger_cost_gbp is not None
            else float(self._bank_cost_gbp)
        )
        stored_p = (
            snap.ledger_stored_cost_p
            if snap.ledger_stored_cost_p is not None
            else self._stored_cost_per_dc_kwh_p()
        )
        delivered_p = (
            snap.ledger_delivered_cost_p
            if snap.ledger_delivered_cost_p is not None
            else self._delivered_cost_per_kwh_p()
        )
        basis = snap.ledger_basis or self._stored_cost_basis()
        seeded = snap.ledger_seeded
        saved_today = (
            snap.ledger_saved_today_gbp
            if snap.ledger_saved_today_gbp is not None
            else float(self._saved_today_gbp)
        )
        min_basis = self.MIN_LEDGER_BASIS_KWH
        trusted = dc_kwh >= min_basis
        fallback_p = self._bank_fallback_cost_per_dc_kwh() * 100.0
        min_discharge_a = float(self.MIN_DISCHARGE_CURRENT)
        max_discharge_a = float(self.MAX_DISCHARGE_CURRENT)
        discharge_at_min = self.EFFICIENCY_MODEL.discharge_efficiency(
            min_discharge_a
        )
        discharge_at_max = self.EFFICIENCY_MODEL.discharge_efficiency(
            max_discharge_a
        )
        delivered_at_max = stored_p / max(discharge_at_max, 1e-6)
        charge_reference_a = float(self.GRID_CHARGE_REFERENCE_CURRENT)
        charge_reference_eff = self.EFFICIENCY_MODEL.charge_efficiency(
            charge_reference_a
        )

        def money(value: float) -> str:
            sign = "-" if value < 0 else ""
            return f"{sign}£{abs(value):.4f}"

        rows = [
            {
                "label": "Energy in the ledger (DC)",
                "value": f"{dc_kwh:.2f} kWh",
            },
            {
                "label": "Cost booked",
                "value": (
                    f"£{cost_gbp:.4f}"
                    if abs(cost_gbp) < 10
                    else f"£{cost_gbp:.2f}"
                ),
            },
            {
                "label": "Stored-energy cost (in pack)",
                "value": f"{stored_p:.1f}p/kWh",
            },
            {
                "label": f"Delivered at {min_discharge_a:.0f} A",
                "value": (
                    f"{delivered_p:.1f}p/kWh "
                    f"({discharge_at_min:.0%} discharge)"
                ),
            },
            {
                "label": f"Delivered at {max_discharge_a:.0f} A",
                "value": (
                    f"{delivered_at_max:.1f}p/kWh "
                    f"({discharge_at_max:.0%} discharge)"
                ),
            },
            {
                "label": "Basis",
                "value": (
                    basis
                    if trusted
                    else (
                        f"Off-peak fallback — ledger {dc_kwh:.2f} kWh is "
                        f"below the {min_basis:.2f} kWh trust floor"
                    )
                ),
            },
            {
                "label": "Seeded from SoC at startup",
                "value": "Yes" if seeded else "No",
            },
            {
                "label": "Saved today",
                "value": money(saved_today),
            },
            {
                "label": "SOC capacity used",
                "value": f"{self.SOC_CAPACITY_KWH:.2f} kWh",
            },
        ]

        steps = [
            {
                "title": "What is in the ledger?",
                "question": (
                    "The ledger holds energy that has passed through the "
                    "EVSE in DC pack terms, and what that energy cost."
                ),
                "calculation": (
                    f"{cost_gbp:.4f} ÷ {dc_kwh:.2f} kWh = "
                    f"{stored_p:.1f}p/kWh in the pack."
                    if trusted and dc_kwh > 0
                    else (
                        f"The ledger is below the {min_basis:.2f} kWh trust "
                        f"floor, so the off-peak fallback "
                        f"({config.IMPORT_LOW:.1f}p/kWh ÷ "
                        f"{charge_reference_eff:.0%} charge efficiency at "
                        f"{charge_reference_a:.0f} A) gives "
                        f"{fallback_p:.1f}p/kWh in the pack."
                    )
                ),
                "result": (
                    f"{dc_kwh:.2f} kWh at {stored_p:.1f}p/kWh in the pack."
                ),
                "conclusion": (
                    "This is the cost basis for every discharge decision. "
                    "It is an average, so older and newer energy blend."
                ),
            },
            {
                "title": "How does charging add to the ledger?",
                "question": (
                    "Charging adds DC energy and the money paid for it; "
                    "surplus solar is charged at the export revenue it "
                    "displaced."
                ),
                "calculation": (
                    "DC added = AC energy × charge efficiency at the actual "
                    "current. Cost added = grid kWh × import price + solar "
                    "kWh × export price. Solar is capped at the live surplus; "
                    "only the rest is paid import."
                ),
                "result": (
                    f"At the {charge_reference_a:.0f} A grid-charge reference "
                    f"the model stores {charge_reference_eff:.0%} of AC "
                    f"energy."
                ),
                "conclusion": (
                    "A charge does not create a saving yet. The saving is "
                    "booked when the energy is discharged."
                ),
            },
            {
                "title": "How does discharging release it?",
                "question": (
                    "Discharging drains DC energy and removes the matching "
                    "share of the ledger cost."
                ),
                "calculation": (
                    f"DC drained = AC delivered ÷ discharge efficiency. "
                    f"Released cost = DC drained × {stored_p:.1f}p/kWh. "
                    f"The delivered cost is the in-pack cost divided by the "
                    f"discharge efficiency."
                ),
                "result": (
                    f"At {min_discharge_a:.0f} A: {stored_p:.1f}p/kWh ÷ "
                    f"{discharge_at_min:.0%} = {delivered_p:.1f}p/kWh "
                    f"delivered. At {max_discharge_a:.0f} A: "
                    f"{delivered_at_max:.1f}p/kWh."
                ),
                "conclusion": (
                    "The same stored energy is worth less at low current "
                    "because a larger share is lost on the way out. That is "
                    "why the discharge break-even uses the 3 A figure."
                ),
            },
            {
                "title": "What happens when the ledger is empty or too small?",
                "question": (
                    "After a restart the pack may hold charge bought before "
                    "the ledger existed, or the ledger may be only metering "
                    "noise."
                ),
                "calculation": (
                    f"Below {min_basis:.2f} kWh the ledger is not trusted as "
                    f"a price basis. Existing charge is seeded at the "
                    f"off-peak replacement price: {config.IMPORT_LOW:.1f}p/kWh "
                    f"÷ {charge_reference_eff:.0%} = {fallback_p:.1f}p/kWh DC."
                ),
                "result": (
                    f"Fallback in-pack cost = {fallback_p:.1f}p/kWh."
                ),
                "conclusion": (
                    "This keeps the estimate conservative rather than "
                    "reporting free energy, and a tiny sample cannot become "
                    "the price of the whole pack."
                ),
            },
            {
                "title": "What does 'saved today' mean?",
                "question": (
                    "The saving is measured against not cycling the battery, "
                    "not against the price of the hardware."
                ),
                "calculation": (
                    "On discharge: avoided import (or export revenue) minus "
                    "the released ledger cost. The daily total resets at "
                    "local midnight; the ledger itself carries over."
                ),
                "result": f"Saved today: {money(saved_today)}.",
                "conclusion": (
                    "A negative figure is a real loss or forgone export "
                    "revenue, not a capital-cost estimate."
                ),
            },
        ]

        return {
            "timing": timing,
            "rows": rows,
            "steps": steps,
        }

    def _efficiency_explanation(self) -> dict:
        """Build the current-dependent efficiency table for the page.

        Rows are charge currents (AC → pack), columns are discharge currents
        (pack → AC), and each cell is the product.  Header cells show the
        single-direction efficiency at that current.
        """
        model = self.EFFICIENCY_MODEL
        min_charge = max(1, int(round(config.WALLBOX_MIN_CHARGE_CURRENT)))
        max_charge = max(
            min_charge, int(round(config.WALLBOX_MAX_CHARGE_CURRENT))
        )
        min_discharge = max(1, int(round(config.WALLBOX_MIN_DISCHARGE_CURRENT)))
        max_discharge = max(
            min_discharge, int(round(config.WALLBOX_MAX_DISCHARGE_CURRENT))
        )
        charge_reference = round(self.GRID_CHARGE_REFERENCE_CURRENT)
        export_reference = round(self.EXPORT_REFERENCE_CURRENT)

        charge_rows = []
        for current in range(min_charge, max_charge + 1):
            efficiency = model.charge_efficiency(float(current))
            charge_rows.append(
                {
                    "current": f"{current} A",
                    "efficiency": f"{efficiency * 100:.0f}%",
                    "is_reference": current == charge_reference,
                }
            )

        discharge_columns = []
        for current in range(min_discharge, max_discharge + 1):
            efficiency = model.discharge_efficiency(float(current))
            discharge_columns.append(
                {
                    "current": f"{current} A",
                    "efficiency": f"{efficiency * 100:.0f}%",
                    "is_reference": current == export_reference,
                }
            )

        matrix = []
        for charge_current in range(min_charge, max_charge + 1):
            row = []
            charge_eff = model.charge_efficiency(float(charge_current))
            for discharge_current in range(min_discharge, max_discharge + 1):
                discharge_eff = model.discharge_efficiency(
                    float(discharge_current)
                )
                round_trip = charge_eff * discharge_eff
                pct = round_trip * 100.0
                if pct < 50:
                    band = "low"
                elif pct < 65:
                    band = "mid"
                elif pct < 75:
                    band = "good"
                else:
                    band = "high"
                row.append(
                    {
                        "value": f"{pct:.0f}%",
                        "exact": (
                            f"{charge_current} A × {discharge_current} A: "
                            f"{charge_eff * 100:.1f}% × "
                            f"{discharge_eff * 100:.1f}% = {pct:.1f}%"
                        ),
                        "band": band,
                        "diagonal": charge_current == discharge_current,
                    }
                )
            matrix.append(row)

        measured_max = model.max_current_a
        configured_max = max(max_charge, max_discharge)
        note = (
            f"The model is measured from {model.min_current_a:.0f} A to "
            f"{measured_max:.0f} A. The table follows the configured limits "
            f"({max_charge} A charge, {max_discharge} A discharge)."
        )
        if configured_max > measured_max:
            note += (
                f" Above {measured_max:.0f} A the efficiency is held at the "
                f"{measured_max:.0f} A measurement; AC power still scales "
                "with current, but the extra cells are planning values, not "
                "new measurements."
            )

        return {
            "charge_rows": charge_rows,
            "discharge_columns": discharge_columns,
            "matrix": matrix,
            "note": note,
            "measured_min_a": model.min_current_a,
            "measured_max_a": measured_max,
            "max_charge_a": max_charge,
            "max_discharge_a": max_discharge,
            "charge_reference_a": charge_reference,
            "export_reference_a": export_reference,
        }

    def _plan_explanation(self, snap: DecisionSnapshot) -> dict:
        """Build the remaining-day SoC projection and export-option tables.

        The rows use the same per-slot model as
        :meth:`_project_soc_at_2330`, including the committed export plan, so
        the number at the bottom of the table is the number the controller
        used.  Every future slot carries enough context to answer "why not?"
        when it qualifies on price but the plan leaves it alone.
        """
        start_slot = snap.updated_at.hour * 2 + (
            1 if snap.updated_at.minute >= 30 else 0
        )
        current_soc = (
            float(snap.soc_percent)
            if snap.soc_percent is not None and snap.soc_percent >= 0
            else None
        )

        free_import_slots = self._free_import_slots_today()
        grid_support_slots = self._grid_support_slots_today()
        away_slots = self._away_slots_today()
        planned_export_slots = set(
            snap.planned_export_slots
            if snap.planned_export_slots is not None
            else self._planned_export_slots
        )
        planned_slot_actions = (
            dict(snap.planned_slot_actions)
            if snap.planned_slot_actions is not None
            else None
        )
        if planned_slot_actions is not None:
            planned_export_slots = {
                slot
                for slot, action in planned_slot_actions.items()
                if action == "export"
            }
        exported_slots = set(self._exported_slots)
        priority_slots = self._export_priority_slots(start_slot)
        planned_priority_slots = planned_export_slots & priority_slots

        rate_by_slot: dict[int, float] = {}
        for rate_data in self._today_rates():
            slot = rate_data['start'].hour * 2 + (
                1 if rate_data['start'].minute == 30 else 0
            )
            rate_by_slot[slot] = float(rate_data['rate'])

        bulk_target_soc = float(config.MIN_AGILE_DISCHARGE_SOC)
        absolute_min_soc = self.absolute_min_soc_percent()
        target_soc = max(bulk_target_soc, absolute_min_soc)
        house_floor_soc = self._house_load_floor_soc()
        export_loss = self._export_slot_soc_loss_percent()
        non_export_loss = self.NON_EXPORT_SLOT_SOC_LOSS_PERCENT
        full_charge_gain = self._full_charge_slot_soc_gain_percent()
        charge_cap = self.charge_cap_percent()
        slot_export_kwh = self.EXPORT_POWER_KW * 0.5
        aggressive_export_kwh = self._aggressive_export_kwh()
        aggressive_soc_loss = self._aggressive_slot_soc_loss_percent()
        min_export_rate_p = self.export_min_rate_p()
        default_break_even_p = self.export_house_break_even_p(config.IMPORT_HIGH)

        if current_soc is None:
            baseline_projection = None
            plan_projection = None
            headroom = None
            priority_headroom = None
        elif planned_slot_actions is not None:
            # Stage-1 plan: the budget is measured against a day with no
            # discretionary house load, using the running minimum so a later
            # charge cannot hide an earlier dip.
            baseline_projection = self._project_soc_at_2330(
                current_soc,
                start_slot,
                slot_actions={},
            )
            baseline_minimum = self._minimum_projected_soc(
                current_soc,
                start_slot,
                slot_actions={},
            )
            plan_projection = self._project_soc_at_2330(
                current_soc,
                start_slot,
                slot_actions=planned_slot_actions,
            )
            headroom = baseline_minimum - target_soc
            priority_headroom = baseline_minimum - absolute_min_soc
        else:
            baseline_projection = self._project_soc_at_2330(
                current_soc, start_slot
            )
            plan_projection = self._project_soc_at_2330(
                current_soc,
                start_slot,
                export_slots=planned_export_slots,
            )
            headroom = baseline_projection - target_soc
            # High-value slots may spend below the bulk target, down to the
            # hard absolute minimum, so their budget is wider.
            priority_headroom = baseline_projection - absolute_min_soc

        rows: list[dict] = []
        projected = current_soc
        for slot in range(start_slot, 47):
            delta, change_kind = self._slot_soc_delta(
                slot,
                projected if projected is not None else 0.0,
                free_import_slots=free_import_slots,
                grid_support_slots=grid_support_slots,
                away_slots=away_slots,
                planned_export_slots=planned_export_slots,
                charge_cap=charge_cap,
                full_charge_gain=full_charge_gain,
                export_loss=export_loss,
                non_export_loss=non_export_loss,
                slot_actions=planned_slot_actions,
            )
            if projected is not None:
                projected += delta

            rate = rate_by_slot.get(slot)
            import_price_p = self._current_import_rate_p(slot * 30)
            break_even_p = self.export_house_break_even_p(import_price_p)
            qualifies = slot in priority_slots
            planned = slot in planned_export_slots
            exported = slot in exported_slots
            off_peak = self.is_off_peak(slot * 30)
            aggressive_gate = None

            revenue_gbp = None
            gain_gbp = None
            gain_p_per_kwh = None
            if (
                change_kind in (
                    "load_follow",
                    "planned_export",
                    "aggressive",
                    "idle",
                )
                and not off_peak
                and rate is not None
            ):
                export_kwh = (
                    aggressive_export_kwh
                    if change_kind == "aggressive"
                    else slot_export_kwh
                )
                gain_p_per_kwh = rate - break_even_p
                revenue_gbp = rate * export_kwh / 100.0
                gain_gbp = gain_p_per_kwh * export_kwh / 100.0

            if change_kind == "away":
                status = "away"
                status_label = "EV AWAY"
                status_class = "status-session"
                reason = (
                    "EV planned away — the pack neither supplies nor charges, "
                    "and the slot is not an export target."
                )
            elif change_kind == "free_import":
                status = "free_import"
                status_label = "FREE IMPORT"
                status_class = "status-session"
                if delta <= 0:
                    reason = (
                        "Free-import session — the pack is already at the "
                        f"{charge_cap:.0f}% charge cap."
                    )
                else:
                    reason = (
                        "Free-import session — the pack charges at full rate. "
                        "The Wallbox cannot import and export at the same time."
                    )
            elif change_kind == "grid_support":
                status = "grid_support"
                status_label = "GRID SUPPORT"
                status_class = "status-session"
                reason = (
                    "Grid-support session — already exports at maximum. It is "
                    "excluded from the export plan so its drain is not counted "
                    "twice."
                )
            elif off_peak:
                status = "off_peak"
                status_label = "OFF-PEAK"
                status_class = "status-session"
                reason = (
                    "Off-peak charging window — the car is charging and the "
                    "slot is not available for export."
                )
            elif rate is None:
                status = "no_price"
                status_label = "NO PRICE"
                status_class = "status-normal"
                reason = "No export price is available for this slot."
            elif exported:
                status = "exported"
                status_label = "EXPORTED"
                status_class = "status-exported"
                reason = "The controller exported in this slot."
            elif planned:
                status = "planned"
                status_label = "PLANNED"
                status_class = "status-planned"
                if qualifies:
                    reason = (
                        f"Planned — {rate:.1f}p is "
                        f"{rate - break_even_p:.1f}p above the "
                        f"{break_even_p:.1f}p house-offset break-even."
                    )
                else:
                    reason = (
                        f"Planned — above the {min_export_rate_p:.1f}p export "
                        "minimum, using spare headroom after the priority slots."
                    )
            elif change_kind == "aggressive":
                status = "aggressive"
                status_label = "AGGRESSIVE"
                status_class = "status-normal"
                gate = self._aggressive_slot_gates.get(slot)
                if gate is None:
                    gate = self._aggressive_slot_gate(slot)
                aggressive_gate = gate
                activation_w = gate["activation_w"]
                activation_a = gate.get("activation_current_a")
                if activation_w >= self.NEVER_DISCHARGE_ACTIVATION_W:
                    gate_text = (
                        "The live gate is closed: even the lowest output "
                        "cannot repay the stored energy's best alternative "
                        f"use ({gate['hurdle_p']:.1f}p/kWh delivered), so "
                        "this slot will idle."
                    )
                elif activation_w <= 0:
                    gate_text = (
                        "It starts on any grid import because the "
                        f"{rate:.1f}p export alone clears the "
                        f"{gate['hurdle_p']:.1f}p/kWh stored-energy hurdle "
                        "(best alternative use or delivered cost, whichever "
                        "is higher, plus the profit margin)."
                    )
                else:
                    current_text = (
                        f" ({activation_a:.0f} A)"
                        if activation_a is not None
                        else ""
                    )
                    gate_text = (
                        f"It starts only when the house imports at least "
                        f"{activation_w:.0f} W{current_text}; below that the "
                        "forced export would cost more than it saves. The "
                        f"{gate['hurdle_p']:.1f}p/kWh hurdle is the higher "
                        "of the energy's best alternative use and its "
                        "delivered cost, plus the profit margin."
                    )
                reason = (
                    f"Aggressive load-following — if the house load reaches "
                    f"the gate, it covers the house and exports the surplus "
                    f"at {rate:.1f}p. The extra "
                    f"{self._aggressive_extra_soc_loss_percent():.2f}% of SoC "
                    f"sells about {aggressive_export_kwh:.3f} kWh this slot. "
                    f"{gate_text}"
                )
                if not qualifies:
                    reason += (
                        f" It is below the {break_even_p:.1f}p house-offset "
                        "break-even, so another conservative slot would "
                        "normally be worth more."
                    )
            elif change_kind == "load_follow" and planned_slot_actions is not None:
                status = "conservative"
                status_label = "CONSERVATIVE"
                status_class = "status-normal"
                reason = (
                    "Conservative house coverage — about 3 A, using "
                    f"{non_export_loss:.1f}% of SoC this slot."
                )
                if qualifies:
                    reason += (
                        f" Its {rate:.1f}p export price qualifies, but the "
                        "remaining budget went to higher-priced slots."
                    )
            elif change_kind == "idle":
                if qualifies:
                    status = "skipped"
                    status_label = "QUALIFIES"
                    status_class = "status-skipped"
                    if priority_headroom is None:
                        reason = (
                            "Qualifies on price, but the battery state of "
                            "charge is unknown, so no headroom can be "
                            "committed."
                        )
                    elif priority_headroom <= 0:
                        reason = (
                            f"Qualifies at {rate:.1f}p, but the no-load "
                            f"baseline already reaches {baseline_projection:.0f}% "
                            f"at 23:30, at or below the "
                            f"{absolute_min_soc:.0f}% absolute minimum, so "
                            "there is no headroom."
                        )
                    else:
                        reason = (
                            f"Qualifies at {rate:.1f}p, but the "
                            f"{priority_headroom:.1f}% headroom above the "
                            f"{absolute_min_soc:.0f}% absolute minimum was "
                            "used by higher-priced slots."
                        )
                else:
                    status = "idle"
                    status_label = "IDLE"
                    status_class = "status-normal"
                    if rate < min_export_rate_p:
                        reason = (
                            f"Left idle — {rate:.1f}p is below the "
                            f"{min_export_rate_p:.1f}p export minimum; the "
                            "house imports in this slot."
                        )
                    else:
                        reason = (
                            f"Left idle to protect the "
                            f"{absolute_min_soc:.0f}% floor — {rate:.1f}p is "
                            f"below the {break_even_p:.1f}p house-offset "
                            "break-even, and the remaining budget went to "
                            "higher-value export slots. The house imports."
                        )
            elif qualifies:
                status = "skipped"
                status_label = "QUALIFIES"
                status_class = "status-skipped"
                if priority_headroom is None:
                    reason = (
                        "Qualifies on price, but the battery state of charge "
                        "is unknown, so no headroom can be committed."
                    )
                elif priority_headroom <= 0:
                    reason = (
                        f"Qualifies at {rate:.1f}p, but the session-aware "
                        f"23:30 projection ({baseline_projection:.0f}%) is at "
                        f"or below the {absolute_min_soc:.0f}% absolute "
                        "minimum, so there is no headroom."
                    )
                else:
                    reason = (
                        f"Qualifies at {rate:.1f}p, but the "
                        f"{priority_headroom:.1f}% headroom above the "
                        f"{absolute_min_soc:.0f}% absolute minimum was used by "
                        "higher-priced slots."
                    )
            else:
                status = "not_selected"
                status_label = "NOT SELECTED"
                status_class = "status-normal"
                if rate < min_export_rate_p:
                    reason = (
                        f"Below the {min_export_rate_p:.1f}p export minimum."
                    )
                elif headroom is None:
                    reason = "The battery state of charge is unknown."
                elif headroom <= 0:
                    if planned_priority_slots:
                        reason = (
                            f"Not selected — the high-value priority slots used "
                            f"the reserve below the {target_soc:.0f}% target."
                        )
                    else:
                        reason = (
                            f"Not selected — the 23:30 projection is at or "
                            f"below the {target_soc:.0f}% export floor."
                        )
                else:
                    reason = (
                        f"{rate:.1f}p is below the {break_even_p:.1f}p "
                        "house-offset break-even; the spare headroom went to "
                        "higher-priced priority slots."
                    )

            rows.append(
                {
                    "slot": slot,
                    "time": self._slot_range_label(slot),
                    "kind": change_kind,
                    "kind_label": {
                        "away": "EV away",
                        "free_import": "Free import",
                        "grid_support": "Grid support",
                        "planned_export": "Export",
                        "aggressive": "Aggressive",
                        "load_follow": (
                            "Conservative"
                            if planned_slot_actions is not None
                            else "Normal"
                        ),
                        "idle": "Idle",
                    }.get(change_kind, change_kind),
                    "status": status,
                    "status_label": status_label,
                    "status_class": status_class,
                    "export_price_p": rate,
                    "import_price_p": import_price_p,
                    "break_even_p": break_even_p,
                    "aggressive_activation_w": (
                        aggressive_gate["activation_w"]
                        if aggressive_gate is not None
                        else None
                    ),
                    "aggressive_hurdle_p": (
                        aggressive_gate["hurdle_p"]
                        if aggressive_gate is not None
                        else None
                    ),
                    "qualifies": qualifies,
                    "planned": planned,
                    "exported": exported,
                    "off_peak": off_peak,
                    "soc_delta": delta,
                    "projected_soc_after": projected,
                    "revenue_gbp": revenue_gbp,
                    "gain_gbp": gain_gbp,
                    "gain_p_per_kwh": gain_p_per_kwh,
                    "reason": reason,
                }
            )

        planned_rows = [row for row in rows if row["planned"]]
        aggressive_rows = [
            row for row in rows if row["kind"] == "aggressive"
        ]
        skipped_qualifying = [
            row
            for row in rows
            if row["qualifies"]
            and not row["planned"]
            and row["status"] == "skipped"
        ]
        options = sorted(
            [
                row
                for row in rows
                if row["kind"] in (
                    "load_follow",
                    "planned_export",
                    "aggressive",
                    "idle",
                )
                and not row["off_peak"]
                and row["export_price_p"] is not None
            ],
            key=lambda row: (-row["export_price_p"], row["slot"]),
        )

        additional_cost = export_loss

        if current_soc is None:
            diagnosis = (
                "The battery state of charge is unknown, so the controller "
                "cannot calculate a 23:30 projection or commit export headroom."
            )
        elif start_slot >= 47:
            diagnosis = (
                "No export slots remain today: the export window closes at "
                "23:30 (slot 47)."
            )
        elif not rate_by_slot:
            diagnosis = (
                "No Agile Outgoing prices are loaded, so the planner has no "
                "profitable slots to choose from."
            )
        elif not planned_rows:
            if aggressive_rows:
                diagnosis = (
                    f"No full export slot fits, but {len(aggressive_rows)} "
                    "high-value slot(s) are planned for aggressive "
                    "load-following: if the house load reaches the gate, the "
                    "house is covered and the small surplus is exported. This "
                    f"spends {aggressive_soc_loss:.2f}% of SoC per slot "
                    f"instead of {export_loss:.1f}% for a bulk export."
                )
            elif priority_headroom is None:
                diagnosis = (
                    "No export is planned because the battery state of charge "
                    "is unknown."
                )
            elif priority_headroom <= 0:
                diagnosis = (
                    "No export is planned. With ordinary slots idle, the "
                    "weakest point before 23:30 is at or below the "
                    f"{absolute_min_soc:.0f}% absolute minimum, so there is no "
                    "headroom to spend."
                )
                if skipped_qualifying:
                    diagnosis += (
                        f" {len(skipped_qualifying)} slot(s) beat the "
                        f"{default_break_even_p:.1f}p house-offset break-even, "
                        "but exporting in them would breach the absolute "
                        "minimum."
                    )
            elif priority_headroom < additional_cost:
                diagnosis = (
                    f"No export is planned. The no-load baseline leaves "
                    f"{priority_headroom:.1f}% above the "
                    f"{absolute_min_soc:.0f}% absolute minimum, which is less "
                    f"than the {additional_cost:.1f}% cost of one export slot."
                )
                if skipped_qualifying:
                    diagnosis += (
                        f" {len(skipped_qualifying)} slot(s) beat the "
                        f"{default_break_even_p:.1f}p house-offset break-even."
                    )
            elif skipped_qualifying:
                diagnosis = (
                    f"No export is planned even though "
                    f"{priority_headroom:.1f}% is available above the "
                    f"{absolute_min_soc:.0f}% absolute minimum. "
                    f"{len(skipped_qualifying)} slot(s) beat the "
                    f"{default_break_even_p:.1f}p house-offset break-even, but "
                    "none could be fitted without crossing the floor."
                )
            else:
                diagnosis = (
                    f"No export is planned. The no-load baseline leaves "
                    f"{priority_headroom:.1f}% above the "
                    f"{absolute_min_soc:.0f}% absolute minimum, but no future "
                    f"slot reaches the {min_export_rate_p:.1f}p export minimum."
                )
        else:
            diagnosis = (
                f"{len(planned_rows)} export slot(s) planned. With ordinary "
                f"slots idle, the day ends at {baseline_projection:.0f}%; the "
                f"plan ends at {plan_projection:.0f}%. High-value slots may "
                f"spend down to the {absolute_min_soc:.0f}% absolute minimum; "
                f"ordinary slots stop at the {target_soc:.0f}% target, and "
                "the remaining budget is used for aggressive or conservative "
                "house coverage."
            )
            if aggressive_rows:
                diagnosis += (
                    f" {len(aggressive_rows)} high-value slot(s) are also "
                    "planned for aggressive load-following; they only run if "
                    "the live house import reaches their break-even."
                )
            if skipped_qualifying:
                diagnosis += (
                    f" {len(skipped_qualifying)} qualifying slot(s) are left "
                    "idle because the export budget was used by higher-priced "
                    "slots."
                )

        if any(row["kind"] == "idle" for row in rows):
            # Idle means "no battery discharge" in the plan, not "no solar".
            # Say so once for the table instead of repeating it in every row.
            # The charge floor is price-dependent, so do not imply a fixed 3 A.
            diagnosis += (
                " Idle rows stop battery discharge, not solar charging: "
                "surplus is still harvested when it beats exporting at the "
                "live price."
            )

        return {
            "start_slot": start_slot,
            "current_soc": current_soc,
            "baseline_projection": baseline_projection,
            "plan_projection": plan_projection,
            "target_soc": target_soc,
            "bulk_target_soc": bulk_target_soc,
            "house_floor_soc": house_floor_soc,
            "absolute_min_soc": absolute_min_soc,
            "headroom_soc": headroom,
            "priority_headroom_soc": priority_headroom,
            "planned_slots": sorted(
                slot for slot in planned_export_slots if start_slot <= slot < 47
            ),
            "aggressive_slots": sorted(
                row["slot"] for row in aggressive_rows
            ),
            "aggressive_activation_w": {
                row["slot"]: row["aggressive_activation_w"]
                for row in aggressive_rows
            },
            "aggressive_hurdle_p": {
                row["slot"]: row["aggressive_hurdle_p"]
                for row in aggressive_rows
            },
            "aggressive_conditional": bool(aggressive_rows),
            "qualifying_slots": sorted(priority_slots),
            "skipped_qualifying_slots": [
                row["slot"] for row in skipped_qualifying
            ],
            "slot_export_kwh": slot_export_kwh,
            "aggressive_export_kwh": aggressive_export_kwh,
            "aggressive_soc_loss_percent": aggressive_soc_loss,
            "aggressive_extra_soc_loss_percent": (
                self._aggressive_extra_soc_loss_percent()
            ),
            "export_soc_loss_percent": export_loss,
            "non_export_soc_loss_percent": non_export_loss,
            "full_charge_soc_gain_percent": full_charge_gain,
            "additional_export_soc_cost_percent": additional_cost,
            "min_export_rate_p": min_export_rate_p,
            "default_break_even_p": default_break_even_p,
            "selected_revenue_gbp": sum(
                row["revenue_gbp"] or 0.0
                for row in planned_rows + aggressive_rows
            ),
            "selected_gain_gbp": sum(
                row["gain_gbp"] or 0.0
                for row in planned_rows + aggressive_rows
            ),
            "diagnosis": diagnosis,
            "rows": rows,
            "options": options,
        }

    def get_decision_explanation(self) -> dict:
        """Return a frozen, plain-language explanation of the last decision.

        The dashboard calls this once when ``/decision`` is loaded.  The
        snapshot is deep-copied, so the explanation cannot change under the
        reader while they study it.  The numbers are taken from the snapshot
        wherever possible and from the same helpers the control loop used, so
        the page and the decision cannot disagree.
        """
        snapshot = self._decision_snapshot
        if snapshot is None:
            return {
                "available": False,
                "message": (
                    "No control decision has been recorded yet. The controller "
                    "writes one on its first control cycle; reload this page "
                    "after a few seconds."
                ),
            }

        snap = copy.deepcopy(snapshot)
        slot = snap.updated_at.hour * 2 + (1 if snap.updated_at.minute >= 30 else 0)
        slot_label = self._slot_range_label(slot)
        cap = (
            snap.charge_cap_percent
            if snap.charge_cap_percent is not None
            else self.charge_cap_percent()
        )
        import_p = (
            snap.import_rate_p
            if snap.import_rate_p is not None
            else (
                config.IMPORT_LOW
                if self.is_off_peak(snap.updated_at.hour * 60 + snap.updated_at.minute)
                else config.IMPORT_HIGH
            )
        )
        export_p = max(0.0, snap.current_rate_p or 0.0)
        if snap.decision_tier in (
            "free", "off_peak", "unknown_soc", "protection"
        ):
            # These branches store the import price (or nothing) in
            # ``current_rate_p``.  The explanation still needs the real export
            # price for the state table and the discharge comparison.
            export_p = max(0.0, self._current_export_rate_p(slot))
        target_p = snap.target_rate_p
        target_label = (
            self._slot_range_label(snap.target_slot)
            if snap.target_slot is not None
            else None
        )
        margin = self.MIN_PROFIT_MARGIN_P_PER_KWH
        min_discharge_a = float(self.MIN_DISCHARGE_CURRENT)
        min_discharge_w = min_discharge_a * 240.0
        stored_cost_p = (
            snap.stored_cost_p
            if snap.stored_cost_p is not None
            else self._delivered_cost_per_kwh_p()
        )
        if (
            snap.discharge_aggressive
            and snap.decision_tier
            not in (
                "planned_export",
                "export_priority",
                "export_reserve",
                "grid_support",
            )
        ):
            # Aggressive load-following values the energy at its best
            # uncommitted export alternative, so the answer page must use the
            # same hurdle as the live gate rather than the ledger alone.
            gate = self._aggressive_slot_gate(slot)
            stored_cost_p = max(0.0, gate["hurdle_p"] - margin)
        hurdle_p = stored_cost_p + margin
        harvest_a = snap.harvest_current_a
        voltage = (
            snap.voltage_v if snap.voltage_v and snap.voltage_v > 0 else 240.0
        )
        charge_floor_a = snap.charge_min_a
        charge_activation_w = (
            snap.charge_activation_w
            if snap.charge_activation_w is not None
            else self._charge_activation_w_for_current(charge_floor_a or 0.0)
        )
        if charge_floor_a and not charge_activation_w:
            # The store branch always records one, but a snapshot written
            # before that field existed should still explain the floor.
            charge_activation_w = self._charge_activation_w_for_current(
                charge_floor_a
            )
        at_cap = snap.soc_percent >= cap if snap.soc_percent >= 0 else False

        self_use_rt = (
            self.EFFICIENCY_MODEL.round_trip_efficiency(
                harvest_a or self.SOLAR_CHARGE_REFERENCE_CURRENT,
                self.SELF_USE_DISCHARGE_REFERENCE_CURRENT,
            )
            if harvest_a is not None
            else None
        )
        self_use_value_p = (
            self.self_use_value_threshold_p(harvest_a)
            if harvest_a is not None
            else None
        )
        export_rt = self.solar_export_round_trip(harvest_a)
        store_value_p = (
            (target_p - margin) * export_rt
            if target_p is not None and target_p > 0
            else None
        )

        steps: list[dict] = []

        def add_step(
            title: str,
            question: str,
            calculation: str,
            result: str,
            conclusion: str,
        ) -> None:
            steps.append(
                {
                    "title": title,
                    "question": question,
                    "calculation": calculation,
                    "result": result,
                    "conclusion": conclusion,
                }
            )

        def surplus_text(value: Optional[float]) -> Optional[str]:
            """Readable grid balance: negative is a house import, not surplus."""
            if value is None:
                return None
            if value < 0:
                return f"{abs(value):.0f} W house import (no surplus)"
            return f"{value:.0f} W surplus"

        def discharge_step() -> None:
            activation = snap.discharge_activation_w
            if activation is None:
                add_step(
                    "When does the battery start powering the home?",
                    "Discharging cannot follow a house load below the "
                    f"Wallbox's {min_discharge_a:.0f} A minimum.",
                    "No activation threshold was recorded for this decision.",
                    "Unknown.",
                    "The next control cycle will record one.",
                )
                return
            if activation >= self.NEVER_DISCHARGE_ACTIVATION_W:
                add_step(
                    "When does the battery start powering the home?",
                    "Would discharging save more than the energy costs?",
                    f"Stored energy costs {stored_cost_p:.1f}p/kWh delivered "
                    f"+ {margin:.1f}p margin = {hurdle_p:.1f}p/kWh. The import "
                    f"it would avoid costs {import_p:.1f}p/kWh.",
                    f"{hurdle_p:.1f}p ≥ {import_p:.1f}p, so every kWh "
                    "discharged would cost more than it saves.",
                    "Discharge is disabled. The planner exports only when a "
                    "sale clears the stored cost on its own.",
                )
                return
            if activation <= 0:
                add_step(
                    "When does the battery start powering the home?",
                    "Would the forced minimum output already cover the stored "
                    "energy?",
                    f"The {min_discharge_a:.0f} A minimum is "
                    f"{min_discharge_w:.0f} W. The surplus it cannot use is "
                    f"exported at {export_p:.1f}p/kWh. Stored energy costs "
                    f"{stored_cost_p:.1f}p/kWh delivered + {margin:.1f}p "
                    f"margin = {hurdle_p:.1f}p/kWh.",
                    f"{export_p:.1f}p ≥ {hurdle_p:.1f}p, so the exported "
                    "surplus alone covers the stored cost.",
                    "Any house import justifies starting discharge. "
                    "Answer ≥0 W because the exported surplus alone covers "
                    "the stored cost, so start immediately.",
                )
                return
            spread_p = import_p - export_p
            bias_a = self.AGGRESSIVE_DISCHARGE_BIAS_A
            bias_w = bias_a * voltage
            if spread_p <= 0:
                calculation = (
                    f"The import price ({import_p:.1f}p) is not above the "
                    f"export price ({export_p:.1f}p), so there is no saving "
                    "to recover."
                )
            else:
                clamped_w = (
                    min_discharge_w * (hurdle_p - export_p) / spread_p
                )
                if not snap.discharge_aggressive:
                    calculation = (
                        f"{min_discharge_w:.0f} W × ({hurdle_p:.1f}p − "
                        f"{export_p:.1f}p) ÷ ({import_p:.1f}p − "
                        f"{export_p:.1f}p) = {clamped_w:.0f} W. Conservative "
                        f"discharge refuses to start below the "
                        f"{min_discharge_w:.0f} W minimum, so the floor is "
                        f"{max(clamped_w, min_discharge_w):.0f} W."
                    )
                elif clamped_w <= min_discharge_w - bias_w:
                    calculation = (
                        f"{min_discharge_w:.0f} W × ({hurdle_p:.1f}p − "
                        f"{export_p:.1f}p) ÷ ({import_p:.1f}p − "
                        f"{export_p:.1f}p) = {clamped_w:.0f} W."
                    )
                else:
                    calculation = (
                        f"The {bias_a:.1f} A bias already exports "
                        f"{bias_w:.0f} W at this load. Allowing half an amp "
                        "for the Wallbox's whole-amp steps: "
                        f"({bias_w:.0f} W + {voltage / 2:.0f} W) × "
                        f"({hurdle_p:.1f}p − {export_p:.1f}p) ÷ "
                        f"({import_p:.1f}p − {hurdle_p:.1f}p) = "
                        f"{activation:.0f} W."
                    )
            add_step(
                "When does the battery start powering the home?",
                "The Wallbox cannot run below "
                f"{min_discharge_a:.0f} A (≈{min_discharge_w:.0f} W). Below "
                "the break-even load, the surplus it exports costs more than "
                "the import it avoids.",
                calculation,
                f"Break-even house import = {activation:.0f} W.",
                f"Answer {activation:.0f} W because that is the point where "
                "the import avoided plus the exported surplus exactly covers "
                "the cost of the stored energy.",
            )

        def charge_step_store() -> None:
            if at_cap:
                add_step(
                    "Will the car take any charge?",
                    f"Is the battery below its {cap:.0f}% charge cap?",
                    f"State of charge = {snap.soc_percent}%; cap = {cap:.0f}%.",
                    f"{snap.soc_percent}% is at or above {cap:.0f}%.",
                    "No charging, from solar or grid. The pack is full enough.",
                )
                return
            floor = charge_floor_a if charge_floor_a is not None else 3.0
            activation = charge_activation_w
            add_step(
                "When will the car start charging from surplus?",
                "How strong must the solar surplus be before charging starts?",
                f"The charge floor is {floor:.0f} A. "
                f"{floor:.0f} A × 240 V = {activation:.0f} W.",
                f"Charging starts at ≥{floor:.0f} A surplus "
                f"(≈{activation:.0f} W).",
                "Below that the surplus is left to export. The charge and "
                "discharge ranges are independent, so the car can charge from "
                "a strong surplus while the house still runs from the battery.",
            )

        def charge_step_no_store() -> None:
            if at_cap:
                add_step(
                    "Will the car take any charge?",
                    f"Is the battery below its {cap:.0f}% charge cap?",
                    f"State of charge = {snap.soc_percent}%; cap = {cap:.0f}%.",
                    f"{snap.soc_percent}% is at or above {cap:.0f}%.",
                    "No charging. The charge range is closed to 0 A.",
                )
                return
            if not charge_floor_a:
                add_step(
                    "Will the car take any charge?",
                    "Is there any charge current at which keeping the solar "
                    "beats selling it now?",
                    f"The best measured self-use value is "
                    f"{self._best_self_use_value_p()[0]:.1f}p after the round "
                    f"trip; the export price now is {export_p:.1f}p.",
                    f"No measured current clears {export_p:.1f}p.",
                    "The charge range is closed to 0 A. The surplus is sold "
                    "rather than stored.",
                )
                return
            floor = charge_floor_a
            activation = charge_activation_w
            current_surplus = snap.surplus_w
            current_harvest = snap.harvest_current_a
            waiting = (
                (current_harvest is not None and current_harvest < floor)
                or (
                    current_surplus is not None
                    and current_surplus < activation
                )
            )
            observed = []
            if current_harvest is not None:
                observed.append(f"{current_harvest:.0f} A harvest estimate")
            if current_surplus is not None:
                surplus_label = surplus_text(current_surplus)
                if surplus_label is not None:
                    observed.append(surplus_label)
            observed_text = (
                " and ".join(observed) if observed else "no live power sample"
            )
            add_step(
                "Can today's surplus reach the charging floor?",
                "Is the live surplus strong enough to start charging?",
                f"Floor = {floor:.0f} A (≈{activation:.0f} W). "
                f"Observed: {observed_text}.",
                (
                    f"No: {observed_text} is below the floor."
                    if waiting
                    else f"Yes: {observed_text} reaches the floor."
                ),
                (
                    "The floor stays armed for a stronger surplus later, but "
                    "the car is not charging yet."
                    if waiting
                    else "The car charges at the floor current, without "
                    "importing from the grid."
                ),
            )

        # ---- Branch-specific reasoning -------------------------------------
        if snap.decision_tier == "shortfall":
            projected = snap.projected_soc_at_2330
            floor = snap.house_floor_soc
            bulk = snap.target_soc_percent
            if projected is not None:
                projection_text = f"{math.floor(projected)}%"
                comparison = (
                    f"Projected 23:30 SoC = {projection_text}; house floor = "
                    f"{floor:.0f}%."
                    if floor is not None
                    else f"Projected 23:30 SoC = {projection_text}."
                )
                conclusion = (
                    "The projection is below the house floor, so the pack must "
                    "not be spent on exports or extra charging."
                    if floor is not None and projected < floor
                    else "The projection is below the target, so the controller "
                    "keeps a conservative margin."
                )
            else:
                comparison = "The 23:30 projection is unavailable."
                conclusion = (
                    "Without a projection the controller conserves energy "
                    "rather than risk running the house from the grid."
                )
            add_step(
                "Will the battery last until 23:30?",
                "The controller projects the state of charge at the end of "
                "the cheap window and compares it with the house floor.",
                (
                    f"House floor = bulk target {bulk:.0f}% − "
                    f"{max(0.0, config.HOUSE_LOAD_MARGIN_PERCENT):.0f}% "
                    f"flexibility = {floor:.0f}%."
                    if bulk is not None and floor is not None
                    else comparison
                ),
                comparison,
                conclusion,
            )
            if at_cap:
                add_step(
                    "Will the car take any charge?",
                    f"Is the battery below its {cap:.0f}% charge cap?",
                    f"State of charge = {snap.soc_percent}%; cap = {cap:.0f}%.",
                    f"{snap.soc_percent}% is at or above {cap:.0f}%.",
                    "No charging. The charge range is closed to 0 A.",
                )
            elif charge_floor_a and charge_floor_a > 0:
                add_step(
                    "Will the car take any charge?",
                    "Can the pack accept solar without risking the 23:30 floor?",
                    f"SoC {snap.soc_percent}% is below the {cap:.0f}% cap. "
                    f"The break-even charge floor is {charge_floor_a:.0f} A "
                    f"(≈{charge_activation_w:.0f} W) at the live export price.",
                    f"Charging is allowed from surplus at "
                    f"≥{charge_floor_a:.0f} A (≈{charge_activation_w:.0f} W).",
                    "The car absorbs only what the roof provides above the "
                    "break-even floor; it does not import at the peak price.",
                )
            else:
                add_step(
                    "Will the car take any charge?",
                    "Is any measured solar current worth keeping right now?",
                    f"SoC {snap.soc_percent}% is below the {cap:.0f}% cap, "
                    f"but no measured current clears the {export_p:.1f}p "
                    "export price now.",
                    "No. The surplus is worth more exported.",
                    "The charge range is closed; the car does not store the "
                    "surplus at a poor round-trip efficiency.",
                )
            discharge_step()
            answer = {
                "heading": "Conserve energy for the house",
                "summary": (
                    f"The pack is projected to finish below its "
                    f"{floor:.0f}% house floor, so today's energy is kept for "
                    "the house rather than sold or used to charge the car."
                    if floor is not None
                    else "The pack is projected to be short of energy, so it "
                    "is kept for the house."
                ),
                "charge": self._answer_charge(
                    snap, charge_floor_a, charge_activation_w, at_cap, cap
                ),
                "discharge": self._answer_discharge(
                    snap.discharge_activation_w,
                    hurdle_p,
                    import_p,
                    export_p,
                    min_discharge_w,
                ),
            }

        elif snap.decision_tier == "export_reserve":
            pending = sorted(
                export_slot
                for export_slot in (snap.planned_export_slots or [])
                if export_slot >= slot
                and export_slot in self._export_priority_slots(slot)
            )
            target_slot = snap.target_slot
            target_rate_p = snap.target_rate_p or 0.0
            break_even_p = self.export_house_break_even_p(import_p)
            projected = snap.projected_soc_at_2330
            floor = snap.house_floor_soc
            projection_text = (
                f"{math.floor(projected)}%"
                if projected is not None
                else "below the floor"
            )
            slot_text = ", ".join(
                self._slot_range_label(export_slot)
                for export_slot in pending[:3]
            )
            if len(pending) > 3:
                slot_text += f" (+{len(pending) - 3} more)"
            add_step(
                "Why is the house load not covered now?",
                "Is the stored energy worth more in a later high-value export?",
                (
                    f"Importing now avoids {import_p:.1f}p through the house "
                    f"at {self.self_use_discharge_efficiency() * 100:.1f}%. "
                    f"Exporting later sells through "
                    f"{self.export_discharge_efficiency() * 100:.1f}% at "
                    f"{target_rate_p:.1f}p. Break-even = "
                    f"{self.self_use_discharge_efficiency() * 100:.1f}% ÷ "
                    f"{self.export_discharge_efficiency() * 100:.1f}% × "
                    f"{import_p:.1f}p = {break_even_p:.1f}p."
                ),
                f"Export {target_rate_p:.1f}p ≥ break-even {break_even_p:.1f}p.",
                "The later export is worth more per stored kWh, so the pack is "
                "held for it and the house imports for now.",
            )
            if floor is not None:
                add_step(
                    "Will the battery last until 23:30?",
                    "Does pausing house discharge protect the planned export?",
                    (
                        f"Projected 23:30 SoC = {projection_text}; house floor = "
                        f"{floor:.0f}%. Planned high-value slots: "
                        f"{slot_text or 'none'}."
                    ),
                    f"{projection_text} is below the {floor:.0f}% house floor.",
                    "Yes. The planned export slots are reserved rather than "
                    "spent on the house at the 3 A minimum.",
                )
            answer = {
                "heading": "Reserve the pack for high-value export",
                "summary": (
                    f"The pack is projected to finish at {projection_text}, "
                    f"below its {floor:.0f}% house floor, but the planned "
                    f"export at {target_rate_p:.1f}p beats covering the house "
                    f"at {import_p:.1f}p. House load is imported for now."
                    if floor is not None
                    else "The pack is projected to be short of energy, but a "
                    "later high-value export is worth more than covering the "
                    "house now."
                ),
                "charge": self._answer_charge(
                    snap, charge_floor_a, charge_activation_w, at_cap, cap
                ),
                "discharge": {
                    "headline": "Do not discharge — holding for export",
                    "because": (
                        f"The planned export at {target_rate_p:.1f}p clears "
                        f"the {break_even_p:.1f}p house-offset break-even, so "
                        "the pack is not spent covering the house first."
                    ),
                },
            }

        elif snap.store_decision:
            add_step(
                "What is the surplus worth if we sell it now?",
                "The alternative to storing is exporting the solar immediately.",
                f"Export price = {export_p:.1f}p/kWh.",
                f"{export_p:.1f}p/kWh.",
                "Every other option has to beat this value.",
            )
            if self_use_value_p is not None and self_use_rt is not None:
                add_step(
                    "What is the surplus worth if the house uses it later?",
                    "Storing the solar to cover the house avoids importing at "
                    "the peak rate.",
                    f"(peak import {config.IMPORT_HIGH:.1f}p − {margin:.1f}p "
                    f"margin) × {self_use_rt * 100:.0f}% round trip at "
                    f"{harvest_a:.0f} A = {self_use_value_p:.1f}p/kWh.",
                    f"{self_use_value_p:.1f}p/kWh.",
                    (
                        f"That beats {export_p:.1f}p now."
                        if self_use_value_p > export_p
                        else f"That does not beat {export_p:.1f}p now."
                    ),
                )
            if store_value_p is not None:
                add_step(
                    "What is the surplus worth if we store it for a later sale?",
                    "The later slot is the best unused export price still "
                    "ahead of us.",
                    f"({target_p:.1f}p at {target_label} − {margin:.1f}p "
                    f"margin) × {export_rt * 100:.0f}% round trip at "
                    f"{harvest_a:.0f} A = {store_value_p:.1f}p/kWh.",
                    f"{store_value_p:.1f}p/kWh.",
                    (
                        f"That beats {export_p:.1f}p now, so storing for the "
                        "later slot wins."
                        if store_value_p > export_p
                        else f"That does not beat {export_p:.1f}p now."
                    ),
                )
            tier = snap.decision_tier
            if tier == "storage floor":
                because = (
                    f"Export at {export_p:.1f}p is below the "
                    f"{self.STORAGE_FLOOR_THRESHOLD_P:.1f}p floor, so selling "
                    "now is not worth doing. The surplus is stored."
                )
            elif tier == "self-use":
                remaining_slots = max(0, 47 - slot)
                dynamic_floor = (
                    config.MIN_AGILE_DISCHARGE_SOC
                    + remaining_slots * self.NON_EXPORT_SLOT_SOC_LOSS_PERCENT
                )
                because = (
                    f"Keeping the solar is worth {self_use_value_p:.1f}p, "
                    f"above the {export_p:.1f}p export price, and SoC "
                    f"{snap.soc_percent}% is below the {dynamic_floor:.0f}% "
                    "curve for this time of day. Storing for the house wins."
                )
            else:
                because = (
                    f"The later {target_p:.1f}p sale is worth "
                    f"{store_value_p:.1f}p after the round trip, above the "
                    f"{export_p:.1f}p available now. Storing for the later "
                    "slot wins."
                    if store_value_p is not None and store_value_p > export_p
                    else f"Storing is worth at least {export_p:.1f}p after "
                    "the round trip, so holding the solar wins."
                )
            add_step(
                "Which option wins?",
                "Compare the values calculated above.",
                because,
                "Store the solar.",
                "The pack fills from surplus solar and sells or uses the "
                "energy later.",
            )
            charge_step_store()
            discharge_step()
            answer = {
                "heading": "Store the solar for later",
                "summary": because,
                "charge": {
                    "headline": (
                        "No charging — at the cap"
                        if at_cap
                        else f"Charge from surplus at "
                        f"≥{charge_floor_a or 3:.0f} A "
                        f"(≈{charge_activation_w:.0f} W)"
                    ),
                    "because": (
                        "The pack is at its charge cap."
                        if at_cap
                        else "The stored energy is worth more later than the "
                        "surplus is worth exported now."
                    ),
                },
                "discharge": self._answer_discharge(
                    snap.discharge_activation_w,
                    hurdle_p,
                    import_p,
                    export_p,
                    min_discharge_w,
                ),
            }

        elif not snap.store_decision and snap.decision_tier in (
            "storage floor", "self-use", "future export"
        ):
            add_step(
                "What is the surplus worth if we sell it now?",
                "The alternative to keeping the solar is exporting it "
                "immediately.",
                f"Export price = {export_p:.1f}p/kWh.",
                f"{export_p:.1f}p/kWh.",
                "Every other option has to beat this value.",
            )
            self_use_floor = self._minimum_harvest_charge_current_a(export_p)
            store_floor = (
                self._minimum_store_harvest_current_a(target_p, export_p)
                if target_p is not None and target_p > 0
                else None
            )
            if self_use_value_p is not None and self_use_rt is not None:
                add_step(
                    "What is the surplus worth if the house uses it later?",
                    "Storing the solar to cover the house avoids importing at "
                    "the peak rate.",
                    f"(peak import {config.IMPORT_HIGH:.1f}p − {margin:.1f}p "
                    f"margin) × {self_use_rt * 100:.0f}% round trip at "
                    f"{harvest_a:.0f} A = {self_use_value_p:.1f}p/kWh.",
                    f"{self_use_value_p:.1f}p/kWh.",
                    (
                        f"That beats {export_p:.1f}p at the current "
                        f"{harvest_a:.0f} A harvest."
                        if self_use_value_p > export_p
                        else f"At the current {harvest_a:.0f} A harvest that "
                        f"does not beat {export_p:.1f}p; the value rises with "
                        "a stronger surplus."
                    ),
                )
            if store_value_p is not None:
                add_step(
                    "What is the surplus worth if we store it for a later sale?",
                    "The later slot is the best unused export price still "
                    "ahead of us.",
                    f"({target_p:.1f}p at {target_label} − {margin:.1f}p "
                    f"margin) × {export_rt * 100:.0f}% round trip at "
                    f"{harvest_a:.0f} A = {store_value_p:.1f}p/kWh.",
                    f"{store_value_p:.1f}p/kWh.",
                    (
                        f"That beats {export_p:.1f}p at the current "
                        f"{harvest_a:.0f} A harvest, but only above the "
                        "break-even charge current."
                        if store_value_p > export_p
                        else f"At the current {harvest_a:.0f} A harvest that "
                        f"does not beat {export_p:.1f}p."
                    ),
                )
            break_even_bits = []
            if self_use_floor is not None:
                break_even_bits.append(
                    f"self-use first clears at {self_use_floor:.0f} A, where "
                    f"it is worth "
                    f"{self.self_use_value_threshold_p(self_use_floor):.1f}p"
                )
            else:
                break_even_bits.append("self-use never clears at any measured current")
            if store_floor is not None:
                break_even_bits.append(
                    f"the later sale first clears at {store_floor:.0f} A, "
                    f"where it is worth "
                    f"{(target_p - margin) * self.solar_export_round_trip(store_floor):.1f}p"
                )
            elif target_p is not None and target_p > 0:
                break_even_bits.append(
                    "the later sale never clears at any measured current"
                )
            floor = charge_floor_a
            if floor and self_use_floor is not None and floor == self_use_floor:
                value_at_floor = self.self_use_value_threshold_p(floor)
            elif (
                floor
                and store_floor is not None
                and floor == store_floor
            ):
                value_at_floor = (
                    (target_p - margin) * self.solar_export_round_trip(floor)
                )
            else:
                value_at_floor = None
            add_step(
                "Where is the break-even for charging?",
                "At what surplus current does keeping the solar start to beat "
                "selling it?",
                "Efficiency rises with current, so the test is monotonic: "
                + "; ".join(break_even_bits) + ".",
                (
                    f"Charging onset = {floor:.0f} A (≈"
                    f"{self._charge_activation_w_for_current(floor):.0f} W), "
                    f"where the surplus is worth {value_at_floor:.1f}p against "
                    f"{export_p:.1f}p now."
                    if floor and value_at_floor is not None
                    else (
                        f"Charging onset = {floor:.0f} A (≈"
                        f"{self._charge_activation_w_for_current(floor):.0f} W)."
                        if floor
                        else "No measured current clears the export price."
                    )
                ),
                (
                    "The stronger the surplus, the more of it survives the "
                    "round trip and the more valuable it is. The floor is the "
                    "first whole-amp step that wins."
                    if floor
                    else "The surplus is worth more sold now than stored."
                ),
            )
            charge_step_no_store()
            discharge_step()
            if at_cap:
                heading = "Cover the house; do not charge"
                summary = (
                    f"The pack is already at its {cap:.0f}% charge cap, so "
                    "extra solar cannot be stored. It is exported and the "
                    "battery covers the house."
                )
            elif not charge_floor_a:
                heading = "Sell the surplus; do not charge"
                summary = (
                    f"This surplus is worth more exported at {export_p:.1f}p "
                    "than kept for the house or a later sale."
                )
            else:
                waiting = (
                    (snap.harvest_current_a is not None
                     and snap.harvest_current_a < charge_floor_a)
                    or (snap.surplus_w is not None
                        and snap.surplus_w < charge_activation_w)
                )
                if waiting:
                    heading = "Do not charge yet; sell the current surplus"
                    summary = (
                        f"Charging becomes worthwhile at "
                        f"≥{charge_floor_a:.0f} A "
                        f"(≈{charge_activation_w:.0f} W) of surplus. Today's "
                        "surplus is below that, so the floor is armed for a "
                        "stronger surplus later."
                    )
                else:
                    heading = "Charge from surplus and cover the house"
                    summary = (
                        f"Surplus at or above {charge_floor_a:.0f} A "
                        f"(≈{charge_activation_w:.0f} W) is worth keeping."
                    )
            answer = {
                "heading": heading,
                "summary": summary,
                "charge": self._answer_charge(
                    snap, charge_floor_a, charge_activation_w, at_cap, cap
                ),
                "discharge": self._answer_discharge(
                    snap.discharge_activation_w,
                    hurdle_p,
                    import_p,
                    export_p,
                    min_discharge_w,
                ),
            }

        elif snap.decision_tier == "absolute_floor":
            absolute_floor = self.absolute_min_soc_percent()
            projected = snap.projected_soc_at_2330
            if snap.soc_percent <= absolute_floor:
                comparison = (
                    f"SoC {snap.soc_percent}% is at or below the "
                    f"{absolute_floor:.0f}% absolute minimum."
                )
            elif projected is not None:
                comparison = (
                    f"Projected 23:30 SoC {math.floor(projected)}% is below "
                    f"the {absolute_floor:.0f}% absolute minimum."
                )
            else:
                comparison = (
                    f"The pack is at or below the {absolute_floor:.0f}% "
                    "absolute minimum."
                )
            add_step(
                "Why is the pack not discharging?",
                "Can the pack still cover the house or export today?",
                comparison,
                "Discharge is inhibited.",
                "The absolute minimum is a hard floor: the controller stops "
                "discharging and exporting there even when the price is "
                "attractive. Surplus solar may still refill the pack.",
            )
            solar_floor_a = (
                float(charge_floor_a)
                if charge_floor_a is not None
                else float(self.SOLAR_CHARGE_REFERENCE_CURRENT)
            )
            solar_activation_w = (
                charge_activation_w
                if charge_activation_w is not None
                else self._charge_activation_w_for_current(solar_floor_a)
            )
            if not at_cap and solar_floor_a > 0:
                add_step(
                    "Will the car still take surplus solar?",
                    "Does the absolute minimum close the charge path as well?",
                    f"Discharge floor = {absolute_floor:.0f}%. Charge floor = "
                    f"{solar_floor_a:.0f} A "
                    f"(≈{solar_activation_w:.0f} W); cap = {cap:.0f}%.",
                    f"Charging is allowed from surplus at "
                    f"≥{solar_floor_a:.0f} A "
                    f"(≈{solar_activation_w:.0f} W).",
                    "The floor stops discharge and export, not free solar. The "
                    "pack can refill during the day without importing at the "
                    "peak rate.",
                )
            elif not at_cap:
                add_step(
                    "Will the car still take surplus solar?",
                    "Does the absolute minimum close the charge path as well?",
                    f"Discharge floor = {absolute_floor:.0f}%. No measured "
                    f"charge current clears the {export_p:.1f}p export price "
                    "now.",
                    "The surplus is worth more exported, so charging stays off.",
                    "The absolute minimum stops discharge; it does not force "
                    "the pack to accept a poorly performing trickle charge.",
                )
            charge_answer = {
                "headline": (
                    "No charging — at the cap"
                    if at_cap
                    else (
                        f"Charge from surplus solar at ≥{solar_floor_a:.0f} A "
                        f"(≈{solar_activation_w:.0f} W)"
                        if solar_floor_a > 0
                        else "Do not charge — surplus worth more exported"
                    )
                ),
                "because": self._solar_charge_floor_text(
                    at_cap=at_cap,
                    charge_min_a=solar_floor_a,
                    activation_w=solar_activation_w,
                ),
            }
            discharge_answer = {
                "headline": "Do not discharge",
                "because": (
                    f"The {absolute_floor:.0f}% absolute minimum is a hard "
                    "floor for discharge and export, regardless of the "
                    "quoted price."
                ),
            }
            answer = {
                "heading": "Hold at the absolute minimum",
                "summary": comparison,
                "charge": charge_answer,
                "discharge": discharge_answer,
            }

        elif snap.decision_tier == "export_priority":
            break_even_p = self.export_house_break_even_p(import_p)
            self_use_eff = self.self_use_discharge_efficiency()
            export_eff = self.export_discharge_efficiency()
            ratio = self_use_eff / export_eff if export_eff > 0 else 0.0
            add_step(
                "Why export instead of offsetting house load?",
                "Both options use the same stored energy. Which returns more "
                "per kWh?",
                f"Offsetting the house avoids {import_p:.1f}p of import "
                f"through ~{self.SELF_USE_DISCHARGE_REFERENCE_CURRENT:.0f} A "
                f"at {self_use_eff * 100:.1f}%. Exporting sells through "
                f"{self.EXPORT_REFERENCE_CURRENT:.0f} A at "
                f"{export_eff * 100:.1f}%. Break-even = "
                f"{self_use_eff * 100:.1f}% ÷ {export_eff * 100:.1f}% × "
                f"{import_p:.1f}p = {break_even_p:.1f}p.",
                f"Export {export_p:.1f}p ≥ break-even {break_even_p:.1f}p.",
                "Exporting returns more per stored kWh than offsetting the "
                "house, so this slot is exported at maximum rate.",
            )
            answer = {
                "heading": "Export beats offsetting the house",
                "summary": (
                    f"This slot pays {export_p:.1f}p/kWh, at or above the "
                    f"{break_even_p:.1f}p break-even for using the same "
                    "energy to avoid importing at the peak rate."
                ),
                "charge": {
                    "headline": "No charging",
                    "because": (
                        "The controller is exporting at the maximum rate for "
                        "this slot, so the charge range is closed."
                    ),
                },
                "discharge": {
                    "headline": (
                        f"Export at maximum rate "
                        f"({self.MAX_DISCHARGE_CURRENT:.0f} A)"
                    ),
                    "because": (
                        "Exporting returns more than offsetting house load, "
                        "so the stored energy is sold while the price clears "
                        "the break-even."
                    ),
                },
            }

        else:
            # Simple branches with a single reason: free energy, off-peak,
            # supplier support, protection, unknown SoC, or a planned export.
            add_step(
                "What state was the controller in?",
                "The decision came from a priority rule, before the ordinary "
                "store-or-sell comparison.",
                snap.reason or "No further detail was recorded.",
                snap.mode,
                "The rule applies directly; no break-even comparison is needed.",
            )
            if snap.decision_tier not in (
                "planned_export", "grid_support", "protection",
                "off_peak", "free", "unknown_soc",
            ):
                discharge_step()
            if snap.decision_tier in ("planned_export", "grid_support"):
                charge_answer = {
                    "headline": "No charging",
                    "because": (
                        "The controller is exporting at the maximum rate for "
                        "this slot, so the charge range is closed."
                    ),
                }
                discharge_answer = {
                    "headline": (
                        f"Export at maximum rate "
                        f"({self.MAX_DISCHARGE_CURRENT:.0f} A)"
                    ),
                    "because": (
                        "This is a planned export slot priced at "
                        f"{export_p:.1f}p/kWh."
                        if snap.decision_tier == "planned_export"
                        else "The supplier requested grid support for this "
                        "window."
                    ),
                }
            elif snap.decision_tier == "protection":
                charge_answer = {
                    "headline": "No charging",
                    "because": (
                        f"SoC {snap.soc_percent}% is at the protection "
                        "reserve; the pack is left alone."
                    ),
                }
                discharge_answer = {
                    "headline": "Do not discharge",
                    "because": (
                        f"SoC {snap.soc_percent}% is at or below the "
                        f"{self.BATTERY_PROTECTION_RESERVE_SOC}% protection "
                        "reserve, so discharging would risk the battery."
                    ),
                }
            elif snap.decision_tier in ("free", "off_peak"):
                charge_answer = {
                    "headline": (
                        "Charge at maximum rate"
                        if not at_cap
                        else "No charging — at the cap"
                    ),
                    "because": snap.detail
                    or "The price for this window decides it directly.",
                }
                discharge_answer = {
                    "headline": "No discharge while charging",
                    "because": (
                        "The car is filling from a free or cheap import "
                        "window; discharging would work against that."
                    ),
                }
            elif snap.decision_tier == "unknown_soc":
                charge_answer = {
                    "headline": "Charge gently at 3 A",
                    "because": (
                        "The battery has not reported a state of charge, so "
                        "the controller cannot safely choose a discharge."
                    ),
                }
                discharge_answer = {
                    "headline": "No discharge until SoC returns",
                    "because": (
                        "Without a state of charge the controller cannot tell "
                        "whether discharging would enter the protection "
                        "reserve."
                    ),
                }
            else:
                charge_answer = {
                    "headline": (
                        f"Charge at {snap.charge_min_a:.0f} A"
                        if snap.charge_min_a
                        else "No charging"
                    ),
                    "because": snap.detail
                    or "The state above decides this directly.",
                }
                discharge_answer = self._answer_discharge(
                    snap.discharge_activation_w,
                    hurdle_p,
                    import_p,
                    export_p,
                    min_discharge_w,
                )
            answer = {
                "heading": snap.mode,
                "summary": snap.reason or "The controller applied a priority rule.",
                "charge": charge_answer,
                "discharge": discharge_answer,
            }

        plan = self._plan_explanation(snap)

        state = [
            {"label": "Decision taken at", "value": snap.updated_at.strftime("%H:%M:%S")},
            {"label": "Half-hour slot", "value": slot_label},
            {"label": "Mode", "value": snap.mode},
            {"label": "Battery state of charge", "value": f"{snap.soc_percent}%"},
            {"label": "Charge cap", "value": f"{cap:.0f}%"},
            {
                "label": "Import price",
                "value": f"{import_p:.1f}p/kWh",
            },
            {
                "label": "Export price now",
                "value": f"{export_p:.1f}p/kWh",
            },
        ]
        if target_p is not None and target_label is not None:
            state.append(
                {
                    "label": "Best later export",
                    "value": f"{target_p:.1f}p/kWh at {target_label}",
                }
            )
        if snap.harvest_current_a is not None:
            state.append(
                {
                    "label": "Solar harvest estimate",
                    "value": f"{snap.harvest_current_a:.0f} A",
                }
            )
        if snap.surplus_w is not None:
            grid_balance = surplus_text(snap.surplus_w)
            if grid_balance is not None:
                state.append(
                    {
                        "label": "Grid balance",
                        "value": grid_balance,
                    }
                )
        state.append(
            {
                "label": "Stored energy cost",
                "value": f"{stored_cost_p:.1f}p/kWh delivered",
            }
        )
        state.append(
            {
                "label": "Minimum discharge",
                "value": (
                    f"{min_discharge_a:.0f} A "
                    f"(≈{min_discharge_w:.0f} W at 240 V)"
                ),
            }
        )
        if plan["baseline_projection"] is not None:
            state.append(
                {
                    "label": "Projected 23:30 SoC (before house load/exports)",
                    "value": f"{math.floor(plan['baseline_projection'])}%",
                }
            )
            state.append(
                {
                    "label": "Projected 23:30 SoC (with plan)",
                    "value": f"{math.floor(plan['plan_projection'])}%",
                }
            )
        if snap.house_floor_soc is not None:
            state.append(
                {
                    "label": "House floor",
                    "value": f"{snap.house_floor_soc:.0f}%",
                }
            )
        state.append(
            {
                "label": "Absolute minimum",
                "value": f"{plan['absolute_min_soc']:.0f}%",
            }
        )
        state.append(
            {
                "label": "Export floor",
                "value": f"{plan['target_soc']:.0f}%",
            }
        )
        if plan["headroom_soc"] is not None:
            state.append(
                {
                    "label": "SoC headroom above target",
                    "value": f"{max(0.0, plan['headroom_soc']):.1f}%",
                }
            )
        if plan.get("priority_headroom_soc") is not None:
            state.append(
                {
                    "label": "SoC headroom above absolute minimum",
                    "value": f"{max(0.0, plan['priority_headroom_soc']):.1f}%",
                }
            )
        state.append(
            {
                "label": "Export slots planned",
                "value": f"{len(plan['planned_slots'])}",
            }
        )
        if plan.get("aggressive_slots"):
            state.append(
                {
                    "label": "Aggressive house+export slots (conditional)",
                    "value": f"{len(plan['aggressive_slots'])}",
                }
            )
        if snap.decision_tier in (
            "planned_export", "export_priority", "export_reserve", "grid_support"
        ):
            break_even_p = self.export_house_break_even_p(import_p)
            state.append(
                {
                    "label": "House-offset break-even",
                    "value": f"{break_even_p:.1f}p/kWh",
                }
            )
        if snap.decision_tier in ("free", "off_peak"):
            charging_value = (
                "None — at the charge cap"
                if at_cap
                else f"Grid charge at "
                f"{(charge_floor_a or self.MAX_CHARGE_CURRENT):.0f} A"
            )
        elif snap.decision_tier in (
            "planned_export", "export_priority", "grid_support"
        ):
            charging_value = "None — exporting at maximum rate"
        elif snap.decision_tier == "absolute_floor":
            charging_value = (
                "None — at the charge cap"
                if at_cap
                else "Surplus solar only (≥3 A)"
            )
        elif snap.decision_tier == "protection":
            charging_value = "None — protection reserve"
        elif snap.decision_tier == "unknown_soc":
            charging_value = "Gentle charge at 3 A until SoC returns"
        elif at_cap:
            charging_value = "None — at the charge cap"
        elif charge_floor_a:
            charging_value = (
                f"From surplus at ≥{charge_floor_a:.0f} A "
                f"(≈{charge_activation_w:.0f} W)"
            )
        else:
            charging_value = "None — the surplus is sold instead"
        state.append(
            {"label": "Charging", "value": charging_value}
        )
        if snap.discharge_activation_w is not None:
            if snap.decision_tier == "export_reserve":
                discharge_value = "Paused — holding energy for planned export"
            elif snap.discharge_activation_w >= self.NEVER_DISCHARGE_ACTIVATION_W:
                discharge_value = "Disabled — stored energy costs more than it saves"
            elif snap.discharge_activation_w <= 0:
                discharge_value = "Any house import (≥0 W)"
            else:
                discharge_value = (
                    f"≥{snap.discharge_activation_w:.0f} W house import"
                )
            state.append(
                {"label": "Discharge starts", "value": discharge_value}
            )

        assumptions = [
            "One half-hour slot is the smallest unit. Prices and sessions are "
            "applied to whole slots.",
            "The round-trip efficiency comes from the measured battery table "
            "at the estimated harvest current, not a fixed percentage.",
            f"The minimum profit margin is {margin:.1f}p/kWh delivered, "
            "applied before the round-trip efficiency.",
            "The stored-energy cost comes from the ledger; when the ledger is "
            "too small to trust, the off-peak purchase price is used instead.",
            "The Wallbox cannot run below "
            f"{min_discharge_a:.0f} A (≈{min_discharge_w:.0f} W), so a "
            "discharge decision is a decision to start at that output.",
            "No negative-margin trade is accepted. Every action must cover "
            "its own cost plus the configured margin.",
            "The house-offset break-even is a pure efficiency ratio: "
            "η_house ÷ η_export × the import price. No profit margin is added, "
            "because both options use energy that is already in the pack.",
            "The absolute minimum SoC is a hard floor for discharge and "
            "export. It can be raised above the 25% physical reserve but not "
            "lowered.",
            f"The 23:30 projection applies {self.NON_EXPORT_SLOT_SOC_LOSS_PERCENT:.1f}% "
            "of load-follow loss per ordinary slot, the measured export loss "
            f"of {self._export_slot_soc_loss_percent():.1f}% per export slot, "
            "and the session effects. EV-away slots are skipped entirely.",
            "Export slots are ranked by price and the best are selected first, "
            "but the plan still stops at the export floor. A slot can therefore "
            "qualify on price and still be left unplanned because the reserve "
            "has no headroom.",
        ]

        return {
            "available": True,
            "snapshot_at": snap.updated_at,
            "slot": slot,
            "slot_label": slot_label,
            "mode": snap.mode,
            "reason": snap.reason,
            "detail": snap.detail,
            "state": state,
            "ledger": self._ledger_explanation(snap),
            "efficiency": self._efficiency_explanation(),
            "plan": plan,
            "steps": steps,
            "answer": answer,
            "assumptions": assumptions,
        }

    def _answer_discharge(
        self,
        activation_w: Optional[float],
        hurdle_p: float,
        import_p: float,
        export_p: float,
        min_discharge_w: float,
    ) -> dict:
        """Plain-language discharge half of the explanation's answer."""
        if activation_w is None:
            return {
                "headline": "Discharge threshold not recorded",
                "because": "The next control cycle will record one.",
            }
        if activation_w >= self.NEVER_DISCHARGE_ACTIVATION_W:
            return {
                "headline": "Do not discharge",
                "because": (
                    "Stored energy is worth at least "
                    f"{hurdle_p:.1f}p/kWh delivered in its best alternative "
                    "use or measured cost, which is at or above the "
                    f"{import_p:.1f}p/kWh import price. Discharging would "
                    "lose money."
                ),
            }
        if activation_w <= 0:
            return {
                "headline": "Power the home at any import (≥0 W)",
                "because": (
                    f"Export at {export_p:.1f}p/kWh already covers the "
                    f"{hurdle_p:.1f}p/kWh stored-energy hurdle, so the "
                    f"{min_discharge_w:.0f} W minimum does not lose money."
                ),
            }
        return {
            "headline": (
                f"Power the home at ≥{activation_w:.0f} W house import"
            ),
            "because": (
                "That is the break-even point: below it, the forced output "
                "exports more than the import it avoids, so the stored energy "
                "would cost more than it saves."
            ),
        }

    @staticmethod
    def _answer_charge(
        snap: DecisionSnapshot,
        floor_a: Optional[float],
        activation_w: float,
        at_cap: bool,
        cap: float,
    ) -> dict:
        """Plain-language charging half of the explanation's answer."""
        if at_cap:
            return {
                "headline": "Do not charge",
                "because": (
                    f"State of charge {snap.soc_percent}% is at the "
                    f"{cap:.0f}% cap."
                ),
            }
        if not floor_a:
            return {
                "headline": "Do not charge — sell the surplus",
                "because": (
                    "No measured charge current makes the stored energy worth "
                    "more than exporting it now."
                ),
            }
        waiting = (
            (snap.harvest_current_a is not None
             and snap.harvest_current_a < floor_a)
            or (snap.surplus_w is not None and snap.surplus_w < activation_w)
        )
        if waiting:
            return {
                "headline": (
                    f"Not yet — charging starts at ≥{floor_a:.0f} A "
                    f"(≈{activation_w:.0f} W) of surplus"
                ),
                "because": (
                    "That is the break-even point where the stored energy is "
                    "worth more than exporting the surplus now. Today's "
                    "surplus is below it, so the floor stays armed but the "
                    "car is not charging."
                ),
            }
        return {
            "headline": (
                f"Charge from surplus at ≥{floor_a:.0f} A "
                f"(≈{activation_w:.0f} W)"
            ),
            "because": (
                "That is the break-even point where the stored energy is worth "
                "more than exporting the surplus now."
            ),
        }

    def get_dashboard_html(self) -> str:
        """Return HTML for Agile Outgoing rate display.

        Returns a 48-cell visual strip showing today's export rates with
        red→green gradient coloring. Responsive design adapts to viewport width.
        Missing slots shown as grey placeholders.
        Planned export slots are highlighted with "EX" label.
        Free Energy Session, Save Energy Session and EV Away slots carry a
        coloured bar and appear in the legend below the strip.
        """
        with self._rates_lock:
            mode_line = self._mode_line_html()
            # Only today's rates are valid.  Empty (no fetch yet, or only
            # stale data) shows the loading placeholder rather than yesterday's
            # prices.
            rates = self._today_rates()
            if not rates:
                return (
                    mode_line
                    + """
                <div class="agile-rate-strip" style="height:40px;display:flex;
                    align-items:center;justify-content:center;background:#f5f5f5;
                    border-radius:4px;margin:10px 0;">
                    <span style="color:#666;font-size:12px;">Loading Agile Outgoing rates...</span>
                </div>
                """
                )

            # Get current time to determine current slot
            now = self.get_current_datetime()
            current_slot_idx = now.hour * 2 + (1 if now.minute >= 30 else 0)
            
            # Use cached export plan (calculated in set_home_demand_levels)
            # Don't recalculate here to avoid excessive logging
            planned_slots = self._planned_export_slots

            # Supplier-requested sessions are pinned to whole slots.  Mark the
            # windows on the strip even though the rate colour still shows the
            # quoted export price, which the session may be overriding.
            free_import_slots = self._free_import_slots_today()
            store = getattr(self, "session_store", None)
            grid_support_slots = (
                store.slots_for_kind(now.date(), GRID_SUPPORT)
                if store is not None
                else set()
            )
            away_slots = (
                store.away_slots(now.date()) if store is not None else set()
            )

            # Create a dict for quick lookup by slot index
            # Each slot is 30 minutes, so slot 0 = 00:00, slot 1 = 00:30, etc.
            rate_dict = {}
            for r in rates:
                slot_idx = r['start'].hour * 2 + (1 if r['start'].minute == 30 else 0)
                rate_dict[slot_idx] = r
            
            # Find the slot that may be partially filled (lowest rate, last of ties)
            partial_slot_idx = None
            if planned_slots and rate_dict:
                # Calculate energy needed for all planned slots
                energy_per_slot_kwh = (
                    self.EXPORT_POWER_KW / self.STORED_TO_AC_EFFICIENCY
                ) * 0.5
                total_energy_needed = len(planned_slots) * energy_per_slot_kwh
                
                # Calculate available energy
                battery_capacity_kwh = self.SOC_CAPACITY_KWH
                min_soc_kwh = (config.MIN_AGILE_DISCHARGE_SOC / 100.0) * battery_capacity_kwh
                current_soc = 0
                try:
                    from evse_controller.drivers.evse.async_interface import EvseThreadInterface
                    evse = EvseThreadInterface.get_instance()
                    state = evse.get_state()
                    if state and state.battery_level >= 0:
                        current_soc = state.battery_level
                except:
                    pass
                current_energy_kwh = (current_soc / 100.0) * battery_capacity_kwh
                available_energy_kwh = current_energy_kwh - min_soc_kwh
                
                # Only mark a partial slot if we don't have enough energy for all slots
                if available_energy_kwh < total_energy_needed:
                    # Find minimum rate among planned slots
                    min_rate = min(rate_dict[idx]['rate'] for idx in planned_slots if idx in rate_dict)
                    # Find all slots with minimum rate
                    min_rate_slots = [idx for idx in planned_slots if idx in rate_dict and rate_dict[idx]['rate'] == min_rate]
                    # Take the last one (furthest in future)
                    if min_rate_slots:
                        partial_slot_idx = min_rate_slots[-1]

            # Calculate min/max for color scaling (only for actual rates)
            rate_values = [r['rate'] for r in rates]
            min_rate = min(rate_values) if rate_values else 0
            max_rate = max(rate_values) if rate_values else 0
            rate_range = max_rate - min_rate if max_rate > min_rate else 1

            # Generate HTML - always 48 slots
            html = [mode_line, '<div style="position:relative;margin:6px 0;">']
            html.append(
                '<div class="agile-rate-strip" style="height:40px;display:flex;'
                "gap:1px;padding:5px;background:#fafafa;border-radius:4px;"
                'overflow:hidden;">'
            )
            
            for slot_idx in range(48):
                hour = slot_idx // 2
                minute = 30 if slot_idx % 2 else 0

                session_style = ""
                session_tooltip = ""
                if slot_idx in free_import_slots:
                    session_style = "box-shadow:inset 0 4px 0 #2563eb;"
                    session_tooltip = " - FREE ENERGY SESSION"
                elif slot_idx in grid_support_slots:
                    session_style = "box-shadow:inset 0 4px 0 #d97706;"
                    session_tooltip = " - SAVE ENERGY SESSION (grid support)"
                elif slot_idx in away_slots:
                    session_style = "box-shadow:inset 0 4px 0 #6b7280;"
                    session_tooltip = " - EV AWAY (excluded from projection)"

                if slot_idx in rate_dict:
                    # Actual rate available
                    rate_data = rate_dict[slot_idx]
                    rate = rate_data['rate']
                    
                    # Calculate color (red→green gradient)
                    normalized = (rate - min_rate) / rate_range
                    red = int(255 * (1 - normalized))
                    green = int(255 * normalized)
                    blue = 100
                    
                    # Calculate brightness to determine text color
                    # Using luminance formula: Y = 0.299R + 0.587G + 0.114B
                    # Lower threshold (150) ensures black text on bright colors
                    brightness = 0.299 * red + 0.587 * green + 0.114 * blue
                    text_color = "#000000" if brightness > 150 else "#ffffff"
                    
                    rate_int = int(round(rate))
                    
                    # Check if this slot is planned for export, already exported, or in the past
                    is_planned = slot_idx in planned_slots
                    is_exported = slot_idx in self._exported_slots
                    is_past = slot_idx < current_slot_idx
                    
                    # Check if this is the partial slot (lowest rate, last of ties)
                    is_partial = (slot_idx == partial_slot_idx)
                    
                    # Determine visual indication - all cells have same border width for consistent sizing
                    if is_past and not is_exported:
                        # Past slot that wasn't used - dim it with grey border
                        extra_style = "opacity:0.5;border:2px solid #bbb;"
                        tooltip_extra = " (past)"
                        label = ""
                    elif is_exported:
                        # Already exported - show green border, checkmark in text color
                        extra_style = "border:2px solid #4caf50;"
                        tooltip_extra = " - EXPORTED"
                        label = f'<span class="agile-rate-badge" style="color:{text_color};">✓</span>'
                    elif is_partial:
                        # Partial slot (lowest rate) - show lowercase 'e'
                        extra_style = "border:2px solid #ff9800;"
                        tooltip_extra = " - PLANNED EXPORT (may be partial)"
                        label = f'<span class="agile-rate-badge" style="color:{text_color};">e</span>'
                    elif is_planned:
                        # Planned for export - show orange border, EX in text color
                        extra_style = "border:2px solid #ff9800;"
                        tooltip_extra = " - PLANNED EXPORT"
                        label = f'<span class="agile-rate-badge" style="color:{text_color};">EX</span>'
                    else:
                        # Normal future slot - grey border
                        extra_style = "border:2px solid #ddd;"
                        tooltip_extra = ""
                        label = ""
                    
                    html.append(f'''
                    <div class="agile-rate-cell" data-rate="{rate}" data-rate-int="{rate_int}" data-planned="{is_planned}" data-exported="{is_exported}" data-past="{is_past}" style="
                        flex:1;
                        min-width:0;
                        height:100%;
                        background:rgb({red},{green},{blue});
                        color:{text_color};
                        font-size:10px;
                        font-weight:bold;
                        display:flex;
                        flex-direction:column;
                        align-items:center;
                        justify-content:center;
                        border-radius:1px;
                        cursor:default;
                        position:relative;
                        box-sizing:border-box;
                        padding:0;
                        margin:0;
                        {extra_style}
                        {session_style}
                    " title="{hour:02d}:{minute:02d}: {rate:.1f}p/kWh{tooltip_extra}{session_tooltip}">
                        {label}
                        <span class="rate-value-full" style="font-size:9px;line-height:1.2;">{rate:.1f}</span>
                        <span class="rate-value-int" style="font-size:9px;line-height:1.2;display:none;">{rate_int}</span>
                        <span class="rate-value-none" style="display:none;">&nbsp;</span>
                    </div>
                    ''')
                else:
                    # Missing slot - show placeholder with same sizing as rate cells
                    html.append(f'''
                    <div class="agile-rate-cell-placeholder" data-missing="true" style="
                        flex:1;
                        min-width:0;
                        height:100%;
                        background:#e0e0e0;
                        color:#999;
                        font-size:10px;
                        font-weight:bold;
                        display:flex;
                        flex-direction:column;
                        align-items:center;
                        justify-content:center;
                        border-radius:1px;
                        cursor:default;
                        position:relative;
                        box-sizing:border-box;
                        padding:0;
                        margin:0;
                        border:2px solid #ddd;
                        {session_style}
                    " title="{hour:02d}:{minute:02d}: Rate not available{session_tooltip}">
                        <span style="font-size:14px;line-height:1;font-weight:300;">?</span>
                    </div>
                    ''')
            
            html.append('</div>')  # Close agile-rate-strip
            
            # Add time axis with horizontal line and tick marks
            # Use flex structure matching the cells for perfect alignment
            time_axis = '<div style="display:flex;gap:1px;padding:0 5px;height:20px;position:relative;">'
            
            # Add horizontal line across top (full width of container)
            time_axis += '<span style="position:absolute;left:0;right:0;top:0;height:1px;background:#666;"></span>'
            
            # Generate 48 flex items matching the cells
            for slot_idx in range(48):
                hour = slot_idx // 2
                
                # Add tick at 2-hour boundaries (00:00, 02:00, 04:00, ..., 22:00)
                # 00:00 = slot 0, 02:00 = slot 4, 04:00 = slot 8, etc.
                if slot_idx % 4 == 0:
                    time_axis += f'<div style="flex:1;min-width:0;position:relative;">'
                    # Tick mark (hanging down from horizontal line)
                    time_axis += f'<span style="position:absolute;left:0;top:1px;transform:translateX(-50%);width:1px;height:6px;background:#666;"></span>'
                    # Time label (for 02:00 through 22:00 only, not 00:00)
                    if slot_idx > 0:
                        time_axis += (
                            '<span style="position:absolute;left:0;top:5px;'
                            'transform:translateX(-50%);font-size:9px;color:#666;">'
                            f"{hour:02d}:00</span>"
                        )
                    time_axis += '</div>'
                else:
                    time_axis += '<div style="flex:1;min-width:0;"></div>'
            
            time_axis += '</div>'
            html.append(time_axis)
            html.append('</div>')  # Close outer wrapper

            if free_import_slots or grid_support_slots or away_slots:
                legend_items = []
                if free_import_slots:
                    legend_items.append(
                        '<span><span style="display:inline-block;width:10px;'
                        'height:10px;background:#2563eb;border-radius:2px;'
                        'margin-right:4px;vertical-align:middle;"></span>'
                        'Free Energy Session</span>'
                    )
                if grid_support_slots:
                    legend_items.append(
                        '<span><span style="display:inline-block;width:10px;'
                        'height:10px;background:#d97706;border-radius:2px;'
                        'margin-right:4px;vertical-align:middle;"></span>'
                        'Save Energy Session (grid support)</span>'
                    )
                if away_slots:
                    legend_items.append(
                        '<span><span style="display:inline-block;width:10px;'
                        'height:10px;background:#6b7280;border-radius:2px;'
                        'margin-right:4px;vertical-align:middle;"></span>'
                        'EV Away (excluded from 23:30 projection)</span>'
                    )
                html.append(
                    '<div style="display:flex;flex-wrap:wrap;gap:12px;'
                    'font-size:10px;color:#555;margin:2px 0 6px 5px;">'
                    + ''.join(legend_items)
                    + '</div>'
                )

            # Calculate expected revenue and SoC drop
            num_slots = len(planned_slots)
            if num_slots > 0:
                # Get average rate of planned slots
                total_revenue_p = sum(rate_dict[idx]['rate'] for idx in planned_slots if idx in rate_dict)
                avg_rate = total_revenue_p / num_slots

                # Calculate expected revenue (£)
                # Revenue = rate (p/kWh) × power (kW) × time (h) / 100 (to convert p to £)
                expected_revenue_gbp = (avg_rate * self.EXPORT_POWER_KW * 0.5 * num_slots) / 100

                # Calculate SoC drop per export slot
                # Use configured value if set, otherwise calculate from EXPORT_POWER_KW
                if self.EXPORT_SLOT_SOC_LOSS_PERCENT > 0:
                    soc_drop_per_slot = self.EXPORT_SLOT_SOC_LOSS_PERCENT
                else:
                    soc_drop_per_slot = (
                        self.EXPORT_POWER_KW / self.STORED_TO_AC_EFFICIENCY
                        * 0.5 / self.SOC_CAPACITY_KWH
                    ) * 100
            else:
                expected_revenue_gbp = 0
                soc_drop_per_slot = 0
            
            # Add legend with revenue and SoC drop info
            html.append(f'''
            <div style="display:flex;justify-content:space-between;font-size:10px;color:#666;margin-top:2px;">
                <span>Low: {min_rate:.1f}p</span>
                <span>Agile Outgoing Export (Region {self.region}){f" - Est. £{expected_revenue_gbp:.2f} ({num_slots} slots)" if num_slots > 0 else ""}</span>
                <span>High: {max_rate:.1f}p | SoC: {soc_drop_per_slot:.1f}%/slot</span>
            </div>
            ''')

            # Name the planned slots in text as well as on the strip.  The
            # orange "EX" badge is small and is the first thing to become
            # illegible on a narrow screen, so the plan must also be readable
            # as words.  An empty plan is stated too: it explains a decision
            # that declined to store solar for a later sale.
            priority = self._export_priority_slots(current_slot_idx)
            skipped_priority = priority - set(planned_slots)
            aggressive_slots = sorted(
                slot
                for slot, action in self._planned_slot_actions.items()
                if action == "aggressive"
            )
            if self._planned_slot_actions:
                # A qualifying slot that is used conservatively is not a
                # skipped export.  Only idle qualifying slots were left out.
                skipped_priority = {
                    slot
                    for slot in skipped_priority
                    if self._planned_slot_actions.get(slot) == "idle"
                }
            if planned_slots:
                plan_items = [
                    f"{self._slot_range_label(idx)} @ {rate_dict[idx]['rate']:.1f}p"
                    + (" (partial)" if idx == partial_slot_idx else "")
                    for idx in planned_slots
                    if idx in rate_dict
                ]
            else:
                plan_items = []
            if plan_items:
                if len(plan_items) > 6:
                    plan_items = plan_items[:6] + [
                        f"+{len(planned_slots) - 6} more"
                    ]
                plan_text = "Export plan: " + " · ".join(plan_items)
                if skipped_priority:
                    plan_text += (
                        f" · {len(skipped_priority)} qualifying slot(s) "
                        "skipped (headroom used)"
                    )
                if aggressive_slots:
                    plan_text += (
                        f" · {len(aggressive_slots)} aggressive "
                        "(conditional house + surplus export)"
                    )
                plan_colour = "#e65100"
            elif aggressive_slots:
                plan_text = (
                    f"Aggressive plan: {len(aggressive_slots)} conditional "
                    "slot(s) cover the house and export the surplus when the "
                    "load reaches the break-even — no full export fits"
                )
                plan_colour = "#e65100"
            else:
                # An empty plan is easy to misread as "nothing qualified".  If
                # slots did clear the break-even, say explicitly that the SoC
                # floor, not the prices, is what stopped the export.
                target = max(
                    config.MIN_AGILE_DISCHARGE_SOC,
                    self.absolute_min_soc_percent(),
                )
                absolute_floor = self.absolute_min_soc_percent()
                projected = self._projected_soc_at_2330
                if priority:
                    qualifier = (
                        f"{len(priority)} slot(s) beat the "
                        f"{self.export_house_break_even_p(config.IMPORT_HIGH):.1f}p "
                        "break-even, but "
                    )
                    if projected is None:
                        plan_text = (
                            "Export plan: none planned — " + qualifier
                            + "no 23:30 projection is available"
                        )
                    elif projected <= absolute_floor:
                        plan_text = (
                            "Export plan: none planned — " + qualifier
                            + f"the 23:30 projection is {projected:.0f}% "
                            f"against the {absolute_floor:.0f}% absolute minimum"
                        )
                    elif projected <= target:
                        plan_text = (
                            "Export plan: none planned — " + qualifier
                            + f"the 23:30 projection is {projected:.0f}% "
                            f"against the {target:.0f}% target"
                        )
                    else:
                        plan_text = (
                            "Export plan: none planned — " + qualifier
                            + f"the remaining {projected - absolute_floor:.1f}% "
                            f"above the {absolute_floor:.0f}% absolute minimum "
                            "is less than one export slot"
                        )
                    plan_colour = "#a15c00"
                else:
                    plan_text = "Export plan: none planned"
                    plan_colour = "#888"
            html.append(
                '<div style="font-size:10px;color:' + plan_colour + ";"
                'margin-top:1px;" title="Slots the planner will export in. '
                'They carry an orange border and an EX badge on the strip.">'
                + html_module.escape(plan_text)
                + "</div>"
            )
            
            # Add CSS for responsive behavior with progressive degradation
            html.append('''
            <style>
            /* Default: show full rate with 1dp */
            .agile-rate-cell .rate-value-full { display: inline; }
            .agile-rate-cell .rate-value-int { display: none; }
            .agile-rate-cell .rate-value-none { display: none; }

            /* Export/exported badge in the top-right corner of a cell.  It
               needs a fixed, small size and no wrap: the label is what marks
               a bulk-export slot, so it must survive every breakpoint. */
            .agile-rate-cell .agile-rate-badge {
                position: absolute;
                top: 2px;
                right: 2px;
                font-size: 8px;
                line-height: 1;
                font-weight: bold;
                white-space: nowrap;
                text-shadow: 0 0 2px #fff;
            }

            /* < 1200px: smaller fonts */
            @media (max-width: 1200px) {
                .agile-rate-cell { font-size: 9px !important; }
                .agile-rate-cell .rate-value-full { font-size: 8px !important; }
            }

            /* < 900px: integer rates only */
            @media (max-width: 900px) {
                .agile-rate-cell .rate-value-full { display: none; }
                .agile-rate-cell .rate-value-int { display: inline; }
                .agile-rate-cell { font-size: 8px !important; }
            }

            /* < 700px: hide rate values, show only color */
            @media (max-width: 700px) {
                .agile-rate-cell .rate-value-full { display: none; }
                .agile-rate-cell .rate-value-int { display: none; }
                .agile-rate-cell .rate-value-none { display: inline; }
                .agile-rate-cell { min-height: 28px; }
                .agile-rate-strip { height: 36px !important; }
                .agile-rate-cell .agile-rate-badge {
                    top: 1px; right: 1px; font-size: 7px;
                }
            }

            /* < 400px: keep a readable strip rather than squeezing the badge */
            @media (max-width: 400px) {
                .agile-rate-cell { min-height: 24px; }
                .agile-rate-strip { height: 32px !important; padding: 4px !important; }
                /* Cells are ~7px wide here, so the two-letter badge cannot
                   fit.  Keep a dot (currentColor) and let the export-plan
                   line below the strip carry the detail. */
                .agile-rate-cell .agile-rate-badge {
                    font-size: 0;
                    top: 2px;
                    right: 2px;
                    width: 4px;
                    height: 4px;
                    border-radius: 50%;
                    background: currentColor;
                    text-shadow: none;
                }
            }
            </style>
            ''')
            
            return ''.join(html)

    def is_off_peak(self, dayMinute: int) -> bool:
        """Check if current time is during off-peak period (23:30-05:30)"""
        return dayMinute >= 1410 or dayMinute < 330

    def is_expensive_period(self, dayMinute: int) -> bool:
        """No specifically expensive periods in Intelligent Octopus Go"""
        return False

    def calculate_target_discharge_current(self, current_soc: float, dayMinute: int) -> float:
        """Calculate discharge current to hit target SoC at bulk discharge end time."""
        if not self.ENABLE_BULK_DISCHARGE:
            return 0
        
        if dayMinute < self.BULK_DISCHARGE_START_TIME or dayMinute >= self.BULK_DISCHARGE_END_TIME:
            return 0
        
        minutes_until_bulk_discharge_end = self.BULK_DISCHARGE_END_TIME - dayMinute
        hours_until_bulk_discharge_end = minutes_until_bulk_discharge_end / 60.0
        soc_difference = current_soc - self.TARGET_SOC_AT_BULK_DISCHARGE_END
        
        if soc_difference <= 0:
            return 0
        
        required_discharge_rate = soc_difference / hours_until_bulk_discharge_end
        if required_discharge_rate < self.EFFICIENCY_MODEL.soc_rate_percent_per_hour(
            self.MIN_DISCHARGE_CURRENT
        ):
            return 0
        required_amps = self.EFFICIENCY_MODEL.discharge_current_for_soc_rate(
            required_discharge_rate,
            max_current_a=self.MAX_DISCHARGE_CURRENT,
        )
        required_amps = max(0, min(required_amps, self.MAX_DISCHARGE_CURRENT))
        
        if required_amps < self.MIN_DISCHARGE_CURRENT:
            return 0
        
        return required_amps

    def get_control_state(self, state: EvseAsyncState, dayMinute: int) -> tuple:
        """Determine charging strategy based on time, battery level, and export plan.

        Logic:
        - Off-peak (23:30-05:30): Charge at max rate (IOCTGO cheap rate) - ABSOLUTE PRIORITY
        - Battery depleted: Dormant
        - During planned export slots: Discharge at max rate
        - Energy shortfall: Bidirectional mode with conservative discharge
          (cover most of the house load, no planned export)
        - Other times: Three-tier decision for bidirectional vs export:
          1. Rate < STORAGE_FLOOR_THRESHOLD_P: STORE (too trivial to export)
          2. Rate < self-use value and low SoC: STORE (self-use value)
          3. Otherwise: STORE if future_rate × solar round trip > current_rate

        The solar round trip is evaluated at the harvest current implied by the
        live solar surplus, not at the 3 A floor, so a strong surplus is stored
        when a weak one would be exported.
        """
        battery_level = state.battery_level
        current_slot = dayMinute // 30
        target_soc = config.MIN_AGILE_DISCHARGE_SOC
        charge_cap = self.charge_cap_percent()

        # Roll the dashboard saving forward with the latest power sample.
        self._accumulate_daily_savings()

        # If we have just reached the 00:00 slot or the 16:00 slot then we should schedule fetching
        # the rates again.
        should_fetch_rates = (current_slot != self._last_plan_slot) and (current_slot == 0 or current_slot == 32)

        if should_fetch_rates:
            self._fetch_rates_async()

        # Recalculate export plan only at slot boundaries (every 30 minutes)
        # unless we triggered fetching the rates again (which should trigger a
        # recalculation by itself).
        # This prevents mid-slot thrashing where a high-value slot gets partially
        # completed then dropped due to SoC changes, only to be replaced by a
        # lower-value slot later. Once committed to a slot, see it through.
        should_recalculate = (current_slot != self._last_plan_slot)
        # We should also recalculate if the SoC has changed significantly
        # (by more than 3%). This will cover SoC unknown cases and cases where the
        # the EV has been plugged in after being disconnected and driven.
        should_recalculate = should_recalculate or abs(self._prev_battery_level - battery_level) > 3
        should_recalculate = should_recalculate and not should_fetch_rates

        if should_recalculate:
            self._planned_export_slots = self.calculate_export_plan(battery_level)
            self._last_plan_soc = battery_level
            self._last_plan_slot = current_slot
            self._plan_cache = self._planned_export_slots.copy()
            if self._planned_export_slots:
                info(f"IOCTGO_AGILEOUT: Export plan updated at slot boundary - {len(self._planned_export_slots)} slots planned")
        else:
            # Use cached plan
            self._planned_export_slots = self._plan_cache

        debug(f"IOCTGO_AGILEOUT: get_control_state - SoC={battery_level}%, dayMinute={dayMinute}, current_slot={current_slot}, planned_slots={self._planned_export_slots}")

        self._prev_battery_level = battery_level

        power_snapshot = self._read_power_snapshot()
        surplus_w = power_snapshot["surplus_w"] if power_snapshot else None

        # Supplier-requested sessions are whole-slot windows, so they outrank
        # the ordinary price logic while they are active.
        now = self.get_current_datetime()
        session = self._session_at(now)

        # Free Energy Session: import costs nothing, so fill the pack even when
        # the normal target SoC would say to stop.  The charge cap still holds.
        if session is not None and session.kind == FREE_IMPORT:
            charge_efficiency = (
                self.EFFICIENCY_MODEL.charge_efficiency(
                    self.GRID_CHARGE_REFERENCE_CURRENT
                ) * 100
            )
            if battery_level < charge_cap:
                self._record_decision(
                    mode=(
                        f"Free energy {session.start:%H:%M}–{session.end:%H:%M}"
                        f" · max charge ({self.MAX_CHARGE_CURRENT:.0f} A)"
                    ),
                    reason=(
                        f"{session.label} — your supplier is offering free "
                        f"energy; charging at {self.MAX_CHARGE_CURRENT:.0f} A "
                        f"to the {charge_cap:.0f}% cap"
                    ),
                    soc_percent=battery_level,
                    detail=(
                        f"Import is 0p for {session.start:%H:%M}–"
                        f"{session.end:%H:%M}, so the pack is filled regardless "
                        f"of the normal price comparison. SoC {battery_level}%, "
                        f"cap {charge_cap:.0f}%, charge efficiency "
                        f"{charge_efficiency:.0f}% at "
                        f"{self.GRID_CHARGE_REFERENCE_CURRENT:.0f} A. The "
                        "ledger books this energy at 0p."
                    ),
                    current_rate_p=0.0,
                    efficiency_pct=charge_efficiency,
                    efficiency_label="charge",
                    min_gain_p=self.MIN_PROFIT_MARGIN_P_PER_KWH,
                    charge_min_a=self.MAX_CHARGE_CURRENT,
                    charge_max_a=self.MAX_CHARGE_CURRENT,
                    charge_activation_w=0,
                    decision_tier="free",
                    import_rate_p=0.0,
                    charge_cap_percent=charge_cap,
                )
                return (
                    ControlState.CHARGE,
                    self.MAX_CHARGE_CURRENT,
                    self.MAX_CHARGE_CURRENT,
                    f"IOCTGO_AGILEOUT {session.label}: charge at max rate",
                )
            self._record_decision(
                mode=(
                    f"Free energy {session.start:%H:%M}–{session.end:%H:%M}"
                    f" · SoC at {charge_cap:.0f}% cap"
                ),
                reason=(
                    f"{session.label} — free import, but SoC {battery_level}% "
                    f"is at the {charge_cap:.0f}% cap"
                ),
                soc_percent=battery_level,
                detail=(
                    f"Import is 0p for {session.start:%H:%M}–"
                    f"{session.end:%H:%M}, but the pack is already at its "
                    f"{charge_cap:.0f}% charge cap, so no energy is taken."
                ),
                current_rate_p=0.0,
                decision_tier="free",
                import_rate_p=0.0,
                charge_cap_percent=charge_cap,
            )
            return (
                ControlState.DORMANT,
                None,
                None,
                f"IOCTGO_AGILEOUT {session.label}: SoC at cap",
            )

        # OFF-PEAK CHARGING HAS ABSOLUTE PRIORITY (23:30-05:30)
        # Charge at max rate regardless of SoC or other states
        if self.is_off_peak(dayMinute):
            cap = self.charge_cap_percent()
            charge_efficiency = (
                self.EFFICIENCY_MODEL.charge_efficiency(
                    self.GRID_CHARGE_REFERENCE_CURRENT
                ) * 100
            )
            if battery_level < cap:
                mode = f"Max charge ({self.MAX_CHARGE_CURRENT:.0f} A)"
                reason = (
                    f"off-peak {config.IMPORT_LOW:.1f}p — charging to the "
                    f"{cap:.0f}% cap"
                )
                self._record_decision(
                    mode=mode,
                    reason=reason,
                    soc_percent=battery_level,
                    detail=(
                        f"Off-peak import at {config.IMPORT_LOW:.1f}p/kWh. "
                        f"SoC {battery_level}%, cap {cap:.0f}%. Charge "
                        f"efficiency {charge_efficiency:.0f}% at "
                        f"{self.GRID_CHARGE_REFERENCE_CURRENT:.0f} A."
                    ),
                    current_rate_p=config.IMPORT_LOW,
                    efficiency_pct=charge_efficiency,
                    efficiency_label="charge",
                    min_gain_p=self.MIN_PROFIT_MARGIN_P_PER_KWH,
                    charge_min_a=self.MAX_CHARGE_CURRENT,
                    charge_max_a=self.MAX_CHARGE_CURRENT,
                    charge_activation_w=0,
                    decision_tier="off_peak",
                    import_rate_p=config.IMPORT_LOW,
                    charge_cap_percent=cap,
                )
                return (
                    ControlState.CHARGE,
                    self.MAX_CHARGE_CURRENT,
                    self.MAX_CHARGE_CURRENT,
                    "IOCTGO_AGILEOUT Off-peak: CHARGE AT MAX RATE (priority)",
                )
            else:
                self._record_decision(
                    mode=f"Idle — SoC at {cap:.0f}% cap",
                    reason=(
                        f"off-peak {config.IMPORT_LOW:.1f}p — no charge needed "
                        f"at SoC {battery_level}%"
                    ),
                    soc_percent=battery_level,
                    detail=(
                        f"Off-peak import at {config.IMPORT_LOW:.1f}p/kWh. "
                        f"SoC {battery_level}% is at or above the {cap:.0f}% "
                        "charge cap, so the battery is left alone."
                    ),
                    current_rate_p=config.IMPORT_LOW,
                    decision_tier="off_peak",
                    import_rate_p=config.IMPORT_LOW,
                    charge_cap_percent=cap,
                )
                return ControlState.DORMANT, None, None, "IOCTGO_AGILEOUT Off-peak: SoC max, dormant"

        # Handle unknown SoC (outside off-peak hours)
        if battery_level == -1:
            charge_efficiency = (
                self.EFFICIENCY_MODEL.charge_efficiency(
                    self.SOLAR_CHARGE_REFERENCE_CURRENT
                ) * 100
            )
            self._record_decision(
                mode="Charge at 3 A (SoC unknown)",
                reason="no BMS SoC reading — charging gently until it returns",
                soc_percent=battery_level,
                detail=(
                    "The vehicle has not reported a state of charge, so the "
                    "controller cannot project the 23:30 target or choose a "
                    "discharge strategy. It charges at the minimum current "
                    "instead."
                ),
                efficiency_pct=charge_efficiency,
                efficiency_label="charge",
                min_gain_p=self.MIN_PROFIT_MARGIN_P_PER_KWH,
                charge_min_a=3,
                charge_max_a=3,
                charge_activation_w=0,
                decision_tier="unknown_soc",
                import_rate_p=self._current_import_rate_p(dayMinute),
            )
            return ControlState.CHARGE, 3, 3, "IOCTGO_AGILEOUT SoC unknown, charge at 3A"

        # Battery depleted - protect battery
        if battery_level <= self.BATTERY_PROTECTION_RESERVE_SOC:
            self._record_decision(
                mode="Idle — battery protection",
                reason=(
                    f"SoC {battery_level}% at or below the "
                    f"{self.BATTERY_PROTECTION_RESERVE_SOC}% reserve"
                ),
                soc_percent=battery_level,
                detail=(
                    f"SoC {battery_level}% is at or below the "
                    f"{self.BATTERY_PROTECTION_RESERVE_SOC}% protection "
                    "reserve. The controller will not discharge the pack."
                ),
                decision_tier="protection",
                import_rate_p=self._current_import_rate_p(dayMinute),
                charge_cap_percent=charge_cap,
            )
            return ControlState.DORMANT, None, None, "IOCTGO_AGILEOUT Battery depleted"

        # Configured hard floor.  Discharge and export stop here even when the
        # price is attractive; the physical 25% reserve above still wins if it
        # is higher.  The floor applies both when the pack is already at the
        # minimum and when the 23:30 projection is below it.  Charging from
        # solar remains allowed, so this is not a DORMANT decision except for
        # grid-support, which is an export mode.
        absolute_floor = self.absolute_min_soc_percent()
        at_absolute_floor = battery_level <= absolute_floor
        discharge_inhibited = at_absolute_floor or self._discharge_inhibited
        if discharge_inhibited:
            self._discharge_policy = "inhibited"
            self._discharge_inhibited = True
            self._energy_shortfall = True
        if at_absolute_floor:
            floor_reason = (
                f"SoC {battery_level}% is at or below the "
                f"{absolute_floor:.0f}% absolute minimum"
            )
            floor_detail = (
                f"SoC {battery_level}% has reached the configured "
                f"{absolute_floor:.0f}% absolute minimum."
            )
        else:
            floor_reason = (
                "the projected 23:30 SoC would fall below the "
                f"{absolute_floor:.0f}% absolute minimum"
            )
            floor_detail = (
                f"The 23:30 projection is "
                f"{math.floor(self._projected_soc_at_2330)}%, below the "
                f"configured {absolute_floor:.0f}% absolute minimum."
                if self._projected_soc_at_2330 is not None
                else "The 23:30 projection is below the configured "
                f"{absolute_floor:.0f}% absolute minimum."
            )

        # Save Energy Session: the supplier has asked for reduced grid demand,
        # and for a battery-backed installation the useful response is to push
        # as much out as possible.  This sits above the export planner because
        # the reason for exporting is the request, not the quoted export price.
        # The absolute floor is the limit: at or below it the pack idles
        # instead of exporting, even though the mode is explicit.
        if (
            session is not None
            and session.kind == GRID_SUPPORT
            and at_absolute_floor
        ):
            self._record_decision(
                mode=(
                    f"Save Energy Session {session.start:%H:%M}–"
                    f"{session.end:%H:%M} · export inhibited"
                ),
                reason=(
                    f"{session.label} — SoC {battery_level}% is at or below "
                    f"the {absolute_floor:.0f}% absolute minimum, so the pack "
                    "will not export"
                ),
                soc_percent=battery_level,
                detail=(
                    f"{session.start:%H:%M}–{session.end:%H:%M} is a "
                    "supplier-requested grid support window, but SoC "
                    f"{battery_level}% has reached the configured "
                    f"{absolute_floor:.0f}% absolute minimum. The pack idles "
                    "instead of exporting. Raise absolute_min_soc_percent only "
                    "after checking the battery reserve."
                ),
                current_rate_p=self._current_export_rate_p(current_slot),
                discharge_min_a=0,
                discharge_max_a=0,
                discharge_aggressive=False,
                decision_tier="absolute_floor",
                import_rate_p=self._current_import_rate_p(dayMinute),
                charge_cap_percent=charge_cap,
            )
            return (
                ControlState.DORMANT,
                None,
                None,
                "IOCTGO_AGILEOUT Grid support inhibited at absolute minimum SoC",
            )

        if session is not None and session.kind == GRID_SUPPORT:
            export_rate_p = self._current_export_rate_p(current_slot)
            discharge_efficiency = (
                self.EFFICIENCY_MODEL.discharge_efficiency(
                    self.EXPORT_REFERENCE_CURRENT
                ) * 100
            )
            expected_gbp = (
                export_rate_p * self.EXPORT_POWER_KW * 0.5 / 100.0
            )
            self._record_decision(
                mode=(
                    f"Save Energy Session {session.start:%H:%M}–"
                    f"{session.end:%H:%M} · max export"
                ),
                reason=(
                    f"{session.label} — your supplier has asked for grid "
                    f"support; exporting at {self.MAX_DISCHARGE_CURRENT:.0f} A "
                    "regardless of the quoted export price"
                ),
                soc_percent=battery_level,
                detail=(
                    f"{session.start:%H:%M}–{session.end:%H:%M} is a "
                    "supplier-requested grid support window. SoC "
                    f"{battery_level}% is above the "
                    f"{absolute_floor:.0f}% absolute minimum, so the "
                    f"pack exports at {self.EXPORT_POWER_KW:.2f} kW. The quoted "
                    f"export price is {export_rate_p:.1f}p (expected "
                    f"£{expected_gbp:.2f} for the slot at "
                    f"{discharge_efficiency:.0f}% discharge efficiency), but "
                    "the support request is the reason for exporting, not the "
                    "price. The supplier reward is not booked in the ledger."
                ),
                current_rate_p=export_rate_p,
                efficiency_pct=discharge_efficiency,
                efficiency_label="discharge",
                min_gain_p=self.MIN_PROFIT_MARGIN_P_PER_KWH,
                discharge_min_a=self.MAX_DISCHARGE_CURRENT,
                discharge_max_a=self.MAX_DISCHARGE_CURRENT,
                discharge_aggressive=True,
                discharge_activation_w=0,
                decision_tier="grid_support",
                import_rate_p=self._current_import_rate_p(dayMinute),
                charge_cap_percent=charge_cap,
            )
            return (
                ControlState.DISCHARGE,
                None,
                None,
                f"IOCTGO_AGILEOUT {session.label}: export at max rate",
            )

        # Configured hard floor: no discharge or export, even at a good price.
        # Charging from surplus solar is still allowed, so the pack can recover
        # during the day without importing at the peak rate.
        if discharge_inhibited:
            charge_at_cap = battery_level >= charge_cap
            if charge_at_cap:
                solar_floor_a, solar_activation_w = 0.0, 0.0
                solar_reason = (
                    "the pack is at its charge cap, so surplus solar is "
                    "exported"
                )
            else:
                solar_floor_a, solar_activation_w = (
                    self._solar_charge_floor_for_slot(current_slot)
                )
                if solar_floor_a <= 0:
                    solar_reason = (
                        "surplus solar is worth more exported now, so "
                        "charging stays off"
                    )
                else:
                    solar_reason = (
                        f"surplus solar still charges at "
                        f"≥{solar_floor_a:.0f} A "
                        f"(≈{solar_activation_w:.0f} W) up to "
                        f"{self.MAX_CHARGE_CURRENT:.0f} A"
                    )
            self._record_decision(
                mode="Idle — absolute minimum SoC",
                reason=(
                    f"{floor_reason}, so the pack will not discharge; "
                    f"{solar_reason}"
                ),
                soc_percent=battery_level,
                detail=(
                    f"{floor_detail} The pack will not discharge or export, "
                    "even at an attractive price. "
                    + self._solar_charge_floor_text(
                        at_cap=charge_at_cap,
                        charge_min_a=solar_floor_a,
                        activation_w=solar_activation_w,
                    )
                ),
                current_rate_p=self._current_export_rate_p(current_slot),
                charge_min_a=0 if charge_at_cap else solar_floor_a,
                charge_max_a=(
                    0 if charge_at_cap else self.MAX_CHARGE_CURRENT
                ),
                charge_activation_w=0 if charge_at_cap else solar_activation_w,
                discharge_min_a=0,
                discharge_max_a=0,
                discharge_aggressive=False,
                discharge_activation_w=self.NEVER_DISCHARGE_ACTIVATION_W,
                decision_tier="absolute_floor",
                import_rate_p=self._current_import_rate_p(dayMinute),
                charge_cap_percent=charge_cap,
            )
            return (
                ControlState.LOAD_FOLLOW_BIDIRECTIONAL,
                None,
                None,
                "IOCTGO_AGILEOUT Absolute minimum SoC - discharge inhibited",
            )

        # Check if current slot is a planned export slot
        if current_slot in self._planned_export_slots:
            # Export at maximum rate during planned slots
            export_rate_p = self._current_export_rate_p(current_slot)
            import_price_p = self._current_import_rate_p(dayMinute)
            break_even_p = self.export_house_break_even_p(import_price_p)
            export_is_priority = (
                current_slot in self._export_priority_slots(current_slot)
            )
            discharge_efficiency = (
                self.EFFICIENCY_MODEL.discharge_efficiency(
                    self.EXPORT_REFERENCE_CURRENT
                ) * 100
            )
            expected_gbp = (
                export_rate_p * self.EXPORT_POWER_KW * 0.5 / 100.0
            )
            debug(f"IOCTGO_AGILEOUT: In export slot {current_slot}, commanding DISCHARGE")
            self._record_decision(
                mode=(
                    f"Max discharge ({self.MAX_DISCHARGE_CURRENT:.0f} A) · "
                    + (
                        "export priority"
                        if export_is_priority
                        else "aggressive"
                    )
                ),
                reason=(
                    f"planned export slot {self._slot_range_label(current_slot)} "
                    f"at {export_rate_p:.1f}p"
                    + (
                        f" — clears the {break_even_p:.1f}p house-offset "
                        "break-even"
                        if export_is_priority
                        else ""
                    )
                ),
                soc_percent=battery_level,
                detail=(
                    f"Export slot {self._slot_range_label(current_slot)} at "
                    f"{export_rate_p:.1f}p/kWh. SoC {battery_level}%, "
                    f"expected {expected_gbp:.2f} for the slot at "
                    f"{self.EXPORT_POWER_KW:.2f} kW, discharge efficiency "
                    f"{discharge_efficiency:.0f}%."
                    + (
                        f" The break-even against offsetting house load at "
                        f"~{self.SELF_USE_DISCHARGE_REFERENCE_CURRENT:.0f} A "
                        f"is {break_even_p:.1f}p "
                        f"({self.self_use_discharge_efficiency() * 100:.0f}% "
                        f"÷ {self.export_discharge_efficiency() * 100:.0f}% "
                        f"× {import_price_p:.1f}p import), so this slot is "
                        "given priority."
                        if export_is_priority
                        else ""
                    )
                ),
                current_rate_p=export_rate_p,
                target_rate_p=export_rate_p,
                target_slot=current_slot,
                efficiency_pct=discharge_efficiency,
                efficiency_label="discharge",
                min_gain_p=self.MIN_PROFIT_MARGIN_P_PER_KWH,
                discharge_min_a=self.MAX_DISCHARGE_CURRENT,
                discharge_max_a=self.MAX_DISCHARGE_CURRENT,
                discharge_aggressive=True,
                decision_tier=(
                    "export_priority" if export_is_priority else "planned_export"
                ),
                import_rate_p=import_price_p,
                charge_cap_percent=charge_cap,
            )
            return (
                ControlState.DISCHARGE,
                self.MAX_DISCHARGE_CURRENT,
                self.MAX_DISCHARGE_CURRENT,
                "IOCTGO_AGILEOUT Export slot (max discharge)",
            )

        # The planner may have selected this slot for aggressive load-following
        # even though the day as a whole is projected to be short.  Aggressive
        # slots are the small surplus-export top-up after full exports; they
        # must not be overridden by the global conservative policy or by the
        # reserve-for-a-later-export branch, because the plan already budgeted
        # their extra SoC.
        slot_action = self._planned_slot_actions.get(current_slot)
        force_aggressive = slot_action == "aggressive"

        # Shortfall with a high-value export still ahead: keep the energy for
        # that export instead of spending it on the house at 3 A.  This is the
        # same comparison as the per-slot priority rule, applied across time:
        # importing the house now is cheaper than losing a later export that
        # clears the house-offset break-even.
        reserve_slots = self._reserve_slots_for_priority_export(
            current_slot, dayMinute
        )
        if self._energy_shortfall and not force_aggressive and reserve_slots:
            harvest_current_a = self._estimate_solar_harvest_current()
            target_slot = max(
                reserve_slots,
                key=lambda slot: self._current_export_rate_p(slot),
            )
            target_rate_p = self._current_export_rate_p(target_slot)
            import_price_p = self._current_import_rate_p(dayMinute)
            break_even_p = self.export_house_break_even_p(import_price_p)
            projected = self._projected_soc_at_2330
            projection_text = (
                f"{math.floor(projected)}%"
                if projected is not None
                else "below floor"
            )
            detail_projection = (
                f"projected 23:30 SoC {math.floor(projected)}%"
                if projected is not None
                else "23:30 SoC projection unavailable"
            )
            house_floor = self._house_load_floor_soc()
            slot_text = ", ".join(
                f"{self._slot_range_label(slot)} @ "
                f"{self._current_export_rate_p(slot):.1f}p"
                for slot in reserve_slots[:3]
            )
            if len(reserve_slots) > 3:
                slot_text += f" (+{len(reserve_slots) - 3} more)"
            at_cap = battery_level >= charge_cap
            if at_cap:
                charge_min_a, charge_activation_w = 0.0, 0.0
                charge_mode = (
                    f"Do not charge — SoC at {charge_cap:.0f}% cap · "
                    "reserve for export"
                )
            else:
                charge_min_a, charge_activation_w = (
                    self._solar_charge_floor_for_slot(current_slot)
                )
                if charge_min_a <= 0:
                    charge_mode = (
                        "Do not charge — surplus worth more exported · "
                        "reserve for export"
                    )
                else:
                    charge_mode = (
                        f"Charge from surplus ≥{charge_min_a:.0f} A · "
                        "reserve for export"
                    )
            self._record_decision(
                mode=charge_mode,
                reason=(
                    f"projected 23:30 SoC {projection_text} < "
                    f"{house_floor:.0f}% house floor — holding for "
                    f"{target_rate_p:.1f}p export"
                ),
                soc_percent=battery_level,
                detail=(
                    f"SoC {battery_level}%, {detail_projection}. Current "
                    f"import is {import_price_p:.1f}p, while the best planned "
                    f"export is {target_rate_p:.1f}p, above the "
                    f"{break_even_p:.1f}p house-offset break-even. Discharge is "
                    "paused so the pack is not spent covering the house now. "
                    f"High-value slots: {slot_text}. "
                    + self._solar_charge_floor_text(
                        at_cap=at_cap,
                        charge_min_a=charge_min_a,
                        activation_w=charge_activation_w,
                    )
                ),
                target_soc_percent=target_soc,
                projected_soc_at_2330=projected,
                current_rate_p=self._current_export_rate_p(current_slot),
                target_rate_p=target_rate_p,
                target_slot=target_slot,
                harvest_current_a=harvest_current_a,
                surplus_w=surplus_w,
                efficiency_pct=self.export_discharge_efficiency() * 100,
                efficiency_label="discharge",
                min_gain_p=self.MIN_PROFIT_MARGIN_P_PER_KWH,
                charge_min_a=charge_min_a,
                charge_max_a=(
                    0.0 if charge_min_a <= 0 else self.MAX_CHARGE_CURRENT
                ),
                charge_activation_w=charge_activation_w,
                discharge_min_a=0,
                discharge_max_a=0,
                discharge_aggressive=False,
                discharge_activation_w=self.NEVER_DISCHARGE_ACTIVATION_W,
                decision_tier="export_reserve",
                shortfall=True,
                import_rate_p=import_price_p,
                house_floor_soc=house_floor,
                charge_cap_percent=charge_cap,
                stored_cost_p=self._delivered_cost_per_kwh_p(),
                voltage_v=power_snapshot["voltage"] if power_snapshot else None,
            )
            return (
                ControlState.LOAD_FOLLOW_BIDIRECTIONAL,
                None,
                None,
                "IOCTGO_AGILEOUT Shortfall: reserve energy for planned export",
            )

        # Energy shortfall: conserve energy for the house rather than exporting.
        # Stay bidirectional so surplus solar can still be absorbed, but use
        # the conservative discharge configuration in set_home_demand_levels().
        if self._energy_shortfall and not force_aggressive:
            harvest_current_a = self._estimate_solar_harvest_current()
            debug(
                f"IOCTGO_AGILEOUT: Energy shortfall (SoC {battery_level}%), "
                f"using conservative bidirectional mode"
            )
            charge_at_cap = battery_level >= charge_cap
            if charge_at_cap:
                charge_min_a, charge_activation_w = 0.0, 0.0
                mode = (
                    f"Do not charge — SoC at {charge_cap:.0f}% cap · "
                    "conservative discharge"
                )
            else:
                charge_min_a, charge_activation_w = (
                    self._solar_charge_floor_for_slot(current_slot)
                )
                if charge_min_a <= 0:
                    mode = (
                        "Do not charge — surplus worth more exported · "
                        "conservative discharge"
                    )
                else:
                    mode = (
                        f"Charge from surplus ≥{charge_min_a:.0f} A · "
                        "conservative discharge"
                    )
            projected = self._projected_soc_at_2330
            # Display the projection floored, not rounded.  With a 45% house
            # floor a projection of 44.6% would otherwise read
            # "45% < 45% house floor", which looks like a contradiction even
            # though the comparison itself is correct.
            projection_text = (
                f"{math.floor(projected)}%" if projected is not None else "below floor"
            )
            detail_projection = (
                f"projected 23:30 SoC {math.floor(projected)}%"
                if projected is not None
                else "23:30 SoC projection unavailable"
            )
            house_floor = self._house_load_floor_soc()
            try:
                margin = max(0.0, float(config.HOUSE_LOAD_MARGIN_PERCENT))
            except (TypeError, ValueError):
                margin = 5.0
            # Conservative discharge deliberately refuses to start below the
            # Wallbox's minimum output, so this is usually the 3 A floor.
            activation_w = self._current_discharge_activation_w(
                current_slot, dayMinute, aggressive=False
            )
            activation_text = self._activation_note(activation_w)
            if charge_at_cap:
                reason = (
                    f"SoC {battery_level}% at the {charge_cap:.0f}% cap — "
                    "conserving energy"
                )
            elif projected is not None:
                reason = (
                    f"projected 23:30 SoC {projection_text} < "
                    f"{house_floor:.0f}% house floor — conserving energy"
                )
            else:
                reason = (
                    f"projected to miss the {house_floor:.0f}% house floor — "
                    "conserving energy"
                )
            self._record_decision(
                mode=mode,
                reason=reason,
                soc_percent=battery_level,
                detail=(
                    f"SoC {battery_level}%, {detail_projection}. "
                    f"House floor {house_floor:.0f}% "
                    f"({target_soc:.0f}% bulk target − {margin:.0f}% "
                    f"flexibility). "
                    f"Conservative discharge {activation_text}. "
                    f"Stored energy costs "
                    f"{self._delivered_cost_per_kwh_p():.1f}p/kWh delivered "
                    f"at the {self.MIN_DISCHARGE_CURRENT:.0f} A minimum plus a "
                    f"{self.MIN_PROFIT_MARGIN_P_PER_KWH:.1f}p margin. "
                    f"Harvest estimate {harvest_current_a:.0f} A. "
                    + self._solar_charge_floor_text(
                        at_cap=charge_at_cap,
                        charge_min_a=charge_min_a,
                        activation_w=charge_activation_w,
                    )
                ),
                target_soc_percent=target_soc,
                projected_soc_at_2330=projected,
                current_rate_p=self._current_export_rate_p(current_slot),
                harvest_current_a=harvest_current_a,
                surplus_w=surplus_w,
                min_gain_p=self.MIN_PROFIT_MARGIN_P_PER_KWH,
                charge_min_a=charge_min_a,
                charge_max_a=(
                    0.0 if charge_min_a <= 0 else self.MAX_CHARGE_CURRENT
                ),
                charge_activation_w=charge_activation_w,
                discharge_min_a=3,
                discharge_max_a=self.MAX_DISCHARGE_CURRENT,
                discharge_aggressive=False,
                discharge_activation_w=activation_w,
                decision_tier="shortfall",
                shortfall=True,
                import_rate_p=self._current_import_rate_p(dayMinute),
                house_floor_soc=house_floor,
                charge_cap_percent=charge_cap,
                stored_cost_p=self._delivered_cost_per_kwh_p(),
                voltage_v=power_snapshot["voltage"] if power_snapshot else None,
            )
            return (
                ControlState.LOAD_FOLLOW_BIDIRECTIONAL,
                3,
                self.MAX_DISCHARGE_CURRENT,
                "IOCTGO_AGILEOUT Shortfall: conserve energy for house load",
            )

        # All other times: Decide between load-follow discharge and bidirectional mode
        # Get current slot's export rate for comparison
        # No rates means 0p; the tier logic then stores rather than exports.
        current_export_rate_p = self._current_export_rate_p(current_slot)

        # Determine if bidirectional mode is better than export (uses SoC-aware
        # logic).  Cost the solar at the current the surplus will actually
        # support, not at the 3 A floor: a sunny slot can harvest at 6-12 A,
        # where the round trip is materially better.
        harvest_current_a = self._estimate_solar_harvest_current()
        decision = self._evaluate_storage_decision(
            current_slot,
            current_export_rate_p,
            battery_level,
            harvest_current_a=harvest_current_a,
        )
        efficiency_pct = (
            decision.round_trip * 100 if decision.round_trip is not None else None
        )
        target_slot = decision.target_slot
        target_rate_p = decision.future_rate_p

        if decision.use_bidirectional:
            harvest_label = (
                f"Harvest {harvest_current_a:.0f} A"
                if harvest_current_a > self.SOLAR_CHARGE_REFERENCE_CURRENT
                else f"Harvest ≥{self.SOLAR_CHARGE_REFERENCE_CURRENT:.0f} A"
            )
            charge_at_cap = battery_level >= charge_cap
            activation_w = self._current_discharge_activation_w(
                current_slot, dayMinute, aggressive=True
            )
            activation_text = self._activation_note(activation_w)
            if charge_at_cap:
                mode = (
                    f"Do not charge — SoC at {charge_cap:.0f}% cap · "
                    "aggressive discharge"
                )
                reason = (
                    f"SoC {battery_level}% at the {charge_cap:.0f}% cap — "
                    "covering the house instead of storing"
                )
            else:
                mode = f"{harvest_label} · aggressive discharge"
                reason = decision.reason
            reason = (
                f"{reason.rstrip('.')}. "
                f"{self._activation_explanation(activation_w)}"
            )
            # Bidirectional mode: charge from solar, prefer small export over any import
            debug(f"IOCTGO_AGILEOUT: Using LOAD_FOLLOW_BIDIRECTIONAL (current rate {current_export_rate_p:.1f}p, SoC {battery_level}%)")
            self._record_decision(
                mode=mode,
                reason=reason,
                soc_percent=battery_level,
                detail=(
                    f"{decision.tier.capitalize()} decision. SoC {battery_level}%, "
                    f"current export {current_export_rate_p:.1f}p, harvest "
                    f"estimate {harvest_current_a:.0f} A, surplus "
                    f"{surplus_w:.0f} W. Aggressive discharge {activation_text}. "
                    "Stored energy is valued at the higher of its ledger "
                    "cost and its best alternative export use; the break-even "
                    "adds the delivery loss at the actual discharge current "
                    f"and a {self.MIN_PROFIT_MARGIN_P_PER_KWH:.1f}p/kWh "
                    "margin. "
                    "Detailed slot and SoC projections are in the log."
                    if surplus_w is not None
                    else (
                        f"{decision.tier.capitalize()} decision. SoC "
                        f"{battery_level}%, current export "
                        f"{current_export_rate_p:.1f}p, harvest estimate "
                        f"{harvest_current_a:.0f} A. No live power sample yet. "
                        f"Aggressive discharge {activation_text}."
                    )
                ),
                target_soc_percent=target_soc,
                projected_soc_at_2330=self._projected_soc_at_2330,
                current_rate_p=current_export_rate_p,
                target_rate_p=target_rate_p,
                target_slot=target_slot,
                harvest_current_a=harvest_current_a,
                surplus_w=surplus_w,
                efficiency_pct=efficiency_pct,
                efficiency_label=decision.efficiency_label,
                min_gain_p=self.MIN_PROFIT_MARGIN_P_PER_KWH,
                charge_min_a=0 if charge_at_cap else 3,
                charge_max_a=0 if charge_at_cap else self.MAX_CHARGE_CURRENT,
                charge_activation_w=720,
                discharge_min_a=3,
                discharge_max_a=self.MAX_DISCHARGE_CURRENT,
                discharge_aggressive=True,
                discharge_activation_w=activation_w,
                decision_tier=decision.tier,
                store_decision=True,
                import_rate_p=self._current_import_rate_p(dayMinute),
                house_floor_soc=self._house_load_floor_soc(),
                charge_cap_percent=charge_cap,
                stored_cost_p=self._delivered_cost_per_kwh_p(),
                voltage_v=power_snapshot["voltage"] if power_snapshot else None,
            )
            return ControlState.LOAD_FOLLOW_BIDIRECTIONAL, 3, self.MAX_DISCHARGE_CURRENT, "IOCTGO_AGILEOUT Bidirectional (store for better rate)"
        else:
            # Self-use load-follow.  The pack is not being filled for a later
            # sale, so it covers the house; a surplus is still taken, but only
            # once it supports the minimum harvest current.  That keeps a
            # trickle from displacing export revenue while letting a strong
            # surplus charge at a current where the round trip is worthwhile.
            best_future_text = (
                f"{target_rate_p:.1f}p"
                if target_rate_p is not None
                else "no future rate"
            )
            charge_at_cap = battery_level >= charge_cap
            activation_w = self._current_discharge_activation_w(
                current_slot, dayMinute, aggressive=True
            )
            activation_text = self._activation_note(activation_w)
            committed_slot, committed_rate = self._best_planned_export_slot_rate(
                current_slot
            )
            committed_note = (
                f"The {committed_rate:.1f}p "
                f"{self._slot_range_label(committed_slot)} slot is already "
                "committed to energy in the pack, so only an unused slot can "
                "sell extra solar. "
                if committed_slot is not None
                and committed_rate > 0
                and (target_rate_p is None or committed_rate > target_rate_p)
                else ""
            )
            reason = (
                f"SoC {battery_level}% at the {charge_cap:.0f}% cap — covering "
                "the house instead of storing"
                if charge_at_cap
                else decision.reason
            )
            reason = (
                f"{reason.rstrip('.')}. "
                f"{self._activation_explanation(activation_w)}"
            )
            charge_floor_a, self_use_floor, store_floor = (
                self._effective_charge_floor_a(
                    current_export_rate_p, target_rate_p
                )
            )
            if charge_at_cap:
                charge_min_a = 0.0
                mode = (
                    f"Do not charge — SoC at {charge_cap:.0f}% cap · "
                    "aggressive discharge"
                )
                charge_note = (
                    f"Charging is inhibited at the {charge_cap:.0f}% cap. "
                )
            elif charge_floor_a is None:
                # Neither use clears the export price, so keeping the solar
                # would destroy more value than it delivers.  Close the charge
                # range rather than leave the previous mode's range in place.
                charge_min_a = 0.0
                best_value_p, best_current_a = self._best_self_use_value_p()
                mode = (
                    "Do not charge — surplus worth more exported · "
                    f"aggressive discharge ≥{self.MIN_DISCHARGE_CURRENT:.0f} A"
                )
                charge_note = (
                    f"No measured charge current makes self-use or a later "
                    f"sale worth more than exporting at "
                    f"{current_export_rate_p:.1f}p now: the best self-use "
                    f"value is {best_value_p:.1f}p at {best_current_a:.0f} A "
                    f"after the round trip, so the surplus is sold instead. "
                )
            else:
                charge_min_a = charge_floor_a
                floor_activation_w = self._charge_activation_w_for_current(
                    charge_floor_a
                )
                # The floor is armed even when the live surplus cannot reach
                # it: a later, stronger surplus may still make charging worth
                # more than exporting.  Say so, rather than implying the car is
                # charging now.  At 22:29 with a 19.2p export the floor is 8 A
                # (self-use worth 19.5p) but the surplus is 3 A, so the honest
                # line is "not yet".
                waiting_for_surplus = (
                    harvest_current_a < charge_floor_a
                    or (
                        surplus_w is not None
                        and surplus_w < floor_activation_w
                    )
                )
                mode = (
                    (
                        f"Do not charge yet — will charge from "
                        f"≥{charge_floor_a:.0f} A surplus"
                    )
                    if waiting_for_surplus
                    else f"Charge from surplus ≥{charge_floor_a:.0f} A"
                )
                mode = (
                    f"{mode} · aggressive discharge "
                    f"≥{self.MIN_DISCHARGE_CURRENT:.0f} A"
                )
                below_text = (
                    f"Below {charge_floor_a:.0f} A the surplus is worth more "
                    "exported. "
                    if waiting_for_surplus
                    else ""
                )
                if (
                    store_floor is not None
                    and store_floor == charge_floor_a
                    and (
                        self_use_floor is None
                        or store_floor < self_use_floor
                    )
                ):
                    self_use_text = (
                        f"self-use alone would need {self_use_floor:.0f} A"
                        if self_use_floor is not None
                        else (
                            "self-use alone never clears "
                            f"{current_export_rate_p:.1f}p"
                        )
                    )
                    charge_note = (
                        f"{below_text}"
                        f"Charging starts at {charge_floor_a:.0f} A because "
                        f"the {target_rate_p:.1f}p slot makes storing worth "
                        f"more than exporting at {current_export_rate_p:.1f}p "
                        f"now; {self_use_text}. "
                        "The charge and discharge ranges are independent, so "
                        "a stronger surplus can charge while the pack still "
                        "covers the house. "
                    )
                else:
                    charge_note = (
                        f"{below_text}"
                        f"At {charge_floor_a:.0f} A the self-use value "
                        f"({self.self_use_value_threshold_p(charge_floor_a):.1f}p) "
                        f"beats exporting at {current_export_rate_p:.1f}p now. "
                        "The charge and discharge ranges are independent, so a "
                        "stronger surplus can charge while the pack still "
                        "covers the house. "
                    )
            charge_activation_w = self._charge_activation_w_for_current(
                charge_min_a
            )
            debug(
                f"IOCTGO_AGILEOUT: Using LOAD_FOLLOW_BIDIRECTIONAL self-use "
                f"(current rate {current_export_rate_p:.1f}p, "
                f"SoC {battery_level}%, charge "
                f"≥{charge_min_a:.0f}A, discharge "
                f"≥{self.MIN_DISCHARGE_CURRENT:.0f}A)"
            )
            self._record_decision(
                mode=mode,
                reason=reason,
                soc_percent=battery_level,
                detail=(
                    f"SoC {battery_level}%, "
                    f"current export {current_export_rate_p:.1f}p, harvest "
                    f"estimate {harvest_current_a:.0f} A, best unused slot "
                    f"{best_future_text}. Aggressive discharge {activation_text}. "
                    f"{charge_note}"
                    f"{committed_note}"
                    "Stored energy costs "
                    f"{self._delivered_cost_per_kwh_p():.1f}p/kWh delivered "
                    f"at the {self.MIN_DISCHARGE_CURRENT:.0f} A minimum plus a "
                    f"{self.MIN_PROFIT_MARGIN_P_PER_KWH:.1f}p margin. "
                    "Detailed slot and SoC projections are in the log."
                ),
                target_soc_percent=target_soc,
                projected_soc_at_2330=self._projected_soc_at_2330,
                current_rate_p=current_export_rate_p,
                target_rate_p=target_rate_p,
                target_slot=target_slot,
                harvest_current_a=harvest_current_a,
                surplus_w=surplus_w,
                efficiency_pct=efficiency_pct,
                efficiency_label=decision.efficiency_label,
                min_gain_p=self.MIN_PROFIT_MARGIN_P_PER_KWH,
                charge_min_a=charge_min_a,
                charge_max_a=(
                    0 if charge_min_a <= 0 else self.MAX_CHARGE_CURRENT
                ),
                charge_activation_w=charge_activation_w,
                discharge_min_a=self.MIN_DISCHARGE_CURRENT,
                discharge_max_a=self.MAX_DISCHARGE_CURRENT,
                discharge_aggressive=True,
                discharge_activation_w=activation_w,
                decision_tier=decision.tier,
                store_decision=False,
                import_rate_p=self._current_import_rate_p(dayMinute),
                house_floor_soc=self._house_load_floor_soc(),
                charge_cap_percent=charge_cap,
                stored_cost_p=self._delivered_cost_per_kwh_p(),
                voltage_v=power_snapshot["voltage"] if power_snapshot else None,
            )
            return (
                ControlState.LOAD_FOLLOW_BIDIRECTIONAL,
                None,
                None,
                "IOCTGO_AGILEOUT Bidirectional self-use "
                f"(charge ≥{charge_min_a:.0f} A)",
            )

    def set_home_demand_levels(self, evseController, state: EvseAsyncState, dayMinute: int):
        """Configure home demand power levels.

        In normal operation the existing tier logic chooses between storing
        solar for a later sale and self-use load-following. The discharge
        activation is not fixed: it is the smallest house import that repays
        the stored energy after the forced minimum output is accounted for.

        When the decision is to *not* store (self-use), the controller stays
        bidirectional so the house can still discharge while the car absorbs a
        surplus. The charge floor is not a fixed current: it is the lower of
        the smallest measured current whose self-use value beats exporting now
        and the smallest whose sale in the best unused slot does. If neither
        qualifies - or the pack is at its charge cap - the charge range is
        explicitly closed to 0 A, so a stale range from the previous mode
        cannot persist.

        When the day is projected to end in an energy shortfall, the system
        instead discharges conservatively, with the activation floored at the
        Wallbox minimum (3 A, ~720 W) and a -0.5 A bias, so it covers the house
        load without intentionally exporting.  The solar charge path in that
        mode uses the same break-even floor as the normal self-use branch, so
        a high export price can close it instead of accepting a 3 A trickle.
        """
        if self.SMART_OCPP_OPERATION:
            self._ocpp_check_turn_on(state, dayMinute)

        if not hasattr(self, 'evseController'):
            self.evseController = evseController
            self.original_calculation_method = evseController.use_new_current_calculation
            evseController.use_new_current_calculation = True

        battery_level = state.battery_level
        current_slot = dayMinute // 30

        # At or above the effective ceiling the pack must not accept charge from
        # any source, including surplus solar.  Outside off-peak/export slots
        # that means a zero charge range rather than the usual 3 A floor.
        charge_at_cap = battery_level >= self.charge_cap_percent()

        # Supplier-requested sessions command a single fixed mode, so
        # load-follow ranges must not be configured on top of them.
        session = self._session_at(self.get_current_datetime())
        if session is not None:
            debug(
                f"IOCTGO_AGILEOUT: {session.label} active - skipping "
                "load-following configuration"
            )
            if session.kind == GRID_SUPPORT and state.current < -1:
                if current_slot not in self._exported_slots:
                    self._exported_slots.append(current_slot)
                    info(
                        f"IOCTGO_AGILEOUT: Export started in slot {current_slot} "
                        f"({dayMinute//60:02d}:{dayMinute%60:02d}) for "
                        f"{session.label}"
                    )
            return

        # During off-peak hours (23:30-05:30), don't configure load-following
        # The control state is already set to CHARGE at max current by get_control_state()
        if self.is_off_peak(dayMinute):
            debug(f"IOCTGO_AGILEOUT: Off-peak hours - skipping load-following configuration")
            return  # Don't configure load-following during off-peak charging

        # If we're in an export slot, don't configure load-following
        # The control state is already set to DISCHARGE at max current
        if current_slot in self._planned_export_slots:
            # Export slot - max discharge already commanded by get_control_state()
            # Just track that we're exporting
            if state.current < -1:  # More than 1A discharge
                if current_slot not in self._exported_slots:
                    self._exported_slots.append(current_slot)
                    info(f"IOCTGO_AGILEOUT: Export started in slot {current_slot} ({dayMinute//60:02d}:{dayMinute%60:02d})")
            return  # Don't configure load-following during export slots

        # A planned aggressive slot must execute as aggressive load-following,
        # even when the day as a whole is in shortfall.  The planner has
        # already budgeted the extra SoC for this slot's surplus export.
        slot_action = self._planned_slot_actions.get(current_slot)
        force_aggressive = slot_action == "aggressive"

        # Absolute floor: close the discharge range entirely.  The pack may
        # still absorb surplus solar, so charging stays available unless the
        # battery is already at its cap.
        if self._discharge_inhibited:
            if charge_at_cap:
                charge_min_a, charge_activation_w = 0.0, 0.0
            else:
                charge_min_a, charge_activation_w = (
                    self._solar_charge_floor_for_slot(current_slot)
                )
            evseController.setDischargeActivationPower(
                self.NEVER_DISCHARGE_ACTIVATION_W
            )
            evseController.setDischargeCurrentBias(0)
            evseController.setDischargeCurrentRange(0, 0)
            evseController.setChargeActivationPower(charge_activation_w)
            evseController.setChargeCurrentBias(-0.5)
            evseController.setChargeCurrentRange(
                charge_min_a,
                0.0 if charge_min_a <= 0 else self.MAX_CHARGE_CURRENT,
            )
            self._update_snapshot_ranges(
                charge_min_a=charge_min_a,
                charge_max_a=(
                    0.0 if charge_min_a <= 0 else self.MAX_CHARGE_CURRENT
                ),
                charge_activation_w=charge_activation_w,
                discharge_min_a=0,
                discharge_max_a=0,
                discharge_aggressive=False,
                discharge_activation_w=self.NEVER_DISCHARGE_ACTIVATION_W,
            )
            debug(
                "IOCTGO_AGILEOUT: Absolute minimum SoC - discharge range "
                f"closed (0 A), solar charge floor {charge_min_a:.0f} A"
            )
            return

        # Shortfall with a high-value export ahead: hold the pack for the
        # export rather than covering the house now.  The pack may still absorb
        # surplus solar, so charging stays available unless it is at the cap.
        if (
            self._energy_shortfall
            and not force_aggressive
            and self._reserve_slots_for_priority_export(current_slot, dayMinute)
        ):
            if charge_at_cap:
                charge_min_a, charge_activation_w = 0.0, 0.0
            else:
                charge_min_a, charge_activation_w = (
                    self._solar_charge_floor_for_slot(current_slot)
                )
            evseController.setDischargeActivationPower(
                self.NEVER_DISCHARGE_ACTIVATION_W
            )
            evseController.setDischargeCurrentBias(0)
            evseController.setDischargeCurrentRange(0, 0)
            evseController.setChargeActivationPower(charge_activation_w)
            evseController.setChargeCurrentBias(-0.5)
            evseController.setChargeCurrentRange(
                charge_min_a,
                0.0 if charge_min_a <= 0 else self.MAX_CHARGE_CURRENT,
            )
            self._update_snapshot_ranges(
                charge_min_a=charge_min_a,
                charge_max_a=(
                    0.0 if charge_min_a <= 0 else self.MAX_CHARGE_CURRENT
                ),
                charge_activation_w=charge_activation_w,
                discharge_min_a=0,
                discharge_max_a=0,
                discharge_aggressive=False,
                discharge_activation_w=self.NEVER_DISCHARGE_ACTIVATION_W,
            )
            debug(
                "IOCTGO_AGILEOUT: Shortfall with planned priority export - "
                f"discharge closed, solar charge floor {charge_min_a:.0f} A"
            )
            return

        # Energy shortfall: conserve energy. Stay bidirectional so surplus
        # solar is still absorbed, but discharge conservatively - cover most
        # of the house load and leave a small import rather than intentionally
        # exporting. This replaces the fixed SOC_THRESHOLD_FOR_STRATEGY test.
        if self._energy_shortfall and not force_aggressive:
            # Conservative discharge floors the activation at the Wallbox's
            # minimum output (3 A, ~720 W), so it never starts a discharge
            # that would have to export at the activation point.
            activation_w = self._current_discharge_activation_w(
                current_slot, dayMinute, aggressive=False
            )
            if charge_at_cap:
                charge_min_a, charge_activation_w = 0.0, 0.0
            else:
                charge_min_a, charge_activation_w = (
                    self._solar_charge_floor_for_slot(current_slot)
                )
            evseController.setDischargeActivationPower(activation_w)
            evseController.setDischargeCurrentBias(
                self.CONSERVATIVE_DISCHARGE_BIAS_A
            )
            evseController.setDischargeCurrentRange(3, self.MAX_DISCHARGE_CURRENT)
            evseController.setChargeActivationPower(charge_activation_w)
            evseController.setChargeCurrentBias(-0.5)
            evseController.setChargeCurrentRange(
                charge_min_a,
                0.0 if charge_min_a <= 0 else self.MAX_CHARGE_CURRENT,
            )
            self._update_snapshot_ranges(
                charge_min_a=charge_min_a,
                charge_max_a=(
                    0.0 if charge_min_a <= 0 else self.MAX_CHARGE_CURRENT
                ),
                charge_activation_w=charge_activation_w,
                discharge_min_a=3,
                discharge_max_a=self.MAX_DISCHARGE_CURRENT,
                discharge_aggressive=False,
                discharge_activation_w=activation_w,
            )
            debug(
                f"IOCTGO_AGILEOUT: Shortfall mode configured - conservative "
                f"discharge (activation={activation_w:.0f}W, bias=-0.5, "
                f"range=3-{self.MAX_DISCHARGE_CURRENT}A), solar charge floor "
                f"{charge_min_a:.0f} A"
            )
            return

        # Not shortfall: use the existing tier logic to decide between storing
        # (bidirectional) and exporting (load-follow discharge).
        # Get current export rate for comparison
        # No rates means 0p; the tier logic then stores rather than exports.
        current_export_rate_p = self._current_export_rate_p(current_slot)

        # Use the same surplus-derived harvest current as get_control_state()
        # so the control state and the configured levels agree.
        harvest_current_a = self._estimate_solar_harvest_current()
        decision = self._evaluate_storage_decision(
            current_slot,
            current_export_rate_p,
            battery_level,
            harvest_current_a=harvest_current_a,
        )

        # Aggressive discharge: start only once the forced output earns back
        # the stored energy's best alternative use or measured delivered cost,
        # whichever is higher, plus the profit margin.  The gate can sit below
        # 720 W for cheap energy, but never at the old blanket 1 W, which
        # exported at a loss.
        activation_w = self._current_discharge_activation_w(
            current_slot, dayMinute, aggressive=True
        )

        if decision.use_bidirectional:
            # Bidirectional mode: prefer small export over any import.
            # Positive bias = favour higher discharge currents.
            evseController.setDischargeActivationPower(activation_w)
            evseController.setDischargeCurrentBias(
                self.AGGRESSIVE_DISCHARGE_BIAS_A
            )
            evseController.setDischargeCurrentRange(3, self.MAX_DISCHARGE_CURRENT)
            evseController.setChargeActivationPower(720)
            evseController.setChargeCurrentBias(-0.5)
            if charge_at_cap:
                evseController.setChargeCurrentRange(0, 0)
            else:
                evseController.setChargeCurrentRange(3, self.MAX_CHARGE_CURRENT)
            self._update_snapshot_ranges(
                charge_min_a=0 if charge_at_cap else 3,
                charge_max_a=0 if charge_at_cap else self.MAX_CHARGE_CURRENT,
                charge_activation_w=720,
                discharge_min_a=3,
                discharge_max_a=self.MAX_DISCHARGE_CURRENT,
                discharge_aggressive=True,
                discharge_activation_w=activation_w,
            )
            debug(f"IOCTGO_AGILEOUT: Bidirectional mode configured (activation={activation_w:.0f}W, bias=+0.5, range=3-{self.MAX_DISCHARGE_CURRENT}A)")
        else:
            # Self-use load-follow.  Cover the house and take only a surplus
            # that is worth keeping.  The minimum charge current is the
            # *effective* onset: either keeping the solar for the house
            # (self-use) or storing it for the best later slot may make it
            # worthwhile, so the lower of the two break-evens wins.  It is
            # derived from the measured efficiency curve and the live prices,
            # not a fixed 3 A or 6 A.  Both ranges are set explicitly: a stale
            # charge range from the previous mode must not leak into this one.
            target_rate_p = decision.future_rate_p
            charge_floor_a, _, _ = self._effective_charge_floor_a(
                current_export_rate_p, target_rate_p
            )
            charge_min_a = (
                0.0
                if charge_at_cap or charge_floor_a is None
                else float(charge_floor_a)
            )
            charge_activation_w = self._charge_activation_w_for_current(
                charge_min_a
            )
            evseController.setDischargeActivationPower(activation_w)
            evseController.setDischargeCurrentBias(
                self.AGGRESSIVE_DISCHARGE_BIAS_A
            )
            evseController.setDischargeCurrentRange(
                config.WALLBOX_MIN_DISCHARGE_CURRENT,
                config.WALLBOX_MAX_DISCHARGE_CURRENT,
            )
            evseController.setChargeActivationPower(charge_activation_w)
            evseController.setChargeCurrentBias(-0.5)
            evseController.setChargeCurrentRange(
                charge_min_a,
                0.0 if charge_min_a <= 0 else self.MAX_CHARGE_CURRENT,
            )
            self._update_snapshot_ranges(
                charge_min_a=charge_min_a,
                charge_max_a=(
                    0.0 if charge_min_a <= 0 else self.MAX_CHARGE_CURRENT
                ),
                charge_activation_w=charge_activation_w,
                discharge_min_a=config.WALLBOX_MIN_DISCHARGE_CURRENT,
                discharge_max_a=config.WALLBOX_MAX_DISCHARGE_CURRENT,
                discharge_aggressive=True,
                discharge_activation_w=activation_w,
            )
            debug(
                f"IOCTGO_AGILEOUT: Self-use bidirectional configured "
                f"(activation={activation_w:.0f}W, bias=+0.5, "
                f"charge={charge_min_a:.0f}-"
                f"{0 if charge_min_a <= 0 else self.MAX_CHARGE_CURRENT}A, "
                f"discharge={config.WALLBOX_MIN_DISCHARGE_CURRENT}-"
                f"{config.WALLBOX_MAX_DISCHARGE_CURRENT}A)"
            )

    def cleanup(self):
        """Persist the ledger, then restore the original calculation mode."""
        self.save_state()
        if hasattr(self, 'original_calculation_method') and hasattr(self, 'evseController'):
            self.evseController.use_new_current_calculation = self.original_calculation_method

    # === OCPP Management Methods (copied from IOCTGO) ===
    
    def should_enable_ocpp_due_to_soc(self, state):
        """Check if OCPP should be enabled due to low SoC."""
        if state.battery_level < 0:
            return False
        return state.battery_level < self.OCPP_ENABLE_SOC_THRESHOLD
    
    def should_enable_ocpp_due_to_time(self, dayMinute):
        """Check if OCPP should be enabled due to time (23:30)."""
        return dayMinute >= self.OCPP_ENABLE_TIME or dayMinute <= 5 * 60 + 30 # between OCPP_ENABLE_TIME and 05:30, wrapping around midnight

    def _schedule_return_to_smart(self):
        """Schedule events to return to smart tariff when OCPP is enabled.
        
        Creates two events:
        1. Unconditional: AT OCPP_DISABLE_TIME -> switch to "smart"
        2. Conditional: BETWEEN 05:30 (next day) AND OCPP_DISABLE_TIME, 
           IF SoC >= OCPP_DISABLE_SOC_THRESHOLD -> switch to "smart"
        """
        from datetime import datetime, timedelta
        from evse_controller.scheduler import ScheduledEvent

        now = self.get_current_datetime()
        
        # Calculate OCPP disable time for today/tomorrow
        disable_hour, disable_minute = map(int, self.OCPP_DISABLE_TIME_STR.split(':'))
        target_disable_time = now.replace(hour=disable_hour, minute=disable_minute, second=0, microsecond=0)
        
        # If disable time is already past today, schedule for tomorrow
        if target_disable_time <= now:
            target_disable_time += timedelta(days=1)
        
        # Event 1: Unconditional switch to smart at OCPP_DISABLE_TIME
        event_unconditional = ScheduledEvent(
            timestamp=target_disable_time,
            state="smart"
        )
        self._add_scheduled_event(event_unconditional)
        info(f"IOCTGO_AGILEOUT Scheduled unconditional return to smart at {target_disable_time}")
        
        # Event 2: Conditional switch to smart if SoC threshold is reached
        # Window: 05:30 next day to OCPP_DISABLE_TIME
        # Calculate 05:30 for the day after today (next morning)
        next_day = now.date() + timedelta(days=1)
        window_start = datetime(next_day.year, next_day.month, next_day.day, 5, 30, 0, 0)
        
        # The window end is the same as the unconditional event time
        # But we need it in HH:MM format for the scheduler
        window_end_str = self.OCPP_DISABLE_TIME_STR
        
        # Create conditional event
        # Note: The event timestamp is the window start (05:30), and time_window_end is the disable time
        event_conditional = ScheduledEvent(
            timestamp=window_start,
            state="smart",
            time_window_end=window_end_str,
            min_soc=float(self.OCPP_DISABLE_SOC_THRESHOLD)
        )
        self._add_scheduled_event(event_conditional)
        info(f"IOCTGO_AGILEOUT Scheduled conditional return to smart: BETWEEN 05:30 AND {window_end_str}, IF SoC >= {self.OCPP_DISABLE_SOC_THRESHOLD}%")

    def _add_scheduled_event(self, event):
        """Add a scheduled event using the scheduler from the main controller."""
        from evse_controller.smart_evse_controller import scheduler
        scheduler.add_event(event)

    def _ocpp_check_turn_on(self, state: EvseAsyncState, dayMinute: int):
        """Turn on OCPP if time or SoC condition is met.
        
        When OCPP is triggered (by SoC or time), this method:
        1. Sends "ocpp" command to switch to OCPP mode
        2. Creates an unconditional event to switch back at OCPP_DISABLE_TIME
        3. Creates a conditional event to switch back early if SoC threshold is reached
           (between 05:30 next day and OCPP_DISABLE_TIME)
        """
        should_enable_due_to_soc = self.should_enable_ocpp_due_to_soc(state)
        should_enable_due_to_time = self.should_enable_ocpp_due_to_time(dayMinute)

        # Handle OCPP enable (both SoC and time triggers use the command queue)
        if should_enable_due_to_soc or should_enable_due_to_time:
            trigger_type = "SoC" if should_enable_due_to_soc else "time"
            info(f"IOCTGO_AGILEOUT Requesting OCPP enable via command queue ({trigger_type}-triggered)")

            # Put the 'ocpp' command in the queue to switch to OCPP mode
            if self.command_queue:
                self.command_queue.put("ocpp")
                info(f"IOCTGO_AGILEOUT OCPP enable command sent to queue ({trigger_type}-triggered)")

                # Schedule events to return to smart tariff
                self._schedule_return_to_smart()
