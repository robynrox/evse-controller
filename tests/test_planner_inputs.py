"""The DP input builder joins the tariff source and the load forecast."""

from datetime import date, datetime, time, timedelta

import pytest

from evse_controller.energy_sessions import EnergySession
from evse_controller.load_profile import SlotLoad
from evse_controller.planner.inputs import (
    build_planner_input,
    uniform_load_prior,
)
from evse_controller.tariffs.source import SlotRate


DAY = date(2026, 11, 7)


class FakeSource:
    name = "Fake"
    dynamic_import = True
    dynamic_export = True

    def __init__(
        self,
        import_p=20.0,
        export_p=10.0,
        sessions=None,
        dynamic_export=True,
    ):
        self.import_p = import_p
        self.export_p = export_p
        self.sessions = list(sessions or [])
        self.dynamic_export = dynamic_export

    def rates_for_day(self, day):
        start = datetime.combine(day, time())
        return [
            SlotRate(
                start=start + timedelta(minutes=30 * slot),
                end=start + timedelta(minutes=30 * (slot + 1)),
                import_p=(
                    self.import_p[slot]
                    if isinstance(self.import_p, (list, tuple))
                    else self.import_p
                ),
                export_p=(
                    self.export_p[slot]
                    if isinstance(self.export_p, (list, tuple))
                    else self.export_p
                ),
            )
            for slot in range(48)
        ]

    def sessions_for_day(self, day):
        return list(self.sessions)


class DaySource(FakeSource):
    """A source whose export prices can differ by local day."""

    def __init__(self, prices_by_day, **kwargs):
        super().__init__(**kwargs)
        self.prices_by_day = prices_by_day

    def rates_for_day(self, day):
        prices = self.prices_by_day.get(day, [None] * 48)
        start = datetime.combine(day, time())
        return [
            SlotRate(
                start=start + timedelta(minutes=30 * slot),
                end=start + timedelta(minutes=30 * (slot + 1)),
                import_p=self.import_p,
                export_p=prices[slot],
            )
            for slot in range(48)
        ]


class FakeProfile:
    def __init__(self, loads):
        self.loads = loads

    def for_date(self, day):
        return self.loads


def _session(kind, start_slot, end_slot):
    start = datetime.combine(DAY, time()) + timedelta(
        minutes=30 * start_slot
    )
    end = datetime.combine(DAY, time()) + timedelta(
        minutes=30 * end_slot
    )
    return EnergySession(kind, start, end)


def test_builds_only_the_remaining_slots():
    loads = [
        SlotLoad(median_w=1200.0, p90_w=2400.0, days=14, coverage_s=1800)
        for _ in range(48)
    ]
    result = build_planner_input(
        FakeSource(
            import_p=20.0,
            export_p=10.0,
            dynamic_export=False,
        ),
        FakeProfile(loads),
        DAY,
        current_slot=46,
        current_soc=60,
        target_soc=50,
        max_charge_current_a=16,
        max_discharge_current_a=16,
    )

    assert len(result.slots) == 2
    assert result.slots[0].start.hour == 23
    assert result.slots[0].load_kw == pytest.approx(1.2)
    assert result.slots[0].import_p == pytest.approx(20.0)
    assert result.slots[0].export_p == pytest.approx(10.0)
    assert result.initial_soc == pytest.approx(60.0)
    assert result.target_soc == pytest.approx(50.0)
    assert result.forecast_days == 14


def test_missing_profile_uses_the_uniform_prior():
    result = build_planner_input(
        FakeSource(import_p=20.0, export_p=10.0),
        FakeProfile([]),
        DAY,
        current_slot=0,
        current_soc=50,
        target_soc=50,
        max_charge_current_a=16,
        max_discharge_current_a=16,
        load_prior=uniform_load_prior(),
    )

    first = result.slots[0]
    assert first.load_source == "prior"
    assert len(first.load_samples) == 16
    assert sum(weight for _, weight in first.load_samples) == pytest.approx(1)
    assert first.load_kw == pytest.approx(0.2)


def test_profile_samples_are_passed_through():
    loads = [
        SlotLoad(
            median_w=250.0,
            p90_w=400.0,
            days=14,
            coverage_s=1800,
            samples=((200.0, 0.25), (300.0, 0.75)),
        )
    ] + [
        SlotLoad(median_w=None, p90_w=None, days=0, coverage_s=0)
        for _ in range(47)
    ]
    result = build_planner_input(
        FakeSource(import_p=20.0, export_p=10.0),
        FakeProfile(loads),
        DAY,
        current_slot=0,
        current_soc=50,
        target_soc=50,
        max_charge_current_a=16,
        max_discharge_current_a=16,
    )

    first = result.slots[0]
    assert first.load_source == "profile"
    assert [kw for kw, _ in first.load_samples] == pytest.approx(
        [0.2, 0.3]
    )
    assert [weight for _, weight in first.load_samples] == pytest.approx(
        [0.25, 0.75]
    )


def test_free_import_overlays_the_price_but_keeps_the_session():
    result = build_planner_input(
        FakeSource(
            import_p=30.0,
            export_p=10.0,
            sessions=[_session("free_import", 0, 2)],
        ),
        FakeProfile([]),
        DAY,
        current_slot=0,
        current_soc=50,
        target_soc=50,
        max_charge_current_a=16,
        max_discharge_current_a=16,
    )

    assert result.slots[0].import_p == 0.0
    assert result.slots[0].session == "free_import"
    assert result.slots[1].import_p == 0.0
    assert result.slots[2].import_p == 30.0


def test_grid_support_forces_max_discharge():
    result = build_planner_input(
        FakeSource(
            import_p=20.0,
            export_p=10.0,
            sessions=[_session("grid_support", 0, 1)],
        ),
        FakeProfile([]),
        DAY,
        current_slot=0,
        current_soc=60,
        target_soc=50,
        max_charge_current_a=16,
        max_discharge_current_a=16,
    )

    assert result.slots[0].session == "grid_support"
    assert result.slots[0].forced == "discharge_max"


def test_ev_away_forces_idle():
    result = build_planner_input(
        FakeSource(
            import_p=20.0,
            export_p=10.0,
            sessions=[_session("ev_away", 0, 1)],
        ),
        FakeProfile([]),
        DAY,
        current_slot=0,
        current_soc=60,
        target_soc=50,
        max_charge_current_a=16,
        max_discharge_current_a=16,
    )

    assert result.slots[0].session == "ev_away"
    assert result.slots[0].forced == "idle"


def test_unknown_export_uses_the_configured_fallback_for_a_fixed_tariff():
    result = build_planner_input(
        FakeSource(
            import_p=20.0,
            export_p=[None] * 48,
            dynamic_export=False,
        ),
        FakeProfile([]),
        DAY,
        current_slot=0,
        current_soc=50,
        target_soc=50,
        max_charge_current_a=16,
        max_discharge_current_a=16,
        unknown_export_p=0.0,
    )

    assert result.slots[0].export_p == 0.0


def test_dynamic_export_horizon_stops_at_the_last_known_price():
    prices = [10.0] * 24 + [None] * 24
    result = build_planner_input(
        FakeSource(import_p=20.0, export_p=prices),
        FakeProfile([]),
        DAY,
        current_slot=0,
        current_soc=50,
        target_soc=50,
        max_charge_current_a=16,
        max_discharge_current_a=16,
    )

    assert len(result.slots) == 24
    assert result.slots[-1].slot == 23
    assert result.horizon_end_slot == 23
    assert result.horizon_limited_by_export is True


def test_dynamic_export_horizon_includes_the_next_day_when_published():
    tomorrow = DAY + timedelta(days=1)
    source = DaySource(
        {
            DAY: [10.0] * 48,
            tomorrow: [12.0] * 24 + [None] * 24,
        }
    )
    result = build_planner_input(
        source,
        FakeProfile([]),
        DAY,
        current_slot=0,
        current_soc=50,
        target_soc=None,
        max_charge_current_a=16,
        max_discharge_current_a=16,
    )

    assert len(result.slots) == 72
    assert result.slots[-1].slot == 23
    assert result.slots[-1].start.date() == tomorrow
    assert result.horizon_end_date == tomorrow
    assert result.horizon_limited_by_export is True


def test_dynamic_export_without_next_day_prices_stops_at_midnight():
    tomorrow = DAY + timedelta(days=1)
    source = DaySource(
        {
            DAY: [10.0] * 48,
            tomorrow: [None] * 48,
        }
    )
    result = build_planner_input(
        source,
        FakeProfile([]),
        DAY,
        current_slot=0,
        current_soc=50,
        target_soc=None,
        max_charge_current_a=16,
        max_discharge_current_a=16,
    )

    assert len(result.slots) == 48
    assert result.slots[-1].start.date() == DAY
    assert result.horizon_limited_by_export is True


def test_dynamic_export_with_no_prices_has_an_empty_horizon():
    result = build_planner_input(
        FakeSource(import_p=20.0, export_p=[None] * 48),
        FakeProfile([]),
        DAY,
        current_slot=0,
        current_soc=50,
        target_soc=50,
        max_charge_current_a=16,
        max_discharge_current_a=16,
    )

    assert result.slots == ()
    assert result.horizon_end_slot is None


def test_unknown_import_without_a_fallback_is_an_error():
    with pytest.raises(ValueError, match="import price"):
        build_planner_input(
            FakeSource(import_p=[None] * 48, export_p=10.0),
            FakeProfile([]),
            DAY,
            current_slot=0,
            current_soc=50,
            target_soc=50,
            max_charge_current_a=16,
            max_discharge_current_a=16,
        )


def test_load_fallback_is_used_when_the_forecast_is_thin():
    result = build_planner_input(
        FakeSource(import_p=20.0, export_p=10.0),
        FakeProfile([]),
        DAY,
        current_slot=0,
        current_soc=50,
        target_soc=50,
        max_charge_current_a=16,
        max_discharge_current_a=16,
        load_fallback_kw=0.4,
    )

    assert result.slots[0].load_kw == pytest.approx(0.4)
    assert result.forecast_days == 0


def test_profile_is_reported_as_profile():
    loads = [
        SlotLoad(
            median_w=200.0,
            p90_w=400.0,
            days=1,
            coverage_s=600.0,
            samples=((100.0, 0.5), (300.0, 0.5)),
        )
    ] + [
        SlotLoad(median_w=None, p90_w=None, days=0, coverage_s=0)
        for _ in range(47)
    ]
    result = build_planner_input(
        FakeSource(import_p=20.0, export_p=10.0),
        FakeProfile(loads),
        DAY,
        current_slot=0,
        current_soc=50,
        target_soc=50,
        max_charge_current_a=16,
        max_discharge_current_a=16,
        load_prior=uniform_load_prior(),
    )

    first = result.slots[0]
    assert first.load_source == "profile"
    assert first.load_samples == ((0.1, 0.5), (0.3, 0.5))
