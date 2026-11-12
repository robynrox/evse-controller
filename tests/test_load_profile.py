"""Rolling load profile used by the export planner."""

from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from evse_controller.load_profile import LoadProfile


DAY = date(2026, 11, 7)
BIN_WIDTH = 25.0


def _write_day(
    directory: Path,
    day: date,
    slot_bins: dict[int, dict[int, float]],
    *,
    closed: bool = True,
    coverage_s: float = 1800.0,
    bin_width: float = BIN_WIDTH,
    series: str = "house",
) -> None:
    slots = []
    for index in range(48):
        bins = slot_bins.get(index, {})
        slots.append(
            {
                "index": index,
                "closed": closed,
                "coverage_s": {series: coverage_s} if bins else {},
                "bins_s": {
                    series: {
                        str(bin_index): seconds
                        for bin_index, seconds in bins.items()
                    }
                }
                if bins
                else {},
            }
        )
    payload = {
        "version": 1,
        "date": day.isoformat(),
        "bin_width_w": bin_width,
        "slots": slots,
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{day.isoformat()}.json").write_text(
        json.dumps(payload)
    )


def _write_days(
    directory: Path,
    days: int,
    slot_bins: dict[int, dict[int, float]],
    **kwargs,
):
    for offset in range(days):
        _write_day(
            directory,
            DAY - timedelta(days=offset),
            slot_bins,
            **kwargs,
        )


def test_median_and_p90_from_merged_histograms(tmp_path):
    # Ten days at 600 W and four at 1,200 W: the median is 600 W and the
    # 90th percentile falls in the 1,200 W group.
    for offset in range(14):
        watts = 600 if offset < 10 else 1200
        _write_day(
            tmp_path,
            DAY - timedelta(days=offset),
            {0: {int(watts / BIN_WIDTH): 1800.0}},
        )

    profile = LoadProfile(tmp_path, min_days=14).for_date(DAY)

    assert profile[0].available
    assert profile[0].days == 14
    assert profile[0].median_w == pytest.approx(600.0, abs=BIN_WIDTH)
    assert profile[0].p90_w == pytest.approx(1200.0, abs=BIN_WIDTH)
    assert not profile[1].available


def test_five_days_are_used_directly(tmp_path):
    _write_days(tmp_path, 5, {0: {24: 1800.0}})  # 600 W

    profile = LoadProfile(tmp_path, min_days=14).for_date(DAY)

    assert profile[0].available
    assert profile[0].days == 5
    assert profile[0].prior_days == 0
    assert profile[0].median_w == pytest.approx(600.0, abs=BIN_WIDTH)


def test_thirteen_days_are_used_directly(tmp_path):
    _write_days(tmp_path, 13, {0: {24: 1800.0}})  # 600 W

    profile = LoadProfile(tmp_path, min_days=14).for_date(DAY)

    assert profile[0].days == 13
    assert profile[0].prior_days == 0
    assert profile[0].median_w == pytest.approx(600.0, abs=BIN_WIDTH)


def test_fourteen_days_are_used_directly(tmp_path):
    _write_days(tmp_path, 14, {0: {24: 1800.0}})  # 600 W

    profile = LoadProfile(tmp_path, min_days=14).for_date(DAY)

    assert profile[0].prior_days == 0
    assert profile[0].median_w == pytest.approx(600.0, abs=BIN_WIDTH)


def test_non_house_series_use_measured_days_directly(tmp_path):
    _write_days(
        tmp_path,
        5,
        {0: {120: 1800.0}},  # 3000 W
        series="solar",
    )

    profile = LoadProfile(tmp_path, min_days=14).for_date(
        DAY, series="solar"
    )

    assert profile[0].available
    assert profile[0].days == 5
    assert profile[0].prior_days == 0
    assert profile[0].median_w == pytest.approx(3000.0, abs=BIN_WIDTH)


def test_non_house_series_with_no_history_is_unavailable(tmp_path):
    profile = LoadProfile(tmp_path, min_days=14).for_date(
        DAY, series="heat_pump"
    )

    assert not profile[0].available
    assert profile[0].days == 0
    assert profile[0].prior_days == 0


def test_non_house_series_one_day_is_used_directly(tmp_path):
    _write_day(
        tmp_path,
        DAY,
        {0: {120: 1800.0}},  # 3000 W
        series="solar",
    )

    slot = LoadProfile(tmp_path, min_days=14).for_date(
        DAY, series="solar"
    )[0]

    assert slot.days == 1
    assert slot.prior_days == 0
    assert slot.median_w == pytest.approx(3000.0, abs=BIN_WIDTH)
    assert all(watts >= 400 for watts, _ in slot.samples)


def test_non_house_series_need_no_prior_after_fourteen_days(tmp_path):
    _write_days(
        tmp_path,
        14,
        {0: {120: 1800.0}},  # 3000 W
        series="solar",
    )

    profile = LoadProfile(tmp_path, min_days=14).for_date(
        DAY, series="solar"
    )

    assert profile[0].prior_days == 0
    assert profile[0].median_w == pytest.approx(3000.0, abs=BIN_WIDTH)


def test_the_lookback_is_capped_at_twenty_eight_days(tmp_path):
    _write_days(tmp_path, 30, {0: {24: 1800.0}})

    profile = LoadProfile(tmp_path, min_days=14).for_date(DAY)

    assert profile[0].days == 28
    assert profile[0].prior_days == 0


def test_unclosed_slots_are_ignored(tmp_path):
    _write_days(
        tmp_path / "unclosed", 14, {0: {24: 1800.0}}, closed=False
    )

    profile = LoadProfile(
        tmp_path / "unclosed", min_days=14
    ).for_date(DAY)

    assert not profile[0].available


def test_low_coverage_slots_are_ignored(tmp_path):
    _write_days(
        tmp_path / "low", 14, {0: {24: 1800.0}}, coverage_s=60.0
    )

    profile = LoadProfile(tmp_path / "low", min_days=14).for_date(DAY)

    assert not profile[0].available


def test_negative_bins_stay_signed(tmp_path):
    _write_days(tmp_path, 14, {0: {int(-1000 / BIN_WIDTH): 1800.0}})

    profile = LoadProfile(tmp_path, min_days=14).for_date(DAY)

    assert profile[0].median_w == pytest.approx(-1000.0, abs=BIN_WIDTH)
    assert profile[0].p90_w == pytest.approx(-1000.0, abs=BIN_WIDTH)


def test_bin_width_mismatch_is_ignored(tmp_path):
    _write_days(tmp_path, 14, {0: {6: 1800.0}})  # bins at 100 W width
    for path in tmp_path.glob("*.json"):
        payload = json.loads(path.read_text())
        payload["bin_width_w"] = 100.0
        path.write_text(json.dumps(payload))

    profile = LoadProfile(tmp_path, min_days=14).for_date(DAY)

    assert not profile[0].available


def test_day_stats_are_a_single_day_distribution(tmp_path):
    _write_day(
        tmp_path,
        DAY,
        {0: {24: 600.0, 48: 1200.0}},  # 600 W and 1200 W, equal time
        coverage_s=1800.0,
    )

    stats = LoadProfile(tmp_path).day_stats(DAY)

    assert stats[0].days == 1
    assert stats[0].p10_w == pytest.approx(607.5, abs=BIN_WIDTH)
    assert stats[0].median_w == pytest.approx(1206.25, abs=BIN_WIDTH)
    assert stats[0].p90_w == pytest.approx(1221.25, abs=BIN_WIDTH)
    assert stats[0].mean_w == pytest.approx(1012.5, abs=BIN_WIDTH)
    assert not stats[1].available


def test_a_series_can_be_selected_for_forecast_and_actual(tmp_path):
    house_dir = tmp_path / "house"
    solar_dir = tmp_path / "solar"
    for offset in range(14):
        file_day = DAY - timedelta(days=offset)
        _write_day(
            house_dir,
            file_day,
            {0: {int(600 / BIN_WIDTH): 1800.0}},
        )
        _write_day(
            solar_dir,
            file_day,
            {0: {int(3000 / BIN_WIDTH): 1800.0}},
            series="solar",
        )

    house_profile = LoadProfile(house_dir, min_days=14)
    solar_profile = LoadProfile(solar_dir, min_days=14)

    house = house_profile.for_date(DAY, series="house")
    solar = solar_profile.for_date(DAY, series="solar")
    actual_solar = solar_profile.day_stats(DAY, series="solar")

    assert house[0].median_w == pytest.approx(600.0, abs=BIN_WIDTH)
    assert solar[0].median_w == pytest.approx(3000.0, abs=BIN_WIDTH)
    assert actual_solar[0].median_w == pytest.approx(3000.0, abs=BIN_WIDTH)


def test_slot_samples_keep_the_time_weighting(tmp_path):
    # 200-225 W for 300 s, 225-250 W for 600 s, 250-275 W for 900 s.
    _write_day(
        tmp_path,
        DAY,
        {0: {8: 300.0, 9: 600.0, 10: 900.0}},
    )

    slot = LoadProfile(tmp_path, min_days=1).for_date(DAY)[0]

    assert len(slot.samples) == 3
    assert slot.samples[0][0] == pytest.approx(212.5)
    assert slot.samples[1][0] == pytest.approx(237.5)
    assert slot.samples[2][0] == pytest.approx(262.5)
    assert [weight for _, weight in slot.samples] == pytest.approx(
        [1 / 6, 1 / 3, 1 / 2]
    )
    assert sum(weight for _, weight in slot.samples) == pytest.approx(1.0)


def test_available_days_are_newest_first(tmp_path):
    _write_day(tmp_path, DAY, {0: {24: 1800.0}})
    _write_day(tmp_path, DAY - timedelta(days=1), {0: {24: 1800.0}})

    assert LoadProfile(tmp_path).available_days() == [
        DAY,
        DAY - timedelta(days=1),
    ]


@pytest.mark.parametrize("coverage_s", [1800.0, 600.0])
def test_one_day_is_used_directly(tmp_path, coverage_s):
    _write_day(
        tmp_path,
        DAY,
        {0: {24: coverage_s}},  # 600 W
        coverage_s=coverage_s,
    )

    slot = LoadProfile(tmp_path, min_days=14).for_date(DAY)[0]

    assert slot.days == 1
    assert slot.prior_days == 0
    assert slot.median_w == pytest.approx(600.0, abs=BIN_WIDTH)
    assert all(watts >= 400 for watts, _ in slot.samples)
