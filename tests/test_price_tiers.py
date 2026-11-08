"""Three-tier price tables, windows and presets."""

import pytest

from evse_controller.price_tiers import (
    PRESETS,
    PriceWindow,
    build_price_table,
    parse_clock,
)


def _prices(standard=20.0, cheap=10.0, peak=30.0):
    return {"standard": standard, "cheap": cheap, "peak": peak}


def _table(preset="iog_prime", **kwargs):
    return build_price_table(
        preset,
        _prices(),
        _prices(standard=8.0, cheap=8.0, peak=16.0),
        **kwargs,
    )


class TestClockAndWindows:
    def test_parse_clock_accepts_midnight_end(self):
        assert parse_clock("00:00") == 0
        assert parse_clock("24:00") == 1440
        assert parse_clock("23:30") == 1410

    @pytest.mark.parametrize("text", ["", "23", "25:00", "12:60", "24:01"])
    def test_rejects_bad_times(self, text):
        with pytest.raises(ValueError):
            parse_clock(text)

    def test_window_covers_half_open(self):
        window = PriceWindow.from_text("16:00-19:00")
        assert window.covers(16 * 60)
        assert window.covers(19 * 60 - 1)
        assert not window.covers(19 * 60)

    def test_window_wraps_midnight(self):
        window = PriceWindow.from_text("23:30-05:30")
        assert window.covers(23 * 60 + 45)
        assert window.covers(0)
        assert window.covers(5 * 60 + 29)
        assert not window.covers(5 * 60 + 30)

    def test_overlapping_windows_are_detected(self):
        overnight = PriceWindow.from_text("23:30-05:30")
        early = PriceWindow.from_text("05:00-06:00")
        daytime = PriceWindow.from_text("12:00-13:00")
        assert overnight.overlaps(early)
        assert not overnight.overlaps(daytime)


class TestPriceLookup:
    def test_standard_is_the_fallback(self):
        table = _table()
        assert table.import_price_p(12 * 60) == 20.0

    def test_import_cheap_window_uses_the_cheap_price(self):
        table = _table()
        assert table.import_price_p(0) == 10.0
        assert table.import_price_p(5 * 60 + 30) == 20.0

    def test_export_peak_window_uses_the_peak_price(self):
        table = _table()
        assert table.export_price_p(16 * 60) == 16.0
        assert table.export_price_p(15 * 60) == 8.0
        assert table.export_price_p(19 * 60) == 8.0

    def test_multiple_windows_are_supported(self):
        table = build_price_table(
            "cosy",
            _prices(),
            _prices(),
        )
        for minute in (4 * 60, 13 * 60, 22 * 60):
            assert table.import_price_p(minute) == 10.0
        assert table.import_price_p(16 * 60) == 30.0

    def test_no_export_tariff_returns_unknown_not_zero(self):
        table = build_price_table(
            "iog_no_export", _prices(), _prices()
        )
        assert table.export_price_p(12 * 60) is None


class TestWindowOverridesAndValidation:
    def test_explicit_windows_replace_the_preset(self):
        table = _table(import_windows={"cheap": ["01:00-02:00"]})
        assert table.import_price_p(1 * 60 + 30) == 10.0
        assert table.import_price_p(23 * 60 + 45) == 20.0

    def test_explicit_empty_list_clears_a_tier(self):
        table = _table(import_windows={"cheap": []})
        assert table.import_price_p(0) == 20.0

    def test_cheap_and_peak_may_not_overlap(self):
        with pytest.raises(ValueError, match="overlaps"):
            _table(
                import_windows={
                    "cheap": ["12:00-14:00"],
                    "peak": ["13:00-15:00"],
                }
            )

    def test_invalid_window_message_is_plain(self):
        with pytest.raises(ValueError, match="HH:MM-HH:MM"):
            _table(import_windows={"cheap": ["noon"]})

    def test_unknown_preset_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown tariff preset"):
            build_price_table("not_a_product", _prices(), _prices())

    def test_unknown_export_source_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown export source"):
            _table(export_source="moonlight")


class TestPresets:
    def test_every_preset_builds(self):
        for key in PRESETS:
            table = build_price_table(key, _prices(), _prices())
            assert table.import_price_p(12 * 60) == 20.0

    def test_agile_preset_keeps_dynamic_export(self):
        table = build_price_table("iog_agile", _prices(), _prices())
        assert table.export_source == "agile"
        assert table.dynamic_export is True

    def test_outgoing_preset_is_flat_standard_export(self):
        table = build_price_table(
            "iog_outgoing",
            _prices(),
            _prices(standard=12.0, cheap=12.0, peak=12.0),
        )
        assert table.dynamic_export is False
        assert table.export_price_p(0) == 12.0
        assert table.export_price_p(17 * 60) == 12.0

    def test_prime_preset_has_a_peak_export_window(self):
        table = build_price_table("iog_prime", _prices(), _prices())
        assert table.export_price_p(17 * 60) == 30.0
        assert table.export_price_p(12 * 60) == 20.0
