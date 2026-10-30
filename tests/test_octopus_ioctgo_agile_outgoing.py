"""Tests for IOctGoWithAgileOutgoingTariff bidirectional storage decision logic."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta
from evse_controller.tariffs.octopus.ioctgo_with_agile_outgoing import IOctGoWithAgileOutgoingTariff
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


def _build_tariff() -> IOctGoWithAgileOutgoingTariff:
    """Create a tariff instance with the Wallbox thread mocked out."""
    mock_thread = Mock()
    mock_thread.getBatteryChargeLevel = Mock(return_value=75)
    mock_thread.get_state = Mock(return_value={})

    with patch('evse_controller.drivers.evse.wallbox.wallbox_thread.WallboxThread.get_instance',
               return_value=mock_thread):
        tariff = IOctGoWithAgileOutgoingTariff()
    tariff.agile_rates = create_mock_agile_rates()
    tariff._planned_export_slots = []  # No planned exports for these tests
    return tariff


@pytest.fixture
def agile_tariff():
    """Create tariff instance with mocked Agile rates."""
    yield _build_tariff()


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

    def test_uses_surplus_and_snaps_down_to_measured_point(self, agile_tariff):
        # 1680 W / 240 V = 7 A, -0.5 A bias = 6.5 A -> 6 A measured point.
        self._attach(agile_tariff, 1680)
        assert agile_tariff._estimate_solar_harvest_current() == pytest.approx(6)

    def test_surplus_just_over_5a_uses_5a(self, agile_tariff):
        # 1320 W / 240 V = 5.5 A, -0.5 A bias = 5.0 A -> 5 A.
        self._attach(agile_tariff, 1320)
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
        assert agile_tariff._estimate_solar_harvest_current() == pytest.approx(6)


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

    def test_no_shortfall_when_projection_meets_house_floor(self, agile_tariff):
        """At 22:00 only 3 slots remain, so 3% is lost."""
        self._set_time(agile_tariff, 22, 0)
        # 70% - 3% = 67%, above the 55% house floor.
        assert agile_tariff._update_energy_shortfall(70) is False
        assert agile_tariff._energy_shortfall is False

    def test_projection_at_house_floor_is_not_shortfall(self, agile_tariff):
        """The comparison is strict: exactly on the floor stays aggressive."""
        self._set_time(agile_tariff, 12, 0)
        # 78% - 23% = 55%, exactly the floor.
        assert agile_tariff._update_energy_shortfall(78) is False
        assert agile_tariff._energy_shortfall is False

    def test_target_headroom_does_not_force_conservative(self, agile_tariff):
        """A projection between the floor and the target stays aggressive."""
        self._set_time(agile_tariff, 12, 0)
        # 80% - 23% = 57%: below the 60% bulk target but above the 55% floor.
        assert agile_tariff._update_energy_shortfall(80) is False
        assert agile_tariff._energy_shortfall is False

    def test_house_floor_can_be_configured(self, agile_tariff, monkeypatch):
        """The margin is configuration, not a hidden constant."""
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 10)
        # 59% - 3% = 56%, below the 60% target but above the 50% floor.
        self._set_time(agile_tariff, 22, 0)
        assert agile_tariff._house_load_floor_soc() == 50
        assert agile_tariff._update_energy_shortfall(59) is False

        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 0)
        assert agile_tariff._house_load_floor_soc() == 60
        assert agile_tariff._update_energy_shortfall(59) is True

    def test_house_floor_never_enters_the_protection_reserve(
        self, agile_tariff, monkeypatch
    ):
        """A low target plus a large margin must not floor below the reserve."""
        monkeypatch.setattr(config, "MIN_AGILE_DISCHARGE_SOC", 25)
        monkeypatch.setattr(config, "HOUSE_LOAD_MARGIN_PERCENT", 10)
        assert agile_tariff._house_load_floor_soc() == 26

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
        state = create_test_state(30)

        control_state, _, _, reason = agile_tariff.get_control_state(state, 12 * 60)

        assert control_state == ControlState.LOAD_FOLLOW_BIDIRECTIONAL
        assert "Shortfall" in reason

    def test_get_control_state_uses_tier_logic_when_not_shortfall(self, agile_tariff):
        """Outside shortfall, the existing tier logic still applies."""
        self._set_time(agile_tariff, 22, 0)
        state = create_test_state(80)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 22 * 60)

        # At 22:00 with 11p current and 10p best future, 10p × solar round trip
        # (~0.547) < 11p, so the tier logic chooses load-follow discharge.
        assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE

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

    def test_no_shortfall_configures_aggressive_discharge(self, agile_tariff):
        """Outside shortfall the aggressive bias is used, at the break-even."""
        mock_controller = Mock()
        mock_controller.use_new_current_calculation = False
        state = create_test_state(80)

        # Slot 24 (12:00) exports at 9p.  The activation must be the
        # break-even load, not the old blanket 1 W.
        expected = agile_tariff.discharge_activation_threshold_w(9.0)

        agile_tariff._energy_shortfall = False
        agile_tariff.set_home_demand_levels(mock_controller, state, 12 * 60)

        assert 0 < expected < 720
        mock_controller.setDischargeActivationPower.assert_called_with(expected)
        mock_controller.setDischargeCurrentBias.assert_called_with(0.5)


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

    def test_bias_region_raises_the_break_even(self, agile_tariff):
        """Once +0.5 A exports, the extra 120 W must clear the hurdle too."""
        efficiency = agile_tariff.EFFICIENCY_MODEL.discharge_efficiency(
            agile_tariff.MIN_DISCHARGE_CURRENT
        )
        target_hurdle_p = 27.3
        dc_cost_p = (
            target_hurdle_p - agile_tariff.MIN_PROFIT_MARGIN_P_PER_KWH
        ) * efficiency
        self._ledger(agile_tariff, dc_kwh=1.0, cost_gbp=dc_cost_p / 100.0)

        hurdle_p = (
            agile_tariff._delivered_cost_per_kwh_p()
            + agile_tariff.MIN_PROFIT_MARGIN_P_PER_KWH
        )
        clamped = 720.0 * hurdle_p / 30.0
        biased = 240.0 * hurdle_p / (30.0 - hurdle_p)
        assert clamped > 600

        assert agile_tariff.discharge_activation_threshold_w(
            0.0, import_rate_p=30.0
        ) == pytest.approx(round(biased), abs=1)

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
    def _pin_plan(tariff, slot, soc, shortfall=False):
        """Stop get_control_state() recalculating the plan for this test."""
        tariff._last_plan_slot = slot
        tariff._last_plan_soc = soc
        tariff._prev_battery_level = soc
        tariff._planned_export_slots = []
        tariff._plan_cache = []
        tariff._energy_shortfall = shortfall

    def test_off_peak_charges_to_the_effective_cap(self, agile_tariff):
        self._set_time(agile_tariff, 23, 45)
        state = create_test_state(50)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 23 * 60 + 45)

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.CHARGE
        assert snapshot.mode.startswith("Max charge")
        assert "charging to the" in snapshot.reason
        assert snapshot.efficiency_label == "charge"
        assert snapshot.min_round_trip_saving_p == pytest.approx(2.3, abs=0.05)

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

    def test_export_slot_mode_is_aggressive(self, agile_tariff):
        self._set_time(agile_tariff, 18, 0)
        self._pin_plan(agile_tariff, 36, 80)
        agile_tariff._planned_export_slots = [36]
        agile_tariff._plan_cache = [36]
        state = create_test_state(80)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 18 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.DISCHARGE
        assert snapshot.mode.startswith("Max discharge")
        assert "aggressive" in snapshot.mode
        assert "18:00–18:30" in snapshot.reason
        assert snapshot.efficiency_label == "discharge"

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

    def test_conservative_reason_names_the_house_floor(self, agile_tariff):
        self._set_time(agile_tariff, 12, 0)
        self._pin_plan(agile_tariff, 24, 30, shortfall=True)
        agile_tariff._projected_soc_at_2330 = 54
        state = create_test_state(30)

        agile_tariff.get_control_state(state, 12 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert "55% house floor" in snapshot.reason
        assert "60% bulk target" in snapshot.detail
        assert "5% flexibility" in snapshot.detail

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

    def test_load_follow_states_do_not_charge(self, agile_tariff):
        self._set_time(agile_tariff, 22, 0)
        self._pin_plan(agile_tariff, 44, 80)
        state = create_test_state(80)

        control_state, _, _, _ = agile_tariff.get_control_state(state, 22 * 60)

        snapshot = agile_tariff._decision_snapshot
        assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
        assert snapshot.mode.startswith("Do not charge")
        assert "aggressive discharge" in snapshot.mode

    def test_aggressive_reason_includes_the_break_even(self, agile_tariff):
        self._set_time(agile_tariff, 22, 0)
        self._pin_plan(agile_tariff, 44, 80)
        agile_tariff._current_discharge_activation_w = Mock(return_value=250)
        state = create_test_state(80)

        agile_tariff.get_control_state(state, 22 * 60)

        reason = agile_tariff._decision_snapshot.reason
        assert "starts at ≥250 W house import" in reason
        assert "3 A minimum would export more than it saves" in reason

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
        assert "stored energy costs more" in reason

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
    """The dashboard can state the spread a round trip must clear."""

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
    def _prime(tariff, clock, ch1, ch2):
        tariff.evseController.current_grid_power = Power(
            ch1Watts=ch1, ch2Watts=ch2, voltage=240
        )
        tariff._savings_last_time = None
        tariff._accumulate_daily_savings()

    @staticmethod
    def _run(tariff, clock, minutes, ch1, ch2):
        tariff.evseController.current_grid_power = Power(
            ch1Watts=ch1, ch2Watts=ch2, voltage=240
        )
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


class TestDashboardModeLine:
    """The compact decision line sits above the 48-slot strip."""

    def test_mode_line_precedes_the_strip(self, agile_tariff):
        agile_tariff._record_decision(
            mode="Harvest 5 A · aggressive discharge",
            reason="future 24.6p at 16:30–17:00 × 73% = 17.7p now",
            soc_percent=47,
            efficiency_pct=73,
            efficiency_label="round trip",
            min_round_trip_saving_p=0.3,
            min_round_trip_saving_basis="solar",
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
        assert "saved today" in html

    def test_strip_is_compact(self, agile_tariff):
        agile_tariff._record_decision(
            mode="Idle — battery protection",
            reason="SoC at the reserve",
            soc_percent=20,
        )

        html = agile_tariff.get_dashboard_html()

        assert 'class="agile-rate-strip" style="height:28px' in html

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
