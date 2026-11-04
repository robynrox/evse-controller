"""Import and export prices for the daily energy report."""

from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from evse_controller import slot_prices
from evse_controller.energy_sessions import EnergySession, session_store
from evse_controller.slot_prices import SlotPriceBook, price_book
from evse_controller.tariffs.octopus.ioctgo_with_agile_outgoing import (
    IOctGoWithAgileOutgoingTariff,
)
from evse_controller.utils.config import config


DAY = date(2026, 11, 3)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(DAY, time(hour, minute))


def _rate(hour: int, minute: int, rate: float) -> dict:
    start = _at(hour, minute)
    return {"start": start, "end": start + timedelta(minutes=30), "rate": rate}


def _tariff() -> IOctGoWithAgileOutgoingTariff:
    mock_thread = Mock()
    mock_thread.getBatteryChargeLevel = Mock(return_value=75)
    mock_thread.get_state = Mock(return_value={})
    with patch(
        'evse_controller.drivers.evse.wallbox.wallbox_thread.WallboxThread.get_instance',
        return_value=mock_thread,
    ):
        return IOctGoWithAgileOutgoingTariff(state_path=None)


def test_import_price_matches_the_tariff_for_all_48_slots():
    tariff = _tariff()
    for slot in range(48):
        assert price_book.import_price_p(DAY, slot) == pytest.approx(
            tariff._current_import_rate_p(slot * 30)
        )


def test_free_import_session_overrides_the_band():
    session_store.add(
        EnergySession("free_import", _at(12, 0), _at(13, 0))
    )
    assert price_book.import_price_p(DAY, 24) == 0.0
    assert price_book.import_price_p(DAY, 26) == pytest.approx(config.IMPORT_HIGH)


def test_raw_import_band_stays_visible_under_a_free_session():
    session_store.add(
        EnergySession("free_import", _at(12, 0), _at(13, 0))
    )
    assert price_book.raw_import_price_p(DAY, 24) == pytest.approx(
        config.IMPORT_HIGH
    )
    assert price_book.import_price_p(DAY, 24) == 0.0


def test_off_peak_band_matches_the_tariff():
    assert price_book.import_price_p(DAY, 0) == pytest.approx(config.IMPORT_LOW)
    assert price_book.import_price_p(DAY, 47) == pytest.approx(config.IMPORT_LOW)
    assert price_book.import_price_p(DAY, 11) == pytest.approx(config.IMPORT_HIGH)
    assert price_book.import_price_p(DAY, 23) == pytest.approx(config.IMPORT_HIGH)


def test_unknown_export_price_is_none_not_zero():
    assert price_book.export_price_p(DAY, 0) is None


def test_export_price_uses_the_fetched_rate():
    price_book.update_rates(DAY, [_rate(0, 0, 5.1), _rate(0, 30, 8.2)])
    assert price_book.export_price_p(DAY, 0) == pytest.approx(5.1)
    assert price_book.export_price_p(DAY, 1) == pytest.approx(8.2)


def test_rate_cache_round_trips_through_a_file(tmp_path):
    first = SlotPriceBook(tmp_path / "rates")
    first.update_rates(DAY, [_rate(0, 0, 5.1)])

    path = tmp_path / "rates" / f"{DAY.isoformat()}.json"
    assert path.exists()
    cached = json.loads(path.read_text())
    assert cached["date"] == DAY.isoformat()
    assert cached["rates"][0]["rate"] == pytest.approx(5.1)

    second = SlotPriceBook(tmp_path / "rates")
    assert second.export_price_p(DAY, 0) == pytest.approx(5.1)


def test_corrupt_cache_is_ignored(tmp_path):
    path = tmp_path / "rates" / f"{DAY.isoformat()}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")

    book = SlotPriceBook(tmp_path / "rates")
    assert book.export_price_p(DAY, 0) is None


def test_rates_are_fetched_once_per_day(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(region, day, tariff_code=None):
        calls.append(day)
        return [_rate(0, 0, 7.5)]

    monkeypatch.setattr(slot_prices, "fetch_day_rates", fake_fetch)
    book = SlotPriceBook(tmp_path / "rates")

    assert book.export_price_p(DAY, 0) == pytest.approx(7.5)
    assert book.export_price_p(DAY, 0) == pytest.approx(7.5)
    assert calls == [DAY]
