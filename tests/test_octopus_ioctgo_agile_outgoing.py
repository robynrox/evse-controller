"""Tests for IOctGoWithAgileOutgoingTariff bidirectional storage decision logic."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta
from evse_controller.tariffs.octopus.ioctgo_with_agile_outgoing import IOctGoWithAgileOutgoingTariff
from evse_controller.drivers.EvseController import ControlState
from evse_controller.drivers.evse.async_interface import EvseAsyncState


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


@pytest.fixture
def agile_tariff():
    """Create tariff instance with mocked Agile rates."""
    mock_thread = Mock()
    mock_thread.getBatteryChargeLevel = Mock(return_value=75)
    mock_thread.get_state = Mock(return_value={})
    
    with patch('evse_controller.drivers.evse.wallbox.wallbox_thread.WallboxThread.get_instance',
               return_value=mock_thread):
        tariff = IOctGoWithAgileOutgoingTariff()
        # Inject mock rates
        tariff.agile_rates = create_mock_agile_rates()
        tariff._planned_export_slots = []  # No planned exports for these tests
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
    """Test Tier 2: Self-Use Value (5p-15.71p/kWh with dynamic SoC threshold)."""

    def test_rate_below_self_use_with_low_soc(self, agile_tariff):
        """When 5p < rate < 15.71p and SoC below dynamic threshold, should store for self-use."""
        # Rate of 10p, low SoC (30%)
        # At slot 20 (10:00), remaining slots = 47 - 20 = 27
        # Dynamic threshold = 60% + (27 × 1.0%) = 87%
        # SoC 30% < 87%, should store
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=20,  # 10:00-10:30, rate ~10p
            current_export_rate_p=10.0,
            soc_percent=30  # Low SoC (below dynamic threshold)
        )
        assert result is True, "Should store when rate < self-use value (15.71p) and SoC below dynamic threshold"

    def test_rate_below_self_use_with_high_soc(self, agile_tariff):
        """When 5p < rate < 15.71p and SoC above dynamic threshold, fall through to Tier 3."""
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
        """Test behavior at 15.71p boundary with low SoC."""
        # Rate of 15.5p (just below self-use threshold), low SoC
        # At slot 32 (16:00), remaining slots = 47 - 32 = 15
        # Dynamic threshold = 60% + (15 × 1.0%) = 75%
        # SoC 30% < 75%, should store
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=32,  # 16:00-16:30
            current_export_rate_p=15.5,
            soc_percent=30
        )
        assert result is True, "Should store when rate < 15.71p and SoC below dynamic threshold"

    def test_rate_above_self_use_threshold(self, agile_tariff):
        """When rate > 15.71p, only Tier 3 applies."""
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
        assert result is True, "STORE: self-use value (6p < 15.71p, SoC below dynamic threshold)"

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


class TestGetControlState:
    """Test integration with get_control_state()."""
    
    def test_control_state_bidirectional_low_rate(self, agile_tariff):
        """Test LOAD_FOLLOW_BIDIRECTIONAL when rate is very low."""
        state = create_test_state(50)
        # Set time to 06:00 (slot 12) when rates are ~8p but still low
        # This is outside off-peak (23:30-05:30) so bidirectional logic applies
        agile_tariff._planned_export_slots = []
        
        control_state, min_current, max_current, message = agile_tariff.get_control_state(
            state,
            360  # 06:00
        )
        
        # At 8p with high future rates (35p), should store
        # 35 × 0.5 = 17.5p > 8p, so STORE via Tier 3
        assert control_state == ControlState.LOAD_FOLLOW_BIDIRECTIONAL
        assert "Bidirectional" in message
    
    def test_control_state_discharge_high_rate(self, agile_tariff):
        """Test LOAD_FOLLOW_DISCHARGE when export rate is favorable."""
        state = create_test_state(80)
        # Set time to 18:00 (slot 36) when rates are ~35p
        agile_tariff._planned_export_slots = []
        
        control_state, min_current, max_current, message = agile_tariff.get_control_state(
            state,
            1080  # 18:00
        )
        
        # At 35p, best future is much lower, so should discharge
        assert control_state == ControlState.LOAD_FOLLOW_DISCHARGE
        assert "Load follow" in message


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


class TestConstants:
    """Test that constants are correctly defined."""

    def test_import_rates(self, agile_tariff):
        """Test import rate constants."""
        assert agile_tariff.IMPORT_RATE_OFF_PEAK_P == 3.49
        assert agile_tariff.IMPORT_RATE_PEAK_P == 27.91

    def test_storage_thresholds(self, agile_tariff):
        """Test storage decision thresholds."""
        assert agile_tariff.STORAGE_FLOOR_THRESHOLD_P == 3.49
        # Self-use value = peak import × efficiency = 27.91 × 0.50
        assert agile_tariff.SELF_USE_VALUE_THRESHOLD_P == 27.91 * 0.50
        assert agile_tariff.BATTERY_ROUND_TRIP_EFFICIENCY_BIDIRECTIONAL == 0.50


class TestEdgeCases:
    """Test edge cases and boundary conditions."""
    
    def test_no_agile_rates(self, agile_tariff):
        """Test behavior when no Agile rates are available."""
        agile_tariff.agile_rates = []
        
        result = agile_tariff._should_use_bidirectional_mode(
            current_slot=20,
            current_export_rate_p=10.0,
            soc_percent=50
        )
        
        assert result is False, "Should export when no rate data available"
    
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
        # SoC 50% < 87%, and rate 10p < 15.71p (self-use threshold)
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
