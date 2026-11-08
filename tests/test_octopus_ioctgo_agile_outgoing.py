"""Tests for IOctGoWithAgileOutgoingTariff bidirectional storage decision logic."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta
import json
from evse_controller.tariffs.octopus.ioctgo_with_agile_outgoing import IOctGoWithAgileOutgoingTariff
from evse_controller.load_profile import SlotLoad
from evse_controller.energy_sessions import (
    EV_AWAY,
    FREE_IMPORT,
    GRID_SUPPORT,
    EnergySession,
    SessionStore,
)
from evse_controller.drivers.EvseController import ControlState, EvseController
from evse_controller.drivers.Power import Power
from evse_controller.drivers.evse.async_interface import EvseAsyncState
from evse_controller.utils.config import config, Config


def create_test_state(battery_level: int, current: float = 0.0) -> EvseAsyncState:
    """Helper function to create a test state with specified battery level"""
    state = EvseAsyncState()
    state.battery_level = battery_level
    state.current = current
    return state


def create_mock_agile_rates():
    """Create mock Agile Outgoing rates for testing.
    
    Creates a realistic rate profile with:
    - Low rates overnight (00:00-05:30): 3-5p
    - Medium rates morning (05:30-16:00): 8-12p
    - Peak rates evening (16:00-19:00): 25-35p
    - Medium rates night (19:00-23:30): 10-15p
    """
    rates = []
    base_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Define rate profile (hour, minute, rate_p)
    rate_profile = [
        # Overnight low rates
        (0, 0, 4.0), (0, 30, 3.5), (1, 0, 3.0), (1, 30, 3.0),
        (2, 0, 3.0), (2, 30, 3.0), (3, 0, 3.5), (3, 30, 4.0),
        (4, 0, 4.5), (4, 30, 5.0), (5, 0, 5.5), (5, 30, 6.0),
        # Morning medium rates
        (6, 0, 8.0), (6, 30, 9.0), (7, 0, 10.0), (7, 30, 11.0),
        (8, 0, 12.0), (8, 30, 11.0), (9, 0, 10.0), (9, 30, 9.0),
        (10, 0, 8.5), (10, 30, 8.0), (11, 0, 8.0), (11, 30, 8.5),
        (12, 0, 9.0), (12, 30, 9.5), (13, 0, 10.0), (13, 30, 10.5),
        (14, 0, 11.0), (14, 30, 11.5), (15, 0, 12.0), (15, 30, 12.5),
        # Evening peak rates
        (16, 0, 18.0), (16, 30, 22.0), (17, 0, 28.0), (17, 30, 32.0),
        (18, 0, 35.0), (18, 30, 30.0), (19, 0, 25.0), (19, 30, 20.0),
        # Night medium rates
        (20, 0, 15.0), (20, 30, 14.0), (21, 0, 13.0), (21, 30, 12.0),
        (22, 0, 11.0), (22, 30, 10.0), (23, 0, 9.0), (23, 30, 8.0),
    ]
    
    for hour, minute, rate in rate_profile:
        start = base_date.replace(hour=hour, minute=minute)
        end = start + timedelta(minutes=30)
        rates.append({
            'start': start,
            'end': end,
            'rate': rate
        })
    
    return rates


def _build_tariff(state_path=None) -> IOctGoWithAgileOutgoingTariff:
    """Create a tariff instance with the Wallbox thread mocked out."""
    mock_thread = Mock()
    mock_thread.getBatteryChargeLevel = Mock(return_value=75)
    mock_thread.get_state = Mock(return_value={})

    with patch('evse_controller.drivers.evse.wallbox.wallbox_thread.WallboxThread.get_instance',
               return_value=mock_thread):
        tariff = IOctGoWithAgileOutgoingTariff(state_path=state_path)
    tariff.agile_rates = create_mock_agile_rates()
    tariff._planned_export_slots = []  # No planned exports for these tests
    return tariff


@pytest.fixture
def agile_tariff(tmp_path):
    """Create tariff instance with mocked Agile rates.

    The rolling load profile is pointed at a temporary directory so a test
    never reads (or writes) the live history.
    """
    tariff = _build_tariff()
    tariff._load_profile.set_directory(tmp_path / "load_profile")
    yield tariff


class TestStorageFloorThreshold:
    """Test Tier 1: Storage Floor (< 5p/kWh) - always store."""
    
    def test_rate_below_floor_always_store(self, agile_tariff):
        """When rate < 5p, should store regardless of SoC or future rates."""
        # Rate of 3p at slot 4 (02:00-02:30)
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=4,
            current_export_rate_p=3.0,
            soc_percent=80  # High SoC
        )
        assert result is True, "Should store when rate < floor (5p), even with high SoC"
    
    def test_rate_at_floor_boundary(self, agile_tariff):
        """Test behavior at exactly 5p boundary."""
        # Rate of 4.9p should store
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=4,
            current_export_rate_p=4.9,
            soc_percent=80
        )
        assert result is True, "Should store when rate < 5p"
        
        # Rate of 5.0p should NOT trigger Tier 1 (may trigger other tiers)
        # This will fall through to Tier 3 evaluation
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=4,
            current_export_rate_p=5.0,
            soc_percent=80
        )
        # At 5p with high SoC, Tier 3 applies: best_future × 0.5 > 5?
        # Best future is 35p, 35 × 0.5 = 17.5p > 5p, so should still store
        assert result is True


class TestSelfUseValueThreshold:
    """Test Tier 2: Self-Use Value (5p-SELF_USE_VALUE_THRESHOLD_P/kWh with dynamic SoC threshold)."""

    def test_rate_below_self_use_with_low_soc(self, agile_tariff):
        """When 5p < rate < SELF_USE_VALUE_THRESHOLD_P and SoC below dynamic threshold, should store for self-use."""
        # Rate of 10p, low SoC (30%)
        # At slot 20 (10:00), remaining slots = 47 - 20 = 27
        # Dynamic threshold = 60% + (27 × 1.0%) = 87%
        # SoC 30% < 87%, should store
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=20,  # 10:00-10:30, rate ~10p
            current_export_rate_p=10.0,
            soc_percent=30  # Low SoC (below dynamic threshold)
        )
        assert result is True, "Should store when rate < self-use value (SELF_USE_VALUE_THRESHOLD_P) and SoC below dynamic threshold"

    def test_rate_below_self_use_with_high_soc(self, agile_tariff):
        """When 5p < rate < SELF_USE_VALUE_THRESHOLD_P and SoC above dynamic threshold, fall through to Tier 3."""
        # Rate of 10p at slot 20 (10:00)
        # Dynamic threshold = 60% + (27 × 1.0%) = 87%
        # SoC 90% > 87%, should fall through to Tier 3
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=20,
            current_export_rate_p=10.0,
            soc_percent=90  # High SoC (above dynamic threshold)
        )
        # Falls to Tier 3: best_future (35p) × 0.5 = 17.5p > 10p, so store
        assert result is True, "Should store when future rate justifies (Tier 3)"

    def test_rate_at_self_use_boundary_low_soc(self, agile_tariff):
        """Test behavior at SELF_USE_VALUE_THRESHOLD_P boundary with low SoC."""
        # Rate of 15.5p (just below self-use threshold), low SoC
        # At slot 32 (16:00), remaining slots = 47 - 32 = 15
        # Dynamic threshold = 60% + (15 × 1.0%) = 75%
        # SoC 30% < 75%, should store
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=32,  # 16:00-16:30
            current_export_rate_p=15.5,
            soc_percent=30
        )
        assert result is True, "Should store when rate < SELF_USE_VALUE_THRESHOLD_P and SoC below dynamic threshold"

    def test_rate_above_self_use_threshold(self, agile_tariff):
        """When rate > SELF_USE_VALUE_THRESHOLD_P, only Tier 3 applies."""
        # Rate of 20p (above self-use threshold)
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=35,  # 17:30-18:00, peak rate
            current_export_rate_p=20.0,
            soc_percent=30  # Low SoC doesn't matter above threshold
        )
        # Tier 3: best remaining future rate after excluding current slot
        # Best might be 35p at slot 36 (18:00), 35 × 0.5 = 17.5p < 20p
        # So should export
        assert result is False, "Should export when current rate > adjusted future rate"

    def test_dynamic_threshold_early_day(self, agile_tariff):
        """Test dynamic threshold is higher early in the day."""
        # At slot 10 (05:00), remaining slots = 47 - 10 = 37
        # Dynamic threshold = 60% + (37 × 1.0%) = 97%
        # SoC 80% < 97%, should store
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=10,
            current_export_rate_p=10.0,  # Below self-use threshold
            soc_percent=80
        )
        assert result is True, "Should store early in day when dynamic threshold is high"

    def test_dynamic_threshold_late_day(self, agile_tariff):
        """Test dynamic threshold approaches minimum late in the day."""
        # At slot 43 (21:30), remaining slots = 47 - 43 = 4
        # Dynamic threshold = 60% + (4 × 1.0%) = 64%
        # SoC 50% < 64%, should store via Tier 2
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=43,
            current_export_rate_p=10.0,  # Below self-use threshold
            soc_percent=50
        )
        # SoC 50% < 64% dynamic threshold, so Tier 2 applies
        assert result is True, "Late day: SoC below dynamic threshold (50% < 64%), Tier 2 applies"


class TestFutureExportOptimization:
    """Test Tier 3: Future Export Optimization."""
    
    def test_future_rate_justifies_storage(self, agile_tariff):
        """When future_rate × 0.5 > current_rate, should store."""
        # Current rate 12p, best future 35p
        # 35 × 0.5 = 17.5p > 12p, should store
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=28,  # 14:00-14:30, rate ~11p
            current_export_rate_p=12.0,
            soc_percent=80  # High SoC
        )
        assert result is True, "Should store when future justifies (35×0.5=17.5 > 12)"
    
    def test_future_rate_does_not_justify(self, agile_tariff):
        """When future_rate × 0.5 < current_rate, should export."""
        # Current rate 25p, best remaining future might be 20p
        # 20 × 0.5 = 10p < 25p, should export
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=38,  # 19:00-19:30, rate ~20p
            current_export_rate_p=25.0,
            soc_percent=80
        )
        assert result is False, "Should export when current > adjusted future"
    
    def test_unknown_soc_falls_back_to_tier3(self, agile_tariff):
        """When SoC is unknown (-1), only Tier 3 applies."""
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=20,
            current_export_rate_p=10.0,
            soc_percent=-1  # Unknown
        )
        # Tier 3 only: 35 × 0.5 = 17.5p > 10p, store
        assert result is True


class TestHarvestCurrentDecision:
    """The store/export decision is costed at the current solar will use."""

    def test_strong_surplus_stores_where_3a_would_export(self, agile_tariff):
        """At 22p with a 35p future: 3 A exports, a 6 A harvest stores.

        35p x 0.547 = 19.1p < 22p (export), but 35p x 0.734 = 25.7p > 22p
        (store).  This is the case the old fixed-3-A assumption got wrong.
        """
        kicker = dict(
            current_slot=33,  # 16:30, next peak is 35p at 18:00
            current_export_rate_p=22.0,
            soc_percent=80,
        )

        assert agile_tariff._should_use_bidirectional_mode(
            **kicker, harvest_current_a=3
        ) is False
        assert agile_tariff._should_use_bidirectional_mode(
            **kicker, harvest_current_a=6
        ) is True

    def test_self_use_threshold_moves_with_harvest_current(self, agile_tariff):
        """15p is above the 3 A self-use value but below the 6 A one."""
        kicker = dict(
            current_slot=39,  # 19:30, best remaining future is 15p
            current_export_rate_p=15.0,
            soc_percent=30,  # below the 60% + 8 slots dynamic threshold
        )

        assert agile_tariff._should_use_bidirectional_mode(
            **kicker, harvest_current_a=3
        ) is False
        assert agile_tariff._should_use_bidirectional_mode(
            **kicker, harvest_current_a=6
        ) is True

    def test_default_harvest_current_is_minimum(self, agile_tariff):
        """Without a surplus estimate the conservative 3 A assumption holds."""
        assert agile_tariff.SOLAR_CHARGE_REFERENCE_CURRENT == pytest.approx(3)
        assert agile_tariff.solar_export_round_trip() == pytest.approx(
            agile_tariff.EFFICIENCY_MODEL.round_trip_efficiency(3, 32)
        )


class TestPlannedExportSlotExplanation:
    """A committed export slot is not a sale price for extra solar.

    At a high SoC the export plan already spends the pack's spare capacity on
    the best slot of the day.  Solar stored now can only be sold in the next
    unused slot, so ``no store`` is the right decision even with a strong
    surplus.  The reason must say that, because naming only the unused rate
    looks like the planner has missed the good price.
    """

    @staticmethod
    def _set_two_peak_rates(tariff, now_slot=24, now_rate=21.4, later_slot=32,
                            later_rate=30.5, default=5.0):
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rates = []
        for slot in range(48):
            start = base + timedelta(minutes=30 * slot)
            rate = now_rate if slot == now_slot else (
                later_rate if slot == later_slot else default
            )
            rates.append({"start": start, "end": start + timedelta(minutes=30),
                          "rate": rate})
        tariff.agile_rates = rates

    def test_committed_slot_is_named_in_the_no_store_reason(self, agile_tariff):
        self._set_two_peak_rates(agile_tariff)
        agile_tariff._planned_export_slots = [32]

        decision = agile_tariff._evaluate_storage_decision(
            24, 21.4, 90, harvest_current_a=6
        )

        assert decision.use_bidirectional is False
        # 30.5p is in the plan, so the marginal sale is the next unused slot.
        assert decision.future_rate_p == pytest.approx(21.4)
        assert "30.5p at 16:00–16:30 is already committed" in decision.reason
        assert "best unused slot is 21.4p" in decision.reason
        assert "15.5p after the 73% round trip" in decision.reason
        assert "no future slot beats" not in decision.reason

    def test_unplanned_slot_still_stores(self, agile_tariff):
        """Without the commitment the same surplus stores at 30.5p."""
        self._set_two_peak_rates(agile_tariff)
        agile_tariff._planned_export_slots = []

        decision = agile_tariff._evaluate_storage_decision(
            24, 21.4, 80, harvest_current_a=6
        )

        assert decision.use_bidirectional is True
        assert decision.target_slot == 32
        assert "storing solar" in decision.reason

    def test_higher_unused_slot_is_valued_not_denied(self, agile_tariff):
        """A 30.5p slot is not "no future slot" just because 3 A loses 45%.

        This is the 09:30 case: no solar surplus, 19.9p now, 30.5p later, and
        a 3 A harvest.  (30.5 - 0.3) x 0.547 = 16.5p, so exporting now wins.
        """
        self._set_two_peak_rates(agile_tariff, now_slot=19, now_rate=19.9)
        agile_tariff._planned_export_slots = []

        decision = agile_tariff._evaluate_storage_decision(
            19, 19.9, 78, harvest_current_a=3
        )

        assert decision.use_bidirectional is False
        assert "future 30.5p at 16:00–16:30" in decision.reason
        assert "30.2p × 55% = 16.5p ≤ 19.9p now" in decision.reason
        assert "no future slot beats" not in decision.reason

    def test_best_planned_rate_ignores_past_slots(self, agile_tariff):
        self._set_two_peak_rates(agile_tariff)
        agile_tariff._planned_export_slots = [20, 32, 47]

        slot, rate = agile_tariff._best_planned_export_slot_rate(24)

        assert slot == 32
        assert rate == pytest.approx(30.5)

    def test_no_planned_slots_returns_nothing(self, agile_tariff):
        agile_tariff._planned_export_slots = []

        assert agile_tariff._best_planned_export_slot_rate(24) == (None, 0.0)


class TestSolarHarvestCurrentEstimate:
    """Estimate the load-following charge current from the live surplus."""

    @staticmethod
    def _attach(tariff, surplus_w, voltage=240.0, bias=-0.5):
        controller = Mock()
        controller.chargeCurrentBias = bias
        # getHomeWatts() = ch1 - ch2, and -getHomeWatts() is the solar surplus.
        controller.current_grid_power = Power(
            ch1Watts=0, ch2Watts=surplus_w, voltage=voltage
        )
        tariff.evseController = controller
        return controller

    def test_uses_surplus_and_snaps_up_to_measured_point(self, agile_tariff):
        # 1680 W / 240 V = 7 A, -0.5 A bias = 6.5 A -> 7 A measured point.
        self._attach(agile_tariff, 1680)
        assert agile_tariff._estimate_solar_harvest_current() == pytest.approx(7)

    def test_fractional_surplus_rounds_up(self, agile_tariff):
        # 1320 W / 240 V = 5.5 A -> 6 A, not 5 A.  Efficiency improves with
        # current, so rounding down would understate a store that pays.
        self._attach(agile_tariff, 1320, bias=0.0)
        assert agile_tariff._estimate_solar_harvest_current() == pytest.approx(6)

    def test_surplus_at_a_measured_point_stays_there(self, agile_tariff):
        # 1200 W / 240 V = 5 A, -0.5 A bias = 4.5 A -> 5 A.
        self._attach(agile_tariff, 1200)
        assert agile_tariff._estimate_solar_harvest_current() == pytest.approx(5)

    def test_weak_surplus_stays_at_minimum(self, agile_tariff):
        # 720 W / 240 V = 3 A, bias takes it to 2.5 A -> clamped to 3 A.
        self._attach(agile_tariff, 720)
        assert agile_tariff._estimate_solar_harvest_current() == pytest.approx(3)

    def test_huge_surplus_clamps_to_measured_model(self, agile_tariff):
        """Efficiency is flat above 16 A, so the estimate stops there."""
        self._attach(agile_tariff, 20000)
        assert agile_tariff._estimate_solar_harvest_current() == pytest.approx(16)

    def test_no_power_reading_falls_back_to_minimum(self, agile_tariff):
        agile_tariff.evseController = None
        assert agile_tariff._estimate_solar_harvest_current() == pytest.approx(3)

    def test_missing_voltage_uses_nominal(self, agile_tariff):
        self._attach(agile_tariff, 1680, voltage=0)
        assert agile_tariff._estimate_solar_harvest_current() == pytest.approx(7)


class TestExampleScenarios:
    """Test the example scenarios from the docstring."""

    def test_scenario_1_trivial_rate(self, agile_tariff):
        """Example 1: 3p/kWh current, 10p/kWh future → STORE (Tier 1)."""
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=4,  # 02:00-02:30, ~3p
            current_export_rate_p=3.0,
            soc_percent=50
        )
        assert result is True, "STORE: rate too trivial (3p < 5p floor)"

    def test_scenario_2_low_rate_low_soc(self, agile_tariff):
        """Example 2: 6p/kWh current, 8p/kWh future, low SoC → STORE (Tier 2)."""
        # At slot 12 (06:00), remaining slots = 47 - 12 = 35
        # Dynamic threshold = 60% + (35 × 1.0%) = 95%
        # SoC 30% < 95%, should store via Tier 2
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=12,  # 06:00-06:30, ~8p
            current_export_rate_p=6.0,
            soc_percent=30  # Low SoC (below dynamic threshold)
        )
        assert result is True, "STORE: self-use value (6p < SELF_USE_VALUE_THRESHOLD_P, SoC below dynamic threshold)"

    def test_scenario_3_low_rate_high_soc(self, agile_tariff):
        """Example 3: 6p/kWh current, 8p/kWh future, high SoC → Tier 3 evaluation."""
        # At slot 12 (06:00), remaining slots = 35
        # Dynamic threshold = 60% + 35 = 95%
        # SoC 80% < 95%, still below dynamic threshold, so Tier 2 applies
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=12,
            current_export_rate_p=6.0,
            soc_percent=80  # High SoC but still below dynamic threshold
        )
        # Actually stores via Tier 2 since 80% < 95%
        assert result is True, "STORE: SoC below dynamic threshold (Tier 2)"

    def test_scenario_4_high_rate_good_future(self, agile_tariff):
        """Example 4: 20p/kWh current, 45p/kWh future → STORE (Tier 3)."""
        # Note: Our mock rates max at 35p, so adjust test
        # With 20p current and 35p future: 35×0.5=17.5p < 20p → EXPORT
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=38,  # 19:00-19:30
            current_export_rate_p=20.0,
            soc_percent=80
        )
        # Best remaining future after 19:00 is ~15p at 20:00
        # 15 × 0.5 = 7.5p < 20p, EXPORT
        assert result is False, "EXPORT: current (20p) > adjusted future"

    def test_scenario_5_high_rate_poor_future(self, agile_tariff):
        """Example 5: 20p/kWh current, 30p/kWh future → EXPORT (Tier 3)."""
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=36,  # 18:00-18:30, peak
            current_export_rate_p=30.0,
            soc_percent=80
        )
        # Best remaining: 25p at 19:00, 25×0.5=12.5p < 30p → EXPORT
        assert result is False, "EXPORT: current (30p) > adjusted future (12.5p)"


class TestSetHomeDemandLevels:
    """Test set_home_demand_levels() configures correctly for bidirectional."""
    
    def test_bidirectional_configuration(self, agile_tariff):
        """Test demand levels when bidirectional mode is active."""
        mock_controller = Mock()
        mock_controller.use_new_current_calculation = False
        state = create_test_state(50)
        
        # Force bidirectional by setting low rate
        agile_tariff._planned_export_slots = []
        
        agile_tariff.set_home_demand_levels(mock_controller, state, 120)  # 02:00
        
        # Verify initialization
        assert mock_controller.use_new_current_calculation is True
        
        # When in bidirectional mode, should configure:
        # - Activation power: 1W (discharge at low surplus)
        # - Bias: +0.5 (favor higher currents)
        # - Range: 3A to max
        # Note: This depends on the rate at 02:00 triggering bidirectional
        # which it should (3-4p < 5p floor)
    
    def test_export_slot_skips_configuration(self, agile_tariff):
        """Test that export slots skip load-following configuration."""
        mock_controller = Mock()
        mock_controller.use_new_current_calculation = False
        state = create_test_state(50)

        # Mark slot 4 (02:00-02:30) as export slot
        agile_tariff._planned_export_slots = [4]

        agile_tariff.set_home_demand_levels(mock_controller, state, 120)  # 02:00

        # Should return early without configuring discharge parameters
        assert not mock_controller.setDischargeActivationPower.called

    def test_off_peak_skips_load_following_configuration(self, agile_tariff):
        """Test that off-peak hours (23:30-05:30) skip load-following configuration.
        
        This prevents the load-following configuration from overriding the 
        off-peak charging behavior (which should charge at max current).
        
        Regression test for issue where charging dropped to 3A after midnight
        during off-peak hours.
        """
        mock_controller = Mock()
        mock_controller.use_new_current_calculation = False
        state = create_test_state(50)
        
        # Clear any planned export slots
        agile_tariff._planned_export_slots = []
        
        # Test at 00:00 (midnight, during off-peak)
        agile_tariff.set_home_demand_levels(mock_controller, state, 0)  # 00:00
        
        # Should return early without configuring discharge/charge parameters
        assert not mock_controller.setDischargeActivationPower.called
        assert not mock_controller.setChargeActivationPower.called
        assert not mock_controller.setChargeCurrentRange.called
        
        # Test at 03:00 (during off-peak)
        mock_controller.reset_mock()
        agile_tariff.set_home_demand_levels(mock_controller, state, 180)  # 03:00 = 3*60
        
        # Should return early
        assert not mock_controller.setDischargeActivationPower.called
        assert not mock_controller.setChargeActivationPower.called
        assert not mock_controller.setChargeCurrentRange.called
        
        # Test at 23:45 (during off-peak, before midnight)
        mock_controller.reset_mock()
        agile_tariff.set_home_demand_levels(mock_controller, state, 1425)  # 23:45 = 23*60+45
        
        # Should return early
        assert not mock_controller.setDischargeActivationPower.called
        assert not mock_controller.setChargeActivationPower.called
        assert not mock_controller.setChargeCurrentRange.called
        
        # Test at 06:00 (outside off-peak, should configure load-following)
        mock_controller.reset_mock()
        agile_tariff.set_home_demand_levels(mock_controller, state, 360)  # 06:00 = 6*60
        
        # Should configure load-following parameters (not in export slot, not off-peak)
        # Either discharge or charge parameters should be set
        assert (mock_controller.setDischargeActivationPower.called or 
                mock_controller.setChargeActivationPower.called), \
               "Should configure load-following outside off-peak hours"


class TestConstants:
    """Test that constants are correctly defined."""

    def test_storage_thresholds(self, agile_tariff):
        """Test storage decision thresholds."""
        assert agile_tariff.STORAGE_FLOOR_THRESHOLD_P == config.IMPORT_LOW
        # Reference round trip: minimum harvest current -> export current.
        solar_round_trip = agile_tariff.EFFICIENCY_MODEL.round_trip_efficiency(
            agile_tariff.SOLAR_CHARGE_REFERENCE_CURRENT,
            agile_tariff.EXPORT_REFERENCE_CURRENT,
        )
        assert agile_tariff.BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL == pytest.approx(
            solar_round_trip
        )
        # The self-use value discharge is at ~3 A, not the export current, and
        # the avoided import price is reduced by the minimum profit margin.
        self_use_round_trip = agile_tariff.EFFICIENCY_MODEL.round_trip_efficiency(
            agile_tariff.SOLAR_CHARGE_REFERENCE_CURRENT,
            agile_tariff.SELF_USE_DISCHARGE_REFERENCE_CURRENT,
        )
        assert agile_tariff.SELF_USE_DISCHARGE_REFERENCE_CURRENT == pytest.approx(3)
        assert agile_tariff.MIN_PROFIT_MARGIN_P_PER_KWH == pytest.approx(0.3)
        assert agile_tariff.SELF_USE_VALUE_THRESHOLD_P == pytest.approx(
            (config.IMPORT_HIGH - agile_tariff.MIN_PROFIT_MARGIN_P_PER_KWH)
            * self_use_round_trip
        )
        assert agile_tariff.SELF_USE_VALUE_THRESHOLD_P == pytest.approx(12.64, abs=0.05)

    def test_self_use_value_rises_with_harvest_current(self, agile_tariff):
        """A strong surplus harvest is worth more than a 3 A trickle."""
        assert agile_tariff.self_use_value_threshold_p(3) == pytest.approx(
            12.64, abs=0.05
        )
        assert agile_tariff.self_use_value_threshold_p(6) == pytest.approx(
            16.96, abs=0.1
        )
        assert agile_tariff.self_use_value_threshold_p(6) > (
            agile_tariff.self_use_value_threshold_p(3)
        )

    def test_export_min_rate_includes_margin(self, agile_tariff):
        """Cheap grid -> export must cover the round trip plus the margin."""
        assert agile_tariff.export_min_rate_p() == pytest.approx(12.3, abs=0.05)

        agile_tariff.MIN_PROFIT_MARGIN_P_PER_KWH = 0.0
        assert agile_tariff.export_min_rate_p() == pytest.approx(12.0, abs=0.05)

    def test_margin_suppresses_a_marginal_cycle(self, agile_tariff):
        """A 35p future justifies 18.5p at 3 A only without a margin."""
        kicker = dict(
            current_slot=33,  # best remaining future is 35p at 18:00
            current_export_rate_p=18.5,
            soc_percent=80,  # above the Tier 2 dynamic threshold
            harvest_current_a=3,
        )

        agile_tariff.MIN_PROFIT_MARGIN_P_PER_KWH = 0.0
        assert agile_tariff._should_use_bidirectional_mode(**kicker) is True

        agile_tariff.MIN_PROFIT_MARGIN_P_PER_KWH = 2.0
        assert agile_tariff._should_use_bidirectional_mode(**kicker) is False

    def test_round_trips_are_current_dependent(self, agile_tariff):
        """A blanket 50% is wrong at both ends of the current range."""
        model = agile_tariff.EFFICIENCY_MODEL

        assert model.round_trip_efficiency(3, 16) == pytest.approx(0.547, abs=0.002)
        assert model.round_trip_efficiency(16, 16) == pytest.approx(0.833, abs=0.002)
        assert model.round_trip_efficiency(3, 3) == pytest.approx(0.426, abs=0.002)
        assert agile_tariff.BATTERY_ROUND_TRIP_EFFICIENCY == pytest.approx(0.833, abs=0.002)

    def test_export_power_and_soc_drop_follow_configured_current(self, agile_tariff):
        """A 32 A default plans the extrapolated power, not the derated limit."""
        model = agile_tariff.EFFICIENCY_MODEL

        # The committed config defaults to 32 A, so export is planned at the
        # extrapolated 32 A AC power (~7.07 kW), capped by MAX_EXPORT_POWER_KW.
        assert agile_tariff.EXPORT_REFERENCE_CURRENT == 32
        assert agile_tariff.EXPORT_POWER_KW == pytest.approx(7.068, abs=0.002)
        assert agile_tariff.STORED_TO_AC_EFFICIENCY == pytest.approx(0.943, abs=0.002)
        # 7.068 AC / 0.943 / 48.292 kWh × 0.5 h × 100 = ~7.76 %/slot
        expected_drop = (
            agile_tariff.EXPORT_POWER_KW
            / agile_tariff.STORED_TO_AC_EFFICIENCY
            * 0.5
            / agile_tariff.SOC_CAPACITY_KWH
            * 100
        )
        assert expected_drop == pytest.approx(7.76, abs=0.1)
        # And the model itself still honours the measured 16 A point.
        assert model.discharge_ac_power_kw(16) == pytest.approx(3.534, abs=0.002)

    def test_derated_16a_site_plans_measured_limit(self):
        """A physically derated unit should plan at its configured 16 A."""
        with patch.object(Config, "WALLBOX_MAX_DISCHARGE_CURRENT", 16):
            tariff = _build_tariff()

        assert tariff.EXPORT_REFERENCE_CURRENT == 16
        assert tariff.EXPORT_POWER_KW == pytest.approx(3.534, abs=0.002)
        expected_drop = (
            tariff.EXPORT_POWER_KW
            / tariff.STORED_TO_AC_EFFICIENCY
            * 0.5
            / tariff.SOC_CAPACITY_KWH
            * 100
        )
        assert expected_drop == pytest.approx(3.88, abs=0.1)

    def test_bulk_discharge_current_uses_measured_soc_rate(self, agile_tariff):
        """The old 0.46 %/h/A fudge is replaced by the measured curve."""
        # 67% -> 60% over 3 hours is 2.33 %/h, between the 3 A and 4 A points.
        current = agile_tariff.calculate_target_discharge_current(67, 960)
        assert current == pytest.approx(3.57, abs=0.1)

        # 90% -> 60% over 3 hours needs 10 %/h, above the 16 A measured rate
        # (~7.8 %/h), so it scales into the extrapolated range but stays within
        # the configured 32 A limit.
        current = agile_tariff.calculate_target_discharge_current(90, 960)
        assert current == pytest.approx(20.62, abs=0.1)
        assert current <= agile_tariff.MAX_DISCHARGE_CURRENT

        # A tiny difference below the 3 A minimum is not worth discharging.
        assert agile_tariff.calculate_target_discharge_current(60.2, 960) == 0


class TestEnergyShortfall:
    """Test the shortfall projection and conservative discharge mode."""

    @pytest.fixture(autouse=True)
    def _sixty_percent_target(self, monkeypatch):
        """Pin the target these cases describe.

        The shipped default is covered in test_config_class; keeping it out of
        these examples preserves their 55% house-floor arithmetic.
        """
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)

    @staticmethod
    def _set_time(tariff, hour, minute=0):
        fixed = datetime.now().replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        tariff.set_time_functions(datetime_func=lambda: fixed)

    def test_shortfall_true_when_projection_below_house_floor(self, agile_tariff):
        """At 12:00, 23 slots remain, so 23% is lost at 1%/slot."""
        self._set_time(agile_tariff, 12, 0)
        # 77% - 23% = 54%, below the 55% house floor (60% target - 5%).
        assert agile_tariff._update_energy_shortfall(77) is True
        assert agile_tariff._energy_shortfall is True
        assert agile_tariff._discharge_policy == "idle"
        assert agile_tariff._house_discharge_inhibited is True

    def test_no_shortfall_when_projection_meets_the_target(self, agile_tariff):
        """At 22:00 only 3 slots remain, so 3% is lost."""
        self._set_time(agile_tariff, 22, 0)
        # 70% - 3% = 67%, above the 60% bulk target.
        assert agile_tariff._update_energy_shortfall(70) is False
        assert agile_tariff._energy_shortfall is False
        assert agile_tariff._discharge_policy == "aggressive"

    def test_projection_at_house_floor_is_conservative(self, agile_tariff):
        """Exactly on the house floor still allows conservative coverage."""
        self._set_time(agile_tariff, 12, 0)
        # 78% - 23% = 55%, exactly the floor.
        assert agile_tariff._update_energy_shortfall(78) is True
        assert agile_tariff._energy_shortfall is True
        assert agile_tariff._discharge_policy == "conservative"
        assert agile_tariff._house_discharge_inhibited is False

    def test_target_headroom_is_conservative(self, agile_tariff):
        """A projection between the floor and the target is conservative."""
        self._set_time(agile_tariff, 12, 0)
        # 80% - 23% = 57%: below the 60% bulk target but above the 55% floor.
        assert agile_tariff._update_energy_shortfall(80) is True
        assert agile_tariff._energy_shortfall is True
        assert agile_tariff._discharge_policy == "conservative"

    def test_house_floor_can_be_configured(self, agile_tariff, monkeypatch):
        """The margin is configuration, not a hidden constant."""
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 10)
        # 59% - 3% = 56%, below the 60% target but above the 50% floor.
        self._set_time(agile_tariff, 22, 0)
        assert agile_tariff._house_load_floor_soc() == 50
        assert agile_tariff._update_energy_shortfall(59) is True
        assert agile_tariff._discharge_policy == "conservative"

        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 0)
        assert agile_tariff._house_load_floor_soc() == 60
        assert agile_tariff._update_energy_shortfall(59) is True
        assert agile_tariff._discharge_policy == "idle"

    def test_house_floor_is_clamped_to_the_absolute_minimum(
        self, agile_tariff, monkeypatch
    ):
        """A low target plus a large margin must not floor below the reserve."""
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 25)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 10)
        assert agile_tariff._house_load_floor_soc() == 25

    def test_negative_margin_is_treated_as_zero(self, agile_tariff, monkeypatch):
        """Bad configuration must not raise the floor above the target."""
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", -5)
        assert agile_tariff._house_load_floor_soc() == 60

    def test_unknown_soc_clears_shortfall(self, agile_tariff):
        """An unknown SoC should not leave a stale shortfall flag."""
        agile_tariff._energy_shortfall = True
        assert agile_tariff._update_energy_shortfall(-1) is False
        assert agile_tariff._energy_shortfall is False

    def test_plan_recalculation_updates_shortfall(self, agile_tariff):
        """The flag is refreshed whenever the export plan is calculated."""
        self._set_time(agile_tariff, 12, 0)
        agile_tariff.calculate_export_plan(77)
        assert agile_tariff._energy_shortfall is True

    def test_export_planning_still_uses_the_bulk_target(
        self, agile_tariff, monkeypatch
    ):
        """The flexibility margin must not add export slots."""
        self._set_time(agile_tariff, 12, 0)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 0)
        target_plan = agile_tariff.calculate_export_plan(100)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 50)
        flexible_plan = agile_tariff.calculate_export_plan(100)

        assert target_plan
        assert flexible_plan == target_plan

    def test_get_control_state_bidirectional_in_shortfall(self, agile_tariff):
        """Shortfall should force bidirectional mode, even if export is otherwise better."""
        self._set_time(agile_tariff, 12, 0)
        # 50% against a 40% stage-1 floor leaves little headroom, so the pack
        # is still in conservative mode rather than inhibited.
        state = create_test_state(50)

        control_state, _, _, reason = agile_tariff.get_control_state(state, 12 * 60)

        assert control_state == ControlState.LOAD_FOLLOW_BIDIRECTIONAL
        assert "Shortfall" in reason

    def test_get_control_state_inhibits_discharge_below_absolute_floor(
        self, agile_tariff, monkeypatch
    ):
        """A projection below the absolute minimum stops discharge entirely."""
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)
        self._set_time(agile_tariff, 12, 0)
        # The stage-1 planner treats ordinary slots as idle, so the running
        # minimum is the current SoC.  At 30% the pack is above the 25%
        # physical reserve but below the configured 40% absolute floor.
        state = create_test_state(30)

        control_state, _, _, reason = agile_tariff.get_control_state(state, 12 * 60)

        assert control_state == ControlState.LOAD_FOLLOW_BIDIRECTIONAL
        assert "absolute minimum" in reason.lower()
        snapshot = agile_tariff._decision_snapshot
        assert snapshot.decision_tier == "absolute_floor"
        assert snapshot.discharge_min_a == 0
        assert snapshot.discharge_max_a == 0

    def test_get_control_state_stays_bidirectional_when_not_shortfall(self, agile_tariff):
        """Outside shortfall the self-use branch still allows solar charging."""
        self._set_time(agile_tariff, 22, 0)
        state = create_test_state(80)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 22 * 60)

        # At 22:00 with 11p current and 10p best future, 10p × solar round trip
        # (~0.547) < 11p, so storing for a later sale is not worth it.  The
        # pack still covers the house, and 11p is below the 12.6p self-use
        # value at 3 A, so surplus solar is worth keeping.
        assert control_state == ControlState.LOAD_FOLLOW_BIDIRECTIONAL
        snapshot = agile_tariff._decision_snapshot
        assert snapshot.mode.startswith("Charge from surplus ≥3 A")
        assert snapshot.charge_min_a == pytest.approx(3.0)

    def test_shortfall_configures_conservative_discharge(self, agile_tariff):
        """Shortfall must use 720W activation and -0.5A bias."""
        mock_controller = Mock()
        mock_controller.use_new_current_calculation = False
        state = create_test_state(50)

        agile_tariff._energy_shortfall = True
        agile_tariff.set_home_demand_levels(mock_controller, state, 12 * 60)

        mock_controller.setDischargeActivationPower.assert_called_with(720)
        mock_controller.setDischargeCurrentBias.assert_called_with(-0.5)
        mock_controller.setChargeActivationPower.assert_called_with(720)
        mock_controller.setChargeCurrentBias.assert_called_with(-0.5)

    def test_shortfall_sells_surplus_when_export_is_worth_more(
        self, agile_tariff
    ):
        """A high export price must close the conservative solar charge path."""
        TestDecisionSnapshot._set_time(agile_tariff, 12, 0)
        TestDecisionSnapshot._pin_plan(
            agile_tariff, 24, 30, shortfall=True
        )
        TestStageOneExportBudget._set_rates(
            agile_tariff, {24: 19.9}, default=5.0
        )
        controller = Mock()
        controller.use_new_current_calculation = False
        state = create_test_state(30)

        agile_tariff.get_control_state(state, 12 * 60)
        snapshot = agile_tariff._decision_snapshot

        assert snapshot.mode.startswith(
            "Do not charge — surplus worth more exported"
        )
        assert snapshot.charge_min_a == pytest.approx(0.0)
        agile_tariff.set_home_demand_levels(controller, state, 12 * 60)
        controller.setChargeCurrentRange.assert_called_with(0.0, 0.0)
        controller.setChargeActivationPower.assert_called_with(0.0)

    def test_no_shortfall_configures_aggressive_discharge(self, agile_tariff):
        """Outside shortfall the aggressive bias is used, at the break-even."""
        mock_controller = Mock()
        mock_controller.use_new_current_calculation = False
        state = create_test_state(80)

        # Slot 24 (12:00) exports at 9p and no better uncommitted slot is
        # ahead, so the ledger cost sets the hurdle.  The activation must be
        # the break-even load, not the old blanket 1 W.
        TestStageOneExportBudget._set_rates(
            agile_tariff, {24: 9.0}, default=0.0
        )
        expected = agile_tariff.discharge_activation_threshold_w(9.0)

        agile_tariff._energy_shortfall = False
        agile_tariff.set_home_demand_levels(mock_controller, state, 12 * 60)

        assert 0 < expected < 720
        mock_controller.setDischargeActivationPower.assert_called_with(expected)
        mock_controller.setDischargeCurrentBias.assert_called_with(0.5)


class TestSessionAwareSocProjection:
    """Session slots replace the load-follow baseline in the 23:30 projection."""

    @pytest.fixture(autouse=True)
    def _sixty_percent_target(self, monkeypatch):
        """Pin the 60% target used by these projection examples."""
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)

    @staticmethod
    def _set_time(tariff, hour, minute=0):
        fixed = datetime.now().replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        tariff.set_time_functions(datetime_func=lambda: fixed)
        return fixed

    @staticmethod
    def _store(tariff, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        tariff.session_store = store
        return store

    def test_projection_without_sessions_is_the_load_follow_baseline(
        self, agile_tariff, tmp_path
    ):
        self._store(agile_tariff, tmp_path)
        self._set_time(agile_tariff, 12, 0)

        assert agile_tariff._project_soc_at_2330(77, 24) == pytest.approx(
            77 - 23 * agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT
        )

    def test_free_import_slots_raise_the_projection(
        self, agile_tariff, tmp_path
    ):
        store = self._store(agile_tariff, tmp_path)
        start = self._set_time(agile_tariff, 12, 0)
        store.add(
            EnergySession(
                FREE_IMPORT,
                start.replace(hour=18, minute=0),
                start.replace(hour=19, minute=0),
            )
        )
        agile_tariff.FULL_CHARGE_SLOT_SOC_GAIN_PERCENT = 5.0

        # Two free slots replace the 1% loss with a 5% gain each.
        expected = 77 - 23 * 1.0 + 2 * (5.0 + 1.0)

        assert agile_tariff._project_soc_at_2330(77, 24) == pytest.approx(
            expected
        )
        # Without the session 77% projects to 54%, below the 55% house floor.
        assert agile_tariff._update_energy_shortfall(77) is False
        assert agile_tariff._projected_soc_at_2330 == pytest.approx(expected)

    def test_grid_support_slots_use_the_export_loss(
        self, agile_tariff, tmp_path
    ):
        store = self._store(agile_tariff, tmp_path)
        start = self._set_time(agile_tariff, 12, 0)
        store.add(
            EnergySession(
                GRID_SUPPORT,
                start.replace(hour=18, minute=0),
                start.replace(hour=19, minute=0),
            )
        )
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 5.0

        # Two grid-support slots lose 5% each instead of the 1% baseline.
        expected = 77 - 23 * 1.0 - 2 * (5.0 - 1.0)

        assert agile_tariff._project_soc_at_2330(77, 24) == pytest.approx(
            expected
        )
        assert agile_tariff._update_energy_shortfall(77) is True

    def test_mixed_sessions_combine_charge_and_export(
        self, agile_tariff, tmp_path
    ):
        store = self._store(agile_tariff, tmp_path)
        start = self._set_time(agile_tariff, 12, 0)
        store.add(
            EnergySession(FREE_IMPORT, start, start + timedelta(hours=1))
        )
        store.add(
            EnergySession(
                GRID_SUPPORT,
                start.replace(hour=18, minute=0),
                start.replace(hour=19, minute=0),
            )
        )
        agile_tariff.FULL_CHARGE_SLOT_SOC_GAIN_PERCENT = 4.0
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 5.0

        expected = 77 - 23 * 1.0 + 2 * (4.0 + 1.0) - 2 * (5.0 - 1.0)

        assert agile_tariff._project_soc_at_2330(77, 24) == pytest.approx(
            expected
        )

    def test_free_import_gain_is_derived_when_unset(self, agile_tariff):
        agile_tariff.FULL_CHARGE_SLOT_SOC_GAIN_PERCENT = 0
        current = agile_tariff.GRID_CHARGE_REFERENCE_CURRENT
        stored_kw = (
            agile_tariff.EFFICIENCY_MODEL.charge_ac_power_kw(current)
            * agile_tariff.EFFICIENCY_MODEL.charge_efficiency(current)
        )
        expected = stored_kw * 0.5 / agile_tariff.SOC_CAPACITY_KWH * 100

        assert agile_tariff._full_charge_slot_soc_gain_percent() == pytest.approx(
            expected
        )
        assert expected > agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT

    def test_free_import_gain_stops_at_the_charge_cap(
        self, agile_tariff, tmp_path
    ):
        store = self._store(agile_tariff, tmp_path)
        start = self._set_time(agile_tariff, 12, 0)
        store.add(
            EnergySession(FREE_IMPORT, start, start + timedelta(hours=1))
        )
        agile_tariff.FULL_CHARGE_SLOT_SOC_GAIN_PERCENT = 5.0
        cap = agile_tariff.charge_cap_percent()

        # The first free slot reaches the cap; the second cannot add more.
        expected = cap - 21 * agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT

        assert agile_tariff._project_soc_at_2330(cap - 2, 24) == pytest.approx(
            expected
        )

    def test_export_plan_uses_the_session_aware_projection(
        self, agile_tariff, tmp_path
    ):
        """A free session creates headroom that the planner can spend."""
        store = self._store(agile_tariff, tmp_path)
        start = self._set_time(agile_tariff, 12, 0)
        agile_tariff.FULL_CHARGE_SLOT_SOC_GAIN_PERCENT = 5.0

        # At the 25% absolute minimum there is no headroom for an export
        # before the later free-import window.  The running-minimum check must
        # not let the later charge pay for an earlier dip.
        assert agile_tariff.calculate_export_plan(25) == []

        store.add(
            EnergySession(
                FREE_IMPORT,
                start.replace(hour=18, minute=0),
                start.replace(hour=19, minute=0),
            )
        )

        # The two free slots lift the pack to 35%, so a later priority slot
        # fits while the pre-charge peaks are still rejected.
        plan = agile_tariff.calculate_export_plan(25)
        assert plan
        assert all(slot >= 38 for slot in plan)

    def test_grid_support_slots_are_not_planned_twice(
        self, agile_tariff, tmp_path
    ):
        store = self._store(agile_tariff, tmp_path)
        start = self._set_time(agile_tariff, 12, 0)
        store.add(
            EnergySession(
                GRID_SUPPORT,
                start.replace(hour=18, minute=0),
                start.replace(hour=19, minute=0),
            )
        )
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 2.0

        plan = agile_tariff.calculate_export_plan(100)

        assert plan
        # 18:00–19:00 carries the best rates, but those two slots already
        # export for grid support and their drain is in the projection.
        assert 36 not in plan
        assert 37 not in plan

    def test_away_slots_are_left_out_of_the_projection(
        self, agile_tariff, tmp_path
    ):
        store = self._store(agile_tariff, tmp_path)
        start = self._set_time(agile_tariff, 12, 0)
        store.add(
            EnergySession(
                EV_AWAY,
                start.replace(hour=18, minute=0),
                start.replace(hour=19, minute=0),
            )
        )

        # 23 remaining slots minus the two away slots still lose 1% each.
        expected = 77 - 21 * agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT

        assert agile_tariff._project_soc_at_2330(77, 24) == pytest.approx(
            expected
        )
        # Without the away window the projection is 54%, below the 55% floor.
        # With the away window it is 56%: above the house floor but below the
        # 60% bulk target, so it is conservative rather than short.
        assert agile_tariff._update_energy_shortfall(77) is True
        assert agile_tariff._discharge_policy == "conservative"
        assert agile_tariff._projected_soc_at_2330 == pytest.approx(expected)

    def test_away_slots_are_not_export_targets(self, agile_tariff, tmp_path):
        store = self._store(agile_tariff, tmp_path)
        start = self._set_time(agile_tariff, 12, 0)
        store.add(
            EnergySession(
                EV_AWAY,
                start.replace(hour=18, minute=0),
                start.replace(hour=19, minute=0),
            )
        )
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 2.0

        plan = agile_tariff.calculate_export_plan(100)
        slot, rate = agile_tariff._get_best_unused_export_slot_rate(24)

        assert plan
        # 18:00–19:00 carries the best rates, but the EV is planned away.
        assert 36 not in plan
        assert 37 not in plan
        assert slot != 36
        assert slot == 35
        assert rate == pytest.approx(32.0)

    def test_away_slot_does_not_override_the_normal_heuristic(
        self, agile_tariff, tmp_path
    ):
        """Away is a planning hint; connected control still runs normally."""
        store = self._store(agile_tariff, tmp_path)
        start = self._set_time(agile_tariff, 12, 0)
        store.add(EnergySession(EV_AWAY, start, start + timedelta(hours=1)))

        control_state, _, _, _ = agile_tariff.get_control_state(
            create_test_state(79), 12 * 60
        )

        snapshot = agile_tariff._decision_snapshot
        assert agile_tariff._session_at(start).kind == EV_AWAY
        assert snapshot.decision_tier not in ("free", "grid_support")
        # The import price is the normal band, not the free-session 0p.
        assert agile_tariff._current_import_rate_p(12 * 60) == pytest.approx(
            config.IMPORT_HIGH
        )
        assert control_state is not None


class TestDischargeActivation:
    """The discharge activation threshold is the economic break-even load.

    The Wallbox cannot discharge below 3 A (~720 W).  Starting for a small
    house load therefore exports the difference, so the activation must be
    high enough that the avoided import pays for the forced export.
    """

    @staticmethod
    def _ledger(tariff, dc_kwh, cost_gbp):
        tariff._bank_dc_kwh = dc_kwh
        tariff._bank_cost_gbp = cost_gbp

    def test_threshold_matches_the_break_even_formula(self, agile_tariff):
        # 1 GBP for 10 DC kWh = 10p per DC kWh.
        self._ledger(agile_tariff, dc_kwh=10.0, cost_gbp=1.0)
        delivered_p = 10.0 / agile_tariff.EFFICIENCY_MODEL.discharge_efficiency(
            agile_tariff.MIN_DISCHARGE_CURRENT
        )
        hurdle_p = delivered_p + agile_tariff.MIN_PROFIT_MARGIN_P_PER_KWH
        expected = round(
            720.0 * (hurdle_p - 5.0) / (30.0 - 5.0)
        )

        assert agile_tariff.discharge_activation_threshold_w(
            5.0, import_rate_p=30.0
        ) == pytest.approx(expected, abs=1)

    def test_any_import_starts_when_export_covers_the_hurdle(self, agile_tariff):
        """If the forced export alone clears the hurdle, do not wait."""
        self._ledger(agile_tariff, dc_kwh=1.0, cost_gbp=0.0)  # free energy

        assert agile_tariff.discharge_activation_threshold_w(
            1.0, import_rate_p=30.0
        ) == 0

    def test_never_starts_when_stored_energy_costs_more_than_import(
        self, agile_tariff
    ):
        # 50p per DC kWh, before discharge losses and margin.
        self._ledger(agile_tariff, dc_kwh=1.0, cost_gbp=0.50)

        threshold = agile_tariff.discharge_activation_threshold_w(
            0.0, import_rate_p=30.0
        )

        assert threshold == agile_tariff.NEVER_DISCHARGE_ACTIVATION_W
        assert agile_tariff._activation_note(threshold).startswith("will not run")

    def test_expensive_stored_energy_raises_the_threshold(self, agile_tariff):
        self._ledger(agile_tariff, dc_kwh=1.0, cost_gbp=0.10)
        cheap = agile_tariff.discharge_activation_threshold_w(
            0.0, import_rate_p=30.0
        )
        self._ledger(agile_tariff, dc_kwh=1.0, cost_gbp=0.20)
        dear = agile_tariff.discharge_activation_threshold_w(
            0.0, import_rate_p=30.0
        )

        assert dear > cheap

    def test_export_price_lowers_the_threshold(self, agile_tariff):
        self._ledger(agile_tariff, dc_kwh=1.0, cost_gbp=0.08)
        at_zero = agile_tariff.discharge_activation_threshold_w(
            0.0, import_rate_p=30.0
        )
        at_eight = agile_tariff.discharge_activation_threshold_w(
            8.0, import_rate_p=30.0
        )

        assert 0 < at_eight < at_zero < 720

    def test_negative_export_price_raises_the_threshold(self, agile_tariff):
        """Paying to export means the forced excess hurts more, not less."""
        self._ledger(agile_tariff, dc_kwh=1.0, cost_gbp=0.08)
        at_zero = agile_tariff.discharge_activation_threshold_w(
            0.0, import_rate_p=30.0
        )
        at_minus_five = agile_tariff.discharge_activation_threshold_w(
            -5.0, import_rate_p=30.0
        )

        assert at_minus_five > at_zero

    def test_activation_scan_steps_up_to_4a_and_5a(self, agile_tariff):
        """Higher-current efficiency can make 4 A or 5 A pay when 3 A cannot.

        The old closed form priced the forced output at the 3 A efficiency and
        could miss the step where a whole-amp increase makes the load
        economic.  The scan evaluates the integer current at each load.
        """
        self._ledger(agile_tariff, dc_kwh=10.0, cost_gbp=2.20)
        at_22, _, current_22 = agile_tariff._aggressive_activation_scan(
            0.0,
            import_rate_p=30.0,
            alternative_rate_p=0.0,
            voltage=240.0,
        )
        assert current_22 >= 4
        assert at_22 >= 720

        self._ledger(agile_tariff, dc_kwh=10.0, cost_gbp=2.35)
        at_235, _, current_235 = agile_tariff._aggressive_activation_scan(
            0.0,
            import_rate_p=30.0,
            alternative_rate_p=0.0,
            voltage=240.0,
        )
        assert current_235 >= 5
        assert at_235 > at_22

    def test_uncommitted_export_alternative_raises_the_gate(
        self, agile_tariff, monkeypatch
    ):
        """A better unplanned slot is a real alternative for the energy."""
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        self._ledger(agile_tariff, dc_kwh=10.0, cost_gbp=1.0)  # 10p DC

        # No better slot ahead: the cheap ledger cost sets a modest gate.
        TestStageOneExportBudget._set_rates(
            agile_tariff, {24: 5.0}, default=0.0
        )
        without_better = agile_tariff._aggressive_slot_gate(
            24, planned_slots=set()
        )
        assert 0 < without_better["activation_w"] < 720

        # The same slot with a 26p uncommitted export ahead: spending the
        # energy now forgoes that sale, so the gate rises above 720 W.
        TestStageOneExportBudget._set_rates(
            agile_tariff, {24: 5.0, 36: 26.0}, default=0.0
        )
        with_better = agile_tariff._aggressive_slot_gate(
            24, planned_slots=set()
        )
        assert with_better["alternative_rate_p"] == pytest.approx(26.0)
        assert with_better["activation_w"] > without_better["activation_w"]
        assert with_better["activation_w"] > 720
        assert with_better["activation_current_a"] > (
            without_better["activation_current_a"]
        )

    def test_unaffordable_stored_energy_never_load_follows(self, agile_tariff):
        """H above import is a job for the export planner, not load-following."""
        self._ledger(agile_tariff, dc_kwh=1.0, cost_gbp=0.50)

        assert agile_tariff.discharge_activation_threshold_w(
            45.0, import_rate_p=30.0
        ) == agile_tariff.NEVER_DISCHARGE_ACTIVATION_W

    def test_conservative_mode_never_starts_below_the_minimum(
        self, agile_tariff
    ):
        """Conservative mode refuses to force an export at the threshold."""
        self._ledger(agile_tariff, dc_kwh=10.0, cost_gbp=1.0)
        aggressive = agile_tariff.discharge_activation_threshold_w(
            0.0, import_rate_p=30.0, aggressive=True
        )
        conservative = agile_tariff.discharge_activation_threshold_w(
            0.0, import_rate_p=30.0, aggressive=False
        )

        assert aggressive < 720
        assert conservative == 720

    def test_voltage_scales_the_minimum_output(self, agile_tariff):
        self._ledger(agile_tariff, dc_kwh=1.0, cost_gbp=0.10)
        at_240 = agile_tariff.discharge_activation_threshold_w(
            5.0, import_rate_p=30.0, voltage=240.0
        )
        at_230 = agile_tariff.discharge_activation_threshold_w(
            5.0, import_rate_p=30.0, voltage=230.0
        )

        assert at_230 == pytest.approx(at_240 * 230.0 / 240.0, abs=1)

    def test_small_load_stays_off_and_affordable_load_starts(
        self, agile_tariff
    ):
        """End-to-end with the controller's own activation comparison."""
        self._ledger(agile_tariff, dc_kwh=0.0, cost_gbp=0.0)
        threshold = agile_tariff.discharge_activation_threshold_w(
            0.0, import_rate_p=30.0
        )
        assert 0 < threshold < 720

        def current_for(house_w):
            return EvseController._compute_wallbox_current(
                home_power=house_w,
                discharge_current_min=3,
                discharge_current_max=32,
                minimum_discharge_activation_power=threshold,
                discharge_current_bias=0.5,
                charge_current_min=3,
                charge_current_max=16,
                minimum_charge_activation_power=720,
                charge_current_bias=-0.5,
                grid_voltage=240,
            )

        # A 30 W base load must not force the 3 A minimum and export 690 W.
        assert current_for(30) == 0
        assert current_for(threshold - 1) == 0
        # Above the break-even the clamp to 3 A is economic.
        assert current_for(threshold + 1) == -3
        assert current_for(480) == -3


class TestEdgeCases:
    """Test edge cases and boundary conditions."""
    
    def test_no_agile_rates(self, agile_tariff):
        """No rates means every export price is 0p, so the normal tier logic stores."""
        agile_tariff.agile_rates = []

        # The rate lookup itself defaults to 0p; there is no special branch.
        assert agile_tariff._current_export_rate_p(20) == 0.0
        assert agile_tariff._get_best_unused_export_rate(20) == 0.0

        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=20,
            current_export_rate_p=agile_tariff._current_export_rate_p(20),
            soc_percent=50
        )

        assert result is True, "0p export price should store via Tier 1"

    def test_stale_rates_are_treated_as_zero_price(self, agile_tariff):
        """Yesterday's prices must not be used after a failed fetch."""
        yesterday = datetime.now() - timedelta(days=1)
        for rate_data in agile_tariff.agile_rates:
            rate_data['start'] = rate_data['start'].replace(
                year=yesterday.year, month=yesterday.month, day=yesterday.day
            )
            rate_data['end'] = rate_data['end'].replace(
                year=yesterday.year, month=yesterday.month, day=yesterday.day
            )

        assert agile_tariff._today_rates() == []
        assert agile_tariff._current_export_rate_p(20) == 0.0
        assert agile_tariff._should_use_bidirectional_mode(
            current_slot=20,
            current_export_rate_p=0.0,
            soc_percent=50
        ) is True

    def test_no_rates_plans_no_exports(self, agile_tariff):
        """The export planner sees 0p everywhere, so no slot is profitable."""
        agile_tariff.agile_rates = []
        assert agile_tariff.calculate_export_plan(80) == []

    def test_no_rates_stores_instead_of_exporting(self, agile_tariff):
        """An API outage must not silently put the controller into export."""
        fixed = datetime.now().replace(hour=22, minute=0, second=0, microsecond=0)
        agile_tariff.set_time_functions(datetime_func=lambda: fixed)
        agile_tariff.agile_rates = []
        state = create_test_state(80)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 22 * 60)

        assert control_state == ControlState.LOAD_FOLLOW_BIDIRECTIONAL

    def test_negative_export_prices_store_and_plan_nothing(self, agile_tariff):
        """Agile can go negative; paying to export must never be chosen."""
        for rate_data in agile_tariff.agile_rates:
            rate_data['rate'] = -5.0

        assert agile_tariff._should_use_bidirectional_mode(
            current_slot=20, current_export_rate_p=-5.0, soc_percent=50
        ) is True
        assert agile_tariff.calculate_export_plan(80) == []

    def test_dashboard_renders_with_flat_zero_rates(self, agile_tariff):
        """All-zero rates collapse the colour range; that must not divide by 0."""
        for rate_data in agile_tariff.agile_rates:
            rate_data['rate'] = 0.0

        html = agile_tariff.get_dashboard_html()

        assert "agile-rate-strip" in html
        assert "ioctgo-decision-line" in html
    
    def test_all_slots_already_exporting(self, agile_tariff):
        """Test when all future slots are already planned for export."""
        # Mark all slots 0-46 as export slots
        agile_tariff._planned_export_slots = list(range(47))

        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=20,
            current_export_rate_p=10.0,
            soc_percent=50
        )

        # At slot 20, remaining slots = 47 - 20 = 27
        # Dynamic threshold = 60% + (27 × 1.0%) = 87%
        # SoC 50% < 87%, and rate 10p < SELF_USE_VALUE_THRESHOLD_P (self-use threshold)
        # So Tier 2 applies - should store even if all slots are exporting
        # (the plan should be recalculated)
        assert result is True, "Should store via Tier 2 when SoC below dynamic threshold"
    
    def test_current_slot_is_best_rate(self, agile_tariff):
        """Test when current slot has the best rate of the day."""
        # At 18:00 with 35p rate (peak)
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=36,  # 18:00-18:30
            current_export_rate_p=35.0,
            soc_percent=80
        )
        
        # All future rates are lower, so export now
        assert result is False, "Should export when current rate is best"


class TestDecisionSnapshot:
    """The dashboard snapshot must explain the mode and the reason."""

    @staticmethod
    def _set_time(tariff, hour, minute=0):
        fixed = datetime.now().replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        tariff.set_time_functions(datetime_func=lambda: fixed)
        return fixed

    @staticmethod
    def _pin_plan(tariff, slot, soc, shortfall=False, house_floor=False):
        """Stop get_control_state() recalculating the plan for this test."""
        tariff._last_plan_slot = slot
        tariff._last_plan_soc = soc
        tariff._prev_battery_level = soc
        tariff._planned_export_slots = []
        tariff._plan_cache = []
        tariff._energy_shortfall = shortfall
        tariff._discharge_policy = (
            "idle"
            if house_floor
            else ("conservative" if shortfall else "aggressive")
        )
        tariff._house_discharge_inhibited = house_floor
        tariff._discharge_inhibited = False

    def test_off_peak_charges_to_the_effective_cap(self, agile_tariff):
        self._set_time(agile_tariff, 23, 45)
        state = create_test_state(50)

        control_state, min_current, max_current, _ = agile_tariff.get_control_state(
            state, 23 * 60 + 45
        )

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.CHARGE
        # The range must be explicit so a stale 3 A range from the SoC-unknown
        # branch cannot survive a repeated CHARGE decision.
        assert min_current == agile_tariff.MAX_CHARGE_CURRENT
        assert max_current == agile_tariff.MAX_CHARGE_CURRENT
        assert snapshot.mode.startswith("Max charge")
        assert "charging to the" in snapshot.reason
        assert snapshot.efficiency_label == "charge"
        assert snapshot.min_gain_p == pytest.approx(
            agile_tariff.MIN_PROFIT_MARGIN_P_PER_KWH
        )

        # The displayed gain is the policy spread, not the larger
        # efficiency-adjusted break-even spread on the input price.
        assert agile_tariff.minimum_round_trip_saving_grid_p() > snapshot.min_gain_p
        html = agile_tariff.get_dashboard_html()
        assert "min gain 0.3p/kWh" in html
        assert "min gain 2.3p/kWh" not in html

    def test_off_peak_idles_at_the_cap(self, agile_tariff):
        self._set_time(agile_tariff, 23, 45)
        state = create_test_state(95)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 23 * 60 + 45)

        assert control_state == ControlState.DORMANT
        assert "cap" in agile_tariff._decision_snapshot.mode

    def test_unknown_soc_mode(self, agile_tariff):
        self._set_time(agile_tariff, 12, 0)
        state = create_test_state(-1)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 12 * 60)

        assert control_state == ControlState.CHARGE
        assert agile_tariff._decision_snapshot.mode == "Charge at 3 A (SoC unknown)"

    def test_depleted_battery_mode(self, agile_tariff):
        self._set_time(agile_tariff, 12, 0)
        state = create_test_state(20)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 12 * 60)

        assert control_state == ControlState.DORMANT
        assert agile_tariff._decision_snapshot.mode == "Idle — battery protection"

    def test_export_slot_mode_is_export_priority(self, agile_tariff):
        self._set_time(agile_tariff, 18, 0)
        self._pin_plan(agile_tariff, 36, 80)
        agile_tariff._planned_export_slots = [36]
        agile_tariff._plan_cache = [36]
        state = create_test_state(80)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 18 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.DISCHARGE
        assert snapshot.mode.startswith("Max discharge")
        assert "export priority" in snapshot.mode
        assert snapshot.decision_tier == "export_priority"
        assert "clears the" in snapshot.reason
        assert "18:00–18:30" in snapshot.reason
        assert snapshot.efficiency_label == "discharge"

    def test_export_slot_below_break_even_is_not_priority(self, agile_tariff):
        self._set_time(agile_tariff, 19, 30)
        self._pin_plan(agile_tariff, 39, 80)
        agile_tariff._planned_export_slots = [39]
        agile_tariff._plan_cache = [39]
        state = create_test_state(80)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 19 * 60 + 30)

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.DISCHARGE
        assert "aggressive" in snapshot.mode
        assert snapshot.decision_tier == "planned_export"

    def test_shortfall_mode_is_conservative(self, agile_tariff):
        self._set_time(agile_tariff, 12, 0)
        self._pin_plan(agile_tariff, 24, 30, shortfall=True)
        state = create_test_state(30)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 12 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.LOAD_FOLLOW_BIDIRECTIONAL
        assert "conservative discharge" in snapshot.mode
        assert "conserving" in snapshot.reason
        assert snapshot.discharge_aggressive is False
        assert snapshot.discharge_activation_w == 720

    def test_dashboard_shows_the_discharge_activation(self, agile_tariff):
        self._set_time(agile_tariff, 12, 0)
        self._pin_plan(agile_tariff, 24, 30, shortfall=True)
        state = create_test_state(30)

        agile_tariff.get_control_state(state, 12 * 60)
        html = agile_tariff.get_dashboard_html()

        assert "discharge ≥720 W" in html

    def test_house_floor_reason_names_the_floor(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        self._set_time(agile_tariff, 12, 0)
        self._pin_plan(
            agile_tariff, 24, 30, shortfall=True, house_floor=True
        )
        agile_tariff._projected_soc_at_2330 = 54
        state = create_test_state(30)

        agile_tariff.get_control_state(state, 12 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert "55% house floor" in snapshot.reason
        assert "no house discharge" in snapshot.reason
        assert "House floor 55%" in snapshot.detail
        assert "aggressive floor 60%" in snapshot.detail
        assert snapshot.decision_tier == "house_floor"

    def test_house_floor_reason_floors_the_projection(
        self, agile_tariff, monkeypatch
    ):
        """54.6% must read as 54% against a 55% floor, not 55% vs 55%."""
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        self._set_time(agile_tariff, 12, 0)
        self._pin_plan(
            agile_tariff, 24, 30, shortfall=True, house_floor=True
        )
        agile_tariff._projected_soc_at_2330 = 54.6
        state = create_test_state(30)

        agile_tariff.get_control_state(state, 12 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert "54% < 55% house floor" in snapshot.reason
        assert "no house discharge" in snapshot.reason
        assert "55% < 55%" not in snapshot.reason
        assert "projected 23:30 SoC 54%" in snapshot.detail

    def test_bidirectional_mode_is_aggressive(self, agile_tariff):
        self._set_time(agile_tariff, 10, 0)
        self._pin_plan(agile_tariff, 20, 80)
        state = create_test_state(80)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 10 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.LOAD_FOLLOW_BIDIRECTIONAL
        assert "aggressive discharge" in snapshot.mode
        assert snapshot.discharge_aggressive is True
        assert snapshot.efficiency_label in ("round trip", "solar → house")

    def test_self_use_mode_charges_above_the_efficiency_floor(self, agile_tariff):
        self._set_time(agile_tariff, 22, 0)
        self._pin_plan(agile_tariff, 44, 80)
        state = create_test_state(80)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 22 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.LOAD_FOLLOW_BIDIRECTIONAL
        assert snapshot.mode.startswith("Charge from surplus ≥3 A")
        assert "aggressive discharge ≥3 A" in snapshot.mode
        assert snapshot.charge_min_a == pytest.approx(3.0)
        assert snapshot.discharge_min_a == pytest.approx(3.0)

    def test_charge_floor_follows_the_efficiency_curve(self, agile_tariff):
        """The floor is the first measured current that beats export now.

        Efficiency rises with current, so the hurdle is monotonic.  These
        values come from the measured Leaf/Quasar table at the configured
        30p peak import price, not from a fixed constant.
        """
        assert agile_tariff._minimum_harvest_charge_current_a(11.0) == pytest.approx(3.0)
        assert agile_tariff._minimum_harvest_charge_current_a(13.0) == pytest.approx(4.0)
        assert agile_tariff._minimum_harvest_charge_current_a(18.0) == pytest.approx(8.0)
        assert agile_tariff._minimum_harvest_charge_current_a(19.0) == pytest.approx(12.0)
        # The best measured point is worth ~19.25p, so a 19.9p export wins.
        assert agile_tariff._minimum_harvest_charge_current_a(19.9) is None

    def test_self_use_charge_range_is_computed_for_the_case(self, agile_tariff):
        """A 13p export sets a 4 A floor; it is not the old 3 A default."""
        TestPlannedExportSlotExplanation._set_two_peak_rates(
            agile_tariff, now_slot=24, now_rate=13.0,
            later_slot=32, later_rate=5.0, default=5.0,
        )
        self._pin_plan(agile_tariff, 24, 88)
        controller = Mock()
        controller.use_new_current_calculation = False
        state = create_test_state(88)

        agile_tariff.get_control_state(state, 12 * 60)
        agile_tariff.set_home_demand_levels(controller, state, 12 * 60)

        controller.setChargeCurrentRange.assert_called_with(
            4.0, agile_tariff.MAX_CHARGE_CURRENT
        )
        controller.setChargeActivationPower.assert_called_with(4.0 * 240)
        assert agile_tariff._decision_snapshot.charge_min_a == pytest.approx(4.0)

    def test_no_suitable_current_closes_the_charge_range(self, agile_tariff):
        """No stale charge range: when export wins, the range is set to zero."""
        TestPlannedExportSlotExplanation._set_two_peak_rates(
            agile_tariff, now_slot=24, now_rate=19.9,
            later_slot=32, later_rate=5.0, default=5.0,
        )
        self._pin_plan(agile_tariff, 24, 88)
        controller = Mock()
        controller.use_new_current_calculation = False
        state = create_test_state(88)

        agile_tariff.get_control_state(state, 12 * 60)
        agile_tariff.set_home_demand_levels(controller, state, 12 * 60)

        controller.setChargeCurrentRange.assert_called_with(0.0, 0.0)
        controller.setChargeActivationPower.assert_called_with(0.0)
        snapshot = agile_tariff._decision_snapshot
        assert snapshot.charge_min_a == pytest.approx(0.0)
        assert snapshot.charge_max_a == pytest.approx(0.0)

    def test_no_store_detail_names_the_committed_slot(self, agile_tariff):
        """The hover text explains why a strong surplus was not stored."""
        self._set_time(agile_tariff, 12, 0)
        self._pin_plan(agile_tariff, 24, 88)
        agile_tariff._planned_export_slots = [32]
        agile_tariff._plan_cache = [32]
        TestPlannedExportSlotExplanation._set_two_peak_rates(agile_tariff)
        controller = Mock()
        controller.chargeCurrentBias = -0.5
        controller.current_grid_power = Power(
            ch1Watts=-1440, ch2Watts=0, voltage=240
        )
        agile_tariff.evseController = controller
        state = create_test_state(88)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 12 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.LOAD_FOLLOW_BIDIRECTIONAL
        assert snapshot.mode.startswith("Do not charge — surplus worth more exported")
        assert snapshot.charge_min_a == pytest.approx(0.0)
        assert snapshot.charge_max_a == pytest.approx(0.0)
        assert "30.5p" in snapshot.reason
        assert "committed" in snapshot.reason
        assert "harvest estimate 6 A" in snapshot.detail
        assert "30.5p 16:00–16:30 slot is already committed" in snapshot.detail
        assert "best unused slot 21.4p" in snapshot.detail

    def test_floor_gates_charge_activation_instead_of_importing(
        self, agile_tariff, monkeypatch
    ):
        """The 09:30 case: 19.8p now, 30.4p later, 32.26p peak import.

        Charging can pay off either by keeping the solar for the house or by
        storing it for the 30.4p slot.  The self-use floor alone is 9 A, but
        the store break-even is 5 A, so 5 A is the effective onset.  A 3 A
        surplus must not activate a 5 A charge and import the difference at
        the peak price.
        """
        monkeypatch.setattr(config, "IMPORT_HIGH", 32.26)
        TestPlannedExportSlotExplanation._set_two_peak_rates(
            agile_tariff, now_slot=19, now_rate=19.8,
            later_slot=36, later_rate=30.4, default=5.0,
        )
        self._pin_plan(agile_tariff, 19, 79)
        controller = Mock()
        controller.use_new_current_calculation = False
        controller.chargeCurrentBias = -0.5
        controller.current_grid_power = Power(
            ch1Watts=85, ch2Watts=0, voltage=243
        )
        agile_tariff.evseController = controller
        state = create_test_state(79)

        agile_tariff.get_control_state(state, 9 * 60 + 30)

        snapshot = agile_tariff._decision_snapshot
        # The floor is armed at 5 A, but the live 3 A surplus cannot reach it,
        # so the dashboard must not imply the car is charging now.
        assert snapshot.mode.startswith(
            "Do not charge yet — will charge from ≥5 A surplus"
        )
        assert snapshot.charge_min_a == pytest.approx(5.0)
        assert snapshot.charge_activation_w == pytest.approx(5 * 240)
        assert "Below 5 A the surplus is worth more exported" in snapshot.detail
        # The components are still reported/derivable independently.
        assert agile_tariff._minimum_harvest_charge_current_a(19.8) == pytest.approx(9.0)
        assert agile_tariff._minimum_store_harvest_current_a(30.4, 19.8) == pytest.approx(5.0)

        def charge_at(home_power):
            return EvseController._compute_wallbox_current(
                home_power=home_power,
                discharge_current_min=3,
                discharge_current_max=16,
                minimum_discharge_activation_power=0,
                discharge_current_bias=0.5,
                charge_current_min=5,
                charge_current_max=16,
                minimum_charge_activation_power=snapshot.charge_activation_w,
                charge_current_bias=-0.5,
                grid_voltage=240,
            )

        # A 3 A surplus is below the floor, so charging stays off.
        assert charge_at(-750) == 0
        # A surplus at the floor charges at it without importing.
        assert charge_at(-1200) == pytest.approx(5.0)

    def test_armed_floor_says_not_yet_when_the_surplus_is_below_it(
        self, agile_tariff, monkeypatch
    ):
        """The 22:29 case: 19.2p now, 19.8p later, 3 A surplus, 52% SoC.

        Keeping an 8 A surplus for the house is worth 19.5p after the round
        trip, beating the 19.2p export, so the floor stays armed at 8 A.  But
        the live surplus is only 3 A, so the dashboard must say "not yet"
        rather than implying the car is charging now.
        """
        monkeypatch.setattr(config, "IMPORT_HIGH", 32.26)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 50)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        TestPlannedExportSlotExplanation._set_two_peak_rates(
            agile_tariff, now_slot=44, now_rate=19.2,
            later_slot=45, later_rate=19.8, default=5.0,
        )
        self._set_time(agile_tariff, 22, 29)
        self._pin_plan(agile_tariff, 44, 52)
        controller = Mock()
        controller.chargeCurrentBias = -0.5
        controller.current_grid_power = Power(
            ch1Watts=370, ch2Watts=0, voltage=243
        )
        agile_tariff.evseController = controller
        state = create_test_state(52)

        agile_tariff.get_control_state(state, 22 * 60 + 29)

        snapshot = agile_tariff._decision_snapshot
        assert snapshot.mode.startswith(
            "Do not charge yet — will charge from ≥8 A surplus"
        )
        assert snapshot.charge_min_a == pytest.approx(8.0)
        assert snapshot.charge_activation_w == pytest.approx(8 * 240)
        # The tooltip explains both sides of the 8 A threshold in plain words.
        assert "Below 8 A the surplus is worth more exported" in snapshot.detail
        assert "19.5p" in snapshot.detail
        assert "19.2p" in snapshot.detail
        # No internal tier names in anything the user can see.
        for text in (snapshot.mode, snapshot.reason, snapshot.detail):
            assert "Tier" not in text
            assert "tier" not in text

    def test_aggressive_reason_includes_the_break_even(self, agile_tariff):
        self._set_time(agile_tariff, 22, 0)
        self._pin_plan(agile_tariff, 44, 80)
        agile_tariff._current_discharge_activation_w = Mock(return_value=250)
        state = create_test_state(80)

        agile_tariff.get_control_state(state, 22 * 60)

        reason = agile_tariff._decision_snapshot.reason
        assert "starts at ≥250 W house import" in reason
        assert "forced output would export more than it saves" in reason

    def test_aggressive_reason_explains_a_zero_break_even(self, agile_tariff):
        self._set_time(agile_tariff, 10, 0)
        self._pin_plan(agile_tariff, 20, 80)
        agile_tariff._current_discharge_activation_w = Mock(return_value=0)
        state = create_test_state(80)

        agile_tariff.get_control_state(state, 10 * 60)

        reason = agile_tariff._decision_snapshot.reason
        assert "Any import starts discharge" in reason
        assert "export alone clears the stored cost" in reason

    def test_aggressive_reason_explains_a_disabled_discharge(self, agile_tariff):
        self._set_time(agile_tariff, 22, 0)
        self._pin_plan(agile_tariff, 44, 80)
        agile_tariff._current_discharge_activation_w = Mock(
            return_value=agile_tariff.NEVER_DISCHARGE_ACTIVATION_W
        )
        state = create_test_state(80)

        agile_tariff.get_control_state(state, 22 * 60)

        reason = agile_tariff._decision_snapshot.reason
        assert "Discharge is disabled" in reason
        assert "worth more in its best alternative use" in reason

    def test_soc_at_cap_stops_solar_charging(self, agile_tariff):
        """At the cap the mode says so, and no charge range is offered."""
        self._set_time(agile_tariff, 10, 0)
        self._pin_plan(agile_tariff, 20, 95)
        state = create_test_state(95)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 10 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.LOAD_FOLLOW_BIDIRECTIONAL
        assert "SoC at" in snapshot.mode
        assert "cap" in snapshot.mode
        assert snapshot.charge_min_a == 0
        assert snapshot.charge_max_a == 0


class TestDecisionExplanation:
    """The /decision data must teach the calculation, not ask for trust."""

    @staticmethod
    def _flat(explanation):
        parts = [explanation.get("mode", ""), explanation.get("reason", "")]
        parts.append(explanation.get("detail", ""))
        for row in explanation.get("state", []):
            parts.extend((row["label"], row["value"]))
        for step in explanation.get("steps", []):
            parts.extend(
                (
                    step["title"],
                    step["question"],
                    step["calculation"],
                    step["result"],
                    step["conclusion"],
                )
            )
        for key in ("heading", "summary"):
            parts.append(explanation["answer"][key])
        for key in ("charge", "discharge"):
            parts.extend(
                (
                    explanation["answer"][key]["headline"],
                    explanation["answer"][key]["because"],
                )
            )
        ledger = explanation.get("ledger") or {}
        for row in ledger.get("rows", []):
            parts.extend((row["label"], row["value"]))
        for step in ledger.get("steps", []):
            parts.extend(
                (
                    step["title"],
                    step["question"],
                    step["calculation"],
                    step["result"],
                    step["conclusion"],
                )
            )
        efficiency = explanation.get("efficiency") or {}
        parts.append(efficiency.get("note", ""))
        parts.extend(explanation.get("assumptions", []))
        return " ".join(str(part) for part in parts)

    def _run_2229(self, agile_tariff, monkeypatch):
        monkeypatch.setattr(config, "IMPORT_HIGH", 32.26)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 50)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        TestPlannedExportSlotExplanation._set_two_peak_rates(
            agile_tariff, now_slot=44, now_rate=19.2,
            later_slot=45, later_rate=19.8, default=5.0,
        )
        TestDecisionSnapshot._set_time(agile_tariff, 22, 29)
        TestDecisionSnapshot._pin_plan(agile_tariff, 44, 52)
        controller = Mock()
        controller.chargeCurrentBias = -0.5
        controller.current_grid_power = Power(
            ch1Watts=370, ch2Watts=0, voltage=243
        )
        agile_tariff.evseController = controller
        state = create_test_state(52)
        agile_tariff.get_control_state(state, 22 * 60 + 29)
        return agile_tariff.get_decision_explanation()

    def test_2229_explanation_names_both_sides_of_the_floor(
        self, agile_tariff, monkeypatch
    ):
        """8 A is armed because self-use is worth 19.5p vs 19.2p exported.

        The live 3 A surplus is below the floor, so the answer must say the
        car is not charging yet.  No internal tier language may leak out.
        """
        explanation = self._run_2229(agile_tariff, monkeypatch)
        text = self._flat(explanation)

        assert explanation["available"] is True
        assert explanation["slot_label"] == "22:00–22:30"
        assert explanation["answer"]["heading"] == (
            "Do not charge yet; sell the current surplus"
        )
        assert "≥8 A" in explanation["answer"]["charge"]["headline"]
        assert "Not yet" in explanation["answer"]["charge"]["headline"]
        assert "19.5p" in text
        assert "19.2p" in text
        assert "Answer" in text and "W" in text
        assert "Tier" not in text
        assert "tier" not in text

        dashboard_html = agile_tariff.get_dashboard_html()
        assert 'href="/decision"' in dashboard_html
        assert "Explanation" in dashboard_html

    def test_explanation_is_a_frozen_copy(self, agile_tariff, monkeypatch):
        explanation = self._run_2229(agile_tariff, monkeypatch)
        original_mode = explanation["mode"]

        agile_tariff._decision_snapshot.mode = "A newer decision"

        assert explanation["mode"] == original_mode
        assert explanation["mode"] != "A newer decision"

    def test_store_decision_explains_the_house_value(self, agile_tariff):
        TestDecisionSnapshot._set_time(agile_tariff, 10, 0)
        TestDecisionSnapshot._pin_plan(agile_tariff, 20, 80)
        state = create_test_state(80)

        agile_tariff.get_control_state(state, 10 * 60)
        assert agile_tariff._decision_snapshot.store_decision is True

        explanation = agile_tariff.get_decision_explanation()
        text = self._flat(explanation)

        assert explanation["answer"]["heading"] == "Store the solar for later"
        assert (
            "What is the surplus worth if the house uses it later?"
            in text
        )
        assert "Charge from surplus at ≥3 A" in (
            explanation["answer"]["charge"]["headline"]
        )
        assert "Tier" not in text
        assert "tier" not in text

    def test_shortfall_explanation_names_the_house_floor(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        TestDecisionSnapshot._set_time(agile_tariff, 12, 0)
        TestDecisionSnapshot._pin_plan(
            agile_tariff, 24, 30, shortfall=True
        )
        agile_tariff._projected_soc_at_2330 = 54
        state = create_test_state(30)

        agile_tariff.get_control_state(state, 12 * 60)
        explanation = agile_tariff.get_decision_explanation()
        text = self._flat(explanation)

        assert explanation["answer"]["heading"] == (
            "Conserve energy for the house"
        )
        assert "Will the battery last until 23:30?" in text
        assert "55% house floor" in text
        assert "Charge from surplus at ≥3 A" in (
            explanation["answer"]["charge"]["headline"]
        )

    def test_reserve_explanation_names_the_later_export(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 25)
        TestDecisionSnapshot._set_time(agile_tariff, 12, 0)
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 8.0
        agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = 1.0

        agile_tariff.get_control_state(create_test_state(77), 12 * 60)
        explanation = agile_tariff.get_decision_explanation()
        text = self._flat(explanation)

        assert explanation["answer"]["heading"] == (
            "Reserve the pack for high-value export"
        )
        assert "house floor" in text
        assert "house-offset break-even" in text
        assert "Do not discharge — holding for export" in (
            explanation["answer"]["discharge"]["headline"]
        )
        assert "House load is imported for now" in text

    def test_ledger_explanation_matches_the_decision_snapshot(
        self, agile_tariff
    ):
        """The ledger shown must be the one that priced the decision.

        Mutating the live ledger after the decision must not change the page.
        """
        TestDecisionSnapshot._set_time(agile_tariff, 10, 0)
        TestDecisionSnapshot._pin_plan(agile_tariff, 20, 80)
        agile_tariff._bank_dc_kwh = 10.0
        agile_tariff._bank_cost_gbp = 0.8
        agile_tariff._bank_seeded = True

        agile_tariff.get_control_state(create_test_state(80), 10 * 60)

        # A later control cycle has moved the live ledger on.
        agile_tariff._bank_dc_kwh = 40.0
        agile_tariff._bank_cost_gbp = 4.0

        explanation = agile_tariff.get_decision_explanation()
        ledger = explanation["ledger"]
        rows = {row["label"]: row["value"] for row in ledger["rows"]}
        text = self._flat(explanation)

        assert ledger["timing"] == "decision time"
        assert rows["Energy in the ledger (DC)"] == "10.00 kWh"
        assert rows["Cost booked"] == "£0.8000"
        assert rows["Stored-energy cost (in pack)"] == "8.0p/kWh"
        assert rows["Basis"] == "ledger of 10.0 kWh"
        assert rows["Seeded from SoC at startup"] == "Yes"
        # Delivered cost divides by the measured 3 A discharge efficiency.
        delivered = 8.0 / agile_tariff.EFFICIENCY_MODEL.discharge_efficiency(3)
        assert f"{delivered:.1f}p/kWh" in rows["Delivered at 3 A"]
        assert "8.0p/kWh in the pack" in text
        assert "How does charging add to the ledger?" in text
        assert "How does discharging release it?" in text

    def test_efficiency_matrix_uses_the_configured_limits(self, agile_tariff):
        efficiency = agile_tariff._efficiency_explanation()
        model = agile_tariff.EFFICIENCY_MODEL

        min_charge = max(1, round(config.WALLBOX_MIN_CHARGE_CURRENT))
        max_charge = max(min_charge, round(config.WALLBOX_MAX_CHARGE_CURRENT))
        min_discharge = max(
            1, round(config.WALLBOX_MIN_DISCHARGE_CURRENT)
        )
        max_discharge = max(
            min_discharge, round(config.WALLBOX_MAX_DISCHARGE_CURRENT)
        )

        assert len(efficiency["charge_rows"]) == max_charge - min_charge + 1
        assert len(efficiency["discharge_columns"]) == (
            max_discharge - min_discharge + 1
        )
        assert efficiency["charge_rows"][0]["current"] == f"{min_charge} A"
        assert efficiency["charge_rows"][-1]["current"] == f"{max_charge} A"
        assert efficiency["discharge_columns"][-1]["current"] == (
            f"{max_discharge} A"
        )
        assert len(efficiency["matrix"]) == len(efficiency["charge_rows"])
        assert all(
            len(row) == len(efficiency["discharge_columns"])
            for row in efficiency["matrix"]
        )

        charge_3 = model.charge_efficiency(3)
        discharge_3 = model.discharge_efficiency(3)
        assert efficiency["charge_rows"][0]["efficiency"] == (
            f"{charge_3 * 100:.0f}%"
        )
        assert efficiency["discharge_columns"][0]["efficiency"] == (
            f"{discharge_3 * 100:.0f}%"
        )
        assert efficiency["matrix"][0][0]["value"] == (
            f"{charge_3 * discharge_3 * 100:.0f}%"
        )

        # Above the measured ceiling the model holds the last efficiency, so
        # a 32 A row/column is a planning value, not a new measurement.
        measured_max = round(model.max_current_a)
        assert f"measured from {model.min_current_a:.0f} A to " in (
            efficiency["note"]
        )
        if max(max_charge, max_discharge) > measured_max:
            assert (
                f"Above {measured_max} A the efficiency is held"
                in efficiency["note"]
            )
            assert model.charge_efficiency(max_charge) == (
                model.charge_efficiency(measured_max)
            )

    def test_decision_template_renders_without_auto_refresh(
        self, agile_tariff, monkeypatch
    ):
        from pathlib import Path
        from jinja2 import Environment, FileSystemLoader

        explanation = self._run_2229(agile_tariff, monkeypatch)
        templates = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "evse_controller"
            / "templates"
        )
        env = Environment(
            loader=FileSystemLoader(str(templates)), autoescape=True
        )
        env.globals["url_for"] = lambda endpoint, **kwargs: f"/{endpoint}"
        html = env.get_template("decision.html").render(**explanation)

        assert "Frozen at 22:29:00" in html
        assert "Refresh snapshot" in html
        assert "19.5p" in html
        assert "19.2p" in html
        assert "The stored-energy ledger" in html
        assert "How the ledger is calculated" in html
        assert "Efficiency at each current" in html
        assert 'http-equiv="refresh"' not in html
        assert "setInterval" not in html
        assert "Tier" not in html


class TestChargeCap:
    """Charging cannot exceed 97% even if the config asks for more."""

    def test_cap_is_limited_to_97(self):
        original = config.MAX_CHARGE_PERCENT
        try:
            config.MAX_CHARGE_PERCENT = 100
            tariff = _build_tariff()
            assert tariff.charge_cap_percent() == 97
        finally:
            config.MAX_CHARGE_PERCENT = original

    def test_cap_can_be_lowered_by_config(self):
        original = config.MAX_CHARGE_PERCENT
        try:
            config.MAX_CHARGE_PERCENT = 80
            tariff = _build_tariff()
            assert tariff.charge_cap_percent() == 80
        finally:
            config.MAX_CHARGE_PERCENT = original

    def test_solar_charge_range_stops_at_cap(self, agile_tariff):
        """Surplus solar must not keep charging a pack that is at the cap."""
        controller = Mock()
        controller.use_new_current_calculation = False
        agile_tariff._planned_export_slots = []
        agile_tariff._energy_shortfall = False

        agile_tariff.set_home_demand_levels(
            controller, create_test_state(50), 10 * 60
        )
        controller.setChargeCurrentRange.assert_called_with(
            3, agile_tariff.MAX_CHARGE_CURRENT
        )

        controller.reset_mock()
        agile_tariff.set_home_demand_levels(
            controller, create_test_state(95), 10 * 60
        )
        controller.setChargeCurrentRange.assert_called_with(0, 0)

    def test_shortfall_charge_range_stops_at_cap(self, agile_tariff):
        controller = Mock()
        controller.use_new_current_calculation = False
        agile_tariff._planned_export_slots = []
        agile_tariff._energy_shortfall = True

        agile_tariff.set_home_demand_levels(
            controller, create_test_state(95), 12 * 60
        )

        controller.setChargeCurrentRange.assert_called_with(0, 0)


class TestMinimumRoundTripSaving:
    """The tooltip can state the efficiency-adjusted break-even spread.

    This is the extra spread needed on the input price to cover the
    round-trip loss and still earn the configured margin.  It is not the
    displayed ``min gain``, which is the margin itself.
    """

    def test_grid_spread_includes_efficiency_and_margin(self, agile_tariff):
        expected = (
            config.IMPORT_LOW / agile_tariff.BATTERY_ROUND_TRIP_EFFICIENCY
            + agile_tariff.MIN_PROFIT_MARGIN_P_PER_KWH
            - config.IMPORT_LOW
        )
        assert agile_tariff.minimum_round_trip_saving_grid_p() == pytest.approx(
            expected
        )
        assert agile_tariff.minimum_round_trip_saving_grid_p() == pytest.approx(
            2.3, abs=0.05
        )

    def test_solar_spread_is_just_the_margin(self, agile_tariff):
        assert agile_tariff.minimum_round_trip_saving_solar_p() == pytest.approx(
            agile_tariff.MIN_PROFIT_MARGIN_P_PER_KWH
        )


class TestDailySavings:
    """Saved today is the price spread captured by cycling stored energy."""

    @staticmethod
    def _start(tariff, hour, minute=0):
        clock = [datetime.now().replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )]
        tariff.set_time_functions(datetime_func=lambda: clock[0])
        controller = Mock()
        controller.chargeCurrentBias = -0.5
        tariff.evseController = controller
        return clock

    @staticmethod
    def _prime(tariff, clock, ch1, ch2, soc=None):
        power = Power(ch1Watts=ch1, ch2Watts=ch2, voltage=240)
        if soc is not None:
            power.soc = soc
        tariff.evseController.current_grid_power = power
        tariff._savings_last_time = None
        tariff._accumulate_daily_savings()

    @staticmethod
    def _run(tariff, clock, minutes, ch1, ch2, soc=None):
        power = Power(ch1Watts=ch1, ch2Watts=ch2, voltage=240)
        if soc is not None:
            power.soc = soc
        tariff.evseController.current_grid_power = power
        clock[0] = clock[0] + timedelta(minutes=minutes)
        tariff._accumulate_daily_savings()

    def test_off_peak_charge_then_daytime_discharge(self, agile_tariff):
        """A 7.62p charge displacing 32.26p import saves the difference/η."""
        clock = self._start(agile_tariff, 2, 0)

        # House 300 W plus 3.6 kW charging, for 30 minutes.
        self._prime(agile_tariff, clock, ch1=3900, ch2=3600)
        self._run(agile_tariff, clock, 30, ch1=3900, ch2=3600)
        assert agile_tariff._saved_today_gbp == pytest.approx(0.0)
        assert agile_tariff._bank_dc_kwh > 0

        # Midday: the EVSE covers the whole 300 W house load for one minute.
        clock[0] = clock[0].replace(hour=12, minute=0)
        self._prime(agile_tariff, clock, ch1=0, ch2=-300)
        self._run(agile_tariff, clock, 1, ch1=0, ch2=-300)

        delivered_kwh = 0.3 / 60.0
        eta_chg = agile_tariff.EFFICIENCY_MODEL.charge_efficiency(
            3600 / 240.0
        )
        eta_dis = agile_tariff.EFFICIENCY_MODEL.discharge_efficiency(
            300 / 240.0
        )
        expected = delivered_kwh * (
            config.IMPORT_HIGH - config.IMPORT_LOW / (eta_chg * eta_dis)
        ) / 100.0
        assert agile_tariff._saved_today_gbp == pytest.approx(
            expected, rel=0.01
        )

    def test_charge_is_booked_when_the_energy_is_used(self, agile_tariff):
        """Charging is an asset, not a loss, until the energy comes back out."""
        clock = self._start(agile_tariff, 2, 0)

        self._prime(agile_tariff, clock, ch1=3900, ch2=3600)
        self._run(agile_tariff, clock, 30, ch1=3900, ch2=3600)

        assert agile_tariff._saved_today_gbp == pytest.approx(0.0)
        assert agile_tariff._bank_cost_gbp > 0

    def test_solar_basis_is_the_export_price(self, agile_tariff):
        """Harvested solar is costed at what exporting it would have earned."""
        clock = self._start(agile_tariff, 12, 0)
        current_slot = 24

        # 2 kW surplus, charging at 1 kW for two minutes.
        self._prime(agile_tariff, clock, ch1=-1000, ch2=1000)
        self._run(agile_tariff, clock, 2, ch1=-1000, ch2=1000)

        self._prime(agile_tariff, clock, ch1=0, ch2=-300)
        self._run(agile_tariff, clock, 1, ch1=0, ch2=-300)

        export_rate_p = agile_tariff._current_export_rate_p(current_slot)
        delivered_kwh = 0.3 / 60.0
        eta_chg = agile_tariff.EFFICIENCY_MODEL.charge_efficiency(
            1000 / 240.0
        )
        eta_dis = agile_tariff.EFFICIENCY_MODEL.discharge_efficiency(
            300 / 240.0
        )
        expected = delivered_kwh * (
            config.IMPORT_HIGH - export_rate_p / (eta_chg * eta_dis)
        ) / 100.0
        assert agile_tariff._saved_today_gbp == pytest.approx(
            expected, rel=0.01
        )

    def test_export_beyond_the_house_earns_the_agile_rate(self, agile_tariff):
        clock = self._start(agile_tariff, 2, 0)

        self._prime(agile_tariff, clock, ch1=3900, ch2=3600)
        self._run(agile_tariff, clock, 30, ch1=3900, ch2=3600)
        cost_per_dc_kwh = (
            agile_tariff._bank_cost_gbp / agile_tariff._bank_dc_kwh
        )

        clock[0] = clock[0].replace(hour=18, minute=0)
        self._prime(agile_tariff, clock, ch1=-3300, ch2=-3600)
        self._run(agile_tariff, clock, 1, ch1=-3300, ch2=-3600)

        export_rate_p = agile_tariff._current_export_rate_p(36)
        delivered_kwh = 3.6 / 60.0
        house_kwh = 0.3 / 60.0
        avoided = (
            house_kwh * config.IMPORT_HIGH
            + (delivered_kwh - house_kwh) * export_rate_p
        ) / 100.0
        eta_dis = agile_tariff.EFFICIENCY_MODEL.discharge_efficiency(
            3600 / 240.0
        )
        released_cost = delivered_kwh / eta_dis * cost_per_dc_kwh
        assert agile_tariff._saved_today_gbp == pytest.approx(
            avoided - released_cost, rel=0.01
        )

    def test_solar_at_zero_price_is_worth_nothing_to_export(self, agile_tariff):
        """Zero-price solar exported at zero price must book exactly £0.

        No cash changed hands, so there is no operational loss: the ledger
        cost is zero because the displaced export was worth zero.
        """
        clock = self._start(agile_tariff, 12, 0)
        for rate_data in agile_tariff.agile_rates:
            rate_data['rate'] = 0.0

        # 2 kW surplus, charging at 1 kW for five minutes.
        self._prime(agile_tariff, clock, ch1=-1000, ch2=1000)
        self._run(agile_tariff, clock, 5, ch1=-1000, ch2=1000)
        assert agile_tariff._bank_cost_gbp == pytest.approx(0.0)
        assert agile_tariff._bank_dc_kwh > 0.03

        # Export 1 kW at 0p for one minute, with no house load to offset.
        clock[0] = clock[0].replace(hour=18, minute=0)
        self._prime(agile_tariff, clock, ch1=-1000, ch2=-1000)
        self._run(agile_tariff, clock, 1, ch1=-1000, ch2=-1000)

        assert agile_tariff._saved_today_gbp == pytest.approx(0.0)

    def test_zero_export_price_books_a_loss_on_export(self, agile_tariff):
        """Exporting bought energy at 0p loses the money spent on it."""
        clock = self._start(agile_tariff, 2, 0)

        self._prime(agile_tariff, clock, ch1=3900, ch2=3600)
        self._run(agile_tariff, clock, 30, ch1=3900, ch2=3600)

        for rate_data in agile_tariff.agile_rates:
            rate_data['rate'] = 0.0

        clock[0] = clock[0].replace(hour=18, minute=0)
        self._prime(agile_tariff, clock, ch1=-3300, ch2=-3600)
        self._run(agile_tariff, clock, 1, ch1=-3300, ch2=-3600)

        assert agile_tariff._saved_today_gbp < 0

    def test_saved_today_rate_tracks_recent_discharge(self, agile_tariff):
        """The dashboard can show how fast saved-today is moving."""
        clock = self._start(agile_tariff, 2, 0)
        self._prime(agile_tariff, clock, ch1=3900, ch2=3600)
        self._run(agile_tariff, clock, 30, ch1=3900, ch2=3600)

        clock[0] = clock[0].replace(hour=12, minute=0)
        self._prime(agile_tariff, clock, ch1=0, ch2=-300)
        saved_before = agile_tariff._saved_today_gbp
        self._run(agile_tariff, clock, 1, ch1=0, ch2=-300)

        gained = agile_tariff._saved_today_gbp - saved_before
        assert gained > 0
        assert agile_tariff._savings_rate_gbp_per_hour == pytest.approx(
            gained * 60.0, rel=0.01
        )
        assert agile_tariff._format_savings_rate().startswith("↑")

    def test_ledger_survives_midnight_but_daily_total_resets(self, agile_tariff):
        clock = self._start(agile_tariff, 23, 0)

        self._prime(agile_tariff, clock, ch1=3900, ch2=3600)
        self._run(agile_tariff, clock, 30, ch1=3900, ch2=3600)
        bank_dc = agile_tariff._bank_dc_kwh
        bank_cost = agile_tariff._bank_cost_gbp
        assert bank_dc > 0

        clock[0] = clock[0] + timedelta(days=1)
        agile_tariff._savings_last_time = None
        agile_tariff._accumulate_daily_savings()

        assert agile_tariff._saved_today_gbp == pytest.approx(0.0)
        assert agile_tariff._bank_dc_kwh == pytest.approx(bank_dc)
        assert agile_tariff._bank_cost_gbp == pytest.approx(bank_cost)

    def test_first_discharge_prices_unknown_energy_at_off_peak(
        self, agile_tariff
    ):
        """Energy already in the pack has no purchase price.

        It is not treated as free: the ledger falls back to the off-peak
        rate, so the first discharge books the import it avoids minus that
        conservative cost.  At 30p import that is still a saving, but a
        smaller one than a charge the ledger actually saw.
        """
        clock = self._start(agile_tariff, 12, 0)
        assert agile_tariff._bank_dc_kwh == 0.0

        # The EVSE covers the whole 300 W house load for one minute.
        self._prime(agile_tariff, clock, ch1=0, ch2=-300)
        self._run(agile_tariff, clock, 1, ch1=0, ch2=-300)

        delivered_kwh = 0.3 / 60.0
        eta_chg = agile_tariff.EFFICIENCY_MODEL.charge_efficiency(
            agile_tariff.GRID_CHARGE_REFERENCE_CURRENT
        )
        eta_dis = agile_tariff.EFFICIENCY_MODEL.discharge_efficiency(
            300 / 240.0
        )
        expected = delivered_kwh * (
            config.IMPORT_HIGH - config.IMPORT_LOW / (eta_chg * eta_dis)
        ) / 100.0
        assert agile_tariff._saved_today_gbp == pytest.approx(
            expected, rel=0.01
        )
        # The cost basis is the fallback, not zero.
        assert agile_tariff._delivered_cost_per_kwh_p() == pytest.approx(
            config.IMPORT_LOW / (eta_chg * eta_dis), rel=0.01
        )

    def test_startup_transient_is_not_booked(self, agile_tariff):
        """The Wallbox's power-up draw is not priced into the ledger."""
        clock = self._start(agile_tariff, 22, 40)
        self._prime(agile_tariff, clock, ch1=300, ch2=0, soc=50)
        bank_before = agile_tariff._bank_dc_kwh
        saved_before = agile_tariff._saved_today_gbp

        # It powers up drawing ~200 W before it can discharge.
        self._run(agile_tariff, clock, 0.5, ch1=500, ch2=200, soc=50)

        assert agile_tariff._bank_dc_kwh == pytest.approx(bank_before)
        assert agile_tariff._saved_today_gbp == pytest.approx(saved_before)
        assert (
            agile_tariff.discharge_activation_threshold_w(
                8.0, import_rate_p=config.IMPORT_HIGH
            )
            < agile_tariff.NEVER_DISCHARGE_ACTIVATION_W
        )

    def test_steady_operation_is_booked_after_startup(self, agile_tariff):
        """Once the settle window has passed, normal charging is recorded."""
        clock = self._start(agile_tariff, 22, 40)
        self._prime(agile_tariff, clock, ch1=300, ch2=0, soc=50)
        bank_before = agile_tariff._bank_dc_kwh

        # First sample starts the 21.9 s window and is discarded.
        self._run(agile_tariff, clock, 0.5, ch1=500, ch2=200, soc=50)
        # Thirty seconds later the Wallbox is active and the energy counts.
        self._run(agile_tariff, clock, 0.5, ch1=500, ch2=200, soc=50)

        assert agile_tariff._bank_dc_kwh > bank_before

    def test_shutdown_transient_is_not_booked(self, agile_tariff):
        """The power-down draw is not booked as a charge."""
        clock = self._start(agile_tariff, 12, 0)
        self._prime(agile_tariff, clock, ch1=0, ch2=-300, soc=50)
        # Move past the startup settle so the discharge is recorded.
        self._run(agile_tariff, clock, 1, ch1=0, ch2=-300, soc=50)
        bank_after = agile_tariff._bank_dc_kwh
        saved_after = agile_tariff._saved_today_gbp

        # Stopping draws ~200 W the other way for a few seconds.
        self._run(agile_tariff, clock, 0.1, ch1=300, ch2=200, soc=50)

        assert agile_tariff._bank_dc_kwh == pytest.approx(bank_after)
        assert agile_tariff._saved_today_gbp == pytest.approx(saved_after)

    def test_current_change_within_a_direction_is_not_delayed(
        self, agile_tariff
    ):
        """A larger discharge while already discharging books immediately."""
        clock = self._start(agile_tariff, 12, 0)
        self._prime(agile_tariff, clock, ch1=0, ch2=-300, soc=50)
        self._run(agile_tariff, clock, 1, ch1=0, ch2=-300, soc=50)
        bank_after = agile_tariff._bank_dc_kwh

        self._run(agile_tariff, clock, 0.5, ch1=0, ch2=-700, soc=50)

        assert agile_tariff._bank_dc_kwh < bank_after

    def test_startup_and_shutdown_use_the_wallbox_delays(self, agile_tariff):
        clock = self._start(agile_tariff, 12, 0)
        self._prime(agile_tariff, clock, ch1=0, ch2=0, soc=50)

        self._run(agile_tariff, clock, 0.1, ch1=0, ch2=-700, soc=50)
        assert (
            agile_tariff._ledger_settle_until - clock[0]
        ).total_seconds() == pytest.approx(
            agile_tariff.LEDGER_STARTUP_SETTLE_SECONDS
        )

        # Move past the startup window, then stop.
        self._run(agile_tariff, clock, 1, ch1=0, ch2=-700, soc=50)
        self._run(agile_tariff, clock, 0.1, ch1=0, ch2=0, soc=50)
        assert (
            agile_tariff._ledger_settle_until - clock[0]
        ).total_seconds() == pytest.approx(
            agile_tariff.LEDGER_SHUTDOWN_SETTLE_SECONDS
        )

    def test_pack_energy_at_restart_is_valued_at_the_fallback(
        self, agile_tariff
    ):
        """A restart does not make the existing charge look free or dear."""
        clock = self._start(agile_tariff, 22, 40)
        self._prime(agile_tariff, clock, ch1=300, ch2=0, soc=50)

        assert agile_tariff._bank_dc_kwh == pytest.approx(
            0.5 * agile_tariff.SOC_CAPACITY_KWH
        )
        eta_dis = agile_tariff.EFFICIENCY_MODEL.discharge_efficiency(
            agile_tariff.MIN_DISCHARGE_CURRENT
        )
        assert agile_tariff._delivered_cost_per_kwh_p() == pytest.approx(
            agile_tariff._bank_fallback_cost_per_dc_kwh() * 100.0 / eta_dis
        )

    def test_stored_cost_reports_pack_and_delivered_basis(
        self, agile_tariff
    ):
        """The pack cost and the delivered cost are both shown.

        A 7.6p/kWh charge at 88% charge efficiency puts energy in the pack at
        about 8.6p/kWh.  The discharge threshold needs the cost per kWh
        delivered, which adds the discharge loss on top.
        """
        agile_tariff._bank_dc_kwh = 20.0
        agile_tariff._bank_cost_gbp = 20.0 * 0.086
        low_p, high_p = agile_tariff._delivered_cost_range_per_kwh_p()

        assert agile_tariff._stored_cost_per_dc_kwh_p() == pytest.approx(8.6)
        assert low_p < high_p
        assert high_p == pytest.approx(
            8.6
            / agile_tariff.EFFICIENCY_MODEL.discharge_efficiency(
                agile_tariff.MIN_DISCHARGE_CURRENT
            )
        )
        assert low_p == pytest.approx(
            8.6
            / agile_tariff.EFFICIENCY_MODEL.discharge_efficiency(
                config.WALLBOX_MAX_DISCHARGE_CURRENT
            )
        )
        assert agile_tariff._format_stored_cost() == (
            f"stored 8.6p/kWh ({low_p:.1f}–{high_p:.1f}p delivered)"
        )

    def test_tiny_high_price_sample_does_not_disable_discharge(
        self, agile_tariff
    ):
        """Regression: a transient charge must not become the whole basis.

        A 2 W positive EVSE reading for 30 s at the 30p import rate used to
        set the cost of the entire pack to ~70p/kWh delivered, so the very
        next control cycle disabled discharge even though the pack had been
        charged long before the process started.
        """
        clock = self._start(agile_tariff, 22, 40)
        self._prime(agile_tariff, clock, ch1=300, ch2=0, soc=50)
        self._run(agile_tariff, clock, 0.5, ch1=302, ch2=2, soc=50)

        threshold = agile_tariff.discharge_activation_threshold_w(
            8.0, import_rate_p=config.IMPORT_HIGH
        )

        assert threshold < agile_tariff.NEVER_DISCHARGE_ACTIVATION_W
        assert "disabled" not in agile_tariff._activation_explanation(
            threshold
        )

    def test_unknown_soc_noise_keeps_the_fallback_basis(self, agile_tariff):
        """With no SoC to seed from, a sub-threshold ledger is still noise."""
        clock = self._start(agile_tariff, 22, 40)
        self._prime(agile_tariff, clock, ch1=300, ch2=0, soc=-1)
        self._run(agile_tariff, clock, 0.5, ch1=302, ch2=2, soc=-1)

        assert agile_tariff._bank_dc_kwh < agile_tariff.MIN_LEDGER_BASIS_KWH
        assert agile_tariff._delivered_cost_per_kwh_p() == pytest.approx(
            agile_tariff._bank_fallback_cost_per_dc_kwh()
            * 100.0
            / agile_tariff.EFFICIENCY_MODEL.discharge_efficiency(
                agile_tariff.MIN_DISCHARGE_CURRENT
            )
        )

    def test_saving_is_attributed_to_the_slot_it_happened_in(
        self, agile_tariff
    ):
        """The report needs a per-slot split whose sum is the daily total."""
        clock = self._start(agile_tariff, 2, 0)
        self._prime(agile_tariff, clock, ch1=3900, ch2=3600, soc=50)
        self._run(agile_tariff, clock, 30, ch1=3900, ch2=3600, soc=50)

        clock[0] = clock[0].replace(hour=12, minute=0)
        self._prime(agile_tariff, clock, ch1=0, ch2=-300, soc=50)
        self._run(agile_tariff, clock, 1, ch1=0, ch2=-300, soc=50)

        saved = agile_tariff._saved_today_gbp
        assert saved > 0
        assert sum(agile_tariff._saved_by_slot_gbp) == pytest.approx(saved)
        assert agile_tariff.saved_for_slot(clock[0].date(), 24) == pytest.approx(
            saved
        )
        assert agile_tariff.saved_for_slot(clock[0].date(), 25) == pytest.approx(
            0.0
        )

    def test_slot_savings_reset_at_midnight(self, agile_tariff):
        old_day = datetime.now().replace(
            hour=23, minute=30, second=0, microsecond=0
        ) - timedelta(days=1)
        clock = [old_day]
        agile_tariff.set_time_functions(datetime_func=lambda: clock[0])
        controller = Mock()
        controller.chargeCurrentBias = -0.5
        agile_tariff.evseController = controller
        controller.current_grid_power = Power(
            ch1Watts=300, ch2Watts=0, voltage=240, soc=50
        )

        agile_tariff._savings_day = old_day.date()
        agile_tariff._saved_today_gbp = 0.5
        agile_tariff._saved_by_slot_gbp = [0.0] * 48
        agile_tariff._saved_by_slot_gbp[47] = 0.5
        agile_tariff._savings_last_time = None
        agile_tariff._accumulate_daily_savings()

        clock[0] = old_day + timedelta(minutes=30)
        agile_tariff._accumulate_daily_savings()

        assert agile_tariff._saved_today_gbp == pytest.approx(0.0)
        assert agile_tariff._saved_by_slot_gbp == [0.0] * 48
        assert agile_tariff.saved_for_slot(old_day.date(), 47) is None

    def test_last_slot_is_still_readable_just_after_midnight(
        self, agile_tariff
    ):
        """The report closes the previous slot before the tariff resets."""
        old_day = datetime.now().replace(
            hour=23, minute=30, second=0, microsecond=0
        ) - timedelta(days=1)
        # The clock has just passed midnight, but the tariff has not run its
        # midnight reset yet.
        now = old_day + timedelta(minutes=30, seconds=5)
        agile_tariff.set_time_functions(datetime_func=lambda: now)
        agile_tariff._savings_day = old_day.date()
        agile_tariff._saved_today_gbp = 0.5
        agile_tariff._saved_by_slot_gbp = [0.0] * 48
        agile_tariff._saved_by_slot_gbp[47] = 0.5

        assert agile_tariff.saved_for_slot(old_day.date(), 47) == pytest.approx(
            0.5
        )
        assert agile_tariff.saved_for_slot(now.date(), 47) is None

    def test_no_accumulation_yet_is_a_zero_for_today(self, agile_tariff):
        """An active tariff with no samples yet has saved nothing today."""
        now = datetime.now()
        agile_tariff.set_time_functions(datetime_func=lambda: now)
        agile_tariff._savings_day = None
        agile_tariff._saved_by_slot_gbp = [0.0] * 48

        assert agile_tariff.saved_for_slot(now.date(), 0) == 0.0
        assert agile_tariff.saved_for_slot(
            now.date() - timedelta(days=1), 0
        ) is None


class TestDashboardModeLine:
    """The compact decision line sits above the 48-slot strip."""

    def test_mode_line_precedes_the_strip(self, agile_tariff):
        agile_tariff._record_decision(
            mode="Harvest 5 A · aggressive discharge",
            reason="future 24.6p at 16:30–17:00 × 73% = 17.7p now",
            soc_percent=47,
            efficiency_pct=73,
            efficiency_label="round trip",
            min_gain_p=0.3,
        )

        html = agile_tariff.get_dashboard_html()

        mode_pos = html.find("ioctgo-decision-line")
        strip_pos = html.find("agile-rate-strip")
        assert mode_pos != -1
        assert strip_pos != -1
        assert mode_pos < strip_pos
        assert "Harvest 5 A" in html
        assert "round trip" in html
        assert "min gain 0.3p/kWh" in html
        assert "min gain 0.3p/kWh (" not in html
        assert "saved today" in html

    def test_mode_line_shows_stored_energy_cost(self, agile_tariff):
        agile_tariff._bank_dc_kwh = 10.0
        agile_tariff._bank_cost_gbp = 1.0
        agile_tariff._record_decision(
            mode="Do not charge · aggressive discharge",
            reason="covering the house",
            soc_percent=47,
        )

        html = agile_tariff.get_dashboard_html()

        low_p, high_p = agile_tariff._delivered_cost_range_per_kwh_p()
        assert (
            f"stored {agile_tariff._stored_cost_per_dc_kwh_p():.1f}p/kWh" in html
        )
        assert f"({low_p:.1f}–{high_p:.1f}p delivered)" in html
        assert "ledger of 10.0 kWh" in html

    def test_zero_activation_does_not_repeat_the_chip(self, agile_tariff):
        """The reason already says that any import starts discharge."""
        agile_tariff._record_decision(
            mode="Harvest ≥3 A · aggressive discharge",
            reason=(
                "Any import starts discharge because the export alone clears "
                "the stored cost."
            ),
            soc_percent=47,
            discharge_activation_w=0,
        )

        html = agile_tariff.get_dashboard_html()

        assert html.count("Any import starts discharge") == 1
        assert "discharge on any import" not in html

    def test_mode_line_marks_the_fallback_cost_basis(self, agile_tariff):
        agile_tariff._bank_dc_kwh = 0.0
        agile_tariff._record_decision(
            mode="Do not charge · aggressive discharge",
            reason="covering the house",
            soc_percent=47,
        )

        html = agile_tariff.get_dashboard_html()

        assert "off-peak fallback" in html

    def test_saved_today_shows_finer_resolution(self, agile_tariff):
        agile_tariff._saved_today_gbp = 0.0243
        assert agile_tariff._format_saved_today() == "£0.0243"

        agile_tariff._saved_today_gbp = -0.0243
        assert agile_tariff._format_saved_today() == "-£0.0243"

        agile_tariff._saved_today_gbp = 12.3456
        assert agile_tariff._format_saved_today() == "£12.35"

    def test_savings_rate_is_hidden_while_flat(self, agile_tariff):
        agile_tariff._savings_rate_gbp_per_hour = 0.0
        assert agile_tariff._format_savings_rate() == ""

        agile_tariff._savings_rate_gbp_per_hour = 0.224
        assert agile_tariff._format_savings_rate() == "↑22.4p/h"

        agile_tariff._savings_rate_gbp_per_hour = -0.224
        assert agile_tariff._format_savings_rate() == "↓22.4p/h"

    def test_mode_line_shows_the_savings_rate_when_moving(self, agile_tariff):
        agile_tariff._record_decision(
            mode="Harvest 5 A · aggressive discharge",
            reason="covering the house",
            soc_percent=47,
        )
        agile_tariff._saved_today_gbp = 0.0243
        agile_tariff._savings_rate_gbp_per_hour = 0.224

        html = agile_tariff.get_dashboard_html()

        assert "saved today £0.0243" in html
        assert "↑22.4p/h" in html

    def test_strip_has_room_for_the_rate_and_badge(self, agile_tariff):
        """The strip must not be squeezed: the EX badge sits above the rate."""
        agile_tariff._record_decision(
            mode="Idle — battery protection",
            reason="SoC at the reserve",
            soc_percent=20,
        )

        html = agile_tariff.get_dashboard_html()

        assert 'class="agile-rate-strip" style="height:40px' in html
        assert ".agile-rate-cell .agile-rate-badge" in html

    def test_export_plan_is_named_in_text(self, agile_tariff):
        """A planned slot is marked on the strip and named in words."""
        # The strip marks planned future slots, so pin the time before the
        # 16:00/17:00 fixtures.  A wall-clock time after those slots would
        # (correctly) render them as past and suppress the EX badge.
        TestDecisionSnapshot._set_time(agile_tariff, 9, 0)
        agile_tariff._planned_export_slots = [32, 34]
        agile_tariff._record_decision(
            mode="Do not charge · aggressive discharge",
            reason="covering the house",
            soc_percent=47,
        )

        html = agile_tariff.get_dashboard_html()

        assert 'class="agile-rate-badge"' in html
        assert ">EX</span>" in html
        assert "Export plan: 16:00–16:30 @ 18.0p" in html
        assert "17:00–17:30 @ 28.0p" in html

    def test_empty_export_plan_is_stated(self, agile_tariff):
        agile_tariff._planned_export_slots = []
        agile_tariff._record_decision(
            mode="Harvest 6 A · aggressive discharge",
            reason="storing solar",
            soc_percent=47,
        )

        html = agile_tariff.get_dashboard_html()

        assert "Export plan: none planned" in html
        assert ">EX</span>" not in html

    def test_mode_line_still_shows_while_rates_load(self, agile_tariff):
        agile_tariff.agile_rates = []
        agile_tariff._record_decision(
            mode="Max charge (32 A)",
            reason="off-peak",
            soc_percent=50,
        )

        html = agile_tariff.get_dashboard_html()

        assert "ioctgo-decision-line" in html
        assert "Max charge (32 A)" in html
        assert "Loading Agile Outgoing rates" in html



class TestDecisionLog:
    """The compact decision line is written to the log once per minute."""

    @staticmethod
    def _set_time(tariff, hour, minute, second=0):
        fixed = datetime.now().replace(
            hour=hour, minute=minute, second=second, microsecond=0
        )
        tariff.set_time_functions(datetime_func=lambda: fixed)
        return fixed

    def test_decision_summary_logs_once_per_minute(self, agile_tariff):
        self._set_time(agile_tariff, 12, 0, second=5)
        target = (
            "evse_controller.tariffs.octopus.ioctgo_with_agile_outgoing.info"
        )

        with patch(target) as log_info:
            agile_tariff._record_decision(
                mode="Harvest 5 A · aggressive discharge",
                reason="storing solar",
                soc_percent=47,
                efficiency_pct=73,
                efficiency_label="round trip",
                min_gain_p=0.3,
                harvest_current_a=6,
                discharge_activation_w=250,
            )
            # A second control cycle in the same minute must not repeat it.
            agile_tariff._record_decision(
                mode="Harvest 6 A · aggressive discharge",
                reason="still storing solar",
                soc_percent=47,
            )

            assert log_info.call_count == 1
            message = log_info.call_args[0][0]
            assert message.startswith("IOCTGO_AGILEOUT: Decision: ")
            assert "Harvest 5 A" in message
            assert "storing solar" in message
            assert "η 73% round trip" in message
            assert "min gain 0.3p/kWh" in message
            assert "min gain 0.3p/kWh (" not in message
            assert "harvest 6 A" in message
            assert "discharge ≥250 W" in message
            assert "stored " in message
            assert "saved today" in message

            # The next minute logs again, with the current decision.
            self._set_time(agile_tariff, 12, 1)
            agile_tariff._record_decision(
                mode="Harvest 6 A · aggressive discharge",
                reason="still storing solar",
                soc_percent=47,
            )

            assert log_info.call_count == 2
            assert "Harvest 6 A" in log_info.call_args[0][0]


class TestStatePersistence:
    """The ledger and today's saving survive a restart or a tariff switch."""

    @staticmethod
    def _populate(tariff, now):
        tariff.set_time_functions(datetime_func=lambda: now)
        tariff._bank_dc_kwh = 9.5
        tariff._bank_cost_gbp = 0.76
        tariff._bank_seeded = True
        tariff._savings_day = now.date()
        tariff._saved_today_gbp = 0.34
        tariff._saved_by_slot_gbp = [0.0] * 48
        tariff._saved_by_slot_gbp[24] = 0.34
        tariff._savings_last_time = now
        tariff._savings_last_baseline_w = 120.0
        tariff._savings_last_evse_w = -240.0
        tariff._ledger_direction = -1
        tariff._ledger_settle_until = now + timedelta(seconds=20)
        tariff._savings_rate_history.append((now, 0.34))
        tariff._savings_rate_gbp_per_hour = 0.12
        tariff._exported_slots = [34, 12, 34, 99]

    def test_save_then_load_restores_ledger_and_saving(self, tmp_path):
        state_path = tmp_path / "tariff-state.json"
        now = datetime.now().replace(microsecond=0)
        first = _build_tariff(state_path=state_path)
        self._populate(first, now)

        assert first.save_state() is True

        second = _build_tariff(state_path=state_path)
        assert second._bank_dc_kwh == pytest.approx(9.5)
        assert second._bank_cost_gbp == pytest.approx(0.76)
        assert second._bank_seeded is True
        assert second._savings_day == now.date()
        assert second._saved_today_gbp == pytest.approx(0.34)
        assert second._saved_by_slot_gbp[24] == pytest.approx(0.34)
        assert sum(second._saved_by_slot_gbp) == pytest.approx(0.34)
        assert second._exported_slots == [12, 34]

        # The interval across the switch must not be booked as if it had
        # just happened: the next control sample starts a fresh baseline.
        assert second._savings_last_time is None
        assert second._savings_last_baseline_w == 0.0
        assert second._savings_last_evse_w == 0.0
        assert second._ledger_direction == 0
        assert second._ledger_settle_until is None
        assert second._savings_rate_gbp_per_hour == 0.0
        assert list(second._savings_rate_history) == []

    def test_cleanup_saves_state(self, tmp_path):
        state_path = tmp_path / "tariff-state.json"
        now = datetime.now().replace(microsecond=0)
        tariff = _build_tariff(state_path=state_path)
        self._populate(tariff, now)

        tariff.cleanup()

        data = json.loads(state_path.read_text())
        assert data["version"] == tariff.STATE_VERSION
        assert data["ledger"]["dc_kwh"] == pytest.approx(9.5)
        assert data["ledger"]["cost_gbp"] == pytest.approx(0.76)
        assert data["ledger"]["seeded"] is True
        assert data["savings"]["day"] == now.date().isoformat()
        assert data["savings"]["saved_today_gbp"] == pytest.approx(0.34)
        assert data["savings"]["saved_by_slot_gbp"][24] == pytest.approx(0.34)
        assert sum(data["savings"]["saved_by_slot_gbp"]) == pytest.approx(0.34)
        assert data["exported_slots"] == [12, 34]

    def test_stale_saving_resets_but_the_ledger_survives(self, tmp_path):
        state_path = tmp_path / "tariff-state.json"
        yesterday = datetime.now().date() - timedelta(days=1)
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "saved_at": f"{yesterday.isoformat()}T23:59:00",
                    "ledger": {
                        "dc_kwh": 7.25,
                        "cost_gbp": 0.58,
                        "seeded": True,
                    },
                    "savings": {
                        "day": yesterday.isoformat(),
                        "saved_today_gbp": 0.41,
                    },
                    "exported_slots": [30, 31],
                }
            )
        )

        tariff = _build_tariff(state_path=state_path)

        # The ledger deliberately crosses midnight, so it is kept.
        assert tariff._bank_dc_kwh == pytest.approx(7.25)
        assert tariff._bank_cost_gbp == pytest.approx(0.58)
        assert tariff._bank_seeded is True
        # Today's total and today's export markers start clean.
        assert tariff._savings_day == datetime.now().date()
        assert tariff._saved_today_gbp == 0.0
        assert tariff._exported_slots == []

    def test_restored_ledger_is_not_seeded_again(self, tmp_path):
        state_path = tmp_path / "tariff-state.json"
        now = datetime.now().replace(microsecond=0)
        first = _build_tariff(state_path=state_path)
        self._populate(first, now)
        first.save_state()

        second = _build_tariff(state_path=state_path)
        before_dc = second._bank_dc_kwh
        before_cost = second._bank_cost_gbp

        second._seed_bank_from_soc(80)

        assert second._bank_dc_kwh == pytest.approx(before_dc)
        assert second._bank_cost_gbp == pytest.approx(before_cost)

    def test_corrupt_state_is_ignored(self, tmp_path):
        state_path = tmp_path / "tariff-state.json"
        state_path.write_text("{this is not json")

        tariff = _build_tariff(state_path=state_path)

        assert tariff._bank_dc_kwh == 0.0
        assert tariff._bank_cost_gbp == 0.0
        assert tariff._bank_seeded is False
        assert tariff._saved_today_gbp == 0.0

    def test_negative_or_invalid_ledger_values_are_ignored(self, tmp_path):
        state_path = tmp_path / "tariff-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "ledger": {
                        "dc_kwh": -2.0,
                        "cost_gbp": "not a number",
                        "seeded": True,
                    },
                    "savings": {
                        "day": datetime.now().date().isoformat(),
                        "saved_today_gbp": 0.12,
                    },
                }
            )
        )

        tariff = _build_tariff(state_path=state_path)

        assert tariff._bank_dc_kwh == 0.0
        assert tariff._bank_cost_gbp == 0.0
        # The invalid ledger allows the startup SoC seed to run again.
        assert tariff._bank_seeded is False
        # The ledger is bad, but a valid today's saving is still usable.
        assert tariff._saved_today_gbp == pytest.approx(0.12)

    def test_missing_state_file_leaves_defaults(self, tmp_path):
        tariff = _build_tariff(state_path=tmp_path / "absent.json")

        assert tariff._bank_dc_kwh == 0.0
        assert tariff._bank_seeded is False
        assert tariff._saved_today_gbp == 0.0


class TestRateFetchState:
    """The rate cache must be usable before the first successful fetch."""

    def test_complete_rates_check_before_first_fetch(self):
        """A fresh process has no _last_fetch_date until a fetch stores one.

        The check short-circuits on an empty rate list today, but the
        attribute must still exist so externally supplied rates or a future
        reordering cannot raise AttributeError.
        """
        mock_thread = Mock()
        mock_thread.getBatteryChargeLevel = Mock(return_value=75)
        mock_thread.get_state = Mock(return_value={})

        with patch.object(
            IOctGoWithAgileOutgoingTariff, "_fetch_rates_async"
        ), patch(
            'evse_controller.drivers.evse.wallbox.wallbox_thread.WallboxThread.get_instance',
            return_value=mock_thread,
        ):
            tariff = IOctGoWithAgileOutgoingTariff()

        tariff.agile_rates = create_mock_agile_rates()

        assert tariff._last_fetch_date is None
        assert tariff._have_complete_rates_for_date(datetime.now().date()) is False


class TestEnergySessions:
    """Supplier-requested half-hour windows change the control mode."""

    @staticmethod
    def _fixed(hour, minute=0):
        return datetime.now().replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )

    @staticmethod
    def _set_up_store(tariff, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        tariff.session_store = store
        return store

    def test_free_session_charges_at_max(self, agile_tariff, tmp_path):
        store = self._set_up_store(agile_tariff, tmp_path)
        start = self._fixed(12)
        store.add(EnergySession(FREE_IMPORT, start, start + timedelta(hours=1)))
        agile_tariff.set_time_functions(datetime_func=lambda: start)

        control_state, min_current, max_current, reason = agile_tariff.get_control_state(
            create_test_state(79), 12 * 60
        )

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.CHARGE
        # Explicit max range: the free session must not inherit whatever
        # narrower range the previous mode left behind.
        assert min_current == agile_tariff.MAX_CHARGE_CURRENT
        assert max_current == agile_tariff.MAX_CHARGE_CURRENT
        assert snapshot.mode.startswith("Free energy 12:00–13:00")
        assert "free energy" in reason.lower()
        assert snapshot.current_rate_p == pytest.approx(0.0)

    def test_free_session_respects_the_charge_cap(self, agile_tariff, tmp_path):
        store = self._set_up_store(agile_tariff, tmp_path)
        start = self._fixed(12)
        store.add(EnergySession(FREE_IMPORT, start, start + timedelta(hours=1)))
        agile_tariff.set_time_functions(datetime_func=lambda: start)

        control_state, _, _, _ = agile_tariff.get_control_state(
            create_test_state(98), 12 * 60
        )

        assert control_state == ControlState.DORMANT
        assert "cap" in agile_tariff._decision_snapshot.mode.lower()

    def test_free_session_sets_the_import_price_to_zero(
        self, agile_tariff, tmp_path
    ):
        store = self._set_up_store(agile_tariff, tmp_path)
        start = self._fixed(12)
        store.add(EnergySession(FREE_IMPORT, start, start + timedelta(hours=1)))
        agile_tariff.set_time_functions(datetime_func=lambda: start)

        assert agile_tariff._current_import_rate_p(12 * 60) == pytest.approx(0.0)
        # Outside the window the normal peak band applies.
        assert agile_tariff._current_import_rate_p(14 * 60) == pytest.approx(
            config.IMPORT_HIGH
        )

    def test_free_session_books_zero_import_cost(self, agile_tariff, tmp_path):
        """The ledger must agree with the decision, not just the dashboard."""
        store = self._set_up_store(agile_tariff, tmp_path)
        start = self._fixed(12)
        store.add(EnergySession(FREE_IMPORT, start, start + timedelta(hours=1)))

        controller = Mock()
        controller.current_grid_power = Power(
            ch1Watts=3456, ch2Watts=3456, voltage=240, soc=50
        )
        agile_tariff.evseController = controller
        agile_tariff.set_time_functions(datetime_func=lambda: start)
        agile_tariff._savings_day = start.date()
        agile_tariff._savings_last_time = start - timedelta(seconds=60)
        agile_tariff._savings_last_baseline_w = 0.0
        agile_tariff._savings_last_evse_w = 0.0
        agile_tariff._ledger_settle_until = None
        agile_tariff._ledger_direction = 1
        agile_tariff._bank_dc_kwh = 0.0
        agile_tariff._bank_cost_gbp = 0.0
        agile_tariff._bank_seeded = True

        agile_tariff._accumulate_daily_savings()

        assert agile_tariff._bank_dc_kwh > 0
        assert agile_tariff._bank_cost_gbp == pytest.approx(0.0)

    def test_free_slot_is_excluded_from_the_export_plan(
        self, agile_tariff, tmp_path
    ):
        store = self._set_up_store(agile_tariff, tmp_path)
        start = self._fixed(12)
        # 18:00–19:00 carries the best rate of the day (35p).
        store.add(
            EnergySession(
                FREE_IMPORT, self._fixed(18), self._fixed(19)
            )
        )
        agile_tariff.set_time_functions(datetime_func=lambda: start)

        slot, rate = agile_tariff._get_best_unused_export_slot_rate(24)

        assert slot != 36
        assert slot == 35
        assert rate == pytest.approx(32.0)

    def test_grid_support_exports_at_max_regardless_of_price(
        self, agile_tariff, tmp_path
    ):
        store = self._set_up_store(agile_tariff, tmp_path)
        start = self._fixed(12)
        store.add(
            EnergySession(GRID_SUPPORT, start, start + timedelta(hours=1))
        )
        agile_tariff.set_time_functions(datetime_func=lambda: start)
        # Even a 0p export price must not stop a support request.
        for rate_data in agile_tariff.agile_rates:
            if rate_data["start"].hour == 12:
                rate_data["rate"] = 0.0

        control_state, _, _, reason = agile_tariff.get_control_state(
            create_test_state(79), 12 * 60
        )

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.DISCHARGE
        assert snapshot.mode.startswith("Save Energy Session 12:00–13:00")
        assert "grid support" in snapshot.reason.lower()
        assert "Save Energy Session" in reason
        assert snapshot.discharge_min_a == pytest.approx(
            agile_tariff.MAX_DISCHARGE_CURRENT
        )

    def test_grid_support_stops_at_the_protection_reserve(
        self, agile_tariff, tmp_path
    ):
        store = self._set_up_store(agile_tariff, tmp_path)
        start = self._fixed(12)
        store.add(
            EnergySession(GRID_SUPPORT, start, start + timedelta(hours=1))
        )
        agile_tariff.set_time_functions(datetime_func=lambda: start)

        control_state, _, _, _ = agile_tariff.get_control_state(
            create_test_state(agile_tariff.BATTERY_PROTECTION_RESERVE_SOC),
            12 * 60,
        )

        assert control_state == ControlState.DORMANT

    def test_sessions_skip_load_follow_configuration(
        self, agile_tariff, tmp_path
    ):
        store = self._set_up_store(agile_tariff, tmp_path)
        start = self._fixed(12)
        store.add(EnergySession(FREE_IMPORT, start, start + timedelta(hours=1)))
        agile_tariff.set_time_functions(datetime_func=lambda: start)

        controller = Mock()
        controller.use_new_current_calculation = False
        state = create_test_state(79)
        agile_tariff.get_control_state(state, 12 * 60)
        agile_tariff.set_home_demand_levels(controller, state, 12 * 60)

        controller.setChargeCurrentRange.assert_not_called()
        controller.setDischargeCurrentRange.assert_not_called()
        controller.setChargeActivationPower.assert_not_called()

    def test_dashboard_marks_session_slots(self, agile_tariff, tmp_path):
        store = self._set_up_store(agile_tariff, tmp_path)
        start = self._fixed(12)
        store.add(
            EnergySession(
                FREE_IMPORT, self._fixed(18), self._fixed(19)
            )
        )
        store.add(
            EnergySession(
                GRID_SUPPORT, self._fixed(20), self._fixed(20, 30)
            )
        )
        store.add(
            EnergySession(
                EV_AWAY, self._fixed(21), self._fixed(21, 30)
            )
        )
        agile_tariff.set_time_functions(datetime_func=lambda: start)

        html = agile_tariff.get_dashboard_html()

        assert "FREE ENERGY SESSION" in html
        assert "SAVE ENERGY SESSION (grid support)" in html
        assert "EV AWAY (excluded from projection)" in html
        assert "box-shadow:inset 0 4px 0 #2563eb;" in html
        assert "box-shadow:inset 0 4px 0 #d97706;" in html
        assert "box-shadow:inset 0 4px 0 #6b7280;" in html
        assert "Free Energy Session" in html  # legend
        assert "EV Away (excluded from 23:30 projection)" in html


class TestBreakEvenExportPriority:
    """Bulk export is preferred when it beats offsetting house load."""

    @staticmethod
    def _set_time(tariff, hour, minute=0):
        fixed = datetime.now().replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        tariff.set_time_functions(datetime_func=lambda: fixed)
        return fixed

    @staticmethod
    def _store(tariff, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        tariff.session_store = store
        return store

    def test_break_even_is_the_discharge_efficiency_ratio(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IMPORT_HIGH", 32.3)
        ratio = (
            agile_tariff.self_use_discharge_efficiency()
            / agile_tariff.export_discharge_efficiency()
        )

        assert ratio == pytest.approx(0.7784, abs=1e-3)
        assert agile_tariff.export_house_break_even_p() == pytest.approx(
            ratio * 32.3
        )
        assert agile_tariff.export_house_break_even_p(30.0) == pytest.approx(
            ratio * 30.0
        )
        assert agile_tariff.export_house_break_even_p() == pytest.approx(
            25.14, abs=0.05
        )

    def test_priority_slots_are_the_rates_above_break_even(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        self._set_time(agile_tariff, 12, 0)

        qualifying = agile_tariff._export_priority_slots(24)

        # 16:30 is 22p (<23.35p), 17:00-19:00 are 28-35p, 19:30 is 20p.
        assert qualifying == {34, 35, 36, 37, 38}

    def test_free_import_slots_are_not_export_priority(
        self, agile_tariff, tmp_path
    ):
        store = self._store(agile_tariff, tmp_path)
        start = self._set_time(agile_tariff, 12, 0)
        store.add(
            EnergySession(
                FREE_IMPORT,
                start.replace(hour=18, minute=0),
                start.replace(hour=19, minute=0),
            )
        )

        qualifying = agile_tariff._export_priority_slots(24)

        assert 36 not in qualifying
        assert 37 not in qualifying
        assert 38 in qualifying

    def test_plan_selects_priority_slots_until_the_floor_is_reached(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        self._set_time(agile_tariff, 17, 0)
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 10.0
        agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = 1.0

        # 17:00 starts at 56% and the 25% absolute minimum gives a 31% export
        # budget.  Three 10% priority exports fit (highest price first):
        # 35 (32p), 36 (35p) and 37 (30p), ending at 26%.  The exports are
        # allowed below the 55% house floor, but no house discharge is.
        plan = agile_tariff.calculate_export_plan(56)

        assert plan == [35, 36, 37]
        assert agile_tariff._project_soc_at_2330(
            56, 34, slot_actions=agile_tariff._planned_slot_actions
        ) == pytest.approx(26.0)
        assert agile_tariff._minimum_projected_soc(
            56, 34, slot_actions=agile_tariff._planned_slot_actions
        ) == pytest.approx(26.0)
        # The remaining priority slot (34, 28p) and 38 are left idle: both
        # are below the house floor, so there is no conservative coverage.
        assert agile_tariff._planned_slot_actions[34] == "idle"
        assert agile_tariff._planned_slot_actions[38] == "idle"
        assert all(
            action != "conservative"
            for action in agile_tariff._planned_slot_actions.values()
        )

    def test_shortfall_reserves_the_pack_for_a_planned_priority_export(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 25)
        self._set_time(agile_tariff, 12, 0)
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 8.0
        agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = 1.0
        state = create_test_state(77)

        control_state, _, _, _ = agile_tariff.get_control_state(
            state, 12 * 60
        )

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.LOAD_FOLLOW_BIDIRECTIONAL
        assert snapshot.decision_tier == "export_reserve"
        assert snapshot.discharge_min_a == 0
        assert snapshot.discharge_max_a == 0
        assert "house floor" in snapshot.reason
        assert "holding for" in snapshot.reason
        assert agile_tariff._planned_export_slots
        assert all(
            slot in agile_tariff._export_priority_slots(24)
            for slot in agile_tariff._planned_export_slots
        )

        # The load-follow range must match the decision: no discharge at all,
        # while surplus solar may still charge.
        controller = Mock()
        controller.use_new_current_calculation = False
        agile_tariff.set_home_demand_levels(controller, state, 12 * 60)

        controller.setDischargeCurrentRange.assert_called_with(0, 0)
        controller.setDischargeActivationPower.assert_called_with(
            agile_tariff.NEVER_DISCHARGE_ACTIVATION_W
        )
        controller.setChargeCurrentRange.assert_called_with(
            3, agile_tariff.MAX_CHARGE_CURRENT
        )

    def test_planned_aggressive_slot_executes_in_the_aggressive_band(
        self, agile_tariff, monkeypatch
    ):
        """A planned aggressive slot uses its stored gate when the band allows."""
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 25)
        TestDecisionSnapshot._set_time(agile_tariff, 11, 0)
        TestDecisionSnapshot._pin_plan(
            agile_tariff, 22, 79, shortfall=False
        )
        agile_tariff._planned_slot_actions = {22: "aggressive"}
        # The live gate is the house load that repays the pack; the planned
        # aggressive slot must use it rather than starting at any import.
        monkeypatch.setattr(
            agile_tariff,
            "_current_discharge_activation_w",
            lambda *args, **kwargs: 275,
        )
        state = create_test_state(79)

        agile_tariff.get_control_state(state, 11 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert snapshot.discharge_aggressive is True
        assert "aggressive discharge" in snapshot.mode
        assert snapshot.discharge_activation_w == 275

        controller = Mock()
        controller.use_new_current_calculation = False
        agile_tariff.set_home_demand_levels(controller, state, 11 * 60)

        controller.setDischargeActivationPower.assert_called_with(275)
        controller.setDischargeCurrentBias.assert_called_with(
            agile_tariff.AGGRESSIVE_DISCHARGE_BIAS_A
        )
        controller.setDischargeCurrentRange.assert_called_with(
            3, agile_tariff.MAX_DISCHARGE_CURRENT
        )

    def test_planned_aggressive_slot_yields_below_the_target(
        self, agile_tariff, monkeypatch
    ):
        """An aggressive plan action must not override a below-target policy."""
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 25)
        TestDecisionSnapshot._set_time(agile_tariff, 11, 0)
        TestDecisionSnapshot._pin_plan(
            agile_tariff, 22, 57, shortfall=True
        )
        agile_tariff._planned_slot_actions = {22: "aggressive"}
        monkeypatch.setattr(
            agile_tariff,
            "_current_discharge_activation_w",
            lambda *args, **kwargs: 720,
        )
        state = create_test_state(57)

        agile_tariff.get_control_state(state, 11 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert snapshot.discharge_aggressive is False
        assert "conservative discharge" in snapshot.mode
        assert snapshot.discharge_activation_w == 720

    def test_dashboard_names_the_reserved_priority_plan(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 25)
        self._set_time(agile_tariff, 12, 0)
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 8.0
        agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = 1.0

        agile_tariff.get_control_state(create_test_state(77), 12 * 60)
        html = agile_tariff.get_dashboard_html()

        assert "Export plan: none planned" not in html
        assert "Export plan: 17:00" in html


class TestStageOneExportBudget:
    """Stage 1 cherry-picks exports and spends the rest on house load."""

    @staticmethod
    def _set_time(tariff, hour, minute=0):
        fixed = datetime.now().replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        tariff.set_time_functions(datetime_func=lambda: fixed)
        return fixed

    @staticmethod
    def _set_rates(tariff, prices, default=8.0):
        """Replace the mock day with a slot -> export-price map."""
        base = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        tariff.agile_rates = []
        for slot in range(48):
            start = base + timedelta(minutes=30 * slot)
            tariff.agile_rates.append(
                {
                    "start": start,
                    "end": start + timedelta(minutes=30),
                    "rate": float(prices.get(slot, default)),
                }
            )

    def test_high_value_exports_stop_house_discharge_below_the_target(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 50)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)
        self._set_time(agile_tariff, 9, 0)  # slot 18, 29 slots to 23:30
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 4.0
        agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = 1.1

        # Twelve slots beat the 23.4p house-offset break-even, prices 26-37p.
        priority = [20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40, 42]
        prices = {
            slot: 26.0 + index for index, slot in enumerate(priority)
        }
        self._set_rates(agile_tariff, prices)

        plan = agile_tariff.calculate_export_plan(79)

        # 79% - 40% = 39% of budget; at 4% per export nine priority slots fit
        # and the tenth would cross the floor.
        assert len(plan) == 9
        assert set(plan) <= set(priority)
        assert plan == sorted(
            sorted(priority, key=lambda slot: prices[slot], reverse=True)[:9]
        )

        actions = agile_tariff._planned_slot_actions
        minimum = agile_tariff._minimum_projected_soc(
            79, 18, slot_actions=actions
        )
        assert minimum == pytest.approx(43.0)

        # The exports have spent the budget below both the 50% aggressive
        # target and the 45% house floor, so no house discharge is planned.
        aggressive = [
            slot for slot, action in actions.items()
            if action == "aggressive"
        ]
        conservative = [
            slot for slot, action in actions.items()
            if action == "conservative"
        ]
        idle = [slot for slot, action in actions.items() if action == "idle"]
        assert aggressive == []
        assert conservative == []
        assert idle
        assert agile_tariff._discharge_policy == "idle"

    def test_aggressive_plan_is_empty_when_exports_max_out_below_the_target(
        self, agile_tariff, monkeypatch
    ):
        """Exports get first claim; no aggressive slot if they spend the pack.

        This is the 16 A / 4%-per-slot case: seven high-value exports fit,
        the running minimum lands below the 50% aggressive target, and the
        remaining 15:00/15:30 slots must stay idle rather than load-follow.
        """
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 50)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)
        self._set_time(agile_tariff, 14, 0)  # slot 28
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 4.0
        agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = 1.1

        # 15:00 and 15:30 are the lowest-priced qualifying slots; 16:00-19:30
        # are the higher-value planned exports.  23p ordinary slots are
        # profitable but below the house-offset break-even.
        priority = {30: 26.0, 31: 26.0}
        priority.update(
            {slot: 30.0 + (slot - 32) for slot in range(32, 40)}
        )
        prices = dict(priority)
        prices.update({40: 23.0, 41: 23.0})
        self._set_rates(agile_tariff, prices)

        plan = agile_tariff.calculate_export_plan(70)

        # Seven 4% exports fit above the 40% absolute floor.
        assert plan == [33, 34, 35, 36, 37, 38, 39]
        assert 30 not in plan
        assert 31 not in plan

        actions = agile_tariff._planned_slot_actions
        assert actions[30] != "aggressive"
        assert actions[31] != "aggressive"
        assert all(
            action != "aggressive" for action in actions.values()
        )
        assert all(
            action != "conservative" for action in actions.values()
        )
        assert agile_tariff._minimum_projected_soc(
            70, 28, slot_actions=actions
        ) == pytest.approx(42.0)
        assert agile_tariff._discharge_policy == "idle"

    def test_aggressive_slots_are_the_next_highest_priority_prices(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 50)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)
        self._set_time(agile_tariff, 9, 0)
        # A large export loss leaves headroom above the 50% aggressive target
        # after the one full export that fits.  The forecast says the house
        # will not need conservative coverage, so aggressive may use it.
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 20.0
        agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = 1.1
        monkeypatch.setattr(
            agile_tariff,
            "_conservative_slot_soc_loss",
            lambda slot, fallback=None: 0.0,
        )
        monkeypatch.setattr(
            agile_tariff, "_conservative_would_activate", lambda slot: False
        )

        priority = [38, 40, 42]
        prices = {42: 37.0, 40: 36.0, 38: 35.0}
        self._set_rates(agile_tariff, prices)

        plan = agile_tariff.calculate_export_plan(79)

        aggressive = sorted(
            slot
            for slot, action in agile_tariff._planned_slot_actions.items()
            if action == "aggressive"
        )
        # 42 is the only full export that fits.  The two remaining priority
        # slots are the next highest prices and still leave the running
        # minimum above the 50% target, so they get aggressive load-following.
        assert plan == [42]
        assert aggressive == [38, 40]
        assert agile_tariff._planned_slot_actions[42] == "export"

    def test_aggressive_cost_sits_between_conservative_and_bulk_export(
        self, agile_tariff
    ):
        agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = 1.1
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 4.0

        conservative = agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT
        aggressive = agile_tariff._aggressive_slot_soc_loss_percent()
        export = agile_tariff._export_slot_soc_loss_percent()

        assert conservative < aggressive < export
        assert agile_tariff._aggressive_extra_soc_loss_percent() == (
            pytest.approx(aggressive - conservative)
        )
        assert agile_tariff._aggressive_export_kwh() > 0

    def test_aggressive_gate_shows_the_break_even_house_load(
        self, agile_tariff, monkeypatch
    ):
        """The gate is the load at which the forced output repays the pack."""
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        self._set_time(agile_tariff, 11, 0)
        agile_tariff._bank_dc_kwh = 10.0
        agile_tariff._bank_cost_gbp = 1.0  # 10p/kWh DC
        self._set_rates(agile_tariff, {22: 5.0}, default=5.0)

        gate = agile_tariff._aggressive_slot_gate(22)

        assert 0 < gate["activation_w"] < 720
        assert gate["hurdle_p"] > 5.0
        assert "starts at" in gate["note"]

        # Free stored energy and a high current export price clear the
        # hurdle, so any import is enough to start.  No future slot is set,
        # so the current price is not competing with a better alternative.
        agile_tariff._bank_dc_kwh = 1.0
        agile_tariff._bank_cost_gbp = 0.0
        self._set_rates(agile_tariff, {22: 27.0}, default=0.0)

        gate = agile_tariff._aggressive_slot_gate(22)

        assert gate["activation_w"] == 0
        assert "starts on any import" in gate["note"]

    def test_aggressive_plan_skips_slots_that_cannot_repay_stored_energy(
        self, agile_tariff, monkeypatch
    ):
        """A gate of NEVER must fall back to conservative/idle, not promise."""
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 50)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)
        self._set_time(agile_tariff, 9, 0)
        # Leave headroom above the 50% target so the aggressive phase really
        # considers these slots; the gate, not the budget, must reject them.
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 20.0
        agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = 1.1
        monkeypatch.setattr(
            agile_tariff,
            "_conservative_slot_soc_loss",
            lambda slot, fallback=None: 0.0,
        )
        monkeypatch.setattr(
            agile_tariff, "_conservative_would_activate", lambda slot: False
        )
        # 50p/kWh DC is above the peak import price even after discharge
        # losses, so the 3 A minimum can never repay the pack.
        agile_tariff._bank_dc_kwh = 1.0
        agile_tariff._bank_cost_gbp = 0.5

        priority = [38, 40, 42]
        prices = {42: 37.0, 40: 36.0, 38: 35.0}
        self._set_rates(agile_tariff, prices)

        plan = agile_tariff.calculate_export_plan(79)

        actions = agile_tariff._planned_slot_actions
        assert plan == [42]
        assert agile_tariff._aggressive_slot_gates == {}
        assert all(action != "aggressive" for action in actions.values())

    def test_plan_explanation_marks_aggressive_slots(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 50)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)
        self._set_time(agile_tariff, 9, 0)
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 20.0
        agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = 1.1
        monkeypatch.setattr(
            agile_tariff,
            "_conservative_slot_soc_loss",
            lambda slot, fallback=None: 0.0,
        )
        monkeypatch.setattr(
            agile_tariff, "_conservative_would_activate", lambda slot: False
        )

        priority = [38, 40, 42]
        prices = {42: 37.0, 40: 36.0, 38: 35.0}
        self._set_rates(agile_tariff, prices)

        agile_tariff.get_control_state(create_test_state(79), 9 * 60)
        explanation = agile_tariff.get_decision_explanation()
        plan = explanation["plan"]

        assert plan["planned_slots"] == [42]
        assert plan["aggressive_slots"] == [38, 40]
        aggressive_rows = [
            row for row in plan["rows"] if row["kind"] == "aggressive"
        ]
        assert [row["slot"] for row in aggressive_rows] == [38, 40]
        assert all(row["status"] == "aggressive" for row in aggressive_rows)
        assert all(row["revenue_gbp"] for row in aggressive_rows)
        # The plan is conditional: every aggressive row carries the live
        # house-load gate and the stored-energy hurdle, and the reason says
        # the slot only runs if the load reaches that gate.
        for row in aggressive_rows:
            assert row["aggressive_activation_w"] is not None
            assert row["aggressive_hurdle_p"] is not None
            assert (
                "starts only when the house imports"
                in row["reason"]
                or "starts on any grid import" in row["reason"]
            )
        assert set(plan["aggressive_activation_w"]) == {38, 40}
        assert set(plan["aggressive_hurdle_p"]) == {38, 40}
        assert plan["aggressive_conditional"] is True
        assert plan["selected_revenue_gbp"] == pytest.approx(
            sum(
                row["revenue_gbp"] or 0.0
                for row in plan["rows"]
                if row["planned"] or row["kind"] == "aggressive"
            )
        )
        assert "aggressive" in plan["diagnosis"].lower()
        assert {row["slot"] for row in plan["options"] if row["kind"] == "aggressive"} == {38, 40}

        html = agile_tariff.get_dashboard_html()
        assert "2 aggressive (conditional house + surplus export)" in html

    def test_ordinary_exports_stop_at_the_bulk_target(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 50)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)
        self._set_time(agile_tariff, 12, 0)
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 4.0
        agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = 1.1
        # The house is forecast below the conservative activation point, so
        # conservative coverage does not consume the surplus headroom.
        monkeypatch.setattr(
            agile_tariff,
            "_conservative_slot_soc_loss",
            lambda slot, fallback=None: 0.0,
        )
        monkeypatch.setattr(
            agile_tariff, "_conservative_would_activate", lambda slot: False
        )

        # Five high-value slots, plus many 23p slots that are profitable but
        # below the 23.4p house-offset break-even.
        prices = {slot: 23.0 for slot in range(24, 47)}
        for slot, rate in zip((34, 35, 36, 37, 38), (28, 32, 35, 30, 25)):
            prices[slot] = float(rate)
        self._set_rates(agile_tariff, prices)

        plan = agile_tariff.calculate_export_plan(100)

        priority = {34, 35, 36, 37, 38}
        ordinary = [slot for slot in plan if slot not in priority]
        assert set(plan) & priority == priority
        assert ordinary
        # The ordinary slots may only use headroom above the 50% bulk target.
        exports_only = {
            slot: "export" for slot in plan
        }
        assert agile_tariff._minimum_projected_soc(
            100, 24, slot_actions=exports_only
        ) >= 50.0 - 1e-9
        # Conservative coverage stops at the 45% house floor, never below it.
        assert agile_tariff._minimum_projected_soc(
            100, 24, slot_actions=agile_tariff._planned_slot_actions
        ) >= 45.0 - 1e-9


class TestFourBandDischargePolicy:
    """The house-load mode follows the projected 23:30 SoC."""

    def test_aggressive_at_or_above_the_target(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 25)

        assert agile_tariff._apply_discharge_policy(70) == "aggressive"
        assert agile_tariff._apply_discharge_policy(60) == "aggressive"
        assert agile_tariff._apply_discharge_policy(59.9) == "conservative"
        assert agile_tariff._energy_shortfall is True
        assert agile_tariff._discharge_inhibited is False
        assert agile_tariff._house_discharge_inhibited is False

    def test_conservative_between_house_floor_and_target(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 25)

        assert agile_tariff._apply_discharge_policy(57) == "conservative"
        assert agile_tariff._energy_shortfall is True
        assert agile_tariff._discharge_inhibited is False
        assert agile_tariff._house_discharge_inhibited is False

    def test_idle_below_the_house_floor(self, agile_tariff, monkeypatch):
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 25)

        assert agile_tariff._apply_discharge_policy(54) == "idle"
        assert agile_tariff._energy_shortfall is True
        assert agile_tariff._discharge_inhibited is False
        assert agile_tariff._house_discharge_inhibited is True

    def test_inhibited_below_the_absolute_floor(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 25)

        assert agile_tariff._apply_discharge_policy(20) == "inhibited"
        assert agile_tariff._energy_shortfall is True
        assert agile_tariff._discharge_inhibited is True

    def test_house_floor_never_falls_below_the_absolute_minimum(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 65)

        assert agile_tariff._house_load_floor_soc() == 65

    def test_absolute_minimum_cannot_go_below_the_physical_reserve(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 10)

        assert agile_tariff.absolute_min_soc_percent() == 25

    def test_projection_uses_the_export_loss_for_planned_slots(
        self, agile_tariff
    ):
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 5.0
        baseline = agile_tariff._project_soc_at_2330(77, 24)
        with_export = agile_tariff._project_soc_at_2330(
            77, 24, export_slots={36}
        )

        assert baseline == pytest.approx(54.0)
        assert with_export == pytest.approx(
            baseline
            - (5.0 - agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT)
        )

    def test_inhibited_discharge_closes_the_range_and_keeps_charging(
        self, agile_tariff
    ):
        controller = Mock()
        controller.use_new_current_calculation = False
        agile_tariff._energy_shortfall = True
        agile_tariff._discharge_inhibited = True

        agile_tariff.set_home_demand_levels(
            controller, create_test_state(80), 12 * 60
        )

        controller.setDischargeCurrentRange.assert_called_with(0, 0)
        controller.setChargeCurrentRange.assert_called_with(
            3, agile_tariff.MAX_CHARGE_CURRENT
        )

    def test_plan_landing_exactly_on_the_house_floor_is_conservative(
        self, agile_tariff, monkeypatch
    ):
        """Floating-point noise must not turn a floor-level plan into Idle."""
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 50)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)

        assert agile_tariff._apply_discharge_policy(45.0) == "conservative"
        assert (
            agile_tariff._apply_discharge_policy(45.0 - 1e-12)
            == "conservative"
        )
        assert agile_tariff._apply_discharge_policy(44.9) == "idle"

    def test_plan_landing_exactly_on_the_absolute_floor_is_idle(
        self, agile_tariff, monkeypatch
    ):
        """The absolute floor allows committed exports but no house load."""
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 50)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 5)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)

        assert agile_tariff._apply_discharge_policy(40.0) == "idle"
        assert agile_tariff._apply_discharge_policy(39.9) == "inhibited"


class TestAbsoluteMinimumSocControl:
    """The configured floor applies to house load and grid support."""

    @staticmethod
    def _fixed(hour, minute=0):
        return datetime.now().replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )

    @staticmethod
    def _set_up_store(tariff, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        tariff.session_store = store
        return store

    def test_grid_support_exports_just_above_the_configured_floor(
        self, agile_tariff, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)
        store = self._set_up_store(agile_tariff, tmp_path)
        start = self._fixed(12)
        store.add(
            EnergySession(GRID_SUPPORT, start, start + timedelta(hours=1))
        )
        agile_tariff.set_time_functions(datetime_func=lambda: start)

        control_state, _, _, _ = agile_tariff.get_control_state(
            create_test_state(45), 12 * 60
        )

        assert control_state == ControlState.DISCHARGE

    def test_grid_support_stops_at_the_configured_floor(
        self, agile_tariff, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)
        store = self._set_up_store(agile_tariff, tmp_path)
        start = self._fixed(12)
        store.add(
            EnergySession(GRID_SUPPORT, start, start + timedelta(hours=1))
        )
        agile_tariff.set_time_functions(datetime_func=lambda: start)

        control_state, _, _, _ = agile_tariff.get_control_state(
            create_test_state(40), 12 * 60
        )

        assert control_state == ControlState.DORMANT
        assert (
            agile_tariff._decision_snapshot.decision_tier == "absolute_floor"
        )

    def test_export_priority_explanation_names_the_break_even(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        TestDecisionSnapshot._set_time(agile_tariff, 18, 0)
        TestDecisionSnapshot._pin_plan(agile_tariff, 36, 80)
        agile_tariff._planned_export_slots = [36]
        agile_tariff._plan_cache = [36]

        agile_tariff.get_control_state(create_test_state(80), 18 * 60)
        explanation = agile_tariff.get_decision_explanation()

        labels = [row["label"] for row in explanation["state"]]
        text = " ".join(
            [explanation["answer"]["heading"]]
            + [step["calculation"] for step in explanation["steps"]]
            + [step["result"] for step in explanation["steps"]]
        )
        assert "House-offset break-even" in labels
        assert explanation["answer"]["heading"] == (
            "Export beats offsetting the house"
        )
        assert "Break-even" in text
        assert "23.4p" in text

    def test_absolute_floor_explanation_stops_discharge(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)
        TestDecisionSnapshot._set_time(agile_tariff, 12, 0)
        # No pinned plan: with ordinary slots idle the running minimum is the
        # current 30%, already below the configured 40% absolute floor.
        agile_tariff.get_control_state(create_test_state(30), 12 * 60)
        explanation = agile_tariff.get_decision_explanation()

        labels = [row["label"] for row in explanation["state"]]
        assert explanation["answer"]["heading"] == "Hold at the absolute minimum"
        assert "Hard protection floor" in labels
        assert "Aggressive floor" in labels
        assert "Conservative floor" in labels
        assert "Bulk discharge floor" in labels
        assert explanation["answer"]["discharge"]["headline"] == (
            "Do not discharge"
        )

    def test_absolute_floor_message_names_the_solar_charge_floor(
        self, agile_tariff, monkeypatch
    ):
        """Idle at the floor still has to say how solar is harvested."""
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)
        TestDecisionSnapshot._set_time(agile_tariff, 12, 0)

        agile_tariff.get_control_state(create_test_state(40), 12 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert snapshot.decision_tier == "absolute_floor"
        assert snapshot.charge_min_a == pytest.approx(3.0)
        assert snapshot.charge_max_a == pytest.approx(
            agile_tariff.MAX_CHARGE_CURRENT
        )
        assert snapshot.charge_activation_w == pytest.approx(720.0)
        assert (
            "surplus solar still charges at ≥3 A (≈720 W)"
            in snapshot.reason
        )

        html = agile_tariff.get_dashboard_html()
        assert "solar ≥3 A (≈720 W)" in html

        explanation = agile_tariff.get_decision_explanation()
        assert explanation["answer"]["charge"]["headline"] == (
            "Charge from surplus solar at ≥3 A (≈720 W)"
        )
        assert any(
            "surplus solar" in step["title"].lower()
            for step in explanation["steps"]
        )

    def test_absolute_floor_uses_the_break_even_harvest_current(
        self, agile_tariff, monkeypatch
    ):
        """3 A is not automatic: the floor follows the export price."""
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)
        TestDecisionSnapshot._set_time(agile_tariff, 12, 0)
        # 18p at the current slot makes 3 A solar storage uneconomic; the
        # measured efficiency curve only clears it from 8 A.
        TestStageOneExportBudget._set_rates(
            agile_tariff, {24: 18.0}, default=5.0
        )
        controller = Mock()
        controller.use_new_current_calculation = False
        state = create_test_state(40)

        agile_tariff.get_control_state(state, 12 * 60)
        snapshot = agile_tariff._decision_snapshot

        assert snapshot.charge_min_a == pytest.approx(8.0)
        assert snapshot.charge_activation_w == pytest.approx(8 * 240)
        agile_tariff.set_home_demand_levels(controller, state, 12 * 60)
        controller.setChargeCurrentRange.assert_called_with(
            8.0, agile_tariff.MAX_CHARGE_CURRENT
        )
        controller.setChargeActivationPower.assert_called_with(8 * 240)

    def test_absolute_floor_sells_surplus_when_export_is_worth_more(
        self, agile_tariff, monkeypatch
    ):
        """No measured current clears a very high export price."""
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)
        TestDecisionSnapshot._set_time(agile_tariff, 12, 0)
        TestStageOneExportBudget._set_rates(
            agile_tariff, {24: 19.9}, default=5.0
        )
        controller = Mock()
        controller.use_new_current_calculation = False
        state = create_test_state(40)

        agile_tariff.get_control_state(state, 12 * 60)
        snapshot = agile_tariff._decision_snapshot

        assert snapshot.charge_min_a == pytest.approx(0.0)
        assert snapshot.charge_activation_w == pytest.approx(0.0)
        assert "surplus solar is worth more exported now" in snapshot.reason
        agile_tariff.set_home_demand_levels(controller, state, 12 * 60)
        controller.setChargeCurrentRange.assert_called_with(0.0, 0.0)
        controller.setChargeActivationPower.assert_called_with(0.0)


class TestPlanExplanation:
    """The explanation page shows the projection and every export option."""

    @staticmethod
    def _set_time(tariff, hour, minute=0):
        return TestDecisionSnapshot._set_time(tariff, hour, minute)

    def test_plan_rows_match_the_projection_and_mark_the_selected_slots(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        self._set_time(agile_tariff, 17, 0)
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 10.0
        agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = 1.0

        agile_tariff.get_control_state(create_test_state(56), 17 * 60)
        explanation = agile_tariff.get_decision_explanation()
        plan = explanation["plan"]

        assert plan["current_soc"] == pytest.approx(56.0)
        # The stage-1 baseline is the day with no discretionary house load.
        assert plan["baseline_projection"] == pytest.approx(56.0)
        assert plan["priority_headroom_soc"] == pytest.approx(31.0)
        assert plan["headroom_soc"] == pytest.approx(-4.0)
        assert plan["planned_slots"] == [35, 36, 37]
        assert plan["skipped_qualifying_slots"] == [34, 38]
        assert plan["plan_projection"] == pytest.approx(
            plan["rows"][-1]["projected_soc_after"]
        )

        planned_rows = [row for row in plan["rows"] if row["planned"]]
        assert [row["slot"] for row in planned_rows] == [35, 36, 37]
        assert all(row["status"] == "planned" for row in planned_rows)
        assert all(row["status_class"] == "status-planned" for row in planned_rows)
        assert all(
            "break-even" in row["reason"] for row in planned_rows
        )
        assert plan["selected_revenue_gbp"] == pytest.approx(
            sum(row["revenue_gbp"] for row in planned_rows)
        )

        # The options table is ranked by price, and the selected slots carry
        # their planned status into it.
        option_prices = [row["export_price_p"] for row in plan["options"]]
        assert option_prices == sorted(option_prices, reverse=True)
        assert {
            row["slot"] for row in plan["options"] if row["planned"]
        } == {35, 36, 37}

        # 34 and 38 qualify but are below the house floor, so both are
        # skipped rather than used conservatively.
        html = agile_tariff.get_dashboard_html()
        assert "2 qualifying slot(s) skipped (headroom used)" in html
        # Idle plan rows mean "no discharge", not "no solar".  The charge floor
        # is price-dependent, so the note must not imply a fixed 3 A.
        assert "Idle rows stop battery discharge, not solar charging" in (
            plan["diagnosis"]
        )

    def test_empty_plan_explains_that_the_floor_blocked_qualifying_slots(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 60)
        self._set_time(agile_tariff, 12, 0)

        agile_tariff.get_control_state(create_test_state(25), 12 * 60)
        explanation = agile_tariff.get_decision_explanation()
        plan = explanation["plan"]

        # With ordinary slots idle, 25% is already at the absolute minimum.
        # Even the high-value afternoon peak cannot be committed without
        # breaching the hard floor.
        assert plan["planned_slots"] == []
        assert plan["headroom_soc"] < 0
        assert plan["priority_headroom_soc"] == pytest.approx(0.0)
        assert plan["skipped_qualifying_slots"] == [34, 35, 36, 37, 38]
        assert "No export is planned" in plan["diagnosis"]
        assert "bulk discharge floor" in plan["diagnosis"]
        assert "5 slot(s) beat" in plan["diagnosis"]

        skipped_rows = [
            row for row in plan["rows"] if row["status"] == "skipped"
        ]
        assert len(skipped_rows) == 5
        assert all(row["qualifies"] for row in skipped_rows)
        assert all("no headroom" in row["reason"] for row in skipped_rows)

        # The options list still shows them, ranked best price first, so the
        # user can see the trade-off rather than an unexplained empty plan.
        assert plan["options"][0]["slot"] == 36
        assert {
            row["slot"] for row in plan["options"] if row["qualifies"]
        } == {34, 35, 36, 37, 38}

        html = agile_tariff.get_dashboard_html()
        assert "break-even, but" in html
        assert "25% absolute minimum" in html

    def test_session_slots_are_named_and_excluded_from_export_options(
        self, agile_tariff, tmp_path
    ):
        start = self._set_time(agile_tariff, 12, 0)
        store = SessionStore(tmp_path / "sessions.json")
        agile_tariff.session_store = store
        store.add(
            EnergySession(
                FREE_IMPORT,
                start.replace(hour=14, minute=0),
                start.replace(hour=15, minute=0),
            )
        )
        store.add(
            EnergySession(
                GRID_SUPPORT,
                start.replace(hour=20, minute=0),
                start.replace(hour=20, minute=30),
            )
        )
        store.add(
            EnergySession(
                EV_AWAY,
                start.replace(hour=21, minute=0),
                start.replace(hour=21, minute=30),
            )
        )

        agile_tariff.get_control_state(create_test_state(50), 12 * 60)
        explanation = agile_tariff.get_decision_explanation()
        rows = {row["slot"]: row for row in explanation["plan"]["rows"]}

        assert rows[28]["kind"] == "free_import"
        assert rows[28]["status"] == "free_import"
        assert "charges at full rate" in rows[28]["reason"]
        assert rows[40]["kind"] == "grid_support"
        assert "already exports at maximum" in rows[40]["reason"]
        assert rows[42]["kind"] == "away"
        assert "not an export target" in rows[42]["reason"]

        option_slots = {
            row["slot"] for row in explanation["plan"]["options"]
        }
        assert not ({28, 29, 40, 42} & option_slots)

    def test_decision_template_renders_the_plan_and_options(
        self, agile_tariff, monkeypatch
    ):
        from pathlib import Path

        from flask import Flask

        monkeypatch.setattr(config, "IMPORT_HIGH", 30.0)
        self._set_time(agile_tariff, 17, 0)
        agile_tariff.EXPORT_SLOT_SOC_LOSS_PERCENT = 10.0
        agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT = 1.0
        agile_tariff.get_control_state(create_test_state(92), 17 * 60)
        explanation = agile_tariff.get_decision_explanation()

        templates = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "evse_controller"
            / "templates"
        )
        app = Flask(
            __name__,
            template_folder=str(templates),
            static_folder=str(templates.parent / "static"),
        )
        app.config.update(TESTING=True)

        @app.route("/")
        def index():
            return "dashboard"

        @app.route("/sessions")
        def sessions_page():
            return "sessions"

        @app.route("/config")
        def config_page():
            return "config"

        @app.route("/decision")
        def decision_page():
            from flask import render_template

            return render_template("decision.html", **explanation)

        html = app.test_client().get("/decision").get_data(as_text=True)

        assert "The rest of today: SoC projection and export plan" in html
        assert "Export options, best price first" in html
        assert "23:30 with plan" in html
        assert "PLANNED" in html
        assert "House break-even" in html


class TestLoadProfileForecast:
    """The planner uses the rolling house-load profile when it exists."""

    @staticmethod
    def _expected_loss(tariff, load_w: float, aggressive: bool) -> float:
        volts = 240.0
        min_current = max(1.0, float(tariff.MIN_DISCHARGE_CURRENT))
        max_current = max(
            min_current, float(config.WALLBOX_MAX_DISCHARGE_CURRENT)
        )
        bias = (
            tariff.AGGRESSIVE_DISCHARGE_BIAS_A
            if aggressive
            else tariff.CONSERVATIVE_DISCHARGE_BIAS_A
        )
        current = float(
            round(max(min_current, min(load_w / volts + bias, max_current)))
        )
        current = max(min_current, min(current, max_current))
        efficiency = tariff.EFFICIENCY_MODEL.discharge_efficiency(current)
        return (
            current * volts / max(efficiency, 1e-6) / 1000.0
            * 0.5
            / tariff.SOC_CAPACITY_KWH
            * 100.0
        )

    def test_conservative_loss_uses_the_p90_forecast(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(
            agile_tariff,
            "_profile_stats",
            lambda slot: SlotLoad(1200.0, 2400.0, 14, 1800.0),
        )

        loss = agile_tariff._conservative_slot_soc_loss(0)

        assert loss == pytest.approx(
            self._expected_loss(agile_tariff, 2400.0, aggressive=False)
        )
        assert loss > agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT

    def test_aggressive_loss_uses_the_median_forecast(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(
            agile_tariff,
            "_profile_stats",
            lambda slot: SlotLoad(1200.0, 2400.0, 14, 1800.0),
        )

        loss = agile_tariff._aggressive_slot_soc_loss(0)

        assert loss == pytest.approx(
            self._expected_loss(agile_tariff, 1200.0, aggressive=True)
        )
        assert loss > agile_tariff._aggressive_slot_soc_loss_percent()

    def test_conservative_does_not_start_below_the_minimum_output(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(
            agile_tariff,
            "_profile_stats",
            lambda slot: SlotLoad(300.0, 400.0, 14, 1800.0),
        )

        assert agile_tariff._conservative_slot_soc_loss(0) == 0.0

    def test_forecast_absent_falls_back_to_the_configured_loss(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(
            agile_tariff, "_profile_stats", lambda slot: None
        )

        assert agile_tariff._conservative_slot_soc_loss(0) == pytest.approx(
            agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT
        )
        assert agile_tariff._aggressive_slot_soc_loss(0) == pytest.approx(
            agile_tariff._aggressive_slot_soc_loss_percent()
        )


class TestLoadProfileForecastHierarchy:
    """Conservative coverage outranks surplus and aggressive use."""

    def test_conservative_activation_uses_the_p90_forecast(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(
            agile_tariff,
            "_profile_stats",
            lambda slot: SlotLoad(300.0, 1200.0, 14, 1800.0),
        )
        assert agile_tariff._conservative_would_activate(0) is True

        monkeypatch.setattr(
            agile_tariff,
            "_profile_stats",
            lambda slot: SlotLoad(200.0, 400.0, 14, 1800.0),
        )
        assert agile_tariff._conservative_would_activate(0) is False

    def test_conservative_activation_defaults_to_true_without_a_forecast(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(
            agile_tariff, "_profile_stats", lambda slot: None
        )
        assert agile_tariff._conservative_would_activate(0) is True


class TestExportFloors:
    """Normal exports stop at their economic floor, not the hard floor."""

    def test_priority_export_stops_at_the_bulk_floor(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "AGGRESSIVE_SOC_LOWER_LIMIT", 50)
        monkeypatch.setattr(config, "CONSERVATIVE_SOC_LOWER_LIMIT", 45)
        monkeypatch.setattr(config, "BULK_DISCHARGE_SOC_LOWER_LIMIT", 40)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 25)
        TestDecisionSnapshot._set_time(agile_tariff, 18, 0)
        TestDecisionSnapshot._pin_plan(agile_tariff, 36, 40)
        agile_tariff._planned_export_slots = [36]
        agile_tariff._plan_cache = [36]
        agile_tariff._planned_slot_actions = {36: "export"}

        agile_tariff.get_control_state(create_test_state(40), 18 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert snapshot.decision_tier != "export_priority"
        assert not snapshot.mode.startswith("Max discharge")

    def test_priority_export_runs_above_the_bulk_floor(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "AGGRESSIVE_SOC_LOWER_LIMIT", 50)
        monkeypatch.setattr(config, "CONSERVATIVE_SOC_LOWER_LIMIT", 45)
        monkeypatch.setattr(config, "BULK_DISCHARGE_SOC_LOWER_LIMIT", 40)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 25)
        TestDecisionSnapshot._set_time(agile_tariff, 18, 0)
        TestDecisionSnapshot._pin_plan(agile_tariff, 36, 41)
        agile_tariff._planned_export_slots = [36]
        agile_tariff._plan_cache = [36]
        agile_tariff._planned_slot_actions = {36: "export"}

        control_state, _, _, _ = agile_tariff.get_control_state(
            create_test_state(41), 18 * 60
        )

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.DISCHARGE
        assert snapshot.decision_tier == "export_priority"

    def test_surplus_export_stops_at_the_aggressive_floor(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "AGGRESSIVE_SOC_LOWER_LIMIT", 50)
        monkeypatch.setattr(config, "CONSERVATIVE_SOC_LOWER_LIMIT", 45)
        monkeypatch.setattr(config, "BULK_DISCHARGE_SOC_LOWER_LIMIT", 40)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 25)
        TestDecisionSnapshot._set_time(agile_tariff, 20, 0)
        TestDecisionSnapshot._pin_plan(agile_tariff, 40, 50)
        agile_tariff._planned_export_slots = [40]
        agile_tariff._plan_cache = [40]
        agile_tariff._planned_slot_actions = {40: "export"}

        agile_tariff.get_control_state(create_test_state(50), 20 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert snapshot.decision_tier != "planned_export"
        assert not snapshot.mode.startswith("Max discharge")

    def test_surplus_export_runs_above_the_aggressive_floor(
        self, agile_tariff, monkeypatch
    ):
        monkeypatch.setattr(config, "AGGRESSIVE_SOC_LOWER_LIMIT", 50)
        monkeypatch.setattr(config, "CONSERVATIVE_SOC_LOWER_LIMIT", 45)
        monkeypatch.setattr(config, "BULK_DISCHARGE_SOC_LOWER_LIMIT", 40)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 25)
        TestDecisionSnapshot._set_time(agile_tariff, 20, 0)
        TestDecisionSnapshot._pin_plan(agile_tariff, 40, 51)
        agile_tariff._planned_export_slots = [40]
        agile_tariff._plan_cache = [40]
        agile_tariff._planned_slot_actions = {40: "export"}

        control_state, _, _, _ = agile_tariff.get_control_state(
            create_test_state(51), 20 * 60
        )

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.DISCHARGE
        assert snapshot.decision_tier == "planned_export"


class TestLoadProfileIntegration:
    """The tariff reads the rolling profile files end to end."""

    def test_tariff_uses_the_rolling_profile(
        self, agile_tariff, tmp_path
    ):
        fixed = datetime.now().replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        agile_tariff.set_time_functions(datetime_func=lambda: fixed)
        day = fixed.date()
        bin_index = int(1200 / 25)
        for offset in range(14):
            file_day = day - timedelta(days=offset)
            slots = [
                {
                    "index": index,
                    "closed": True,
                    "coverage_s": {},
                    "bins_s": {},
                }
                for index in range(48)
            ]
            slots[24] = {
                "index": 24,
                "closed": True,
                "coverage_s": {"house": 1800.0},
                "bins_s": {"house": {str(bin_index): 1800.0}},
            }
            payload = {
                "version": 1,
                "date": file_day.isoformat(),
                "bin_width_w": 25.0,
                "slots": slots,
            }
            (tmp_path / f"{file_day.isoformat()}.json").write_text(
                json.dumps(payload)
            )

        agile_tariff._load_profile.set_directory(tmp_path)
        agile_tariff._profile_day = None
        agile_tariff._profile_slots = None

        stats = agile_tariff._load_profile.for_date(day)[24]
        assert stats.p90_w is not None
        loss = agile_tariff._conservative_slot_soc_loss(24)
        expected = agile_tariff._house_load_soc_loss_percent(
            stats.p90_w, aggressive=False
        )

        assert loss == pytest.approx(expected)
        assert loss > agile_tariff.NON_EXPORT_SLOT_SOC_LOSS_PERCENT


class TestExplicitSocLimitsInTheTariff:
    """The tariff's bands follow the explicit new limits."""

    def test_explicit_limits_drive_the_bands(self, agile_tariff, monkeypatch):
        monkeypatch.setattr(config, "AGGRESSIVE_SOC_LOWER_LIMIT", 55)
        monkeypatch.setattr(config, "CONSERVATIVE_SOC_LOWER_LIMIT", 48)
        monkeypatch.setattr(config, "BULK_DISCHARGE_SOC_LOWER_LIMIT", 42)
        monkeypatch.setattr(config, "IOCTGO_ABSOLUTE_MIN_SOC_PERCENT", 40)

        assert agile_tariff._house_load_floor_soc() == 48
        assert agile_tariff._apply_discharge_policy(55) == "aggressive"
        assert agile_tariff._apply_discharge_policy(50) == "conservative"
        assert agile_tariff._apply_discharge_policy(47) == "idle"
        assert agile_tariff._apply_discharge_policy(39) == "inhibited"


class TestShadowPlannerAndEstimator:
    """The DP observes and records; the heuristic still decides."""

    @staticmethod
    def _write_profile_day(directory, day):
        directory.mkdir(parents=True, exist_ok=True)
        slots = []
        for index in range(48):
            has_data = index == 24
            slots.append(
                {
                    "index": index,
                    "closed": True,
                    "coverage_s": {"house": 1800.0} if has_data else {},
                    "bins_s": (
                        {"house": {"24": 1800.0}} if has_data else {}
                    ),
                }
            )
        (directory / f"{day.isoformat()}.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "date": day.isoformat(),
                    "bin_width_w": 25.0,
                    "slots": slots,
                }
            )
        )

    def test_soc_estimator_seeds_and_tracks_reports(self, tmp_path):
        tariff = _build_tariff()
        tariff._shadow_enabled = True
        tariff._history.set_directory(tmp_path / "history")
        now = datetime.now().replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        tariff.set_time_functions(datetime_func=lambda: now)

        state = create_test_state(60)
        estimator = tariff._update_soc_estimator(state, now)
        assert estimator.soc_percent == pytest.approx(60.0)

        state.ac_power = 3600.0
        state.current = 10.0
        estimator = tariff._update_soc_estimator(
            state, now + timedelta(minutes=30)
        )
        assert estimator.soc_percent > 60.0

        state.battery_level = 63
        estimator = tariff._update_soc_estimator(
            state, now + timedelta(minutes=31)
        )
        assert 60.0 < estimator.soc_percent <= 63.0

        samples = tariff._history.soc_samples_for_day(now.date())
        assert samples
        assert samples[0]["estimate"] == pytest.approx(60.0, abs=0.5)

    def test_shadow_plan_records_a_run(self, tmp_path):
        tariff = _build_tariff()
        tariff._shadow_enabled = True
        tariff._history.set_directory(tmp_path / "history")
        today = datetime.now().date()
        profile_dir = tmp_path / "profiles"
        for offset in range(14):
            self._write_profile_day(
                profile_dir, today - timedelta(days=offset)
            )
        tariff._load_profile.set_directory(profile_dir)
        TestDecisionSnapshot._set_time(tariff, 12, 0)

        tariff.get_control_state(create_test_state(60), 12 * 60)

        runs = tariff._history.runs_for_day(today)
        assert len(runs) == 1
        run = runs[0]
        assert run["slot"] == 24
        assert run["reported_soc"] == 60
        assert run["estimate_soc"] == pytest.approx(60.0)
        assert run["target_soc"] is None
        assert run["forecast_days"] >= 14
        assert run["horizon_limited_by_export"] is True
        assert run["horizon_end_date"] == today.isoformat()
        assert run["inputs"][-1]["horizon_index"] == 23
        assert len(run["actions"]) == 24
        assert len(run["inputs"]) == 24
        assert run["actions"][0]["slot"] == 24
        assert run["actions"][-1]["slot"] == 47
        assert run["inputs"][0]["slot"] == 24
        assert run["inputs"][-1]["slot"] == 47
        assert run["planner"]["capacity_kwh"] > 0
        assert run["planner"]["soc_step_percent"] == 0.1
        assert run["planner"]["load_fallback_kw"] == pytest.approx(0.72)
        assert run["planner"]["load_prior"] == "uniform_0_400"
        assert run["planner"]["target_soc_percent"] is None
        assert run["planner"]["efficiency_model"]["points"]
        assert any(
            item["load_source"] == "prior" for item in run["inputs"]
        )
        assert run["inputs"][0]["load_samples"]
        assert any(
            item["follow_aggressive_gate_w"] is not None
            for item in run["inputs"]
        )

        decisions = tariff._history.decisions_for_day(today)
        assert len(decisions) == 1
        assert decisions[0]["slot"] == 24
        assert decisions[0]["mode"]
        assert decisions[0]["control_state"]

    def test_shadow_plan_runs_once_per_slot_until_the_soc_moves(
        self, tmp_path
    ):
        tariff = _build_tariff()
        tariff._shadow_enabled = True
        tariff._history.set_directory(tmp_path / "history")
        today = datetime.now().date()
        profile_dir = tmp_path / "profiles"
        for offset in range(14):
            self._write_profile_day(
                profile_dir, today - timedelta(days=offset)
            )
        tariff._load_profile.set_directory(profile_dir)
        TestDecisionSnapshot._set_time(tariff, 12, 0)

        tariff.get_control_state(create_test_state(60), 12 * 60)
        tariff.get_control_state(create_test_state(60), 12 * 60)
        assert len(tariff._history.runs_for_day(today)) == 1

        # A report more than 3% away means the old plan is no longer about
        # the same battery state, so plan again in the same slot.
        tariff.get_control_state(create_test_state(64), 12 * 60)
        assert len(tariff._history.runs_for_day(today)) == 2

    def test_disabled_shadow_planner_records_nothing(self, tmp_path):
        tariff = _build_tariff()
        tariff._history.set_directory(tmp_path / "history")
        today = datetime.now().date()
        TestDecisionSnapshot._set_time(tariff, 12, 0)

        tariff.get_control_state(create_test_state(60), 12 * 60)

        assert tariff._shadow_enabled is False
        assert tariff._history.runs_for_day(today) == []
        assert tariff._history.soc_samples_for_day(today) == []

    def test_estimator_failure_does_not_break_control(self, tmp_path):
        tariff = _build_tariff()
        tariff._history.set_directory(tmp_path / "history")
        TestDecisionSnapshot._set_time(tariff, 12, 0)

        def boom(*args, **kwargs):
            raise RuntimeError("estimator failed")

        tariff._update_soc_estimator = boom

        result = tariff.get_control_state(
            create_test_state(60), 12 * 60
        )

        assert isinstance(result, tuple)
        assert result[0] is not None

    def test_shadow_planner_is_disabled_by_default(self):
        tariff = _build_tariff()
        assert tariff._shadow_enabled is False


class TestHighValueExportRecharge:
    def test_low_soc_plans_peak_charge_before_high_export(self):
        tariff = _build_tariff()
        tariff._shadow_enabled = False
        TestDecisionSnapshot._set_time(tariff, 12, 0)

        rates = []
        for rate in tariff.agile_rates:
            slot = rate['start'].hour * 2 + (
                1 if rate['start'].minute == 30 else 0
            )
            updated = dict(rate)
            updated['rate'] = 90.0 if 36 <= slot <= 41 else 5.0
            rates.append(updated)
        tariff.agile_rates = rates

        planned_exports = tariff.calculate_export_plan(30)
        charge_slots = {
            slot
            for slot, action in tariff._planned_slot_actions.items()
            if action == "charge"
        }

        assert planned_exports
        assert set(planned_exports) & set(range(36, 42))
        assert tariff._planned_import_slots
        assert charge_slots & set(range(24, 36))

        html = tariff.get_dashboard_html()
        assert "data-import=\"True\"" in html
        assert ">IM<" in html
        assert "Planned import / charge (IM)" in html

    def test_peak_recharge_charges_at_max(self):
        tariff = _build_tariff()
        tariff._shadow_enabled = False
        TestDecisionSnapshot._set_time(tariff, 12, 0)
        rates = []
        for rate in tariff.agile_rates:
            slot = rate['start'].hour * 2 + (
                1 if rate['start'].minute == 30 else 0
            )
            updated = dict(rate)
            updated['rate'] = 90.0 if 36 <= slot <= 41 else 5.0
            rates.append(updated)
        tariff.agile_rates = rates
        tariff._planned_slot_actions = {24: "charge"}
        tariff._planned_import_slots = [24]
        tariff._planned_export_slots = [36]

        control_state, minimum, maximum, _ = tariff.get_control_state(
            create_test_state(30), 12 * 60
        )

        assert control_state == ControlState.CHARGE
        assert minimum == maximum == config.WALLBOX_MAX_CHARGE_CURRENT


class TestTieredExportVariants:
    """The three outgoing products share one controller and one price table."""

    def _tariff_with(self, preset, export_prices):
        from evse_controller.price_tiers import build_price_table

        tariff = _build_tariff()
        table = build_price_table(
            preset,
            {"standard": 30.0, "cheap": 7.5, "peak": 30.0},
            export_prices,
        )
        tariff._resolved_price_table = table
        tariff.dynamic_export = table.dynamic_export
        return tariff

    def test_flat_outgoing_export_uses_the_standard_tier(self):
        tariff = self._tariff_with(
            "iog_outgoing",
            {"standard": 12.0, "cheap": 12.0, "peak": 12.0},
        )

        assert tariff.export_price_p(datetime.now().date(), 0) == 12.0
        assert tariff.export_price_p(datetime.now().date(), 34) == 12.0
        assert tariff.dynamic_export is False

    def test_prime_export_uses_the_peak_window(self):
        tariff = self._tariff_with(
            "iog_prime",
            {"standard": 9.0, "cheap": 9.0, "peak": 16.0},
        )

        assert tariff.export_price_p(datetime.now().date(), 32) == 16.0  # 16:00
        assert tariff.export_price_p(datetime.now().date(), 20) == 9.0

    def test_no_export_tariff_returns_unknown(self):
        tariff = self._tariff_with(
            "iog_no_export",
            {"standard": 9.0, "cheap": 9.0, "peak": 16.0},
        )

        assert tariff.export_price_p(datetime.now().date(), 32) is None

    def test_live_import_price_uses_the_table(self):
        tariff = self._tariff_with(
            "iog_prime",
            {"standard": 9.0, "cheap": 9.0, "peak": 16.0},
        )

        assert tariff._current_import_rate_p(0) == pytest.approx(7.5)
        assert tariff._current_import_rate_p(12 * 60) == pytest.approx(30.0)
