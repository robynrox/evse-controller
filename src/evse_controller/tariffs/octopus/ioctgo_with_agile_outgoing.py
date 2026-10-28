from ..base import Tariff
from .battery_efficiency import BatteryEfficiencyModel, DASHBOARD_SOC_CAPACITY_KWH
from evse_controller.drivers.EvseController import ControlState
from evse_controller.utils.config import config
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.utils.logging_config import debug, info, warning, error
import threading
import queue
from typing import Optional
import urllib.request
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    current_rate_p: Optional[float] = None
    target_rate_p: Optional[float] = None
    target_slot: Optional[int] = None
    harvest_current_a: Optional[float] = None
    surplus_w: Optional[float] = None
    efficiency_pct: Optional[float] = None
    efficiency_label: str = ""
    min_round_trip_saving_p: Optional[float] = None
    min_round_trip_saving_basis: str = ""
    charge_min_a: Optional[float] = None
    charge_max_a: Optional[float] = None
    discharge_min_a: Optional[float] = None
    discharge_max_a: Optional[float] = None
    discharge_aggressive: bool = True
    charge_activation_w: Optional[float] = None
    discharge_activation_w: Optional[float] = None


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
    23:30 assuming every remaining slot loses NON_EXPORT_SLOT_SOC_LOSS_PERCENT.
    If that projection is below MIN_AGILE_DISCHARGE_SOC, the day is treated as
    an energy shortfall and the system switches to a conservative bidirectional
    mode:

    - No export slots are planned (there is no spare SoC above the target).
    - The car still absorbs surplus solar, since that improves the chance of
      reaching the target.
    - Discharge covers most of the house load but deliberately leaves a small
      import (720 W activation, -0.5 A bias) so it does not intentionally
      export. This replaces the fixed SOC_THRESHOLD_FOR_STRATEGY approach in
      the standard IOCTGO tariff.
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
    current from the live grid surplus, rounds down to a measured point, and
    uses that for the threshold.  A grid charge for arbitrage still runs at the
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
    @media (max-width: 700px) {
        .ioctgo-decision-line { font-size: 11px; gap: 2px 8px; }
    }
    </style>
    """

    def __init__(self, command_queue: Optional[queue.Queue] = None, battery_capacity_kwh=None,
                 bulk_discharge_start_time=None, bulk_discharge_end_time=None, enable_bulk_discharge=None):
        """Initialize tariff with IOCTGO logic and Agile Outgoing rate fetching."""
        super().__init__(command_queue=command_queue)
        
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

        # SoC loss percentages per slot (configured by user)
        # If export_slot_soc_loss_percent is 0, use calculated value from EXPORT_POWER_KW
        self.EXPORT_SLOT_SOC_LOSS_PERCENT = config.IOCTGO_EXPORT_SLOT_SOC_LOSS_PERCENT
        self.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = config.IOCTGO_NON_EXPORT_SLOT_SOC_LOSS_PERCENT
        
        # Planned export slots (calculated dynamically)
        self._planned_export_slots = []
        self._exported_slots = []  # Track slots where export actually occurred

        # Rate fetching state
        self._rate_fetch_timer = None
        self._last_fetch_attempt = None
        
        # Cache for export plan calculation
        self._last_plan_soc = -1
        self._last_plan_slot = -1
        self._plan_cache = []
        self._prev_battery_level = -1

        # True when the projected SoC at 23:30 is below the target, meaning
        # energy should be conserved rather than exported. Updated whenever
        # the export plan is recalculated so the two stay in step.
        self._energy_shortfall = False
        self._projected_soc_at_2330 = None

        # Dashboard snapshot of the last control decision.  Written by the
        # control loop; the dashboard only reads it.
        self._decision_snapshot = None

        # Estimated saving versus the same house load with no EVSE/battery
        # activity, accumulated in GBP and reset at local midnight.
        self._savings_day = None
        self._saved_today_gbp = 0.0
        self._savings_last_time = None
        self._savings_last_grid_w = 0.0
        self._savings_last_baseline_w = 0.0
        self._savings_last_evse_w = 0.0

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

    def _update_energy_shortfall(self, current_soc_percent: float) -> bool:
        """Update and return whether the day is projected to end in shortfall.

        A shortfall means the SoC projected at 23:30 is below
        MIN_AGILE_DISCHARGE_SOC. The projection uses the same assumption as
        calculate_export_plan(): every remaining slot loses
        NON_EXPORT_SLOT_SOC_LOSS_PERCENT.

        Called from calculate_export_plan(), so this state is refreshed at
        exactly the same times as the export plan: at slot boundaries, on
        significant SoC changes, and after a rate refresh.

        Args:
            current_soc_percent: Current battery state of charge (%), or -1
                if unknown.

        Returns:
            True if the projected SoC at 23:30 is below the target.
        """
        if current_soc_percent < 0:
            self._energy_shortfall = False
            self._projected_soc_at_2330 = None
            return False

        now = self.get_current_datetime()
        current_slot_idx = now.hour * 2 + (1 if now.minute >= 30 else 0)
        slots_remaining = max(0, 47 - current_slot_idx)
        projected_soc_at_2330 = (
            current_soc_percent
            - slots_remaining * self.NON_EXPORT_SLOT_SOC_LOSS_PERCENT
        )
        self._projected_soc_at_2330 = projected_soc_at_2330

        target_soc = config.MIN_AGILE_DISCHARGE_SOC
        self._energy_shortfall = projected_soc_at_2330 < target_soc

        debug(
            f"IOCTGO_AGILEOUT: Projected SoC at 23:30={projected_soc_at_2330:.1f}% "
            f"(target {target_soc}%), shortfall={self._energy_shortfall}"
        )
        return self._energy_shortfall

    def calculate_export_plan(self, current_soc_percent: float) -> list:
        """Calculate which slots to use for export based on current SoC and rates.

        Uses a two-phase approach:
        1. Project SoC to 23:30 assuming all slots are non-discharge (load-following)
        2. Select best export slots based on projected SoC and configured loss rates

        Args:
            current_soc_percent: Current battery state of charge (%)

        Returns:
            List of slot indices (0-47) planned for export
        """
        # Keep the shortfall decision in step with the export plan. This is
        # called whenever the plan is recalculated, including the significant
        # SoC change resync and the post-fetch recalculation.
        self._update_energy_shortfall(current_soc_percent)

        if current_soc_percent < 0:
            return []

        battery_capacity_kwh = self.SOC_CAPACITY_KWH
        min_soc_percent = config.MIN_AGILE_DISCHARGE_SOC

        # Calculate export slot SoC loss per slot
        # If configured to 0, calculate from EXPORT_POWER_KW and battery capacity
        if self.EXPORT_SLOT_SOC_LOSS_PERCENT > 0:
            export_slot_soc_loss = self.EXPORT_SLOT_SOC_LOSS_PERCENT
        else:
            # Calculate: (power_kw / efficiency × 0.5h / battery_kwh) × 100
            export_slot_soc_loss = (
                self.EXPORT_POWER_KW / self.STORED_TO_AC_EFFICIENCY
                * 0.5 / battery_capacity_kwh
            ) * 100

        non_export_slot_soc_loss = self.NON_EXPORT_SLOT_SOC_LOSS_PERCENT

        debug(f"IOCTGO_AGILEOUT: Export slot loss={export_slot_soc_loss:.2f}%, non-export slot loss={non_export_slot_soc_loss:.2f}%")
        info(f"IOCTGO_AGILEOUT: Planning export - SoC {current_soc_percent}%, min {min_soc_percent}%, export loss {export_slot_soc_loss:.1f}%/slot, load-follow loss {non_export_slot_soc_loss:.1f}%/slot")

        # Get current time to determine slot range
        now = self.get_current_datetime()
        current_slot_idx = now.hour * 2 + (1 if now.minute >= 30 else 0)

        # Calculate number of slots from current slot until 23:30
        # Slot 47 starts at 23:30, so slots remaining = 47 - current_slot
        # e.g. at 21:33 (slot 43): 47 - 43 = 4 slots (43, 44, 45, 46)
        slots_remaining = 47 - current_slot_idx

        if slots_remaining <= 0:
            debug(f"IOCTGO_AGILEOUT: No slots remaining today")
            return []

        # Phase 1: Project SoC at 23:30 assuming ALL slots are non-discharge (load-following)
        projected_soc_at_2330 = current_soc_percent - slots_remaining * non_export_slot_soc_loss
        debug(f"IOCTGO_AGILEOUT: Projected SoC at 23:30 (all load-follow): {projected_soc_at_2330:.1f}%")

        # Calculate available SoC for export (above minimum reserve)
        available_soc_for_export = projected_soc_at_2330 - min_soc_percent
        debug(f"IOCTGO_AGILEOUT: Available SoC for export: {available_soc_for_export:.1f}%")

        if available_soc_for_export <= 0:
            debug(f"IOCTGO_AGILEOUT: No SoC available for export after load-follow projection")
            return []

        # Calculate SoC cost per export slot
        # Export slot uses export_slot_soc_loss instead of non_export_slot_soc_loss
        # The "extra" cost of choosing export vs load-follow is the difference
        additional_soc_cost_per_export_slot = export_slot_soc_loss - non_export_slot_soc_loss

        if additional_soc_cost_per_export_slot <= 0:
            # Export slots don't cost extra SoC - can export in all profitable slots
            debug(f"IOCTGO_AGILEOUT: Export slots don't cost extra SoC")
            additional_soc_cost_per_export_slot = export_slot_soc_loss  # Use full cost

        debug(f"IOCTGO_AGILEOUT: Additional SoC cost per export slot: {additional_soc_cost_per_export_slot:.2f}%")

        # Calculate minimum profitable rate.  With no rate data this is just a
        # threshold that no (0p) slot can meet, so the plan is empty.
        min_export_rate_p = self.export_min_rate_p()

        info(f"IOCTGO_AGILEOUT: Min export rate {min_export_rate_p:.1f}p/kWh")

        # Create list of (slot_index, rate) for all future slots today that are profitable
        # Only consider slots 0-46 (exclude slot 47 = 23:30-00:00)
        # Missing slots are treated as unprofitable (not included)
        slot_rates = []
        for rate_data in self._today_rates():
            hour = rate_data['start'].hour
            slot_idx = hour * 2 + (1 if rate_data['start'].minute == 30 else 0)

            # Skip slots that have completely passed (current slot is still valid)
            if slot_idx < current_slot_idx:
                continue
            
            # Skip slot 47 (23:30-00:00) - not available for export
            if slot_idx >= 47:
                continue

            rate_p = rate_data['rate']  # Already in p/kWh
            if rate_p >= min_export_rate_p:  # Only consider profitable slots
                slot_rates.append((slot_idx, rate_p))
                debug(f"IOCTGO_AGILEOUT: Slot {slot_idx} ({hour:02d}:{rate_data['start'].minute:02d}) @ {rate_p:.1f}p (profitable)")

        debug(f"IOCTGO_AGILEOUT: Found {len(slot_rates)} profitable slots")

        # Sort by rate (highest first)
        slot_rates.sort(key=lambda x: x[1], reverse=True)

        # Phase 2: Select slots until we run out of available SoC
        planned_slots = []
        remaining_soc = available_soc_for_export

        debug(f"IOCTGO_AGILEOUT: Selecting slots with {remaining_soc:.1f}% SoC available")

        for slot_idx, rate in slot_rates:
            if remaining_soc >= additional_soc_cost_per_export_slot:
                planned_slots.append(slot_idx)
                remaining_soc -= additional_soc_cost_per_export_slot
                debug(f"IOCTGO_AGILEOUT: Selected slot {slot_idx} @ {rate:.1f}p, remaining SoC={remaining_soc:.1f}%")
            else:
                debug(f"IOCTGO_AGILEOUT: Skipping slot {slot_idx} @ {rate:.1f}p (not enough SoC: need {additional_soc_cost_per_export_slot:.1f}%, have {remaining_soc:.1f}%)")

        # Sort planned slots by time for easier processing
        planned_slots.sort()

        debug(f"IOCTGO_AGILEOUT: Final plan: {len(planned_slots)} slots: {planned_slots}")
        info(f"IOCTGO_AGILEOUT: Export plan - {len(planned_slots)} slots planned")

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
        # - Are before slot 47 (23:30-00:00, not available for export)
        unused_slot_rates = []

        for rate_data in self._today_rates():
            hour = rate_data['start'].hour
            slot_idx = hour * 2 + (1 if rate_data['start'].minute == 30 else 0)
            
            # Skip past slots
            if slot_idx < current_slot:
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

    def minimum_round_trip_saving_p(
        self, charge_price_p: float, round_trip_efficiency: float
    ) -> float:
        """Minimum saving per kWh of input needed for a round trip.

        A round trip turns 1 kWh of input into ``round_trip_efficiency`` kWh
        of output, and each output kWh must clear the profit margin.  The
        break-even input price is ``charge_price / efficiency + margin``, so
        the minimum saving is::

            charge_price / efficiency + margin - charge_price

        For grid import at ``IMPORT_LOW`` and the ~0.833 grid round trip this
        is ~2.3p/kWh; for free solar it collapses to the margin itself
        (~0.3p/kWh).
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

    def _read_power_snapshot(self) -> Optional[dict]:
        """Read the latest grid/EVSE channels, or ``None`` if unavailable.

        ``getHomeWatts()`` is ``ch1 - ch2`` (grid minus EVSE), so the house
        load without the EVSE is the baseline a no-storage system would show
        on the grid.  Its negative is the surplus available to the car.
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
        except (AttributeError, TypeError, ValueError):
            return None

        return {
            "grid_w": grid_w,
            "evse_w": evse_w,
            "baseline_w": baseline_w,
            "surplus_w": -baseline_w,
            "voltage": voltage,
        }

    def _grid_interval_cost_gbp(
        self,
        grid_w: float,
        import_rate_p: float,
        export_rate_p: float,
        hours: float,
    ) -> float:
        """Cost of a grid power level over an interval; export is negative."""
        if hours <= 0:
            return 0.0
        energy_kwh = grid_w / 1000.0 * hours
        if energy_kwh >= 0:
            return energy_kwh * import_rate_p / 100.0
        return energy_kwh * export_rate_p / 100.0

    def _accumulate_daily_savings(self) -> None:
        """Accumulate today's estimated saving against a no-storage baseline.

        The baseline is the house load *excluding* the EVSE, so the result
        measures what running the EVSE/battery system has saved (or cost)
        compared with simply importing/exporting that load.  It uses the
        current import rate (off-peak or peak) and the current Agile export
        rate, and subtracts the minimum profit margin as a provision for
        battery throughput.  Resets at local midnight.
        """
        now = self.get_current_datetime()
        today = now.date()
        if self._savings_day != today:
            self._savings_day = today
            self._saved_today_gbp = 0.0
            self._savings_last_time = None
            self._savings_last_grid_w = 0.0
            self._savings_last_baseline_w = 0.0
            self._savings_last_evse_w = 0.0

        power = self._read_power_snapshot()
        if power is None:
            return

        if self._savings_last_time is None:
            self._savings_last_time = now
            self._savings_last_grid_w = power["grid_w"]
            self._savings_last_baseline_w = power["baseline_w"]
            self._savings_last_evse_w = power["evse_w"]
            return

        elapsed = (now - self._savings_last_time).total_seconds()
        if elapsed <= 0:
            return
        # Cap a long gap (process suspended, clock jump) so a single sample
        # cannot invent a large saving.
        hours = min(elapsed, 300.0) / 3600.0

        day_minute = now.hour * 60 + now.minute
        import_rate_p = config.IMPORT_LOW if self.is_off_peak(day_minute) else config.IMPORT_HIGH
        current_slot = now.hour * 2 + (1 if now.minute >= 30 else 0)
        export_rate_p = self._current_export_rate_p(current_slot)

        # Trapezoidal average over the sample interval.
        grid_w = 0.5 * (self._savings_last_grid_w + power["grid_w"])
        baseline_w = 0.5 * (self._savings_last_baseline_w + power["baseline_w"])
        evse_w = 0.5 * (self._savings_last_evse_w + power["evse_w"])

        actual_cost = self._grid_interval_cost_gbp(
            grid_w, import_rate_p, export_rate_p, hours
        )
        baseline_cost = self._grid_interval_cost_gbp(
            baseline_w, import_rate_p, export_rate_p, hours
        )
        provision = (
            abs(evse_w) / 1000.0 * hours
            * self.MIN_PROFIT_MARGIN_P_PER_KWH / 100.0
        )
        self._saved_today_gbp += baseline_cost - actual_cost - provision

        self._savings_last_time = now
        self._savings_last_grid_w = power["grid_w"]
        self._savings_last_baseline_w = power["baseline_w"]
        self._savings_last_evse_w = power["evse_w"]

    def _estimate_solar_harvest_current(self) -> float:
        """Estimate the charge current the load-following loop will use.

        ``EvseController.getHomeWatts()`` is the house load net of PV,
        *excluding* the EVSE, so its negative is the surplus available to the
        car.  Mirror the controller's calculation (``P / V + bias``, clamped to
        the configured charge range) and then round down to a measured
        efficiency point.

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
        estimate = self.EFFICIENCY_MODEL.measured_current_at_or_below(amps)
        debug(
            f"IOCTGO_AGILEOUT: Solar surplus {surplus_w:.0f} W at "
            f"{voltage:.0f} V -> harvest estimate {estimate:.0f} A"
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
                f"future {best_unused_rate:.1f}p at {slot_label} × "
                f"{solar_round_trip:.0%} − {self.MIN_PROFIT_MARGIN_P_PER_KWH:.1f}p "
                f"margin = {adjusted_future_rate:.1f}p > "
                f"{current_export_rate_p:.1f}p now — storing solar"
            )
        else:
            debug(f"IOCTGO_AGILEOUT: EXPORT (Tier 3): current={current_export_rate_p:.1f}p >= "
                  f"future={best_unused_rate:.1f}p - margin {self.MIN_PROFIT_MARGIN_P_PER_KWH:.1f}p "
                  f"= {net_future_rate:.1f}p × {solar_round_trip:.0%} = "
                  f"{adjusted_future_rate:.1f}p")
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
        tariff_id = f"E-1R-{self.tariff_code}-{self.region}"
        base_url = f"https://api.octopus.energy/v1/products/{self.tariff_code}/electricity-tariffs/{tariff_id}"
        
        # Fetch today's rates
        start = today.strftime('%Y-%m-%dT00:00:00Z')
        end = (today + timedelta(days=1)).strftime('%Y-%m-%dT00:00:00Z')
        url = f"{base_url}/standard-unit-rates/?period__started_at__gte={start}&period__started_at__lt={end}"
        
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'EVSE-Controller/1.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
            
            rates = []
            for result in data.get('results', []):
                valid_from = result.get('valid_from')
                valid_to = result.get('valid_to')
                rate_inc = result.get('value_inc_vat')

                if not all([valid_from, valid_to, rate_inc is not None]):
                    continue

                start_dt = datetime.fromisoformat(valid_from.replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(valid_to.replace('Z', '+00:00'))

                # Convert UTC times to local time for correct slot index calculation
                # This is critical on DST transition days when UTC hour != local hour
                start_dt = start_dt.astimezone()
                end_dt = end_dt.astimezone()

                rates.append({
                    'start': start_dt,
                    'end': end_dt,
                    'rate': round(rate_inc, 2)
                })
            
            # Sort by start time
            rates.sort(key=lambda r: r['start'])

            # Filter to today only (using local date after timezone conversion)
            # On DST transition days, this correctly handles the missing hour
            today_date = datetime.now().date()
            
            # Use dict to remove duplicates (key by start time)
            unique_rates = {}
            for r in rates:
                # Check if this rate's start date matches today (in local time)
                if r['start'].date() == today_date:
                    # Create a key from the start time (rounded to minute)
                    key = r['start'].strftime('%Y-%m-%d %H:%M')
                    # Keep the first occurrence (or could keep latest if API returns updates)
                    if key not in unique_rates:
                        unique_rates[key] = r

            # Convert back to list and sort
            rates = sorted(unique_rates.values(), key=lambda r: r['start'])
            
            # Verify we have expected number of slots (46 or 48 is normal, depends on GMT/BST in use)
            if len(rates) != 46 and len(rates) != 48:
                warning(f"IOCTGO_AGILEOUT: Expected 46 or 48 slots, got {len(rates)}.")
            
            return rates
        except Exception as e:
            error(f"IOCTGO_AGILEOUT: API fetch failed: {e}")
            return []

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
        min_round_trip_saving_p: Optional[float] = None,
        min_round_trip_saving_basis: str = "",
        charge_min_a: Optional[float] = None,
        charge_max_a: Optional[float] = None,
        charge_activation_w: Optional[float] = None,
        discharge_min_a: Optional[float] = None,
        discharge_max_a: Optional[float] = None,
        discharge_aggressive: bool = True,
        discharge_activation_w: Optional[float] = None,
    ) -> DecisionSnapshot:
        """Record the decision the dashboard should display."""
        snapshot = DecisionSnapshot(
            updated_at=self.get_current_datetime(),
            mode=mode,
            reason=reason,
            detail=detail,
            soc_percent=soc_percent,
            target_soc_percent=target_soc_percent,
            projected_soc_at_2330=projected_soc_at_2330,
            current_rate_p=current_rate_p,
            target_rate_p=target_rate_p,
            target_slot=target_slot,
            harvest_current_a=harvest_current_a,
            surplus_w=surplus_w,
            efficiency_pct=efficiency_pct,
            efficiency_label=efficiency_label,
            min_round_trip_saving_p=min_round_trip_saving_p,
            min_round_trip_saving_basis=min_round_trip_saving_basis,
            charge_min_a=charge_min_a,
            charge_max_a=charge_max_a,
            charge_activation_w=charge_activation_w,
            discharge_min_a=discharge_min_a,
            discharge_max_a=discharge_max_a,
            discharge_aggressive=discharge_aggressive,
            discharge_activation_w=discharge_activation_w,
        )
        self._decision_snapshot = snapshot
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
        """Format the running saving with an explicit sign."""
        value = self._saved_today_gbp
        return f"{'-' if value < 0 else ''}£{abs(value):.2f}"

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

        if snapshot.min_round_trip_saving_p is not None:
            basis = html_module.escape(snapshot.min_round_trip_saving_basis)
            title = html_module.escape(
                "Minimum saving per kWh of input for a round trip. "
                f"Solar to export: {self.minimum_round_trip_saving_solar_p():.1f}p/kWh. "
                f"Cheap grid to export: {self.minimum_round_trip_saving_grid_p():.1f}p/kWh "
                f"at {config.IMPORT_LOW:.1f}p import. Includes the "
                f"{self.MIN_PROFIT_MARGIN_P_PER_KWH:.1f}p/kWh profit margin.",
                quote=True,
            )
            suffix = f" ({basis})" if basis else ""
            parts.append(
                f'<span class="ioctgo-metric" title="{title}">'
                f"min gain {snapshot.min_round_trip_saving_p:.1f}p/kWh{suffix}</span>"
            )

        saved = self._format_saved_today()
        saved_class = "ioctgo-saved" if self._saved_today_gbp >= 0 else "ioctgo-lost"
        title = html_module.escape(
            "Estimated saving today versus the same house load with no "
            "EVSE/battery activity, at today's import and Agile export prices, "
            "less the minimum profit margin provision. Resets at midnight.",
            quote=True,
        )
        parts.append(
            f'<span class="{saved_class}" title="{title}">saved today {saved}</span>'
        )

        detail_attr = ""
        if snapshot.detail:
            detail_attr = f' title="{html_module.escape(snapshot.detail, quote=True)}"'
        return self.DECISION_LINE_CSS + (
            f'<div class="ioctgo-decision-line"{detail_attr}>'
            + "".join(parts)
            + "</div>"
        )

    def get_dashboard_html(self) -> str:
        """Return HTML for Agile Outgoing rate display.

        Returns a 48-cell visual strip showing today's export rates with
        red→green gradient coloring. Responsive design adapts to viewport width.
        Missing slots shown as grey placeholders.
        Planned export slots are highlighted with "EX" label.
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
                <div class="agile-rate-strip" style="height:28px;display:flex;
                    align-items:center;justify-content:center;background:#f5f5f5;
                    border-radius:4px;margin:6px 0;">
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
                '<div class="agile-rate-strip" style="height:28px;display:flex;'
                "gap:1px;padding:3px;background:#fafafa;border-radius:4px;"
                'overflow:hidden;">'
            )
            
            for slot_idx in range(48):
                hour = slot_idx // 2
                minute = 30 if slot_idx % 2 else 0
                
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
                        label = f'<span style="position:absolute;top:2px;right:2px;font-size:8px;font-weight:bold;color:{text_color};text-shadow:0 0 2px #fff;">✓</span>'
                    elif is_partial:
                        # Partial slot (lowest rate) - show lowercase 'e'
                        extra_style = "border:2px solid #ff9800;"
                        tooltip_extra = " - PLANNED EXPORT (may be partial)"
                        label = f'<span style="position:absolute;top:2px;right:2px;font-size:8px;font-weight:bold;color:{text_color};text-shadow:0 0 2px #fff;">e</span>'
                    elif is_planned:
                        # Planned for export - show orange border, EX in text color
                        extra_style = "border:2px solid #ff9800;"
                        tooltip_extra = " - PLANNED EXPORT"
                        label = f'<span style="position:absolute;top:2px;right:2px;font-size:8px;font-weight:bold;color:{text_color};text-shadow:0 0 2px #fff;">EX</span>'
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
                    " title="{hour:02d}:{minute:02d}: {rate:.1f}p/kWh{tooltip_extra}">
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
                    " title="{hour:02d}:{minute:02d}: Rate not available">
                        <span style="font-size:14px;line-height:1;font-weight:300;">?</span>
                    </div>
                    ''')
            
            html.append('</div>')  # Close agile-rate-strip
            
            # Add time axis with horizontal line and tick marks
            # Use flex structure matching the cells for perfect alignment
            time_axis = '<div style="display:flex;gap:1px;padding:0 3px;height:14px;position:relative;">'
            
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
            
            # Add CSS for responsive behavior with progressive degradation
            html.append('''
            <style>
            /* Default: show full rate with 1dp */
            .agile-rate-cell .rate-value-full { display: inline; }
            .agile-rate-cell .rate-value-int { display: none; }
            .agile-rate-cell .rate-value-none { display: none; }
            
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
                .agile-rate-cell { min-height: 22px; }
                .agile-rate-strip { height: 28px !important; }
            }
            
            /* < 400px: reduce height further */
            @media (max-width: 400px) {
                .agile-rate-cell { min-height: 18px; }
                .agile-rate-strip { height: 22px !important; padding-bottom: 8px !important; }
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
                    min_round_trip_saving_p=self.minimum_round_trip_saving_grid_p(),
                    min_round_trip_saving_basis="grid",
                    charge_min_a=self.MAX_CHARGE_CURRENT,
                    charge_max_a=self.MAX_CHARGE_CURRENT,
                    charge_activation_w=0,
                )
                return ControlState.CHARGE, None, None, "IOCTGO_AGILEOUT Off-peak: CHARGE AT MAX RATE (priority)"
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
                min_round_trip_saving_p=self.minimum_round_trip_saving_grid_p(),
                min_round_trip_saving_basis="grid",
                charge_min_a=3,
                charge_max_a=3,
                charge_activation_w=0,
            )
            return ControlState.CHARGE, 3, 3, "IOCTGO_AGILEOUT SoC unknown, charge at 3A"

        # Battery depleted - protect battery
        if battery_level <= 25:
            self._record_decision(
                mode="Idle — battery protection",
                reason=f"SoC {battery_level}% at or below the 25% reserve",
                soc_percent=battery_level,
                detail=(
                    f"SoC {battery_level}% is at or below the 25% protection "
                    "reserve. The controller will not discharge the pack."
                ),
            )
            return ControlState.DORMANT, None, None, "IOCTGO_AGILEOUT Battery depleted"

        # Check if current slot is a planned export slot
        if current_slot in self._planned_export_slots:
            # Export at maximum rate during planned slots
            export_rate_p = self._current_export_rate_p(current_slot)
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
                mode=f"Max discharge ({self.MAX_DISCHARGE_CURRENT:.0f} A) · aggressive",
                reason=(
                    f"planned export slot {self._slot_range_label(current_slot)} "
                    f"at {export_rate_p:.1f}p"
                ),
                soc_percent=battery_level,
                detail=(
                    f"Export slot {self._slot_range_label(current_slot)} at "
                    f"{export_rate_p:.1f}p/kWh. SoC {battery_level}%, "
                    f"expected {expected_gbp:.2f} for the slot at "
                    f"{self.EXPORT_POWER_KW:.2f} kW, discharge efficiency "
                    f"{discharge_efficiency:.0f}%."
                ),
                current_rate_p=export_rate_p,
                target_rate_p=export_rate_p,
                target_slot=current_slot,
                efficiency_pct=discharge_efficiency,
                efficiency_label="discharge",
                min_round_trip_saving_p=self.minimum_round_trip_saving_grid_p(),
                min_round_trip_saving_basis="grid",
                discharge_min_a=self.MAX_DISCHARGE_CURRENT,
                discharge_max_a=self.MAX_DISCHARGE_CURRENT,
                discharge_aggressive=True,
                discharge_activation_w=0,
            )
            return ControlState.DISCHARGE, None, None, f"IOCTGO_AGILEOUT Export slot (max discharge)"

        # Energy shortfall: conserve energy for the house rather than exporting.
        # Stay bidirectional so surplus solar can still be absorbed, but use
        # the conservative discharge configuration in set_home_demand_levels().
        if self._energy_shortfall:
            harvest_current_a = self._estimate_solar_harvest_current()
            debug(
                f"IOCTGO_AGILEOUT: Energy shortfall (SoC {battery_level}%), "
                f"using conservative bidirectional mode"
            )
            harvest_label = (
                f"Harvest {harvest_current_a:.0f} A"
                if harvest_current_a > self.SOLAR_CHARGE_REFERENCE_CURRENT
                else f"Harvest ≥{self.SOLAR_CHARGE_REFERENCE_CURRENT:.0f} A"
            )
            charge_at_cap = battery_level >= charge_cap
            if charge_at_cap:
                mode = (
                    f"Do not charge — SoC at {charge_cap:.0f}% cap · "
                    "conservative discharge"
                )
            else:
                mode = f"{harvest_label} · conservative discharge"
            projected = self._projected_soc_at_2330
            projection_text = (
                f"{projected:.0f}%" if projected is not None else "below target"
            )
            if charge_at_cap:
                reason = (
                    f"SoC {battery_level}% at the {charge_cap:.0f}% cap — "
                    "conserving; discharge starts at 840 W import"
                )
            elif projected is not None:
                reason = (
                    f"projected 23:30 SoC {projection_text} < "
                    f"{target_soc:.0f}% target — conserving; discharge starts "
                    "at 840 W import"
                )
            else:
                reason = (
                    f"projected to miss the {target_soc:.0f}% 23:30 target — "
                    "conserving; discharge starts at 840 W import"
                )
            self._record_decision(
                mode=mode,
                reason=reason,
                soc_percent=battery_level,
                detail=(
                    f"SoC {battery_level}%, projected 23:30 SoC "
                    f"{projection_text} versus the {target_soc:.0f}% target. "
                    f"Conservative discharge uses 720 W activation and a "
                    f"-0.5 A bias, so it starts at about 840 W import and "
                    f"leaves a 600 W house load on the grid. Harvest estimate "
                    f"{harvest_current_a:.0f} A."
                    + (
                        f" Charging is inhibited at the {charge_cap:.0f}% cap."
                        if charge_at_cap
                        else ""
                    )
                ),
                target_soc_percent=target_soc,
                projected_soc_at_2330=projected,
                current_rate_p=self._current_export_rate_p(current_slot),
                harvest_current_a=harvest_current_a,
                surplus_w=surplus_w,
                min_round_trip_saving_p=self.minimum_round_trip_saving_solar_p(),
                min_round_trip_saving_basis="solar",
                charge_min_a=0 if charge_at_cap else 3,
                charge_max_a=0 if charge_at_cap else self.MAX_CHARGE_CURRENT,
                charge_activation_w=720,
                discharge_min_a=3,
                discharge_max_a=self.MAX_DISCHARGE_CURRENT,
                discharge_aggressive=False,
                discharge_activation_w=720,
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
                    f"{surplus_w:.0f} W. Aggressive discharge starts at 1 W "
                    "import. Detailed slot and SoC projections are in the log."
                    if surplus_w is not None
                    else (
                        f"{decision.tier.capitalize()} decision. SoC "
                        f"{battery_level}%, current export "
                        f"{current_export_rate_p:.1f}p, harvest estimate "
                        f"{harvest_current_a:.0f} A. No live power sample yet. "
                        "Aggressive discharge starts at 1 W import."
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
                min_round_trip_saving_p=self.minimum_round_trip_saving_solar_p(),
                min_round_trip_saving_basis="solar",
                charge_min_a=0 if charge_at_cap else 3,
                charge_max_a=0 if charge_at_cap else self.MAX_CHARGE_CURRENT,
                charge_activation_w=720,
                discharge_min_a=3,
                discharge_max_a=self.MAX_DISCHARGE_CURRENT,
                discharge_aggressive=True,
                discharge_activation_w=1,
            )
            return ControlState.LOAD_FOLLOW_BIDIRECTIONAL, 3, self.MAX_DISCHARGE_CURRENT, "IOCTGO_AGILEOUT Bidirectional (store for better rate)"
        else:
            # Standard load-follow discharge
            best_future_text = (
                f"{target_rate_p:.1f}p"
                if target_rate_p is not None
                else "no future rate"
            )
            charge_at_cap = battery_level >= charge_cap
            reason = (
                f"SoC {battery_level}% at the {charge_cap:.0f}% cap — covering "
                "the house instead of storing"
                if charge_at_cap
                else decision.reason
            )
            debug(f"IOCTGO_AGILEOUT: Using LOAD_FOLLOW_DISCHARGE (current rate {current_export_rate_p:.1f}p, SoC {battery_level}%)")
            self._record_decision(
                mode=(
                    f"Do not charge · aggressive discharge "
                    f"≥{self.MIN_DISCHARGE_CURRENT:.0f} A"
                ),
                reason=reason,
                soc_percent=battery_level,
                detail=(
                    f"{decision.tier.capitalize()} decision. SoC {battery_level}%, "
                    f"current export {current_export_rate_p:.1f}p, best future "
                    f"{best_future_text}. "
                    "Aggressive discharge starts at 1 W import and covers the "
                    "house load. Detailed slot and SoC projections are in the log."
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
                min_round_trip_saving_p=self.minimum_round_trip_saving_solar_p(),
                min_round_trip_saving_basis="solar",
                discharge_min_a=self.MIN_DISCHARGE_CURRENT,
                discharge_max_a=self.MAX_DISCHARGE_CURRENT,
                discharge_aggressive=True,
                discharge_activation_w=1,
            )
            return ControlState.LOAD_FOLLOW_DISCHARGE, 2, self.MAX_DISCHARGE_CURRENT, "IOCTGO_AGILEOUT Load follow"

    def set_home_demand_levels(self, evseController, state: EvseAsyncState, dayMinute: int):
        """Configure home demand power levels.

        In normal operation the existing tier logic chooses between
        bidirectional and load-follow discharge. When the day is projected to
        end in an energy shortfall, the system instead stays bidirectional (so
        surplus solar is absorbed) but discharges conservatively: activation
        720 W and bias -0.5 A, which covers most of the house load while
        leaving a small import rather than intentionally exporting.
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

        # Energy shortfall: conserve energy. Stay bidirectional so surplus
        # solar is still absorbed, but discharge conservatively - cover most
        # of the house load and leave a small import rather than intentionally
        # exporting. This replaces the fixed SOC_THRESHOLD_FOR_STRATEGY test.
        if self._energy_shortfall:
            # 720 W is 3 A at the ~240 V UK supply, so the Wallbox's minimum
            # discharge current is reached at the activation threshold.
            evseController.setDischargeActivationPower(720)
            evseController.setDischargeCurrentBias(-0.5)
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
                discharge_aggressive=False,
                discharge_activation_w=720,
            )
            debug(
                f"IOCTGO_AGILEOUT: Shortfall mode configured - conservative "
                f"discharge (activation=720W, bias=-0.5, range=3-{self.MAX_DISCHARGE_CURRENT}A)"
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

        if decision.use_bidirectional:
            # Bidirectional mode: prefer small export over any import
            # Low activation power = discharge even at low surplus
            # Positive bias = favor higher discharge currents
            evseController.setDischargeActivationPower(1)
            evseController.setDischargeCurrentBias(0.5)
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
                discharge_activation_w=1,
            )
            debug(f"IOCTGO_AGILEOUT: Bidirectional mode configured (activation=1W, bias=+0.5, range=3-{self.MAX_DISCHARGE_CURRENT}A)")
        else:
            # Standard load-follow discharge. With no shortfall we can afford
            # to cover the house load rather than conserve energy.
            evseController.setDischargeActivationPower(1)
            evseController.setDischargeCurrentBias(0.5)
            evseController.setDischargeCurrentRange(config.WALLBOX_MIN_DISCHARGE_CURRENT, config.WALLBOX_MAX_DISCHARGE_CURRENT)
            if charge_at_cap:
                evseController.setChargeCurrentRange(0, 0)
            self._update_snapshot_ranges(
                charge_min_a=0 if charge_at_cap else None,
                charge_max_a=0 if charge_at_cap else None,
                discharge_min_a=config.WALLBOX_MIN_DISCHARGE_CURRENT,
                discharge_max_a=config.WALLBOX_MAX_DISCHARGE_CURRENT,
                discharge_aggressive=True,
                discharge_activation_w=1,
            )

    def cleanup(self):
        """Restore original calculation mode."""
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
