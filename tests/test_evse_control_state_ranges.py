"""Fixed modes must reassert their current range on every call.

A state that has not *changed* used to skip the range assignment entirely.
That left the controller latched at whatever narrower range a previous mode
had set: the SoC-unknown branch enters CHARGE at 3 A, so a later "charge at
max" decision (free-energy session, off-peak) stayed at 3 A while the
dashboard correctly reported the maximum.
"""

import unittest

from evse_controller.drivers.EvseController import EvseController, ControlState
from evse_controller.utils.config import config


class FakePower:
    """Minimal stand-in for the Power object used by calculateTargetCurrent."""

    def __init__(self, home_w: float = 0.0, voltage: float = 240.0):
        self._home_w = home_w
        self.voltage = voltage

    def getHomeWatts(self) -> float:
        return self._home_w


def make_controller() -> EvseController:
    """Build an EvseController without opening any hardware.

    Only the attributes used by setControlState() and
    calculateTargetCurrent() are needed.
    """
    ctl = object.__new__(EvseController)
    ctl.state = ControlState.DORMANT
    ctl.minChargeCurrent = 0
    ctl.maxChargeCurrent = 0
    ctl.minDischargeCurrent = 0
    ctl.maxDischargeCurrent = 0
    ctl.minChargeActivationPower = 0
    ctl.minDischargeActivationPower = 0
    ctl.chargeCurrentBias = 0
    ctl.dischargeCurrentBias = 0
    ctl.use_new_current_calculation = True
    ctl.lastTargetCurrent = 0
    return ctl


class TestControlStateRanges(unittest.TestCase):
    def test_charge_reasserts_max_when_already_charging(self):
        """The production failure: CHARGE latched at 3 A stayed at 3 A."""
        ctl = make_controller()
        ctl.setControlState(ControlState.CHARGE)
        self.assertEqual(ctl.minChargeCurrent, config.WALLBOX_MAX_CHARGE_CURRENT)

        # Simulate the SoC-unknown branch narrowing the range in CHARGE.
        ctl.setChargeCurrentRange(3, 3)
        self.assertEqual(ctl.calculateTargetCurrent(FakePower(0.0)), 3)

        # A repeated CHARGE decision must widen it again, without a
        # transition out of and back into the state.
        ctl.setControlState(ControlState.CHARGE)
        self.assertEqual(ctl.minChargeCurrent, config.WALLBOX_MAX_CHARGE_CURRENT)
        self.assertEqual(ctl.maxChargeCurrent, config.WALLBOX_MAX_CHARGE_CURRENT)
        self.assertEqual(
            ctl.calculateTargetCurrent(FakePower(0.0)),
            config.WALLBOX_MAX_CHARGE_CURRENT,
        )

    def test_charge_reasserts_max_after_a_narrower_load_follow_range(self):
        """A solar charging range must not survive the switch to CHARGE."""
        ctl = make_controller()
        ctl.setControlState(ControlState.LOAD_FOLLOW_BIDIRECTIONAL)
        ctl.setChargeCurrentRange(5, 16)

        ctl.setControlState(ControlState.CHARGE)

        self.assertEqual(ctl.minChargeCurrent, config.WALLBOX_MAX_CHARGE_CURRENT)
        self.assertEqual(
            ctl.calculateTargetCurrent(FakePower(0.0)),
            config.WALLBOX_MAX_CHARGE_CURRENT,
        )

    def test_charge_forces_the_maximum_even_while_exporting(self):
        """With no import and a big export the surplus calc may return 0.

        CHARGE is a fixed mode, so the clamp must lift it to the maximum
        rather than leaving the car idle.
        """
        ctl = make_controller()
        ctl.setControlState(ControlState.CHARGE)

        target = ctl.calculateTargetCurrent(FakePower(-50.0))

        self.assertEqual(target, config.WALLBOX_MAX_CHARGE_CURRENT)

    def test_discharge_reasserts_max_when_already_discharging(self):
        ctl = make_controller()
        ctl.setControlState(ControlState.DISCHARGE)
        ctl.setDischargeCurrentRange(3, 3)
        ctl.setControlState(ControlState.DISCHARGE)

        self.assertEqual(
            ctl.minDischargeCurrent, config.WALLBOX_MAX_DISCHARGE_CURRENT
        )
        self.assertEqual(
            ctl.maxDischargeCurrent, config.WALLBOX_MAX_DISCHARGE_CURRENT
        )

    def test_transition_logging_is_unchanged(self):
        """Repeated calls must not spam the log as if the state changed."""
        ctl = make_controller()
        with self.assertLogs(level="INFO") as captured:
            ctl.setControlState(ControlState.CHARGE)
            ctl.setControlState(ControlState.CHARGE)
        transitions = [
            line for line in captured.output
            if line.rstrip().endswith("Setting control state to ControlState.CHARGE")
        ]
        self.assertEqual(len(transitions), 1)


if __name__ == "__main__":
    unittest.main()
