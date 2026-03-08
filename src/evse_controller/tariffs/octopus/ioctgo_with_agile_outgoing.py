from ..base import Tariff
from evse_controller.drivers.EvseController import ControlState
from evse_controller.utils.config import config
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.drivers.evse.wallbox.wallbox_api_with_ocpp import WallboxAPIWithOCPP
from evse_controller.drivers.evse.event_bus import EventBus, EventType
from evse_controller.utils.logging_config import debug, info, warning, error
import time
import threading
import queue
from typing import Optional
import asyncio
import urllib.request
import json
from datetime import datetime, timedelta


class IOctGoWithAgileOutgoingTariff(Tariff):
    """Intelligent Octopus Go tariff with Agile Outgoing export display.
    
    This tariff combines:
    - Intelligent Octopus Go import (cheap rate 23:30-05:30)
    - Agile Outgoing export rate display on dashboard
    
    The dashboard shows a 48-cell visual strip of today's Agile Outgoing
    export rates, allowing users to see peak export periods at a glance.
    
    Attributes:
        time_of_use (dict): IOCTGO time periods and rates
        agile_rates (list): Today's Agile Outgoing rates (p/kWh)
        agile_rates_fetched_at (datetime): When rates were last fetched
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
        self.time_of_use = {
            "low":  {"start": "23:30", "end": "05:30", "import_rate": 0.0700, "export_rate": 0.15},
            "high": {"start": "05:30", "end": "23:30", "import_rate": 0.3142, "export_rate": 0.15}
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
        self._ocpp_enabled = None
        self._state_lock = threading.Lock()
        self._last_soc_check = -1
        self._dynamic_ocpp_disable_time = None
        self._last_ocpp_request_time = 0
        self._ocpp_request_cooldown = 300
        
        self.OCPP_ENABLE_SOC_THRESHOLD = config.IOCTGO_OCPP_ENABLE_SOC_THRESHOLD
        self.OCPP_DISABLE_SOC_THRESHOLD = config.IOCTGO_OCPP_DISABLE_SOC_THRESHOLD
        self.OCPP_ENABLE_TIME_STR = config.IOCTGO_OCPP_ENABLE_TIME
        self.OCPP_DISABLE_TIME_STR = config.IOCTGO_OCPP_DISABLE_TIME
        self.OCPP_ENABLE_TIME = self._time_to_minutes(config.IOCTGO_OCPP_ENABLE_TIME)
        self.OCPP_DISABLE_TIME = self._time_to_minutes(config.IOCTGO_OCPP_DISABLE_TIME)
        
        # Export optimization parameters
        # Use configured max export power (default 7.2kW = full Wallbox capacity)
        self.EXPORT_POWER_KW = config.MAX_EXPORT_POWER_KW
        self.BATTERY_ROUND_TRIP_EFFICIENCY = 0.80  # 80% round-trip efficiency
        self.DISCHARGE_LOSS_FACTOR = 0.90  # 10% loss when discharging
        
        # Planned export slots (calculated dynamically)
        self._planned_export_slots = []
        self._exported_slots = []  # Track slots where export actually occurred
        
        # Cache for export plan calculation
        self._last_plan_soc = -1
        self._last_plan_slot = -1
        self._plan_cache = []
        
        # Event bus subscription
        self._event_bus = EventBus()
        self._event_bus.subscribe(EventType.OCPP_ENABLED, self._handle_ocpp_enabled)
        self._event_bus.subscribe(EventType.OCPP_DISABLED, self._handle_ocpp_disabled)
        
        # Initialize OCPP
        from evse_controller.drivers.evse.ocpp_manager import ocpp_manager
        ocpp_manager.initialize()
        self._ocpp_enabled = ocpp_manager.get_state()

    def _time_to_minutes(self, time_str: str) -> int:
        """Convert time string in HH:MM format to minutes since midnight."""
        hours, minutes = map(int, time_str.split(":"))
        return hours * 60 + minutes
    
    def calculate_export_plan(self, current_soc_percent: float) -> list:
        """Calculate which slots to use for export based on current SoC and rates.
        
        Args:
            current_soc_percent: Current battery state of charge (%)
            
        Returns:
            List of slot indices (0-47) planned for export
        """
        if not self.agile_rates or current_soc_percent < 0:
            return []
        
        # Calculate energy available for export
        battery_capacity_kwh = self.BATTERY_CAPACITY_KWH
        min_soc_kwh = (config.MIN_AGILE_DISCHARGE_SOC / 100.0) * battery_capacity_kwh
        current_energy_kwh = (current_soc_percent / 100.0) * battery_capacity_kwh
        available_energy_kwh = current_energy_kwh - min_soc_kwh
        
        debug(f"IOCTGO_AGILEOUT: Export plan - SoC={current_soc_percent}%, min={config.MIN_AGILE_DISCHARGE_SOC}%, available={available_energy_kwh:.2f}kWh")
        info(f"IOCTGO_AGILEOUT: Planning export - SoC {current_soc_percent}%, min {config.MIN_AGILE_DISCHARGE_SOC}%, {available_energy_kwh:.1f}kWh available")
        
        if available_energy_kwh <= 0:
            debug(f"IOCTGO_AGILEOUT: No energy available for export")
            return []
        
        # Calculate energy per slot (with 10% discharge loss)
        # To export EXPORT_POWER_KW for 30min, we need more energy from battery
        energy_per_slot_kwh = (self.EXPORT_POWER_KW / self.DISCHARGE_LOSS_FACTOR) * 0.5
        
        info(f"IOCTGO_AGILEOUT: Export power {self.EXPORT_POWER_KW:.2f}kW, {energy_per_slot_kwh:.2f}kWh per slot")
        
        # Calculate minimum profitable rate
        # Need: export_rate ≥ import_rate / round_trip_efficiency
        import_rate = self.time_of_use["low"]["import_rate"]  # £/kWh
        min_export_rate = import_rate / self.BATTERY_ROUND_TRIP_EFFICIENCY  # £/kWh
        min_export_rate_p = min_export_rate * 100  # Convert to p/kWh
        
        info(f"IOCTGO_AGILEOUT: Min export rate {min_export_rate_p:.1f}p/kWh")
        
        # Get current time to filter out past slots
        now = self.get_current_datetime()
        current_slot_idx = now.hour * 2 + (1 if now.minute >= 30 else 0)
        
        # Create list of (slot_index, rate) for all future slots today
        slot_rates = []
        for rate_data in self.agile_rates:
            hour = rate_data['start'].hour
            slot_idx = hour * 2 + (1 if rate_data['start'].minute == 30 else 0)
            
            # Skip slots that have completely passed (current slot is still valid)
            if slot_idx < current_slot_idx:
                continue
            
            rate_p = rate_data['rate']  # Already in p/kWh
            debug(f"IOCTGO_AGILEOUT: Slot {slot_idx} ({hour:02d}:{rate_data['start'].minute:02d}) @ {rate_p:.1f}p, min_rate={min_export_rate_p:.1f}p")
            if rate_p >= min_export_rate_p:  # Only consider profitable slots
                slot_rates.append((slot_idx, rate_p))
        
        debug(f"IOCTGO_AGILEOUT: Found {len(slot_rates)} profitable slots (all day)")
        
        # Sort by rate (highest first)
        slot_rates.sort(key=lambda x: x[1], reverse=True)
        
        # Select slots until we run out of energy
        planned_slots = []
        remaining_energy = available_energy_kwh
        
        debug(f"IOCTGO_AGILEOUT: Considering {len(slot_rates)} slots, selecting up to {int(available_energy_kwh / energy_per_slot_kwh) + 1} slots")
        
        for slot_idx, rate in slot_rates:
            if remaining_energy > 0:  # Take slots while we have any energy left
                planned_slots.append(slot_idx)
                remaining_energy -= energy_per_slot_kwh
                debug(f"IOCTGO_AGILEOUT: Selected slot {slot_idx} ({16+slot_idx//2-8:02d}:{'00' if slot_idx%2==0 else '30'}) @ {rate:.1f}p, remaining={remaining_energy:.2f}kWh")
            else:
                break
        
        debug(f"IOCTGO_AGILEOUT: Final plan: {len(planned_slots)} slots: {planned_slots}")
        
        return planned_slots

    def _fetch_rates_async(self):
        """Fetch Agile Outgoing rates asynchronously (non-blocking)."""
        def fetch_thread():
            try:
                rates = self._fetch_rates_from_api()
                with self._rates_lock:
                    # Check if we're fetching a new day's rates
                    if rates:
                        today = datetime.now().date()
                        last_fetch_date = getattr(self, '_last_fetch_date', None)
                        
                        if last_fetch_date != today:
                            self.agile_rates = rates
                            self.agile_rates_fetched_at = datetime.now()
                            self._last_fetch_date = today
                            info(f"IOCTGO_AGILEOUT: Fetched {len(rates)} Agile Outgoing rates for {today} (region {self.region})")
                            
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
                                    self._last_plan_slot = (datetime.now().hour * 60 + datetime.now().minute) // 30
                                    self._plan_cache = self._planned_export_slots.copy()
                                    if self._planned_export_slots:
                                        info(f"IOCTGO_AGILEOUT: Export plan recalculated with new rates - {len(self._planned_export_slots)} slots planned")
                            except Exception as e:
                                debug(f"IOCTGO_AGILEOUT: Could not recalculate export plan after rate fetch: {e}")
                        else:
                            debug(f"IOCTGO_AGILEOUT: Rates already fetched for {today}, skipping")
            except Exception as e:
                error(f"IOCTGO_AGILEOUT: Failed to fetch Agile rates: {e}")

        thread = threading.Thread(target=fetch_thread, daemon=True)
        thread.start()

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
                
                rates.append({
                    'start': start_dt,
                    'end': end_dt,
                    'rate': round(rate_inc, 2)
                })
            
            # Sort by start time and filter to today only
            rates.sort(key=lambda r: r['start'])
            
            # Filter to today only and remove duplicates
            day_start = datetime(today.year, today.month, today.day, tzinfo=rates[0]['start'].tzinfo) if rates else datetime.now()
            day_end = day_start + timedelta(days=1)
            
            # Use dict to remove duplicates (key by start time)
            unique_rates = {}
            for r in rates:
                if day_start <= r['start'] < day_end:
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
                
                # Calculate SoC drop per slot
                # SoC drop = (power_kw / efficiency × hours / battery_kwh) × 100
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
        
        Simplified logic:
        - Off-peak (23:30-05:30): Charge at max rate (IOCTGO cheap rate)
        - Battery depleted: Dormant
        - During planned export slots: Discharge at max rate
        - All other times: Load-follow discharge
        """
        battery_level = state.battery_level
        current_slot = dayMinute // 30
        
        # Recalculate export plan if needed (before making control decision)
        should_recalculate = (
            battery_level != self._last_plan_soc or
            current_slot != self._last_plan_slot
        )
        
        if should_recalculate:
            self._planned_export_slots = self.calculate_export_plan(battery_level)
            self._last_plan_soc = battery_level
            self._last_plan_slot = current_slot
            self._plan_cache = self._planned_export_slots.copy()
            if self._planned_export_slots:
                info(f"IOCTGO_AGILEOUT: Export plan updated - {len(self._planned_export_slots)} slots planned")
        else:
            # Use cached plan
            self._planned_export_slots = self._plan_cache
        
        debug(f"IOCTGO_AGILEOUT: get_control_state - SoC={battery_level}%, dayMinute={dayMinute}, current_slot={current_slot}, planned_slots={self._planned_export_slots}")
        
        # Handle unknown SoC
        if battery_level == -1:
            return ControlState.CHARGE, 3, 3, "IOCTGO_AGILEOUT SoC unknown, charge at 3A"
        
        # Off-peak charging (23:30-05:30) - IOCTGO cheap rate
        if self.is_off_peak(dayMinute):
            if battery_level < config.MAX_CHARGE_PERCENT:
                return ControlState.CHARGE, None, None, "IOCTGO_AGILEOUT Cheap rate: charge at max"
            else:
                return ControlState.DORMANT, None, None, "IOCTGO_AGILEOUT Cheap rate: SoC max"
        
        # Battery depleted - protect battery
        if battery_level <= 25:
            return ControlState.DORMANT, None, None, "IOCTGO_AGILEOUT Battery depleted"
        
        # Check if current slot is a planned export slot
        if current_slot in self._planned_export_slots:
            # Export at maximum rate during planned slots
            debug(f"IOCTGO_AGILEOUT: In export slot {current_slot}, commanding DISCHARGE")
            return ControlState.DISCHARGE, None, None, f"IOCTGO_AGILEOUT Export slot (max discharge)"
        
        # All other times: Load-follow discharge
        # (set_home_demand_levels will configure the strategy based on SoC threshold)
        debug(f"IOCTGO_AGILEOUT: Not in export slot, commanding LOAD_FOLLOW_DISCHARGE")
        return ControlState.LOAD_FOLLOW_DISCHARGE, 2, self.MAX_DISCHARGE_CURRENT, "IOCTGO_AGILEOUT Load follow"

    def set_home_demand_levels(self, evseController, state: EvseAsyncState, dayMinute: int):
        """Configure home demand power levels."""
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
        
        # Outside export slots: Configure load-following based on SoC threshold
        if battery_level >= self.SOC_THRESHOLD_FOR_STRATEGY:
            evseController.setDischargeActivationPower(1)
            evseController.setDischargeCurrentBias(0.5)
            evseController.setDischargeCurrentRange(config.WALLBOX_MIN_DISCHARGE_CURRENT, config.WALLBOX_MAX_DISCHARGE_CURRENT)
        else:
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

        if self.SMART_OCPP_OPERATION:
            self._manage_ocpp_state(state, dayMinute)

    def cleanup(self):
        """Restore original calculation mode."""
        if hasattr(self, 'original_calculation_method') and hasattr(self, 'evseController'):
            self.evseController.use_new_current_calculation = self.original_calculation_method

    # === OCPP Management Methods (copied from IOCTGO) ===
    
    def _handle_ocpp_enabled(self, event):
        """Handle OCPP enabled event."""
        with self._state_lock:
            self._ocpp_enabled = True
    
    def _handle_ocpp_disabled(self, event):
        """Handle OCPP disabled event."""
        with self._state_lock:
            self._ocpp_enabled = False
    
    def should_enable_ocpp_due_to_soc(self, state):
        """Check if OCPP should be enabled due to low SoC."""
        if state.battery_level < 0:
            return False
        return state.battery_level < self.OCPP_ENABLE_SOC_THRESHOLD
    
    def should_enable_ocpp_due_to_time(self, dayMinute):
        """Check if OCPP should be enabled due to time (23:30)."""
        return dayMinute >= self.OCPP_ENABLE_TIME and dayMinute < (self.OCPP_ENABLE_TIME + 30)
    
    def should_disable_ocpp(self, state, dayMinute):
        """Check if OCPP should be disabled."""
        if state.battery_level < 0:
            return False
        
        # Check dynamic disable time
        with self._state_lock:
            if self._dynamic_ocpp_disable_time is not None:
                if dayMinute >= self._dynamic_ocpp_disable_time:
                    return True
        
        # Check SoC-based disable
        if state.battery_level >= self.OCPP_DISABLE_SOC_THRESHOLD:
            return True
        
        return False
    
    def _schedule_return_to_ioctgo(self):
        """Schedule return to IOCTGO mode at 23:30."""
        pass  # Simplified for now
    
    def _manage_ocpp_state(self, state: EvseAsyncState, dayMinute: int):
        """Manage OCPP state."""
        try:
            with self._state_lock:
                is_ocpp_currently_enabled = self._ocpp_enabled if self._ocpp_enabled is not None else False

            should_enable_due_to_soc = self.should_enable_ocpp_due_to_soc(state)
            should_enable_due_to_time = self.should_enable_ocpp_due_to_time(dayMinute)
            should_disable = self.should_disable_ocpp(state, dayMinute)

            current_time = self.get_current_time()
            if current_time - self._last_ocpp_request_time < self._ocpp_request_cooldown:
                return

            if should_enable_due_to_soc and not is_ocpp_currently_enabled:
                if self.command_queue:
                    self.command_queue.put("ocpp")
                    self._last_ocpp_request_time = current_time
                    self._schedule_return_to_ioctgo()

            elif should_enable_due_to_time and not is_ocpp_currently_enabled:
                from evse_controller.drivers.evse.ocpp_manager import ocpp_manager
                try:
                    ocpp_manager.set_state(True)
                    self._last_ocpp_request_time = current_time
                    with self._state_lock:
                        self._dynamic_ocpp_disable_time = self.OCPP_DISABLE_TIME
                except Exception as e:
                    error(f"IOCTGO_AGILEOUT: Could not enable OCPP: {e}")

            elif should_disable and is_ocpp_currently_enabled:
                from evse_controller.drivers.evse.ocpp_manager import ocpp_manager
                try:
                    ocpp_manager.set_state(False)
                    self._last_ocpp_request_time = current_time
                    with self._state_lock:
                        self._dynamic_ocpp_disable_time = None
                except Exception as e:
                    error(f"IOCTGO_AGILEOUT: Could not disable OCPP: {e}")
        except Exception as e:
            error(f"IOCTGO_AGILEOUT: Error in _manage_ocpp_state: {e}")
