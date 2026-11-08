"""Go, Cosy and Flux can use the shared tier table without changing control."""

from datetime import date, datetime

import pytest

from evse_controller.drivers.EvseController import ControlState
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.tariffs.octopus.cosy import CosyOctopusTariff
from evse_controller.tariffs.octopus.flux import OctopusFluxTariff
from evse_controller.tariffs.octopus.octgo import OctopusGoTariff
from evse_controller.utils.config import config


DAY = date(2026, 11, 8)


def _state(soc=60):
    state = EvseAsyncState()
    state.battery_level = soc
    return state


@pytest.fixture
def preset(monkeypatch):
    def choose(name):
        tariffs = config._config_data.setdefault("tariffs", {})
        monkeypatch.setitem(tariffs, "preset", name)

    return choose


def _set_price(monkeypatch, side, tier, value):
    prices = config._config_data.setdefault("prices", {}).setdefault(side, {})
    monkeypatch.setitem(prices, tier, value)


def _slot_price(tariff, hour, minute=0):
    slot = hour * 2 + (1 if minute >= 30 else 0)
    return tariff.import_price_p(DAY, slot)


def _export_price(tariff, hour, minute=0):
    slot = hour * 2 + (1 if minute >= 30 else 0)
    return tariff.export_price_p(DAY, slot)


class TestOctopusGoTiers:
    def test_matching_preset_uses_the_table_windows(self, preset):
        preset("octopus_go")
        tariff = OctopusGoTariff()

        assert tariff.is_off_peak(30) is True       # 00:30
        assert tariff.is_off_peak(5 * 60 + 30) is False
        assert tariff.is_expensive_period(17 * 60) is False

    def test_prices_come_from_the_configured_tiers(self, preset, monkeypatch):
        preset("octopus_go")
        _set_price(monkeypatch, "import", "cheap", 7.0)
        _set_price(monkeypatch, "import", "standard", 29.0)
        _set_price(monkeypatch, "export", "standard", 12.0)
        tariff = OctopusGoTariff()

        assert _slot_price(tariff, 0) == pytest.approx(29.0)   # standard
        assert _slot_price(tariff, 0, 30) == pytest.approx(7.0)  # cheap
        assert _export_price(tariff, 12) == pytest.approx(12.0)

    def test_mismatched_preset_keeps_the_built_in_bands(self, preset):
        preset("cosy")
        tariff = OctopusGoTariff()

        assert tariff.resolved_price_table() is None
        assert tariff.is_off_peak(30) is True
        # Legacy Go rates, not Cosy's.
        assert _slot_price(tariff, 0, 30) == pytest.approx(8.50)
        assert _export_price(tariff, 12) == pytest.approx(15.0)


class TestCosyTiers:
    def test_matching_preset_uses_all_cheap_windows(self, preset):
        preset("cosy")
        tariff = CosyOctopusTariff()

        for minute in (4 * 60, 13 * 60, 22 * 60):
            assert tariff.is_off_peak(minute) is True
        assert tariff.is_off_peak(12 * 60) is False
        assert tariff.is_expensive_period(17 * 60) is True

    def test_prices_come_from_the_configured_tiers(self, preset, monkeypatch):
        preset("cosy")
        _set_price(monkeypatch, "import", "cheap", 11.0)
        _set_price(monkeypatch, "import", "standard", 25.0)
        _set_price(monkeypatch, "import", "peak", 35.0)
        tariff = CosyOctopusTariff()

        assert _slot_price(tariff, 4, 30) == pytest.approx(11.0)
        assert _slot_price(tariff, 12) == pytest.approx(25.0)
        assert _slot_price(tariff, 17) == pytest.approx(35.0)

    def test_control_state_still_follows_the_windows(self, preset):
        preset("cosy")
        tariff = CosyOctopusTariff()

        assert tariff.get_control_state(_state(40), 4 * 60 + 30)[0] == ControlState.CHARGE
        assert tariff.get_control_state(_state(40), 17 * 60)[0] == ControlState.LOAD_FOLLOW_DISCHARGE


class TestFluxTiers:
    def test_matching_preset_uses_import_and_export_windows(self, preset):
        preset("flux")
        tariff = OctopusFluxTariff()

        assert tariff.is_off_peak(3 * 60) is True
        assert tariff.is_expensive_period(17 * 60) is True
        assert _slot_price(tariff, 3) == pytest.approx(14.91)
        assert _slot_price(tariff, 17) == pytest.approx(34.79)
        assert _export_price(tariff, 3) == pytest.approx(4.69)
        assert _export_price(tariff, 17) == pytest.approx(26.42)

    def test_custom_window_override_drives_control(self, preset):
        preset("flux")
        config.set_price_windows("import", "cheap", ["01:00-02:00"])
        try:
            tariff = OctopusFluxTariff()
            assert tariff.is_off_peak(1 * 60 + 30) is True
            assert tariff.is_off_peak(3 * 60) is False
        finally:
            config.set_price_windows("import", "cheap", None)


class TestMismatchedPresets:
    @pytest.mark.parametrize(
        "preset_name,tariff_class",
        [
            ("iog_agile", CosyOctopusTariff),
            ("octopus_go", OctopusFluxTariff),
            ("flux", OctopusGoTariff),
        ],
    )
    def test_other_products_keep_their_own_control(
        self, preset, preset_name, tariff_class
    ):
        preset(preset_name)
        tariff = tariff_class()

        assert tariff.resolved_price_table() is None
        assert isinstance(tariff.get_control_state(_state(60), 12 * 60)[0], ControlState)
