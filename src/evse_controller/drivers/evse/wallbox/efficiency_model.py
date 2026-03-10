"""
Wallbox Efficiency Model

Provides efficiency data for Wallbox charging/discharging at different current settings.
Uses curve fitting to smooth out measurement errors and provide interpolated values.

Raw measured data (current in Amps, efficiency as ratio 0-1):
- Charging efficiency measured from experiments
- 11A charging data point identified as outlier and excluded from fitting
- Discharging efficiency measured separately

Note: Efficiency plateaus around 13A+. For currents beyond the measured range,
the model uses plateau values (clamping to the maximum measured efficiency range).
"""

from typing import Optional, List, Tuple


# Raw measured charging efficiency data (current -> efficiency)
RAW_CHARGING_DATA = {
    14: 0.892,
    13: 0.891,
    12: 0.886,
    11: 0.874,  # Known outlier - excluded from fitted model
    10: 0.883,
    9: 0.851,
    8: 0.830,
    7: 0.816,
    6: 0.791,
    5: 0.736,
    4: 0.679,
    3: 0.569,
}

# Data points used for charging fit (excluding the 11A outlier)
CHARGING_FITTING_DATA = {
    current: eff
    for current, eff in RAW_CHARGING_DATA.items()
    if current != 11
}

# Raw measured discharging efficiency data (current -> efficiency)
RAW_DISCHARGING_DATA = {
    15: 0.904,
    14: 0.904,
    13: 0.904,
    12: 0.901,
    11: 0.890,
    10: 0.877,
    9: 0.866,
    8: 0.849,
    7: 0.827,
    6: 0.800,
    5: 0.771,
    4: 0.724,
    3: 0.663,
}

# All discharge data appears clean, use all for fitting
DISCHARGING_FITTING_DATA = RAW_DISCHARGING_DATA.copy()

# Pre-computed polynomial coefficients (degree 3) for charging efficiency curve
# Computed from CHARGING_FITTING_DATA using numpy.polyfit (excluding 11A outlier)
# Represents: eff = c0*x^3 + c1*x^2 + c2*x + c3
CHARGING_POLYNOMIAL_COEFFICIENTS = [
    0.0004297,    # coefficient for x^3
    -0.0147606,   # coefficient for x^2
    0.1736874,    # coefficient for x^1
    0.1798733,    # coefficient for x^0 (constant)
]

# Pre-computed polynomial coefficients (degree 3) for discharging efficiency curve
# Computed from DISCHARGING_FITTING_DATA using numpy.polyfit
# Note: Discharge efficiency plateaus at higher currents
DISCHARGING_POLYNOMIAL_COEFFICIENTS = [
    0.0001434,    # coefficient for x^3
    -0.0061209,   # coefficient for x^2
    0.0900804,    # coefficient for x^1
    0.4489870,    # coefficient for x^0 (constant)
]


def _evaluate_polynomial(coefficients: List[float], x: float) -> float:
    """Evaluate polynomial at x using Horner's method for numerical stability."""
    result = 0.0
    for coef in coefficients:
        result = result * x + coef
    return result


class WallboxEfficiencyModel:
    """
    Model for Wallbox charging/discharging efficiency at different current settings.

    Uses polynomial fitting to smooth measurement errors and provide
    interpolated efficiency values for any current in the valid range.
    
    For currents beyond the measured range, efficiency values are clamped
    to the plateau region (efficiency doesn't significantly improve past ~13A).
    """

    # Valid current range (Amps)
    # Charging: 3-14A (measured range), extended with plateau for higher currents
    # Discharging: 3-15A (measured range), extended with plateau for higher currents
    MIN_CURRENT = 3.0
    MAX_CHARGING_CURRENT = 14.0  # Measured max
    MAX_DISCHARGING_CURRENT = 15.0  # Measured max
    
    # Plateau thresholds - currents above these use plateau efficiency
    CHARGING_PLATEAU_CURRENT = 13.0
    DISCHARGING_PLATEAU_CURRENT = 13.0
    
    # Optimal currents for best round-trip efficiency
    OPTIMAL_CHARGING_CURRENT = 14.0  # Peak charging efficiency
    OPTIMAL_DISCHARGING_CURRENT = 15.0  # Peak discharging efficiency

    def __init__(self, use_fitted: bool = True):
        """
        Initialize the efficiency model.

        Args:
            use_fitted: If True, use fitted curve. If False, use raw data with linear interpolation.
        """
        self.use_fitted = use_fitted

    def get_charging_efficiency(self, current: float) -> float:
        """
        Get charging efficiency for a given current.
        
        For currents above the measured range (14A), returns plateau efficiency
        (same as 13-14A region) since efficiency doesn't significantly improve.

        Args:
            current: Current in Amps (minimum 3A, no upper limit but plateaus)

        Returns:
            Efficiency as a ratio (0-1)
        """
        if current < self.MIN_CURRENT:
            raise ValueError(
                f"Current {current}A below minimum ({self.MIN_CURRENT}A)"
            )
        
        # For currents above plateau, use plateau efficiency
        # This handles extended current limits (e.g., 16A, 32A Wallboxes)
        effective_current = min(current, self.MAX_CHARGING_CURRENT)

        if self.use_fitted:
            # Use pre-computed fitted polynomial for charging
            return _evaluate_polynomial(CHARGING_POLYNOMIAL_COEFFICIENTS, effective_current)
        else:
            # Use raw data with linear interpolation
            return self._linear_interpolate(effective_current, RAW_CHARGING_DATA)

    def get_discharging_efficiency(self, current: float) -> float:
        """
        Get discharging efficiency for a given current.
        
        For currents above the measured range (15A), returns plateau efficiency
        (same as 13-15A region) since efficiency doesn't significantly improve.

        Args:
            current: Current in Amps (minimum 3A, no upper limit but plateaus)

        Returns:
            Efficiency as a ratio (0-1)
        """
        if current < self.MIN_CURRENT:
            raise ValueError(
                f"Current {current}A below minimum ({self.MIN_CURRENT}A)"
            )
        
        # For currents above plateau, use plateau efficiency
        effective_current = min(current, self.MAX_DISCHARGING_CURRENT)

        if self.use_fitted:
            # Use pre-computed fitted polynomial for discharging
            return _evaluate_polynomial(DISCHARGING_POLYNOMIAL_COEFFICIENTS, effective_current)
        else:
            # Use raw data with linear interpolation
            return self._linear_interpolate(effective_current, RAW_DISCHARGING_DATA)

    def get_round_trip_efficiency(self, charge_current: float, discharge_current: float) -> float:
        """
        Calculate round-trip efficiency for a charge/discharge cycle.

        Args:
            charge_current: Charging current in Amps
            discharge_current: Discharging current in Amps

        Returns:
            Round-trip efficiency as a ratio (0-1)
        """
        charge_eff = self.get_charging_efficiency(charge_current)
        discharge_eff = self.get_discharging_efficiency(discharge_current)
        return charge_eff * discharge_eff
    
    def get_solar_storage_efficiency(self, charge_current: float, discharge_current: Optional[float] = None) -> float:
        """
        Calculate efficiency for solar storage scenario.
        
        This is the typical use case: charge from solar at whatever current is available,
        then discharge at maximum (or specified) current for export/sale.
        
        Args:
            charge_current: Charging current from solar (Amps)
            discharge_current: Discharging current for export (Amps). 
                               If None, uses maximum efficient current (13A)
        
        Returns:
            Round-trip efficiency as a ratio (0-1)
        """
        if discharge_current is None:
            # Use the current that maximizes discharge efficiency
            discharge_current = self.CHARGING_PLATEAU_CURRENT
        
        return self.get_round_trip_efficiency(charge_current, discharge_current)
    
    def get_export_threshold_efficiency(self) -> float:
        """
        Get the round-trip efficiency at plateau currents.
        
        This represents the best-case efficiency for storing solar energy
        and later exporting it. Useful as a threshold for deciding whether
        to store vs export immediately.
        
        Returns:
            Best-case round-trip efficiency (at plateau currents)
        """
        return self.get_round_trip_efficiency(
            self.CHARGING_PLATEAU_CURRENT,
            self.DISCHARGING_PLATEAU_CURRENT
        )
    
    def _linear_interpolate(self, current: float, data: dict[int, float]) -> float:
        """Linear interpolation between known data points."""
        if current in data:
            return data[int(current)]
        
        # Find surrounding points
        sorted_currents = sorted(data.keys())
        
        if current < sorted_currents[0]:
            return data[sorted_currents[0]]
        if current > sorted_currents[-1]:
            return data[sorted_currents[-1]]
        
        for i in range(len(sorted_currents) - 1):
            lower = sorted_currents[i]
            upper = sorted_currents[i + 1]
            if lower <= current <= upper:
                # Linear interpolation
                t = (current - lower) / (upper - lower)
                return data[lower] + t * (data[upper] - data[lower])
        
        # Should not reach here
        return data[sorted_currents[0]]
    
    def get_optimal_current(self, min_current: float = MIN_CURRENT, max_current: float = MAX_CHARGING_CURRENT) -> float:
        """
        Find the current that maximizes charging efficiency within a range.

        Args:
            min_current: Minimum current to consider
            max_current: Maximum current to consider

        Returns:
            Optimal current in Amps
        """
        # Sample at 0.1A resolution to find maximum
        best_current = min_current
        best_efficiency = 0.0

        current = min_current
        while current <= max_current:
            eff = self.get_charging_efficiency(current)
            if eff > best_efficiency:
                best_efficiency = eff
                best_current = current
            current += 0.1

        return best_current
    
    def get_optimal_discharge_current(self, min_current: float = MIN_CURRENT, max_current: float = MAX_DISCHARGING_CURRENT) -> float:
        """
        Find the current that maximizes discharging efficiency within a range.

        Args:
            min_current: Minimum current to consider
            max_current: Maximum current to consider

        Returns:
            Optimal discharge current in Amps
        """
        # Sample at 0.1A resolution to find maximum
        best_current = min_current
        best_efficiency = 0.0

        current = min_current
        while current <= max_current:
            eff = self.get_discharging_efficiency(current)
            if eff > best_efficiency:
                best_efficiency = eff
                best_current = current
            current += 0.1

        return best_current

    def get_efficiency_table(self, step: float = 1.0) -> list[tuple[float, float, float]]:
        """
        Generate a table of charging and discharging efficiency values.

        Args:
            step: Current step size in Amps

        Returns:
            List of (current, charge_efficiency, discharge_efficiency) tuples
        """
        table = []
        current = self.MIN_CURRENT
        while current <= self.MAX_CHARGING_CURRENT:
            charge_eff = self.get_charging_efficiency(current)
            discharge_eff = self.get_discharging_efficiency(current)
            table.append((current, charge_eff, discharge_eff))
            current += step
        return table


# Singleton instance for convenient access
_default_model: Optional[WallboxEfficiencyModel] = None


def get_efficiency_model() -> WallboxEfficiencyModel:
    """Get the default efficiency model instance."""
    global _default_model
    if _default_model is None:
        _default_model = WallboxEfficiencyModel(use_fitted=True)
    return _default_model


def get_charging_efficiency(current: float) -> float:
    """Get charging efficiency using the default model."""
    return get_efficiency_model().get_charging_efficiency(current)


def get_discharging_efficiency(current: float) -> float:
    """Get discharging efficiency using the default model."""
    return get_efficiency_model().get_discharging_efficiency(current)


def get_round_trip_efficiency(charge_current: float, discharge_current: float) -> float:
    """Get round-trip efficiency using the default model."""
    return get_efficiency_model().get_round_trip_efficiency(charge_current, discharge_current)


def get_solar_storage_efficiency(charge_current: float, discharge_current: Optional[float] = None) -> float:
    """
    Get solar storage efficiency using the default model.
    
    Args:
        charge_current: Charging current from solar (Amps)
        discharge_current: Discharging current for export (Amps). 
                           If None, uses maximum efficient current (13A)
    
    Returns:
        Round-trip efficiency as a ratio (0-1)
    """
    return get_efficiency_model().get_solar_storage_efficiency(charge_current, discharge_current)


def get_export_threshold_efficiency() -> float:
    """
    Get the best-case round-trip efficiency threshold.
    
    This represents the efficiency when storing solar energy at optimal
    current and discharging at optimal current. Use this as a threshold
    for deciding whether to store vs export immediately.
    
    Returns:
        Best-case round-trip efficiency (~81.4% at 14A charge / 15A discharge)
    """
    return get_efficiency_model().get_round_trip_efficiency(
        WallboxEfficiencyModel.OPTIMAL_CHARGING_CURRENT,
        WallboxEfficiencyModel.OPTIMAL_DISCHARGING_CURRENT
    )
