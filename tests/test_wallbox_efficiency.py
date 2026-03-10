"""Tests for Wallbox efficiency model."""

import pytest
import sys
sys.path.insert(0, '/workspaces/evse-controller/src')

from evse_controller.drivers.evse.wallbox.efficiency_model import (
    WallboxEfficiencyModel,
    RAW_CHARGING_DATA,
    RAW_DISCHARGING_DATA,
    CHARGING_FITTING_DATA,
    get_charging_efficiency,
    get_discharging_efficiency,
    get_solar_storage_efficiency,
    get_export_threshold_efficiency,
)


class TestWallboxEfficiencyModel:
    """Test the Wallbox efficiency model."""
    
    def test_raw_data_excludes_outlier(self):
        """Verify 11A is excluded from charging fitting data."""
        assert 11 in RAW_CHARGING_DATA
        assert 11 not in CHARGING_FITTING_DATA
        assert len(CHARGING_FITTING_DATA) == len(RAW_CHARGING_DATA) - 1
        
        # Discharge data has no outliers
        assert 11 in RAW_DISCHARGING_DATA
    
    def test_discharge_data(self):
        """Test discharge efficiency data is available."""
        assert len(RAW_DISCHARGING_DATA) == 13  # 3A through 15A
        assert RAW_DISCHARGING_DATA[15] == 0.904
        assert RAW_DISCHARGING_DATA[3] == 0.663
        
        # Discharge efficiency is generally higher at lower currents
        # but they converge at higher currents
        # At 3A: discharge 0.663 vs charge 0.569 (discharge wins)
        # At 10A: discharge 0.877 vs charge 0.883 (charge wins slightly)
        assert RAW_DISCHARGING_DATA[3] > RAW_CHARGING_DATA[3]
    
    def test_valid_current_range(self):
        """Test that valid current range is correct."""
        model = WallboxEfficiencyModel()
        assert model.MIN_CURRENT == 3.0
        assert model.MAX_CHARGING_CURRENT == 14.0
        assert model.MAX_DISCHARGING_CURRENT == 15.0
    
    def test_efficiency_at_known_points(self):
        """Test efficiency values at measured points."""
        model = WallboxEfficiencyModel(use_fitted=True)
        
        # Test at boundaries
        eff_3a = model.get_charging_efficiency(3.0)
        eff_14a = model.get_charging_efficiency(14.0)
        
        # Efficiency should be between 0 and 1
        assert 0 < eff_3a < 1
        assert 0 < eff_14a < 1
        
        # 14A should be more efficient than 3A
        assert eff_14a > eff_3a
    
    def test_efficiency_increases_with_current(self):
        """Test that efficiency generally increases with current."""
        model = WallboxEfficiencyModel(use_fitted=True)
        
        # Sample at several points
        efficiencies = []
        for current in [3, 5, 7, 9, 11, 13, 14]:
            eff = model.get_charging_efficiency(current)
            efficiencies.append(eff)
        
        # Check general trend (allowing small local variations)
        assert efficiencies[-1] > efficiencies[0]  # 14A > 3A
    
    def test_outlier_interpolation(self):
        """Test that 11A charging gets a smoothed value, not the outlier."""
        model_fitted = WallboxEfficiencyModel(use_fitted=True)
        model_raw = WallboxEfficiencyModel(use_fitted=False)
        
        eff_fitted = model_fitted.get_charging_efficiency(11.0)
        eff_raw = model_raw.get_charging_efficiency(11.0)
        
        # Fitted value should differ from raw outlier
        assert eff_fitted != RAW_CHARGING_DATA[11]
        
        # Raw interpolation should return the outlier value
        assert eff_raw == RAW_CHARGING_DATA[11]
        
        # Discharge at 11A should use actual measured data (not an outlier)
        discharge_fitted = model_fitted.get_discharging_efficiency(11.0)
        discharge_raw = model_raw.get_discharging_efficiency(11.0)
        assert discharge_raw == RAW_DISCHARGING_DATA[11]
    
    def test_current_out_of_range(self):
        """Test that below-minimum currents raise error."""
        model = WallboxEfficiencyModel()
        
        with pytest.raises(ValueError):
            model.get_charging_efficiency(2.0)
        
        with pytest.raises(ValueError):
            model.get_discharging_efficiency(2.0)
    
    def test_plateau_behavior(self):
        """Test that efficiency plateaus for currents above measured range."""
        model = WallboxEfficiencyModel()
        
        # Charging efficiency should plateau around 13-14A
        eff_13a = model.get_charging_efficiency(13.0)
        eff_14a = model.get_charging_efficiency(14.0)
        eff_16a = model.get_charging_efficiency(16.0)
        eff_32a = model.get_charging_efficiency(32.0)
        
        # 16A and 32A should return same as 14A (plateau)
        assert eff_16a == eff_14a
        assert eff_32a == eff_14a
        assert eff_14a >= eff_13a  # Should be at plateau
        
        # Discharging efficiency should plateau around 13-15A
        eff_d_13a = model.get_discharging_efficiency(13.0)
        eff_d_15a = model.get_discharging_efficiency(15.0)
        eff_d_20a = model.get_discharging_efficiency(20.0)
        
        # 20A should return same as 15A (plateau)
        assert eff_d_20a == eff_d_15a
    
    def test_round_trip_efficiency(self):
        """Test round-trip efficiency calculation."""
        model = WallboxEfficiencyModel()
        
        # Round-trip should be product of charge and discharge efficiency
        charge_eff = model.get_charging_efficiency(10.0)
        discharge_eff = model.get_discharging_efficiency(10.0)
        round_trip = model.get_round_trip_efficiency(10.0, 10.0)
        
        assert round_trip == pytest.approx(charge_eff * discharge_eff)
        
        # Round-trip should always be less than individual efficiencies
        assert round_trip < charge_eff
        assert round_trip < discharge_eff
    
    def test_optimal_current(self):
        """Test finding optimal current for efficiency."""
        model = WallboxEfficiencyModel()
        
        optimal = model.get_optimal_current()
        
        # Optimal should be in valid range
        assert model.MIN_CURRENT <= optimal <= model.MAX_CHARGING_CURRENT
        
        # Optimal should be at higher current end (based on data trend)
        assert optimal >= 12.0
    
    def test_efficiency_table(self):
        """Test generating efficiency table."""
        model = WallboxEfficiencyModel()
        
        table = model.get_efficiency_table(step=1.0)
        
        # Should have entries for 3A through 14A (12 values)
        assert len(table) == 12
        
        # All efficiencies should be valid
        for current, charge_eff, discharge_eff in table:
            assert 0 < charge_eff < 1
            assert 0 < discharge_eff < 1
            assert discharge_eff >= charge_eff  # Discharge is generally more efficient
    
    def test_solar_storage_efficiency(self):
        """Test solar storage efficiency calculation."""
        model = WallboxEfficiencyModel()
        
        # Low solar current (e.g., cloudy day)
        eff_low = model.get_solar_storage_efficiency(5.0)
        
        # High solar current (e.g., sunny day)
        eff_high = model.get_solar_storage_efficiency(13.0)
        
        # High current should be more efficient
        assert eff_high > eff_low
        
        # Custom discharge current
        eff_custom = model.get_solar_storage_efficiency(10.0, discharge_current=10.0)
        eff_default = model.get_solar_storage_efficiency(10.0)
        
        # Default (13A discharge) should be more efficient than 10A discharge
        assert eff_default > eff_custom
    
    def test_export_threshold_efficiency(self):
        """Test export threshold efficiency."""
        model = WallboxEfficiencyModel()
        
        threshold = model.get_export_threshold_efficiency()
        
        # Should be around 0.80 (best case round-trip)
        assert 0.75 < threshold < 0.85
        
        # Should equal round-trip at plateau currents
        expected = model.get_round_trip_efficiency(
            model.CHARGING_PLATEAU_CURRENT,
            model.DISCHARGING_PLATEAU_CURRENT
        )
        assert threshold == expected
    
    def test_optimal_discharge_current(self):
        """Test finding optimal discharge current."""
        model = WallboxEfficiencyModel()
        
        optimal = model.get_optimal_discharge_current()
        
        # Should be in valid range
        assert model.MIN_CURRENT <= optimal <= model.MAX_DISCHARGING_CURRENT
        
        # Should be at higher current end (plateau region)
        assert optimal >= 13.0
    
    def test_fitted_vs_raw_at_measured_points(self):
        """Compare fitted values to raw measurements for charge and discharge."""
        model_fitted = WallboxEfficiencyModel(use_fitted=True)
        
        print("\n\nCharging: Fitted vs Raw Data:")
        print("-" * 50)
        print(f"{'Current':<10} {'Raw':<10} {'Fitted':<10} {'Diff':<10}")
        print("-" * 50)
        
        for current in sorted(CHARGING_FITTING_DATA.keys()):
            raw = CHARGING_FITTING_DATA[current]
            fitted = model_fitted.get_charging_efficiency(float(current))
            diff = fitted - raw
            print(f"{current}A        {raw:.4f}     {fitted:.4f}     {diff:+.4f}")
        
        print("-" * 50)
        # Also show the outlier
        raw_outlier = RAW_CHARGING_DATA[11]
        fitted_at_11 = model_fitted.get_charging_efficiency(11.0)
        print(f"11A (outlier): Raw={raw_outlier:.4f}, Fitted={fitted_at_11:.4f}, Diff={fitted_at_11 - raw_outlier:+.4f}")
        
        print("\n\nDischarging: Fitted vs Raw Data:")
        print("-" * 50)
        print(f"{'Current':<10} {'Raw':<10} {'Fitted':<10} {'Diff':<10}")
        print("-" * 50)
        
        for current in sorted(RAW_DISCHARGING_DATA.keys()):
            raw = RAW_DISCHARGING_DATA[current]
            fitted = model_fitted.get_discharging_efficiency(float(current))
            diff = fitted - raw
            print(f"{current}A        {raw:.4f}     {fitted:.4f}     {diff:+.4f}")
        print()


def test_convenience_functions():
    """Test module-level convenience functions."""
    eff = get_charging_efficiency(10.0)
    assert 0 < eff < 1
    
    eff_discharge = get_discharging_efficiency(10.0)
    assert 0 < eff_discharge < 1
    
    # Discharge should be more efficient than charge at 10A
    assert eff_discharge > eff
    
    # Solar storage efficiency
    eff_solar = get_solar_storage_efficiency(10.0)
    assert 0 < eff_solar < 1
    
    # Export threshold
    threshold = get_export_threshold_efficiency()
    assert 0.75 < threshold < 0.85
    
    model = WallboxEfficiencyModel()
    optimal = model.get_optimal_current()
    assert 3.0 <= optimal <= 14.0
