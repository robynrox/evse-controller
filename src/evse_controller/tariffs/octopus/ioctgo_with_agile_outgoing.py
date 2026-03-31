from ..base import Tariff
from evse_controller.drivers.EvseController import ControlState
from evse_controller.utils.config import config
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.utils.logging_config import debug, info, warning, error
import threading
import queue
from typing import Optional
import urllib.request
import json
from datetime import datetime, timedelta


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

    **Three-Tier Storage Decision:**

    Tier 1 - Storage Floor (< 5p/kWh):
      Always store - rate is too trivial to justify exporting

    Tier 2 - Self-Use Value (5p-15.71p/kWh, low SoC):
      Store for self-consumption when SoC is below threshold
      (avoids importing at 31.42p/kWh later, effective value = 15.71p after efficiency)

    Tier 3 - Future Export Optimization (all other cases):
      Store if: best_future_rate × 50% > current_rate
      Export otherwise

    Decision summary:
        if current_rate < 5p:
            STORE (rate too trivial)
        elif current_rate < 15.71p and SoC < threshold:
            STORE (self-use value)
        elif future_rate × 0.50 > current_rate:
            STORE (better future value)
        else:
            EXPORT now

    Attributes:
        time_of_use (dict): IOCTGO time periods and rates
        agile_rates (list): Today's Agile Outgoing rates (p/kWh)
        agile_rates_fetched_at (datetime): When rates were last fetched
        IMPORT_RATE_OFF_PEAK_P (float): Off-peak import rate (7p/kWh)
        IMPORT_RATE_PEAK_P (float): Peak import rate (31.42p/kWh)
        STORAGE_FLOOR_THRESHOLD_P (float): Rate below which always store (5p/kWh)
        SELF_USE_VALUE_THRESHOLD_P (float): Effective self-use value (15.71p/kWh)
        BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL (float): Conservative
            efficiency estimate (50%) for bidirectional decisions
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

    def __init__(self, command_queue: Optional[queue.Queue] = None, battery_capacity_kwh=None,
                 bulk_discharge_start_time=None, bulk_discharge_end_time=None, enable_bulk_discharge=None):
        """Initialize tariff with IOCTGO logic and Agile Outgoing rate fetching."""
        super().__init__(command_queue=command_queue)
        
        # Import rates (p/kWh) - TODO: move to config.yaml
        self.IMPORT_RATE_OFF_PEAK_P = 3.49  #  3.49p/kWh during off-peak (23:30-05:30)
        self.IMPORT_RATE_PEAK_P = 27.91     # 27.91p/kWh at all other times
        
        # Round-trip efficiency for bidirectional decision making
        # Conservative 50% estimate accounts for efficiency loss + battery wear
        # TODO: move to config.yaml
        self.BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL = 0.50
        
        # Storage decision thresholds - TODO: move to config.yaml
        # Below this rate, always store solar energy regardless of other factors
        # This prevents exporting at trivial rates when energy may be needed later
        self.STORAGE_FLOOR_THRESHOLD_P = 3.49
        
        # Self-use value threshold: effective value of stored energy when used
        # for self-consumption instead of importing at peak rates.
        # Calculation: 31.42p × 50% = 15.71p/kWh
        # When SoC is low and export rate < this threshold, storing for self-use
        # is preferable to exporting (avoids peak import later)
        self.SELF_USE_VALUE_THRESHOLD_P = (
            self.IMPORT_RATE_PEAK_P * self.BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL
        )  # = 15.71p/kWh
        
        self.time_of_use = {
            "low":  {"start": "23:30", "end": "05:30", "import_rate": self.IMPORT_RATE_OFF_PEAK_P / 100, "export_rate": 0.15},
            "high": {"start": "05:30", "end": "23:30", "import_rate": self.IMPORT_RATE_PEAK_P / 100, "export_rate": 0.15}
        }
        
        # Agile Outgoing rate storage
        self.agile_rates = []  # List of {start, end, rate} dicts
        self.agile_rates_fetched_at = None
        self._rates_lock = threading.Lock()
        
        # Get region from config
        self.region = getattr(config, 'OCTOPUS_REGION', 'K')
        self.tariff_code = self.AGILE_TARIFF_CODES.get(self.region, 'AGILE-OUTGOING-19-05-13')
        
        # Fetch rates at startup (async)
        self._fetch_rates_async()
        
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
        self.SOC_THRESHOLD_FOR_STRATEGY = config.IOCTGO_SOC_THRESHOLD_FOR_STRATEGY
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
        # Use configured max export power (default 7.2kW = full Wallbox capacity)
        self.EXPORT_POWER_KW = config.MAX_EXPORT_POWER_KW
        self.BATTERY_ROUND_TRIP_EFFICIENCY = 0.80  # 80% round-trip efficiency
        self.DISCHARGE_LOSS_FACTOR = 0.90  # 10% loss when discharging

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
        
        # Ensure OCPP is off
        from evse_controller.drivers.evse.ocpp_manager import ocpp_manager
        ocpp_manager.initialize()
        ocpp_manager.set_state(False)

    def _time_to_minutes(self, time_str: str) -> int:
        """Convert time string in HH:MM format to minutes since midnight."""
        hours, minutes = map(int, time_str.split(":"))
        return hours * 60 + minutes
    
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
        if not self.agile_rates or current_soc_percent < 0:
            return []

        battery_capacity_kwh = self.BATTERY_CAPACITY_KWH
        min_soc_percent = config.MIN_AGILE_DISCHARGE_SOC

        # Calculate export slot SoC loss per slot
        # If configured to 0, calculate from EXPORT_POWER_KW and battery capacity
        if self.EXPORT_SLOT_SOC_LOSS_PERCENT > 0:
            export_slot_soc_loss = self.EXPORT_SLOT_SOC_LOSS_PERCENT
        else:
            # Calculate: (power_kw / efficiency × 0.5h / battery_kwh) × 100
            export_slot_soc_loss = (self.EXPORT_POWER_KW / self.DISCHARGE_LOSS_FACTOR * 0.5 / battery_capacity_kwh) * 100

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

        # Calculate minimum profitable rate
        # Need: export_rate ≥ import_rate / round_trip_efficiency
        import_rate = self.time_of_use["low"]["import_rate"]  # £/kWh
        min_export_rate = import_rate / self.BATTERY_ROUND_TRIP_EFFICIENCY  # £/kWh
        min_export_rate_p = min_export_rate * 100  # Convert to p/kWh

        info(f"IOCTGO_AGILEOUT: Min export rate {min_export_rate_p:.1f}p/kWh")

        # Create list of (slot_index, rate) for all future slots today that are profitable
        # Only consider slots 0-46 (exclude slot 47 = 23:30-00:00)
        # Missing slots are treated as unprofitable (not included)
        slot_rates = []
        for rate_data in self.agile_rates:
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

    def _get_best_unused_export_rate(self, current_slot: int) -> float:
        """Find the best export rate among slots not selected for export.
        
        This is used to determine whether bidirectional load-following is more
        beneficial than exporting at the current slot's rate.
        
        Args:
            current_slot: Current slot index (0-47)
            
        Returns:
            Best export rate (p/kWh) from unused slots, or 0 if none available
        """
        if not self.agile_rates:
            return 0.0
        
        # Get current time to filter out past slots
        now = self.get_current_datetime()
        
        # Build list of (slot_index, rate) for all slots that:
        # - Are in the future (or current slot)
        # - Are NOT in the planned export slots
        # - Are before slot 47 (23:30-00:00, not available for export)
        unused_slot_rates = []
        
        for rate_data in self.agile_rates:
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
            return 0.0
        
        # Return the highest rate among unused slots
        best_rate = max(r[1] for r in unused_slot_rates)
        debug(f"IOCTGO_AGILEOUT: Best unused export rate: {best_rate:.1f}p/kWh")
        return best_rate

    def _should_use_bidirectional_mode(
        self,
        current_slot: int,
        current_export_rate_p: float,
        soc_percent: float
    ) -> bool:
        """Determine if bidirectional load-following is better than export.
        
        Uses a three-tier decision process based on export rate and battery SoC:
        
        **Tier 1: Storage Floor (always store)**
        When export rate is below STORAGE_FLOOR_THRESHOLD_P, the rate
        is too trivial to justify exporting. Always store for future use.
        
        **Tier 2: Self-Use Value (store if SoC is low)**
        When export rate is below SELF_USE_VALUE_THRESHOLD_P:
        - If SoC < SOC_THRESHOLD: Store energy for self-consumption
          (avoids importing at peak rate later)
        - If SoC >= SOC_THRESHOLD: Continue to Tier 3
        
        **Tier 3: Future Export Optimization (store for better rate)**
        When export rate is above self-use threshold, compare against best
        future unused rate adjusted for efficiency:
        - Store if: best_future_rate × 50% > current_rate
        - Export otherwise
        
        Decision summary:
        ```
        if current_rate < 3.49p:
            STORE (rate too trivial)
        elif current_rate < 15.71p:
            if SoC < threshold:
                STORE (self-use value)
            else:
                evaluate Tier 3
        else:
            evaluate Tier 3
        ```
        
        Args:
            current_slot: Current slot index (0-47)
            current_export_rate_p: Current slot's export rate (p/kWh)
            soc_percent: Current battery state of charge (%)
            
        Returns:
            True if bidirectional mode should be used
        """
        if not self.agile_rates:
            return False
        
        # Unknown SoC - fall back to export optimization only
        if soc_percent < 0:
            best_unused_rate = self._get_best_unused_export_rate(current_slot)
            if best_unused_rate <= 0:
                return False
            adjusted_future_rate = best_unused_rate * self.BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL
            return adjusted_future_rate > current_export_rate_p
        
        # === TIER 1: Storage Floor ===
        # Below this rate, always store - rate is too trivial to export
        if current_export_rate_p < self.STORAGE_FLOOR_THRESHOLD_P:
            debug(f"IOCTGO_AGILEOUT: STORE (Tier 1): rate {current_export_rate_p:.1f}p < floor {self.STORAGE_FLOOR_THRESHOLD_P:.1f}p")
            return True
        
        # === TIER 2: Self-Use Value ===
        # When rate is below self-use threshold and SoC is low, store for self-consumption
        if current_export_rate_p < self.SELF_USE_VALUE_THRESHOLD_P:
            if soc_percent < self.SOC_THRESHOLD_FOR_STRATEGY:
                debug(f"IOCTGO_AGILEOUT: STORE (Tier 2): rate {current_export_rate_p:.1f}p < self-use value "
                      f"{self.SELF_USE_VALUE_THRESHOLD_P:.1f}p, SoC {soc_percent}% < threshold "
                      f"{self.SOC_THRESHOLD_FOR_STRATEGY}%")
                return True
            # SoC is adequate, fall through to Tier 3
        
        # === TIER 3: Future Export Optimization ===
        # Get the best rate among slots we're NOT exporting in
        best_unused_rate = self._get_best_unused_export_rate(current_slot)
        
        if best_unused_rate <= 0:
            debug(f"IOCTGO_AGILEOUT: EXPORT (Tier 3): no better future rates available")
            return False
        
        # Apply round-trip efficiency to the future rate
        adjusted_future_rate = best_unused_rate * self.BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL
        
        should_use_bidirectional = adjusted_future_rate > current_export_rate_p
        
        if should_use_bidirectional:
            debug(f"IOCTGO_AGILEOUT: STORE (Tier 3): current={current_export_rate_p:.1f}p, "
                  f"future={best_unused_rate:.1f}p × {self.BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL:.0%} = "
                  f"{adjusted_future_rate:.1f}p")
        else:
            debug(f"IOCTGO_AGILEOUT: EXPORT (Tier 3): current={current_export_rate_p:.1f}p >= "
                  f"future={best_unused_rate:.1f}p × {self.BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL:.0%} = "
                  f"{adjusted_future_rate:.1f}p")
        
        return should_use_bidirectional

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

    def get_dashboard_html(self) -> str:
        """Return HTML for Agile Outgoing rate display.

        Returns a 48-cell visual strip showing today's export rates with
        red→green gradient coloring. Responsive design adapts to viewport width.
        Missing slots shown as grey placeholders.
        Planned export slots are highlighted with "EX" label.
        """
        with self._rates_lock:
            if not self.agile_rates:
                # No rates fetched yet - show loading message
                return """
                <div class="agile-rate-strip" style="height:40px;display:flex;align-items:center;justify-content:center;background:#f5f5f5;border-radius:4px;margin:10px 0;">
                    <span style="color:#666;font-size:14px;">Loading Agile Outgoing rates...</span>
                </div>
                """

            # Get rates for display (today's 48 half-hour slots)
            rates = self.agile_rates
            if not rates:
                return ""

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
                energy_per_slot_kwh = (self.EXPORT_POWER_KW / self.DISCHARGE_LOSS_FACTOR) * 0.5
                total_energy_needed = len(planned_slots) * energy_per_slot_kwh
                
                # Calculate available energy
                battery_capacity_kwh = self.BATTERY_CAPACITY_KWH
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
            html = ['<div style="position:relative;margin:10px 0;">']
            html.append('<div class="agile-rate-strip" style="height:40px;display:flex;gap:1px;padding:5px;background:#fafafa;border-radius:4px;overflow:hidden;">')
            
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
                        time_axis += f'<span style="position:absolute;left:0;top:9px;transform:translateX(-50%);font-size:9px;color:#666;">{hour:02d}:00</span>'
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
                    soc_drop_per_slot = (self.EXPORT_POWER_KW / self.DISCHARGE_LOSS_FACTOR * 0.5 / self.BATTERY_CAPACITY_KWH) * 100
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
                .agile-rate-cell { min-height: 28px; }
                .agile-rate-strip { height: 36px !important; }
            }
            
            /* < 400px: reduce height further */
            @media (max-width: 400px) {
                .agile-rate-cell { min-height: 20px; }
                .agile-rate-strip { height: 28px !important; padding-bottom: 12px !important; }
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
        DISCHARGE_RATE_PER_AMP = (0.46 * 59) / self.BATTERY_CAPACITY_KWH
        required_amps = required_discharge_rate / DISCHARGE_RATE_PER_AMP
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
        - Other times: Three-tier decision for bidirectional vs export:
          1. Rate < 5p: STORE (too trivial to export)
          2. Rate < 15.71p + low SoC: STORE (self-use value)
          3. Otherwise: STORE if future_rate × 50% > current_rate
        """
        battery_level = state.battery_level
        current_slot = dayMinute // 30

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

        # OFF-PEAK CHARGING HAS ABSOLUTE PRIORITY (23:30-05:30)
        # Charge at max rate regardless of SoC or other states
        if self.is_off_peak(dayMinute):
            if battery_level < config.MAX_CHARGE_PERCENT:
                return ControlState.CHARGE, None, None, "IOCTGO_AGILEOUT Off-peak: CHARGE AT MAX RATE (priority)"
            else:
                return ControlState.DORMANT, None, None, "IOCTGO_AGILEOUT Off-peak: SoC max, dormant"

        # Handle unknown SoC (outside off-peak hours)
        if battery_level == -1:
            return ControlState.CHARGE, 3, 3, "IOCTGO_AGILEOUT SoC unknown, charge at 3A"

        # Battery depleted - protect battery
        if battery_level <= 25:
            return ControlState.DORMANT, None, None, "IOCTGO_AGILEOUT Battery depleted"

        # Check if current slot is a planned export slot
        if current_slot in self._planned_export_slots:
            # Export at maximum rate during planned slots
            debug(f"IOCTGO_AGILEOUT: In export slot {current_slot}, commanding DISCHARGE")
            return ControlState.DISCHARGE, None, None, f"IOCTGO_AGILEOUT Export slot (max discharge)"

        # All other times: Decide between load-follow discharge and bidirectional mode
        # Get current slot's export rate for comparison
        current_export_rate_p = 0.0
        for rate_data in self.agile_rates:
            hour = rate_data['start'].hour
            slot_idx = hour * 2 + (1 if rate_data['start'].minute == 30 else 0)
            if slot_idx == current_slot:
                current_export_rate_p = rate_data['rate']
                break

        # Determine if bidirectional mode is better than export (uses SoC-aware logic)
        use_bidirectional = self._should_use_bidirectional_mode(
            current_slot,
            current_export_rate_p,
            battery_level
        )

        if use_bidirectional:
            # Bidirectional mode: charge from solar, prefer small export over any import
            debug(f"IOCTGO_AGILEOUT: Using LOAD_FOLLOW_BIDIRECTIONAL (current rate {current_export_rate_p:.1f}p, SoC {battery_level}%)")
            return ControlState.LOAD_FOLLOW_BIDIRECTIONAL, 3, self.MAX_DISCHARGE_CURRENT, "IOCTGO_AGILEOUT Bidirectional (store for better rate)"
        else:
            # Standard load-follow discharge
            debug(f"IOCTGO_AGILEOUT: Using LOAD_FOLLOW_DISCHARGE (current rate {current_export_rate_p:.1f}p, SoC {battery_level}%)")
            return ControlState.LOAD_FOLLOW_DISCHARGE, 2, self.MAX_DISCHARGE_CURRENT, "IOCTGO_AGILEOUT Load follow"

    def set_home_demand_levels(self, evseController, state: EvseAsyncState, dayMinute: int):
        """Configure home demand power levels."""
        if self.SMART_OCPP_OPERATION:
            self._ocpp_check_turn_on(state, dayMinute)

        if not hasattr(self, 'evseController'):
            self.evseController = evseController
            self.original_calculation_method = evseController.use_new_current_calculation
            evseController.use_new_current_calculation = True

        battery_level = state.battery_level
        current_slot = dayMinute // 30

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

        # Check if we should be in bidirectional mode (uses SoC-aware logic)
        # Get current export rate for comparison
        current_export_rate_p = 0.0
        for rate_data in self.agile_rates:
            hour = rate_data['start'].hour
            slot_idx = hour * 2 + (1 if rate_data['start'].minute == 30 else 0)
            if slot_idx == current_slot:
                current_export_rate_p = rate_data['rate']
                break

        use_bidirectional = self._should_use_bidirectional_mode(
            current_slot,
            current_export_rate_p,
            battery_level
        )

        if use_bidirectional:
            # Bidirectional mode: prefer small export over any import
            # Low activation power = discharge even at low surplus
            # Positive bias = favor higher discharge currents
            evseController.setDischargeActivationPower(1)
            evseController.setDischargeCurrentBias(0.5)
            evseController.setDischargeCurrentRange(3, self.MAX_DISCHARGE_CURRENT)
            evseController.setChargeActivationPower(720)
            evseController.setChargeCurrentBias(-0.5)
            evseController.setChargeCurrentRange(3, self.MAX_CHARGE_CURRENT)
            debug(f"IOCTGO_AGILEOUT: Bidirectional mode configured (activation=1W, bias=+0.5, range=3-{self.MAX_DISCHARGE_CURRENT}A)")
        else:
            # Standard load-follow discharge: Configure based on SoC threshold
            if battery_level >= self.SOC_THRESHOLD_FOR_STRATEGY:
                # High SoC: aggressive discharge
                evseController.setDischargeActivationPower(1)
                evseController.setDischargeCurrentBias(0.5)
                evseController.setDischargeCurrentRange(config.WALLBOX_MIN_DISCHARGE_CURRENT, config.WALLBOX_MAX_DISCHARGE_CURRENT)
            else:
                # Low SoC: conservative discharge
                evseController.setDischargeActivationPower(720)
                evseController.setDischargeCurrentBias(-0.5)
                evseController.setDischargeCurrentRange(config.WALLBOX_MIN_DISCHARGE_CURRENT, config.WALLBOX_MAX_DISCHARGE_CURRENT)

        # Track which slots we actually export in
        current_slot_idx = dayMinute // 30
        # Check if we're in discharge mode during a planned export slot
        if current_slot_idx in self._planned_export_slots:
            # Check if actually discharging (negative current means discharge)
            if state.current < -1:  # More than 1A discharge
                if current_slot_idx not in self._exported_slots:
                    self._exported_slots.append(current_slot_idx)
                    info(f"IOCTGO_AGILEOUT: Export started in slot {current_slot_idx} ({dayMinute//60:02d}:{dayMinute%60:02d})")

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
