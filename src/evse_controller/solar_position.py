"""Solar position helpers used by the forecast analysis and models."""

from __future__ import annotations

import math
from datetime import datetime, timezone


def solar_position(
    when: datetime, latitude: float, longitude: float
) -> tuple[float, float]:
    """Approximate solar elevation and azimuth in degrees.

    Uses the NOAA solar-position equations, including the equation of time.
    *when* is converted to UTC; *longitude* is east-positive.
    """
    when = when.astimezone(timezone.utc)
    day_of_year = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60.0 + when.second / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (
        day_of_year - 1 + (hour - 12.0) / 24.0
    )
    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    time_offset = equation_of_time + 4.0 * longitude
    true_solar_time = hour * 60.0 + time_offset
    hour_angle = math.radians(true_solar_time / 4.0 - 180.0)
    latitude_rad = math.radians(latitude)
    cos_zenith = (
        math.sin(latitude_rad) * math.sin(declination)
        + math.cos(latitude_rad)
        * math.cos(declination)
        * math.cos(hour_angle)
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    elevation = 90.0 - math.degrees(zenith)
    denominator = math.cos(latitude_rad) * math.sin(zenith)
    if abs(denominator) < 1e-9:
        azimuth = 0.0
    else:
        cos_azimuth = (
            math.sin(latitude_rad) * math.cos(zenith)
            - math.sin(declination)
        ) / denominator
        cos_azimuth = max(-1.0, min(1.0, cos_azimuth))
        offset = math.degrees(math.acos(cos_azimuth))
        azimuth = 180.0 - offset if hour_angle < 0 else 180.0 + offset
    return elevation, azimuth
