from evse_controller.utils.config import config
from .octopus.octgo import OctopusGoTariff
from .octopus.flux import OctopusFluxTariff
from .octopus.cosy import CosyOctopusTariff
from .octopus.ioctgo import IntelligentOctopusGoTariff
from .octopus.ioctgo_with_agile_outgoing import IOctGoWithAgileOutgoingTariff
from .base import Tariff
from evse_controller.drivers.evse.async_interface import EvseThreadInterface, EvseState
from evse_controller.utils.logging_config import debug, error

class TariffManager:
    def __init__(self, command_queue):
        self._command_queue = command_queue
        # Store tariff classes instead of instances to instantiate only when needed
        self.tariff_classes = {
            "OCTGO": OctopusGoTariff,
            "IOCTGO": IntelligentOctopusGoTariff,
            "IOCTGO_AGILEOUT": IOctGoWithAgileOutgoingTariff,
            "COSY": CosyOctopusTariff,
            "FLUX": OctopusFluxTariff
        }
        # Check if startup state is a tariff that exists in our tariffs dictionary
        if config.STARTUP_STATE in self.tariff_classes:
            # Instantiate the tariff for the startup state
            self.current_tariff = self.tariff_classes[config.STARTUP_STATE](command_queue=self._command_queue)
            self.tariff_name = config.STARTUP_STATE
        else:
            # For non-tariff startup states (like FREERUN), set to None
            self.current_tariff = None
            self.tariff_name = None
        self._sync_report_savings_provider()
        self._sync_report_price_source()

    def _sync_report_savings_provider(self) -> None:
        """Tell the daily report which tariff can attribute slot savings.

        Only a tariff that tracks stored-energy savings exposes
        ``saved_for_slot``.  Clearing the provider when another controller is
        active makes the report show ``—`` rather than a misleading £0.00.
        """
        from evse_controller.daily_report import daily_log

        provider = getattr(self.current_tariff, "saved_for_slot", None)
        daily_log.set_savings_provider(provider if callable(provider) else None)

    def _sync_report_price_source(self) -> None:
        """Publish the active tariff through the normalised source contract.

        The report keeps its price-book fallback for FREERUN, PAUSE and OCPP.
        When a tariff is active its adapter is authoritative, so every tariff
        prices the report through the same 48-row contract.
        """
        from evse_controller.daily_report import daily_log
        from evse_controller.tariffs.source import TariffSourceAdapter

        source = (
            TariffSourceAdapter(self.current_tariff)
            if self.current_tariff is not None
            else None
        )
        daily_log.set_price_source(source)

    def set_tariff(self, tariff_name):
        if tariff_name in self.tariff_classes:
            self.stop_tariff()
            # Create a new instance of the requested tariff with the command queue
            self.current_tariff = self.tariff_classes[tariff_name](command_queue=self._command_queue)
            # The initialization now happens in the tariff's constructor
            self.tariff_name = tariff_name
            self._sync_report_savings_provider()
            self._sync_report_price_source()
            return True
        return False
    
    def start_tariff(self):
        """Start the tariff configured in config.STARTUP_STATE."""
        # Always use the current config value, not the cached tariff_name
        # This allows config changes to take effect when switching to SMART mode
        if config.STARTUP_STATE in self.tariff_classes:
            return self.set_tariff(config.STARTUP_STATE)
        return False

    def get_tariff(self) -> Tariff:
        return self.current_tariff
    
    def stop_tariff(self):
        # Allow current tariff to exit cleanly.
        if self.current_tariff is not None:
            self.current_tariff.cleanup()
        self.current_tariff = None
        self._sync_report_savings_provider()
        self._sync_report_price_source()

    def save_state(self):
        """Persist the active tariff's state on a clean shutdown.

        Switching tariffs already goes through ``cleanup()``, which saves.
        This explicit hook covers the process exiting while a tariff is still
        active, without requiring the caller to tear the tariff down.
        """
        if self.current_tariff is None:
            return
        try:
            self.current_tariff.save_state()
        except Exception as exc:
            error(f"TariffManager: Failed to save tariff state: {exc}")

    def get_control_state(self, dayMinute):
        """Get control state from current tariff.

        Args:
            dayMinute (int): Minutes since midnight

        Returns:
            tuple: (ControlState, min_current, max_current, reason_string)
        """
        # If no current tariff (e.g., for FREERUN or other non-tariff states), return appropriate values
        if self.current_tariff is None:
            # In non-tariff states, we let the EVSE operate normally without tariff control
            # Return values that allow normal operation
            from evse_controller.drivers.evse.async_interface import EvseState
            # Get the appropriate EVSE instance using the factory method
            evse = EvseThreadInterface.get_instance()
            evse_state = evse.get_state()
            debug(f"TariffManager: Non-tariff state active ({config.STARTUP_STATE}), state={evse_state}")
            # Return values that keep the system in its current state
            return EvseState.FREERUN, 0, 0, f"{config.STARTUP_STATE} - Manual Control Mode"
        
        # Get the appropriate EVSE instance using the factory method
        evse = EvseThreadInterface.get_instance()
        evse_state = evse.get_state()
        debug(f"TariffManager: Getting control state with state={evse_state}")
        return self.current_tariff.get_control_state(evse_state, dayMinute)

    def observe(self, dayMinute):
        """Run the active tariff as an observer, without applying its command.

        Temporary execution overrides still need planner history, SoC samples
        and decision records.  This calls the tariff's normal decision method
        but the caller deliberately ignores the actuator tuple.  It does not
        call ``set_home_demand_levels``.
        """
        if self.current_tariff is None:
            return None
        evse = EvseThreadInterface.get_instance()
        evse_state = evse.get_state()
        return self.current_tariff.get_control_state(evse_state, dayMinute)
