"""The persistent per-day, per-half-hour energy log.

The log integrates instantaneous power and never reads the Shelly cumulative
counters: a power-cycle for Wi-Fi recovery would otherwise book a large
negative slot.  Anything not measured is ``None`` rather than zero.
"""

from datetime import date, datetime, time, timedelta
from pathlib import Path
import json

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from evse_controller.daily_report import (
    DailyEnergyLog,
    PROFILE_BIN_WIDTH_W,
    SeriesSpec,
    _is_clock_change,
    configured_series,
    to_csv,
)
from evse_controller.energy_sessions import EnergySession, session_store
from evse_controller.slot_prices import price_book
from evse_controller.utils.config import config


DAY = date(2026, 11, 3)
GRID = SeriesSpec("grid", "Grid", "Imported", "Exported")
EVSE = SeriesSpec("evse", "EVSE", "Charged", "Discharged")


def _at(day: date, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second))


def _log(tmp_path: Path, *specs: SeriesSpec) -> DailyEnergyLog:
    log = DailyEnergyLog()
    log.set_directory(tmp_path / "reports")
    log.set_profile_directory(tmp_path / "load_profile")
    log.configure(specs or (GRID, EVSE))
    return log


def _rate(day: date, slot: int, rate: float) -> dict:
    start = _at(day, slot // 2, (slot % 2) * 30)
    return {
        "start": start,
        "end": start + timedelta(minutes=30),
        "rate": rate,
    }


def _close_first_slot(log: DailyEnergyLog, watts: float = 1000.0) -> None:
    """Accrue a little of slot 0 and cross its boundary to close it."""
    log.observe(_at(DAY, 0, 0), {"grid": watts})
    log.observe(_at(DAY, 0, 30), {"grid": watts})


class TestAccumulation:
    def test_gross_import_and_export_are_kept_separate(self, tmp_path):
        log = _log(tmp_path, GRID)
        log.observe(_at(DAY, 0, 0), {"grid": 0.0})
        log.observe(_at(DAY, 0, 1), {"grid": 2000.0})
        log.observe(_at(DAY, 0, 2), {"grid": -1000.0})

        slot = log.snapshot_for(DAY)["slots"][0]
        assert slot["energy"]["grid"]["in"] == pytest.approx(2000 * 60 / 3_600_000)
        assert slot["energy"]["grid"]["out"] == pytest.approx(1000 * 60 / 3_600_000)
        assert slot["coverage"]["grid"] == pytest.approx(120.0)

    def test_missing_series_is_null_not_zero(self, tmp_path):
        log = _log(tmp_path, GRID, EVSE)
        log.observe(_at(DAY, 0, 0), {"grid": 1000.0, "evse": None})
        log.observe(_at(DAY, 0, 1), {"grid": 1000.0, "evse": None})

        slot = log.snapshot_for(DAY)["slots"][0]
        assert slot["energy"]["grid"]["in"] == pytest.approx(1000 / 60 / 1000)
        assert slot["energy"]["evse"] == {"in": None, "out": None}
        assert slot["coverage"]["evse"] == 0.0

    def test_measured_zero_is_not_null(self, tmp_path):
        log = _log(tmp_path, GRID)
        log.observe(_at(DAY, 0, 0), {"grid": 0.0})
        log.observe(_at(DAY, 0, 1), {"grid": 0.0})

        assert log.snapshot_for(DAY)["slots"][0]["energy"]["grid"] == {
            "in": 0.0,
            "out": 0.0,
        }

    def test_interval_is_split_at_a_slot_boundary(self, tmp_path):
        log = _log(tmp_path, GRID)
        log.observe(_at(DAY, 0, 0), {"grid": 3600.0})
        for minute in (5, 10, 15, 20, 25):
            log.observe(_at(DAY, 0, minute), {"grid": 3600.0})
        log.observe(_at(DAY, 0, 30), {"grid": 0.0})
        log.observe(_at(DAY, 0, 31), {"grid": 0.0})

        snapshot = log.snapshot_for(DAY)
        first, second = snapshot["slots"][0], snapshot["slots"][1]
        # Five completed five-minute intervals, then the previous reading
        # holds up to the boundary.  Nothing flows in the new slot.
        assert first["energy"]["grid"]["in"] == pytest.approx(1.8)
        assert first["closed"] is True
        assert second["energy"]["grid"]["in"] == pytest.approx(0.0)
        assert second["closed"] is False

    def test_long_gap_is_capped_and_flagged(self, tmp_path):
        log = _log(tmp_path, GRID)
        log.observe(_at(DAY, 0, 0), {"grid": 1000.0})
        log.observe(_at(DAY, 0, 5), {"grid": 1000.0})
        log.observe(_at(DAY, 0, 16), {"grid": 1000.0})

        snapshot = log.snapshot_for(DAY)
        first = snapshot["slots"][0]
        assert first["coverage"]["grid"] == pytest.approx(600.0)
        assert first["energy"]["grid"]["in"] == pytest.approx(1000 * 600 / 3_600_000)
        assert "gap" in snapshot["slots"][0]["flags"] or "gap" in snapshot["slots"][1]["flags"]

    def test_clock_going_backwards_resets_the_baseline(self, tmp_path):
        log = _log(tmp_path, GRID)
        log.observe(_at(DAY, 0, 30), {"grid": 1000.0})
        log.observe(_at(DAY, 0, 20), {"grid": -1000.0})
        log.observe(_at(DAY, 0, 21), {"grid": -1000.0})

        snapshot = log.snapshot_for(DAY)
        slot = snapshot["slots"][0]
        # The backward jump is not booked.  Only the following minute counts,
        # rather than the ten minutes between 00:20 and 00:30.
        assert slot["energy"]["grid"]["in"] == 0.0
        assert slot["energy"]["grid"]["out"] == pytest.approx(1000 * 60 / 3_600_000)
        assert "clock_backwards" in slot["flags"]


class TestLifecycle:
    def test_restart_resumes_without_bridging_the_gap(self, tmp_path):
        first = _log(tmp_path, GRID)
        first.observe(_at(DAY, 0, 0), {"grid": 2000.0})
        first.observe(_at(DAY, 0, 5), {"grid": 2000.0})
        before = first.snapshot_for(DAY)["slots"][0]["energy"]["grid"]["in"]
        assert before == pytest.approx(2000 * 300 / 3_600_000)

        second = _log(tmp_path, GRID)
        second.observe(_at(DAY, 1, 0), {"grid": 2000.0})
        resumed = second.snapshot_for(DAY)
        assert resumed["slots"][0]["energy"]["grid"]["in"] == pytest.approx(before)

        second.observe(_at(DAY, 1, 1), {"grid": 2000.0})
        assert second.snapshot_for(DAY)["slots"][2]["energy"]["grid"]["in"] == pytest.approx(
            2000 * 60 / 3_600_000
        )

    def test_midnight_starts_a_new_file(self, tmp_path):
        log = _log(tmp_path, GRID)
        start = _at(DAY, 23, 59, 0)
        log.observe(start, {"grid": 3600.0})
        log.observe(start + timedelta(seconds=30), {"grid": 3600.0})
        log.observe(start + timedelta(seconds=90), {"grid": 3600.0})

        yesterday = log.snapshot_for(DAY)
        today = log.snapshot_for(DAY + timedelta(days=1))
        assert yesterday["slots"][47]["closed"] is True
        assert yesterday["slots"][47]["energy"]["grid"]["in"] == pytest.approx(
            3600 * 30 / 3_600_000
        )
        assert today["slots"][0]["energy"]["grid"]["in"] == pytest.approx(
            3600 * 30 / 3_600_000
        )
        assert (tmp_path / "reports" / f"{DAY.isoformat()}.json").exists()
        assert (
            tmp_path / "reports" / f"{(DAY + timedelta(days=1)).isoformat()}.json"
        ).exists()

    def test_corrupt_day_file_is_ignored(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir(parents=True)
        path = reports / f"{DAY.isoformat()}.json"
        path.write_text("{not json")

        log = _log(tmp_path, GRID)
        snapshot = log.snapshot_for(DAY)
        assert len(snapshot["slots"]) == 48
        assert snapshot["slots"][0]["energy"]["grid"]["in"] is None


class TestPricesAndMoney:
    def test_prices_are_frozen_when_the_slot_closes(self, tmp_path):
        price_book.update_rates(DAY, [_rate(DAY, 0, 5.0)])
        log = _log(tmp_path, GRID)
        _close_first_slot(log)

        slot = log.snapshot_for(DAY)["slots"][0]
        assert slot["prices"]["export_p"] == pytest.approx(5.0)
        assert slot["prices"]["import_p"] == pytest.approx(config.IMPORT_LOW)
        assert slot["money"]["import_cost_gbp"] == pytest.approx(
            slot["energy"]["grid"]["in"] * config.IMPORT_LOW / 100.0
        )

        price_book.update_rates(DAY, [_rate(DAY, 0, 99.0)])
        assert log.snapshot_for(DAY)["slots"][0]["prices"]["export_p"] == pytest.approx(
            5.0
        )

    def test_unknown_export_price_leaves_net_cost_null(self, tmp_path):
        log = _log(tmp_path, GRID)
        _close_first_slot(log)

        slot = log.snapshot_for(DAY)["slots"][0]
        assert slot["prices"]["export_p"] is None
        assert slot["money"]["import_cost_gbp"] is not None
        assert slot["money"]["export_revenue_gbp"] is None
        assert slot["money"]["net_cost_gbp"] is None
        assert log.snapshot_for(DAY)["totals"]["priced_slots"] == 0

    def test_free_import_session_has_a_zero_import_price(self, tmp_path):
        session_store.add(
            EnergySession(
                "free_import",
                _at(DAY, 0, 0),
                _at(DAY, 1, 0),
            )
        )
        price_book.update_rates(DAY, [_rate(DAY, 0, 5.0)])
        log = _log(tmp_path, GRID)
        _close_first_slot(log)

        slot = log.snapshot_for(DAY)["slots"][0]
        assert slot["prices"]["session"] == "free_import"
        assert slot["prices"]["import_p"] == 0.0
        assert slot["money"]["import_cost_gbp"] == pytest.approx(0.0)
        assert slot["money"]["net_cost_gbp"] == pytest.approx(
            -slot["money"]["export_revenue_gbp"]
        )

    def test_saving_is_a_separate_attribution(self, tmp_path):
        price_book.update_rates(DAY, [_rate(DAY, 0, 5.0)])
        log = _log(tmp_path, GRID)
        log.set_savings_provider(lambda day, slot: 0.021)
        _close_first_slot(log)

        snapshot = log.snapshot_for(DAY)
        slot = snapshot["slots"][0]
        assert slot["saved_gbp"] == pytest.approx(0.021)
        assert snapshot["totals"]["saved_gbp"] == pytest.approx(0.021)
        # The saving never changes the net cost.
        assert slot["money"]["net_cost_gbp"] == pytest.approx(
            slot["money"]["import_cost_gbp"] - slot["money"]["export_revenue_gbp"]
        )

    def test_saving_is_null_when_no_tariff_is_active(self, tmp_path):
        log = _log(tmp_path, GRID)
        _close_first_slot(log)

        snapshot = log.snapshot_for(DAY)
        assert snapshot["slots"][0]["saved_gbp"] is None
        assert snapshot["totals"]["saved_gbp"] is None


class TestControllerFeed:
    """The controller hands every meter to the process-wide log."""

    @staticmethod
    def _controller():
        from evse_controller.drivers.EvseController import EvseController

        controller = object.__new__(EvseController)
        controller.pmon, controller.pmon2 = object(), object()
        return controller

    def test_grid_evse_and_battery_dc_are_sampled(
        self, monkeypatch, isolated_daily_report
    ):
        import time as time_module
        from types import SimpleNamespace
        from unittest.mock import Mock

        import evse_controller.drivers.EvseController as evse_module
        import evse_controller.daily_report as report_module
        from evse_controller.drivers.Power import Power

        specs = [GRID, EVSE, SeriesSpec("battery_dc", "EV battery")]
        monkeypatch.setattr(report_module, "configured_series", lambda: specs)
        monkeypatch.setattr(
            evse_module,
            "config",
            SimpleNamespace(
                SHELLY_GRID_DEVICE="primary",
                SHELLY_GRID_CHANNEL=1,
                SHELLY_EVSE_DEVICE="",
                SHELLY_EVSE_CHANNEL=None,
            ),
        )

        controller = self._controller()
        controller.evse = Mock()
        state = SimpleNamespace(
            battery_level=50,
            ac_power=1500.0,
            dc_voltage=400.0,
            dc_current=4.0,
            last_update=time_module.time(),
        )
        controller.evse.get_state.return_value = state

        clock = [datetime(DAY.year, DAY.month, DAY.day, 0, 0, 0)]

        class FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        monkeypatch.setattr(evse_module, "datetime", FakeDateTime)

        controller._feed_daily_report(
            controller.pmon, Power(ch1Watts=1000, ch2Watts=0, voltage=240)
        )
        clock[0] = clock[0] + timedelta(seconds=60)
        controller._feed_daily_report(
            controller.pmon, Power(ch1Watts=1000, ch2Watts=0, voltage=240)
        )

        slot = isolated_daily_report.snapshot_for(DAY)["slots"][0]
        assert slot["energy"]["grid"]["in"] == pytest.approx(
            1000 * 60 / 3_600_000
        )
        assert slot["energy"]["evse"]["in"] == pytest.approx(
            1500 * 60 / 3_600_000
        )
        assert slot["energy"]["battery_dc"]["in"] == pytest.approx(
            1600 * 60 / 3_600_000
        )
        assert slot["soc"] == {"start": 50, "end": 50}

    def test_a_stale_device_is_null_not_last_reading(
        self, monkeypatch, isolated_daily_report
    ):
        import time as time_module
        from types import SimpleNamespace
        from unittest.mock import Mock

        import evse_controller.drivers.EvseController as evse_module
        import evse_controller.daily_report as report_module
        from evse_controller.drivers.Power import Power

        specs = [GRID, EVSE]
        monkeypatch.setattr(report_module, "configured_series", lambda: specs)
        monkeypatch.setattr(
            evse_module,
            "config",
            SimpleNamespace(
                SHELLY_GRID_DEVICE="primary",
                SHELLY_GRID_CHANNEL=1,
                SHELLY_EVSE_DEVICE="",
                SHELLY_EVSE_CHANNEL=None,
            ),
        )
        controller = self._controller()
        controller.evse = Mock()
        controller.evse.get_state.return_value = SimpleNamespace(
            battery_level=50,
            ac_power=0.0,
            dc_voltage=0.0,
            dc_current=0.0,
            last_update=0.0,
        )

        clock = [datetime(DAY.year, DAY.month, DAY.day, 0, 0, 0)]

        class FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        monkeypatch.setattr(evse_module, "datetime", FakeDateTime)
        controller._feed_daily_report(
            controller.pmon, Power(ch1Watts=1000, voltage=240)
        )
        controller._report_device_seen["primary"] = (
            time_module.monotonic() - 100
        )
        clock[0] = clock[0] + timedelta(seconds=60)
        controller._feed_daily_report(
            controller.pmon2, Power(ch1Watts=0, voltage=240)
        )

        slot = isolated_daily_report.snapshot_for(DAY)["slots"][0]
        assert slot["energy"]["grid"]["in"] is None
        assert slot["energy"]["evse"]["in"] is None


class TestPresentation:
    def test_csv_matches_the_table(self, tmp_path):
        price_book.update_rates(DAY, [_rate(DAY, 0, 5.0)])
        log = _log(tmp_path, GRID)
        _close_first_slot(log)

        csv_text = to_csv(log.snapshot_for(DAY))
        lines = csv_text.splitlines()
        assert "Grid Imported (kWh)" in lines[0]
        assert "Net cost (GBP)" in lines[0]
        assert lines[1].startswith("0,")
        assert "—" not in csv_text  # empty cells are blank, not an em dash

    def test_template_renders_every_series_and_the_totals(self, tmp_path):
        price_book.update_rates(DAY, [_rate(DAY, 0, 5.0)])
        log = _log(tmp_path, GRID, EVSE)
        _close_first_slot(log)

        template_dir = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "evse_controller"
            / "templates"
        )
        env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html"]),
        )
        env.globals["url_for"] = lambda endpoint, **kwargs: "#"
        env.globals["get_flashed_messages"] = lambda **kwargs: []
        template = env.get_template("daily_report.html")

        html = template.render(
            day=log.snapshot_for(DAY),
            selected_date=DAY,
            is_today=True,
            available_days=[DAY],
            fmt_energy=lambda value: "—" if value is None else f"{value:.3f}",
            fmt_price=lambda value: "—" if value is None else f"{value:.1f}",
            fmt_pence=lambda value: "—" if value is None else f"{value * 100:.1f}p",
            fmt_gbp=lambda value: "—" if value is None else f"£{value:.2f}",
            fmt_soc=lambda value: "—" if value is None else f"{value}%",
            slot_label=lambda slot: f"{slot // 2:02d}:{(slot % 2) * 30:02d}",
        )

        assert "Half-hour energy report" in html
        assert "Grid" in html
        assert "Imported" in html
        assert "EVSE" in html
        assert "Controller saving" in html
        assert "00:00" in html
        assert "23:30" in html


class TestConfiguration:
    def test_series_follow_the_configured_roles(self, monkeypatch):
        monkeypatch.setattr(config, "SHELLY_GRID_DEVICE", "primary")
        monkeypatch.setattr(config, "SHELLY_GRID_CHANNEL", 1)
        monkeypatch.setattr(config, "SHELLY_EVSE_DEVICE", "primary")
        monkeypatch.setattr(config, "SHELLY_EVSE_CHANNEL", 2)
        monkeypatch.setattr(
            config, "is_channel_in_use", lambda device, channel: True
        )
        monkeypatch.setattr(
            config,
            "get_channel_name",
            lambda device, channel: f"{device}-{channel}",
        )

        keys = [spec.key for spec in configured_series()]
        assert keys == [
            "grid",
            "evse",
            "battery_dc",
            "shelly.secondary.channel1",
            "shelly.secondary.channel2",
        ]

    def test_clock_change_day_is_flagged(self, monkeypatch):
        import time as time_module

        original = None
        try:
            import os

            original = os.environ.get("TZ")
            os.environ["TZ"] = "Europe/London"
            time_module.tzset()

            assert _is_clock_change(date(2026, 3, 29)) is True
            assert _is_clock_change(date(2026, 10, 25)) is True
            assert _is_clock_change(date(2026, 11, 3)) is False
        finally:
            import os

            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time_module.tzset()

    def test_clock_change_day_still_has_48_rows(self, tmp_path):
        log = _log(tmp_path, GRID)
        snapshot = log.snapshot_for(date(2026, 3, 29))
        assert len(snapshot["slots"]) == 48
        assert snapshot["slots"][0]["index"] == 0
        assert snapshot["slots"][47]["index"] == 47

    def test_slot_times_are_local_wall_clock(self, tmp_path):
        log = _log(tmp_path, GRID)
        snapshot = log.snapshot_for(DAY)

        start = datetime.fromisoformat(snapshot["slots"][0]["start"])
        end = datetime.fromisoformat(snapshot["slots"][0]["end"])
        # The day is 48 local rows, so the pair is a nominal half hour even
        # on a clock-change day.
        assert start.tzinfo is None
        assert end - start == timedelta(minutes=30)
        assert snapshot["timezone"]


class TestPowerProfile:
    """The sparse per-slot histograms used by the house-load forecast."""

    def test_sparse_time_weighted_bins(self, tmp_path):
        log = _log(tmp_path)
        for minute, watts in (
            (0, 600.0),
            (5, 600.0),
            (10, 600.0),
            (15, 600.0),
            (20, 1000.0),
            (25, 1000.0),
            (30, 1000.0),
        ):
            log.observe(_at(DAY, 0, minute), {"grid": watts, "evse": 0.0})

        path = tmp_path / "load_profile" / f"{DAY.isoformat()}.json"
        payload = json.loads(path.read_text())
        assert payload["bin_width_w"] == PROFILE_BIN_WIDTH_W
        slot0 = payload["slots"][0]
        assert slot0["closed"] is True
        assert slot0["coverage_s"]["house"] == pytest.approx(1800.0)

        bins = slot0["bins_s"]["house"]
        # 600 W for 900 s and 1000 W for 900 s; no empty bins are written.
        assert bins[str(int(600 / PROFILE_BIN_WIDTH_W))] == pytest.approx(
            900.0
        )
        assert bins[str(int(1000 / PROFILE_BIN_WIDTH_W))] == pytest.approx(
            900.0
        )
        assert "0" not in bins

    def test_signed_bins_have_no_range_limit(self, tmp_path):
        log = _log(tmp_path)
        for minute in (0, 5):
            log.observe(_at(DAY, 0, minute), {"grid": 15000.0, "evse": 0.0})
        for minute in (10, 15, 20, 25, 30):
            log.observe(_at(DAY, 0, minute), {"grid": -5000.0, "evse": 0.0})

        payload = json.loads(
            (tmp_path / "load_profile" / f"{DAY.isoformat()}.json").read_text()
        )
        bins = payload["slots"][0]["bins_s"]["house"]
        assert bins[str(int(15000 / PROFILE_BIN_WIDTH_W))] == pytest.approx(
            300.0
        )
        assert bins[str(int(-5000 / PROFILE_BIN_WIDTH_W))] == pytest.approx(
            1500.0
        )

    def test_house_is_grid_with_the_wallbox_removed(self, tmp_path):
        log = _log(tmp_path, GRID, EVSE)
        for minute in (0, 5, 10, 15, 20, 25, 30):
            log.observe(
                _at(DAY, 0, minute), {"grid": 1000.0, "evse": 300.0}
            )

        payload = json.loads(
            (tmp_path / "load_profile" / f"{DAY.isoformat()}.json").read_text()
        )
        slot0 = payload["slots"][0]
        house_bins = slot0["bins_s"]["house"]
        grid_bins = slot0["bins_s"]["grid"]
        evse_bins = slot0["bins_s"]["evse"]

        assert house_bins[str(int(700 / PROFILE_BIN_WIDTH_W))] == pytest.approx(
            1800.0
        )
        assert grid_bins[str(int(1000 / PROFILE_BIN_WIDTH_W))] == pytest.approx(
            1800.0
        )
        assert evse_bins[str(int(300 / PROFILE_BIN_WIDTH_W))] == pytest.approx(
            1800.0
        )
