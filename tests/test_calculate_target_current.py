"""Unit tests for the _compute_wallbox_current static method in EvseController."""

import pytest
from evse_controller.drivers.EvseController import EvseController


class TestComputeWallboxCurrent:
    """Test the _compute_wallbox_current static method which calculates desired Wallbox current."""

    def test_zero_home_power_no_bias(self):
        """Test with zero home power and zero bias - should return 0."""
        result = EvseController._compute_wallbox_current(
            home_power=0,
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240
        )
        assert result == 0

    def test_zero_home_power_with_positive_discharge_bias(self):
        """Test with zero home power and positive discharge bias - should discharge slightly."""
        result = EvseController._compute_wallbox_current(
            home_power=0,
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0.5,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240
        )
        assert result < 0  # Negative means discharge
        assert abs(result) >= 3  # Should be at least the minimum discharge

    def test_zero_home_power_with_positive_charge_bias(self):
        """Test with zero home power and positive charge bias - should charge slightly."""
        result = EvseController._compute_wallbox_current(
            home_power=0,
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0.5,
            grid_voltage=240
        )
        assert result > 0  # Positive means charge

    def test_importing_below_threshold(self):
        """Test with importing power below discharge activation threshold - should return 0."""
        result = EvseController._compute_wallbox_current(
            home_power=50,  # Below minimum_discharge_activation_power of 100
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240
        )
        assert result == 0

    def test_importing_above_threshold_no_bias(self):
        """Test with importing power above discharge activation threshold and no bias."""
        result = EvseController._compute_wallbox_current(
            home_power=1200,  # Above minimum_discharge_activation_power of 100, gives 5A discharge
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240
        )
        assert result == -5.0  # Negative means discharge, exact value should be -5.0A

    def test_importing_without_bias_gives_correct_current(self):
        """Test that importing 1200W at 240V with no bias gives exactly -5A discharge (1200W/240V=5A)."""
        result = EvseController._compute_wallbox_current(
            home_power=1200,  # 1200W import
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,  # No bias
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240  # 240V
        )
        # 1200W at 240V should result in 5A discharge to cancel the import
        assert result == -5.0  # Negative means discharge, magnitude should be 5A

    def test_importing_with_negative_bias_gives_correct_current(self):
        """Test that importing 1200W at 240V with -0.5 bias gives -4.5A discharge (-5.0 - (-0.5) = -4.5)."""
        result = EvseController._compute_wallbox_current(
            home_power=1200,  # 1200W import
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=-0.5,  # Negative bias of -0.5
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240  # 240V
        )
        # Base current would be -5.0, with -0.5 bias: -5.0 - (-0.5) = -4.5
        assert result == -4.5  # Negative means discharge, magnitude should be 4.5A

    def test_importing_with_positive_bias_gives_correct_current(self):
        """Test with importing power above threshold and positive discharge bias - should increase discharge."""
        result = EvseController._compute_wallbox_current(
            home_power=1200,  # Same power
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0.3,  # Positive bias of 0.3
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240
        )

        # With positive bias of +0.3, the discharge should be increased: -(5 + 0.3) = -5.3
        assert result == -5.3  # With +0.3 bias: -5.3A discharge

    def test_exporting_below_threshold(self):
        """Test with exporting power below charge activation threshold - should return 0."""
        result = EvseController._compute_wallbox_current(
            home_power=-50,  # Below minimum_charge_activation_power of 100 (in magnitude)
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240
        )
        assert result == 0

    def test_exporting_above_threshold_no_bias(self):
        """Test with exporting power above charge activation threshold and no bias."""
        result = EvseController._compute_wallbox_current(
            home_power=-1200,  # -1200W export (magnitude 1200 > 100), gives 5A charge
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240
        )
        assert result == 5.0  # Positive means charge, exact value should be 5.0A

    def test_exporting_without_bias_gives_correct_current(self):
        """Test with exporting power above threshold and negative charge bias - should reduce charge."""
        result = EvseController._compute_wallbox_current(
            home_power=-1200,  # -1200W (exporting) should give 5A charge without bias
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,  # No bias
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,  # No charge bias
            grid_voltage=240
        )
        assert result == 5.0   # Without bias: 5.0A charge
        
    def test_exporting_with_negative_bias_gives_correct_current(self):
        result = EvseController._compute_wallbox_current(
            home_power=-1200,  # Same export power
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=-0.3,  # Negative charge bias
            grid_voltage=240
        )
        
        # With negative charge bias of -0.3, the charge should be reduced: 5 - 0.3 = 4.7
        assert result == 4.7   # With -0.3 bias: 4.7A charge

    def test_exporting_with_positive_bias_gives_correct_current(self):
        """Test with exporting power above threshold and positive charge bias - should increase charge."""
        result = EvseController._compute_wallbox_current(
            home_power=-1200,  # Same export power
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0.3,  # Positive charge bias
            grid_voltage=240
        )
        
        # With positive charge bias of +0.3, the charge should be increased: 5 + 0.3 = 5.3
        assert result == 5.3   # With +0.3 bias: 5.3A charge

    def test_edge_case_minimum_current_greater_than_maximum(self):
        """Test the edge case where min current is greater than max (should be adjusted)."""
        result = EvseController._compute_wallbox_current(
            home_power=600,
            discharge_current_min=16,  # Greater than max
            discharge_current_max=8,   # Less than min
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240
        )
        # The function should internally adjust min to be <= max
        assert result < 0  # Still discharging, but with adjusted parameters

    def test_minimum_discharge_boundary_conditions(self):
        """Test calculations at boundary conditions."""
        # At exactly the minimum discharge activation power
        result_at_threshold = EvseController._compute_wallbox_current(
            home_power=100,  # Exactly at minimum_discharge_activation_power
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240
        )
        
        # Should be discharging since power equals threshold
        assert result_at_threshold == -3
        
        # Just below the minimum discharge activation power
        result_below_threshold = EvseController._compute_wallbox_current(
            home_power=99,  # Just below minimum_discharge_activation_power
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240
        )
        
        # Should be 0 since power is below threshold
        assert result_below_threshold == 0

    def test_minimum_charge_boundary_conditions(self):
        """Test calculations at boundary conditions."""
        # At exactly the minimum discharge activation power
        result_at_threshold = EvseController._compute_wallbox_current(
            home_power=-100,  # Exactly at minimum_charge_activation_power
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240
        )
        
        # Should be discharging since power equals threshold
        assert result_at_threshold == 3
        
        # Just below the minimum discharge activation power
        result_below_threshold = EvseController._compute_wallbox_current(
            home_power=-99,  # Just below minimum_charge_activation_power
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240
        )
        
        # Should be 0 since power is below threshold
        assert result_below_threshold == 0

    def test_maximum_discharge_boundary_condition(self):
        """Test calculations at boundary conditions."""
        # At exactly the minimum discharge activation power
        result_above_threshold = EvseController._compute_wallbox_current(
            home_power=4000,
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240
        )
        
        # Should be discharging since power equals threshold
        assert result_above_threshold == -16

    def test_maximum_charge_boundary_condition(self):
        """Test calculations at boundary conditions."""
        # At exactly the minimum discharge activation power
        result_above_threshold = EvseController._compute_wallbox_current(
            home_power=-4000,
            discharge_current_min=3,
            discharge_current_max=16,
            minimum_discharge_activation_power=100,
            discharge_current_bias=0,
            charge_current_min=3,
            charge_current_max=16,
            minimum_charge_activation_power=100,
            charge_current_bias=0,
            grid_voltage=240
        )
        
        # Should be discharging since power equals threshold
        assert result_above_threshold == 16
