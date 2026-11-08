"""The daily report prices every slot through the normalised tariff source.

Phase 1 of the smart-tariff migration replaces ``slot_prices``' private band
logic in the report with the same 48-row contract the planner will consume.
These tests pin that equivalence for every current tariff, for free-import
sessions, for unknown Agile export prices, and for the fallback used while no
tariff is running.
"""

from datetime import date, datetime, time, timedelta
from unittest.mock import Mock, patch

import pytest

from evse_controller.daily_report import DailyEnergyLog, SeriesSpec
from evse_controller.energy_sessions import (
    FREE_IMPORT,
    EnergySession,
    session_store,
)
from evse_controller.slot_prices import price_book
from evse_controller.tariffs.octopus.cosy import CosyOctopusTariff
from evse_controller.tariffs.octopus.flux import OctopusFluxTariff
from evse_controller.tariffs.octopus.ioctgo import IntelligentOctopusGoTariff
from evse_controller.tariffs.octopus.ioctgo_with_agile_outgoing import (
    IOctGoWithAgileOutgoingTariff,
)
from evse_controller.tariffs.octopus.octgo import OctopusGoTariff
from evse_controller.tariffs.source import (
    SLOTS_PER_DAY,
    SlotPriceBookSource,
    SlotRate,
    TariffSourceAdapter,
    slot_start,
)
from evse_controller.utils.config import config


DAY = date(2026, 11, 3)
GRID = SeriesSpec("grid", "Grid", "Imported", "Exported")

STATIC_TARIFFS = [
    OctopusGoTariff,
    CosyOctopusTariff,
    OctopusFluxTariff,
    IntelligentOctopusGoTariff,
]


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute))


def _rate(slot: int, rate: float) -> dict:
    start = datetime.combine(DAY, time()) + timedelta(minutes=30 * slot)
    return {
        "start": start,
        "end": start + timedelta(minutes=30),
        "rate": rate,
    }


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


def _build_agile():
    """An Agile-Outgoing tariff with a fixed, offline rate series."""
    mock_thread = Mock()
    mock_thread.getBatteryChargeLevel = Mock(return_value=75)
    mock_thread.get_state = Mock(return_value={})

    with patch(
        'evse_controller.drivers.evse.wallbox.wallbox_thread.WallboxThread.get_instance',
        return_value=mock_thread,
    ), patch.object(IOctGoWithAgileOutgoingTariff, "_fetch_rates_async"):
        tariff = IOctGoWithAgileOutgoingTariff()

    midnight = datetime.combine(DAY, time())
    tariff.agile_rates = [
        {
            "start": midnight + timedelta(minutes=30 * slot),
            "end": midnight + timedelta(minutes=30 * (slot + 1)),
            "rate": rate,
        }
        for slot, rate in ((0, 7.5), (20, 22.0), (47, 9.0))
    ]
    return tariff


def _log(tmp_path) -> DailyEnergyLog:
    log = DailyEnergyLog()
    log.set_directory(tmp_path / "reports")
    log.configure([GRID])
    return log


def _price_all(log: DailyEnergyLog) -> list[dict]:
    """Price every slot without closing it, so no file writes are needed."""
    log._ensure_day(DAY)
    prices = []
    for slot in log._slots:
        log._price_slot(slot)
        prices.append(slot["prices"])
    return prices


class _MutableSource:
    """A tiny source whose prices can be changed between slot closes."""

    name = "mutable"
    dynamic_import = True
    dynamic_export = True

    def __init__(self, import_p=10.0, export_p=5.0):
        self.import_p = import_p
        self.export_p = export_p

    def rates_for_day(self, day):
        return [
            SlotRate(
                start=slot_start(day, slot),
                end=slot_start(day, slot) + timedelta(minutes=30),
                import_p=self.import_p,
                export_p=self.export_p,
            )
            for slot in range(SLOTS_PER_DAY)
        ]

    def sessions_for_day(self, day):
        return []


class _NullExportSource(_MutableSource):
    """A source that knows import but not export, like a failed Agile fetch."""

    def __init__(self, import_p=10.0):
        super().__init__(import_p=import_p, export_p=None)


@pytest.mark.parametrize("tariff_class", STATIC_TARIFFS)
def test_report_prices_match_each_tariff_contract(tariff_class, tmp_path):
    source = TariffSourceAdapter(_build(tariff_class))
    log = _log(tmp_path)
    log.set_price_source(source)

    prices = _price_all(log)
    rates = source.rates_for_day(DAY)

    assert len(prices) == SLOTS_PER_DAY
    for slot, rate in enumerate(rates):
        assert prices[slot]["import_p"] == pytest.approx(rate.import_p)
        assert prices[slot]["export_p"] == pytest.approx(rate.export_p)
        assert prices[slot]["session"] is None


def test_report_prefers_the_active_source_over_the_price_book(tmp_path):
    # The fallback book would value every export at 99p; the Cosy source must
    # win, otherwise the report is not reading the contract.
    price_book.update_rates(DAY, [_rate(0, 99.0)])
    source = TariffSourceAdapter(_build(CosyOctopusTariff))
    log = _log(tmp_path)
    log.set_price_source(source)

    prices = _price_all(log)
    rate = source.rates_for_day(DAY)[0]

    assert prices[0]["export_p"] == pytest.approx(rate.export_p)
    assert prices[0]["export_p"] != pytest.approx(99.0)


def test_report_agile_export_comes_from_the_tariff_series(tmp_path):
    source = TariffSourceAdapter(_build_agile())
    log = _log(tmp_path)
    log.set_price_source(source)

    prices = _price_all(log)

    assert prices[0]["import_p"] == pytest.approx(config.IMPORT_LOW)
    assert prices[11]["import_p"] == pytest.approx(config.IMPORT_HIGH)
    assert prices[0]["export_p"] == pytest.approx(7.5)
    assert prices[20]["export_p"] == pytest.approx(22.0)
    assert prices[47]["export_p"] == pytest.approx(9.0)
    assert prices[1]["export_p"] is None


def test_report_unknown_agile_export_is_unknown_not_zero(tmp_path):
    tariff = _build_agile()
    tariff.agile_rates = []
    log = _log(tmp_path)
    log.set_price_source(TariffSourceAdapter(tariff))

    prices = _price_all(log)

    assert all(price["export_p"] is None for price in prices)


def test_report_fills_a_missing_source_price_from_the_price_book(tmp_path):
    # A dynamic tariff can lose yesterday's series at midnight.  The cached
    # book still knows the price, and must fill only the missing half.
    price_book.update_rates(DAY, [_rate(0, 5.0), _rate(1, 6.0)])
    source = _NullExportSource(import_p=20.0)
    log = _log(tmp_path)
    log.set_price_source(source)

    prices = _price_all(log)

    assert prices[0]["import_p"] == pytest.approx(20.0)
    assert prices[0]["export_p"] == pytest.approx(5.0)
    assert prices[1]["export_p"] == pytest.approx(6.0)
    assert prices[2]["export_p"] is None


def test_report_applies_a_free_import_session_to_the_raw_price(tmp_path):
    session_store.add(
        EnergySession(FREE_IMPORT, _at(12, 0), _at(13, 0))
    )
    source = TariffSourceAdapter(_build(CosyOctopusTariff))
    log = _log(tmp_path)
    log.set_price_source(source)

    prices = _price_all(log)
    raw = source.rates_for_day(DAY)

    assert raw[24].import_p != 0.0
    assert prices[24]["import_p"] == 0.0
    assert prices[24]["session"] == FREE_IMPORT
    assert prices[25]["import_p"] == 0.0
    assert prices[26]["import_p"] == pytest.approx(raw[26].import_p)
    assert prices[26]["session"] is None


def test_report_falls_back_to_the_price_book_without_a_tariff(tmp_path):
    price_book.update_rates(DAY, [_rate(0, 5.0)])
    log = _log(tmp_path)

    prices = _price_all(log)
    fallback = SlotPriceBookSource(price_book).rates_for_day(DAY)

    for slot, rate in enumerate(fallback):
        assert prices[slot]["import_p"] == pytest.approx(rate.import_p)
        assert prices[slot]["export_p"] == rate.export_p
    assert prices[0]["export_p"] == pytest.approx(5.0)
    assert prices[1]["export_p"] is None


def test_closed_prices_stay_frozen_when_the_source_changes(tmp_path):
    source = _MutableSource(import_p=10.0, export_p=5.0)
    log = _log(tmp_path)
    log.set_price_source(source)

    log.observe(_at(0, 0), {"grid": 1000.0})
    log.observe(_at(0, 30), {"grid": 1000.0})
    closed = log.snapshot_for(DAY)["slots"][0]

    source.import_p = 40.0
    source.export_p = 99.0

    assert closed["prices"]["import_p"] == pytest.approx(10.0)
    assert closed["prices"]["export_p"] == pytest.approx(5.0)
    assert log.snapshot_for(DAY)["slots"][0]["prices"] == closed["prices"]


def _flag_slot(log: DailyEnergyLog, slot: int, flag: str) -> None:
    log._ensure_day(DAY)
    log._slots[slot].setdefault("flags", []).append(flag)


def test_ocpp_slot_is_booked_at_the_octopus_off_peak_rate(tmp_path):
    source = TariffSourceAdapter(_build(IntelligentOctopusGoTariff))
    log = _log(tmp_path)
    log.set_price_source(source)
    _flag_slot(log, 24, "ocpp")  # 12:00, outside the overnight cheap band

    prices = _price_all(log)

    assert prices[24]["import_p"] == pytest.approx(config.IMPORT_LOW)


def test_free_import_still_wins_over_the_ocpp_rate(tmp_path):
    session_store.add(
        EnergySession(FREE_IMPORT, _at(12, 0), _at(13, 0))
    )
    source = TariffSourceAdapter(_build(IntelligentOctopusGoTariff))
    log = _log(tmp_path)
    log.set_price_source(source)
    _flag_slot(log, 24, "ocpp")

    prices = _price_all(log)

    assert prices[24]["import_p"] == 0.0
    assert prices[24]["session"] == FREE_IMPORT


def test_ocpp_flag_does_not_rewrite_a_tariff_without_the_rule(tmp_path):
    source = TariffSourceAdapter(_build(CosyOctopusTariff))
    log = _log(tmp_path)
    log.set_price_source(source)
    _flag_slot(log, 24, "ocpp")

    prices = _price_all(log)

    assert prices[24]["import_p"] == pytest.approx(
        source.rates_for_day(DAY)[24].import_p
    )


def test_price_book_has_no_ocpp_rule(tmp_path):
    source = SlotPriceBookSource()

    assert source.ocpp_import_price_p(DAY, 24) is None
