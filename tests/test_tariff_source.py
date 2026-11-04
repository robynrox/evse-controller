"""Golden tests for the normalised tariff source.

The point of this module is to pin today's per-slot prices *before* any tariff
class is replaced.  The adapter is only proven when it reproduces every
existing tariff's own rate methods, so a later config-driven source can be
compared against the same fixtures.
"""

from datetime import date, datetime, time, timedelta
from unittest.mock import Mock, patch

import pytest

from evse_controller.energy_sessions import (
    FREE_IMPORT,
    EnergySession,
    SessionStore,
)
from evse_controller.tariffs.base import Tariff
from evse_controller.tariffs.octopus.cosy import CosyOctopusTariff
from evse_controller.tariffs.octopus.flux import OctopusFluxTariff
from evse_controller.tariffs.octopus.ioctgo import IntelligentOctopusGoTariff
from evse_controller.tariffs.octopus.ioctgo_with_agile_outgoing import (
    IOctGoWithAgileOutgoingTariff,
)
from evse_controller.tariffs.octopus.octgo import OctopusGoTariff
from evse_controller.tariffs.source import (
    SLOTS_PER_DAY,
    TariffSourceAdapter,
    slot_start,
)
from evse_controller.utils.config import config


DAY = date(2026, 1, 15)
SPRING_FORWARD = date(2026, 3, 29)
AUTUMN_BACK = date(2026, 10, 25)

STATIC_TARIFFS = [
    OctopusGoTariff,
    CosyOctopusTariff,
    OctopusFluxTariff,
    IntelligentOctopusGoTariff,
]


def _build(tariff_class):
    """Construct a tariff with the Wallbox thread mocked out."""
    mock_thread = Mock()
    mock_thread.getBatteryChargeLevel = Mock(return_value=75)
    mock_thread.get_state = Mock(return_value={})

    with patch(
        'evse_controller.drivers.evse.wallbox.wallbox_thread.WallboxThread.get_instance',
        return_value=mock_thread,
    ):
        return tariff_class()


def _build_agile(day=DAY):
    """An Agile-Outgoing tariff with a fixed, offline rate series."""
    mock_thread = Mock()
    mock_thread.getBatteryChargeLevel = Mock(return_value=75)
    mock_thread.get_state = Mock(return_value={})

    with patch(
        'evse_controller.drivers.evse.wallbox.wallbox_thread.WallboxThread.get_instance',
        return_value=mock_thread,
    ), patch.object(IOctGoWithAgileOutgoingTariff, "_fetch_rates_async"):
        tariff = IOctGoWithAgileOutgoingTariff()

    midnight = datetime.combine(day, time())
    tariff.agile_rates = [
        {
            "start": midnight + timedelta(minutes=30 * slot),
            "end": midnight + timedelta(minutes=30 * (slot + 1)),
            "rate": rate,
        }
        for slot, rate in ((0, 7.5), (20, 22.0), (47, 9.0))
    ]
    return tariff


def _prices(tariff, day=DAY):
    return TariffSourceAdapter(tariff).rates_for_day(day)


@pytest.mark.parametrize("tariff_class", STATIC_TARIFFS)
def test_static_tariffs_match_their_own_rate_methods(tariff_class):
    tariff = _build(tariff_class)
    rates = _prices(tariff)

    assert len(rates) == SLOTS_PER_DAY
    for slot, rate in enumerate(rates):
        start = slot_start(DAY, slot)
        assert rate.start == start
        assert rate.end == start + timedelta(minutes=30)
        assert rate.import_p == pytest.approx(
            tariff.get_import_rate(start) * 100.0
        )
        assert rate.export_p == pytest.approx(
            tariff.get_export_rate(start) * 100.0
        )


@pytest.mark.parametrize("day", [DAY, SPRING_FORWARD, AUTUMN_BACK])
def test_day_always_has_48_contiguous_local_slots(day):
    """Clock-change days keep 48 local rows; no gap or backwards step."""
    rates = _prices(CosyOctopusTariff(), day)

    assert len(rates) == SLOTS_PER_DAY
    for previous, current in zip(rates, rates[1:]):
        assert current.start == previous.end


def test_octgo_boundaries_are_pinned():
    rates = _prices(OctopusGoTariff())

    assert rates[0].import_p == pytest.approx(31.42)   # 00:00 standard
    assert rates[1].import_p == pytest.approx(8.50)    # 00:30 cheap
    assert rates[10].import_p == pytest.approx(8.50)   # 05:00 cheap
    assert rates[11].import_p == pytest.approx(31.42)  # 05:30 standard
    assert rates[47].import_p == pytest.approx(31.42)  # 23:30 standard
    assert all(rate.export_p == pytest.approx(15.0) for rate in rates)


def test_cosy_boundaries_are_pinned():
    rates = _prices(CosyOctopusTariff())

    assert rates[0].import_p == pytest.approx(26.22)   # 00:00 medium
    assert rates[8].import_p == pytest.approx(12.86)   # 04:00 cheap
    assert rates[32].import_p == pytest.approx(39.32)  # 16:00 peak
    assert rates[44].import_p == pytest.approx(12.86)  # 22:00 cheap


def test_flux_boundaries_are_pinned():
    rates = _prices(OctopusFluxTariff())

    assert rates[4].import_p == pytest.approx(14.91)   # 02:00 night
    assert rates[4].export_p == pytest.approx(4.69)
    assert rates[10].import_p == pytest.approx(24.85)  # 05:00 day
    assert rates[32].import_p == pytest.approx(34.79)  # 16:00 peak
    assert rates[32].export_p == pytest.approx(26.42)


def test_ioctgo_boundaries_are_pinned():
    rates = _prices(IntelligentOctopusGoTariff())

    assert rates[0].import_p == pytest.approx(config.IMPORT_LOW)   # 00:00
    assert rates[11].import_p == pytest.approx(config.IMPORT_HIGH)  # 05:30
    assert rates[47].import_p == pytest.approx(config.IMPORT_LOW)   # 23:30
    assert all(
        rate.export_p == pytest.approx(config.EXPORT_MID) for rate in rates
    )


def test_ioctgo_agile_import_comes_from_the_fixed_band():
    rates = _prices(_build_agile())

    assert rates[0].import_p == pytest.approx(config.IMPORT_LOW)
    assert rates[11].import_p == pytest.approx(config.IMPORT_HIGH)


def test_ioctgo_agile_export_comes_from_the_fetched_series():
    rates = _prices(_build_agile())

    assert rates[0].export_p == pytest.approx(7.5)
    assert rates[20].export_p == pytest.approx(22.0)
    assert rates[47].export_p == pytest.approx(9.0)
    assert rates[1].export_p is None


def test_ioctgo_agile_unknown_export_is_none_not_zero():
    tariff = _build_agile()
    tariff.agile_rates = []

    rates = _prices(tariff)

    assert all(rate.export_p is None for rate in rates)


def test_capability_flags_describe_the_price_series():
    assert TariffSourceAdapter(OctopusGoTariff()).dynamic_import is False
    assert TariffSourceAdapter(OctopusGoTariff()).dynamic_export is False
    assert TariffSourceAdapter(_build_agile()).dynamic_import is False
    assert TariffSourceAdapter(_build_agile()).dynamic_export is True


def test_missing_rate_is_none_not_zero():
    class NoPeriods(Tariff):
        def __init__(self):
            super().__init__()
            self.time_of_use = {}

    tariff = NoPeriods()

    assert tariff.import_price_p(DAY, 0) is None
    assert tariff.export_price_p(DAY, 0) is None


def test_sessions_for_day_returns_overlapping_enabled_windows(tmp_path):
    store = SessionStore(tmp_path / "sessions.json")
    midnight = datetime.combine(DAY, time())
    session = store.add(
        EnergySession(
            FREE_IMPORT,
            midnight + timedelta(hours=12),
            midnight + timedelta(hours=13),
        )
    )
    other_day = store.add(
        EnergySession(
            FREE_IMPORT,
            midnight + timedelta(days=1, hours=12),
            midnight + timedelta(days=1, hours=13),
        )
    )
    store.set_enabled(other_day.id, False)

    source = TariffSourceAdapter(CosyOctopusTariff(), session_store=store)
    sessions = source.sessions_for_day(DAY)

    assert [window.id for window in sessions] == [session.id]
    assert source.sessions_for_day(DAY + timedelta(days=2)) == []


def test_adapter_exposes_the_wrapped_tariff():
    tariff = CosyOctopusTariff()
    source = TariffSourceAdapter(tariff)

    assert source.tariff is tariff
    assert source.name == "CosyOctopusTariff"
