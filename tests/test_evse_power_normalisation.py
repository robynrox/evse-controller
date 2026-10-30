"""The controller must honour the configured Shelly channels.

``Power.getHomeWatts()`` and the tariff ledgers both assume that the power
object carries the grid on channel 1 and the EVSE on channel 2.  That is a
convention, not a hardware order: the Shelly assignment is configuration, so
``EvseController`` resolves it once and exposes a normalised view.
"""

from collections import deque
from types import SimpleNamespace
import time
from unittest.mock import Mock

import evse_controller.drivers.EvseController as evse_module
from evse_controller.drivers.EvseController import ControlState, EvseController
from evse_controller.drivers.Power import Power
from evse_controller.drivers.evse.async_interface import EvseState


def _controller() -> EvseController:
    """A controller with only the state the normalisation needs."""
    controller = object.__new__(EvseController)
    controller.evse_power_model = Mock()
    controller.evse_power_model.get_power.return_value = 1234.0
    controller.auxpower = Power()
    return controller


def _config(**overrides) -> SimpleNamespace:
    values = {
        "SHELLY_GRID_DEVICE": "primary",
        "SHELLY_GRID_CHANNEL": 1,
        "SHELLY_EVSE_DEVICE": "primary",
        "SHELLY_EVSE_CHANNEL": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestConfiguredChannels:
    def test_default_mapping_is_ch1_grid_ch2_evse(self, monkeypatch):
        monkeypatch.setattr(evse_module, "config", _config())
        controller = _controller()
        power = Power(ch1Watts=800, ch2Watts=1200, voltage=241, soc=55)

        normalised = controller._normalise_grid_power(power)

        assert normalised.ch1Watts == 800
        assert normalised.ch2Watts == 1200
        assert normalised.voltage == 241
        assert normalised.soc == 55
        # Grid import minus EVSE draw: 400 W of surplus.
        assert normalised.getHomeWatts() == -400

    def test_swapped_channels_are_resolved_from_config(self, monkeypatch):
        """Grid on ch2 and EVSE on ch1 must not invert the home power."""
        monkeypatch.setattr(
            evse_module,
            "config",
            _config(SHELLY_GRID_CHANNEL=2, SHELLY_EVSE_CHANNEL=1),
        )
        controller = _controller()
        power = Power(ch1Watts=-500, ch2Watts=900, voltage=238)

        normalised = controller._normalise_grid_power(power)

        assert normalised.ch1Watts == 900
        assert normalised.ch2Watts == -500
        # 900 W imported plus 500 W discharged by the car.
        assert normalised.getHomeWatts() == 1400

    def test_evse_on_the_other_device_uses_aux_power(self, monkeypatch):
        monkeypatch.setattr(
            evse_module,
            "config",
            _config(
                SHELLY_EVSE_DEVICE="secondary",
                SHELLY_EVSE_CHANNEL=1,
            ),
        )
        controller = _controller()
        controller.auxpower = Power(ch1Watts=-1500, ch2Watts=0, voltage=240)
        power = Power(ch1Watts=100, ch2Watts=9999, voltage=240)

        normalised = controller._normalise_grid_power(power)

        assert normalised.ch1Watts == 100
        assert normalised.ch2Watts == -1500
        assert normalised.getHomeWatts() == 1600

    def test_missing_aux_reading_does_not_invent_evse_load(self, monkeypatch):
        monkeypatch.setattr(
            evse_module,
            "config",
            _config(
                SHELLY_EVSE_DEVICE="secondary",
                SHELLY_EVSE_CHANNEL=2,
            ),
        )
        controller = _controller()
        del controller.auxpower  # the other Shelly has not reported yet
        power = Power(ch1Watts=250, ch2Watts=4000, voltage=240)

        normalised = controller._normalise_grid_power(power)

        assert normalised.ch1Watts == 250
        assert normalised.ch2Watts == 0.0

    def test_unconfigured_evse_falls_back_to_the_power_model(self, monkeypatch):
        """With no EVSE monitor, ch2 of the grid device is not the EVSE."""
        monkeypatch.setattr(
            evse_module,
            "config",
            _config(SHELLY_EVSE_DEVICE="", SHELLY_EVSE_CHANNEL=None),
        )
        controller = _controller()
        power = Power(ch1Watts=400, ch2Watts=9999, voltage=236)

        normalised = controller._normalise_grid_power(power)

        assert normalised.ch1Watts == 400
        assert normalised.ch2Watts == 1234.0
        controller.evse_power_model.set_voltage.assert_called_once_with(236)

    def test_channel_zero_and_three_fall_back_to_the_model(self, monkeypatch):
        """Only Shelly channels 1 and 2 are valid."""
        controller = _controller()
        power = Power(ch1Watts=400, ch2Watts=9999, voltage=236)

        for bad_channel in (None, 0, 3):
            monkeypatch.setattr(
                evse_module,
                "config",
                _config(SHELLY_EVSE_CHANNEL=bad_channel),
            )
            normalised = controller._normalise_grid_power(power)
            assert normalised.ch2Watts == 1234.0


class TestControllerUpdate:
    def test_update_exposes_and_controls_on_the_normalised_view(
        self, monkeypatch
    ):
        """update() must hand control and the tariff the same resolved view."""
        controller = _controller()
        controller.MAX_HISTORY_POINTS = 10
        controller.history = {"entries": deque()}
        controller.pmon, controller.pmon2 = object(), object()
        controller.gridpower = Power()
        controller.batteryChargeLevel = 50
        controller.powerAtBatteryChargeLevel = None
        controller.powerAtLastHalfHourlyLog = None
        controller.nextHalfHourlyLog = time.time() + 1000
        controller.write_api = None
        controller.state = ControlState.DORMANT
        controller.evseCurrent = 0
        controller.chargerState = EvseState.STANDBY
        controller._ocpp_mode_active = False
        controller.lastTargetCurrent = 0
        controller.evse = Mock()
        controller.evse.get_state.return_value = SimpleNamespace(
            current=0,
            evse_state=EvseState.STANDBY,
            battery_level=50,
            ac_power=0,
            ac_voltage=240,
            ac_current=0,
            dc_voltage=0,
            dc_current=0,
            efficiency=0,
        )
        controller.evse.get_time_until_current_change_allowed.return_value = 0
        controller.getEvseState = Mock(return_value=EvseState.STANDBY)
        controller._build_power_log_message = Mock(return_value=("", {}))
        controller._build_measurements_data = Mock(return_value={})
        controller.calculateTargetCurrent = Mock(return_value=0)

        monkeypatch.setattr(
            evse_module,
            "config",
            _config(SHELLY_GRID_CHANNEL=2, SHELLY_EVSE_CHANNEL=1),
        )
        monkeypatch.setattr(evse_module, "EventBus", Mock())

        controller.update(
            controller.pmon,
            Power(ch1Watts=-500, ch2Watts=900, voltage=238),
        )

        assert controller.current_grid_power.ch1Watts == 900
        assert controller.current_grid_power.ch2Watts == -500
        assert controller.current_grid_power.soc == 50
        # Control saw the same resolved view, not the raw channel order.
        control_power = controller.calculateTargetCurrent.call_args[0][0]
        assert control_power.ch1Watts == 900
        assert control_power.ch2Watts == -500
