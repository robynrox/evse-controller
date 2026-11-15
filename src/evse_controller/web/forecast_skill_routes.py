"""Forecast-skill report for the solar and heat-pump components."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Optional, Sequence

from flask import render_template, request

from evse_controller.forecast_skill import (
    ComponentSkill,
    DaySkill,
    build_day_skill,
)
from evse_controller.utils.config import config


WIDTH = 1200
LEFT = 62
RIGHT = 18
TOP = 18
BOTTOM = 36


def _parse_day(value: Optional[str]) -> date:
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return date.today() - timedelta(days=1)


def _fmt(value, digits=2, suffix="") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


def _line_path(values: Sequence[Optional[float]], x_of, y_of) -> str:
    segments = []
    current = []
    for index, value in enumerate(values):
        if value is None:
            if current:
                segments.append(current)
                current = []
            continue
        current.append((x_of(index), y_of(value)))
    if current:
        segments.append(current)
    return " ".join(
        "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in segment)
        for segment in segments
        if segment
    )


def _line_chart(
    series: Sequence[dict],
    *,
    y_label: str,
    unit: str,
    height: int = 180,
) -> str:
    plot_w = WIDTH - LEFT - RIGHT
    plot_h = height - TOP - BOTTOM
    values = [
        value
        for item in series
        for value in item["values"]
        if value is not None
    ]
    if values:
        low = min(values)
        high = max(values)
    else:
        low, high = 0.0, 1.0
    if high - low < 1e-9:
        high = low + 1.0
    pad = (high - low) * 0.12
    low -= pad
    high += pad
    count = max(1, len(series[0]["values"]))

    def x_of(index: int) -> float:
        return LEFT + (index / max(1, count - 1)) * plot_w

    def y_of(value: float) -> float:
        return TOP + plot_h - ((value - low) / (high - low)) * plot_h

    svg = [
        f'<svg class="chart" viewBox="0 0 {WIDTH} {height}" '
        f'role="img" aria-label="{y_label}">'
    ]
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        value = low + (high - low) * fraction
        y = y_of(value)
        svg.append(
            f'<line x1="{LEFT}" y1="{y:.1f}" x2="{WIDTH - RIGHT}" '
            f'y2="{y:.1f}" class="grid"/>'
        )
        svg.append(
            f'<text x="{LEFT - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'class="axis">{value:.1f}{unit}</text>'
        )
    for index in range(0, count, 4):
        x = x_of(index)
        hour = index // 2
        minute = "30" if index % 2 else "00"
        svg.append(
            f'<text x="{x:.1f}" y="{height - 12}" text-anchor="middle" '
            f'class="axis">{hour:02d}:{minute}</text>'
        )
    for item in series:
        path = _line_path(item["values"], x_of, y_of)
        if path:
            svg.append(
                f'<path d="{path}" fill="none" stroke="{item["color"]}" '
                f'stroke-width="{item.get("width", 2)}" '
                f'stroke-dasharray="{item.get("dash", "")}"/>'
            )
    legend_x = LEFT
    for item in series:
        svg.append(
            f'<line x1="{legend_x}" y1="10" x2="{legend_x + 18}" y2="10" '
            f'stroke="{item["color"]}" stroke-width="3" '
            f'stroke-dasharray="{item.get("dash", "")}"/>'
        )
        svg.append(
            f'<text x="{legend_x + 23}" y="14" class="legend">'
            f'{item["name"]}</text>'
        )
        legend_x += 170
    svg.append("</svg>")
    return "".join(svg)


def _weather_charts(skill: DaySkill) -> tuple[str, str]:
    temperature = _line_chart(
        [
            {
                "name": "Forecast",
                "color": "#1565c0",
                "values": [
                    point.forecast_temperature_c for point in skill.weather
                ],
            },
            {
                "name": "Actual",
                "color": "#222222",
                "values": [
                    point.actual_temperature_c for point in skill.weather
                ],
            },
        ],
        y_label="Temperature forecast vs actual",
        unit="°C",
    )
    gti = _line_chart(
        [
            {
                "name": "Forecast",
                "color": "#ef6c00",
                "values": [
                    point.forecast_gti_wm2 for point in skill.weather
                ],
            },
            {
                "name": "Actual",
                "color": "#222222",
                "values": [
                    point.actual_gti_wm2 for point in skill.weather
                ],
            },
        ],
        y_label="Tilted irradiance forecast vs actual",
        unit=" W",
    )
    return temperature, gti


def _heatmap_color(probability: float) -> str:
    # White to blue; probability is 0..1.
    strength = max(0.0, min(1.0, probability))
    red = int(255 - strength * 220)
    green = int(255 - strength * 120)
    blue = 255
    return f"rgb({red},{green},{blue})"


def _component_chart(component: ComponentSkill) -> str:
    height = 300
    plot_w = WIDTH - LEFT - RIGHT
    plot_h = height - TOP - BOTTOM - 12
    histogram = component.histogram
    probabilities = histogram.probabilities
    bins = max(1, int(math.ceil(histogram.max_kw / histogram.bin_width_kw)))
    line_values = [
        value
        for point in component.points
        for value in (
            point.forecast_kw,
            point.actual_kw,
            point.forecast_p90_kw,
            point.baseline_kw,
        )
        if value is not None
    ]
    chart_max = max(
        histogram.max_kw,
        max(line_values, default=0.0),
    )
    chart_max = max(1.0, chart_max * 1.05)
    cell_w = plot_w / 48
    heatmap_h = plot_h * histogram.max_kw / chart_max
    cell_h = heatmap_h / bins

    def x_of(index: int) -> float:
        return LEFT + (index + 0.5) * cell_w

    def y_of(value: float) -> float:
        return TOP + plot_h - (value / chart_max) * plot_h

    svg = [
        f'<svg class="chart" viewBox="0 0 {WIDTH} {height}" '
        f'role="img" aria-label="{component.name} forecast skill">'
    ]
    for slot in range(48):
        values = probabilities.get(slot, ())
        for bin_index, probability in enumerate(values):
            if probability <= 0:
                continue
            x = LEFT + slot * cell_w
            y = TOP + plot_h - (bin_index + 1) * cell_h
            svg.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell_w + 0.4:.1f}" '
                f'height="{cell_h + 0.4:.1f}" '
                f'fill="{_heatmap_color(probability)}"/>'
            )
    step = 1 if chart_max <= 6 else 2
    kw = 0
    while kw <= int(chart_max):
        y = y_of(float(kw))
        svg.append(
            f'<line x1="{LEFT}" y1="{y:.1f}" x2="{WIDTH - RIGHT}" '
            f'y2="{y:.1f}" class="grid"/>'
        )
        svg.append(
            f'<text x="{LEFT - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'class="axis">{kw} kW</text>'
        )
        kw += step
    for index in range(0, 48, 4):
        x = x_of(index)
        svg.append(
            f'<text x="{x:.1f}" y="{height - 12}" text-anchor="middle" '
            f'class="axis">{index // 2:02d}:{"30" if index % 2 else "00"}</text>'
        )
    series = [
        {
            "name": "Forecast",
            "color": "#1565c0",
            "values": [point.forecast_kw for point in component.points],
        },
        {
            "name": "Actual",
            "color": "#111111",
            "values": [point.actual_kw for point in component.points],
        },
        {
            "name": "Historic P50",
            "color": "#757575",
            "dash": "5 4",
            "values": [point.baseline_kw for point in component.points],
        },
    ]
    if component.name == "Heat pump":
        series.append(
            {
                "name": "Forecast P90",
                "color": "#ef6c00",
                "dash": "3 4",
                "values": [
                    point.forecast_p90_kw for point in component.points
                ],
            }
        )
    for item in series:
        path = _line_path(item["values"], x_of, y_of)
        if path:
            svg.append(
                f'<path d="{path}" fill="none" stroke="{item["color"]}" '
                f'stroke-width="2.2" stroke-dasharray="{item.get("dash", "")}"/>'
            )
    legend_x = LEFT
    for item in series:
        svg.append(
            f'<line x1="{legend_x}" y1="10" x2="{legend_x + 18}" y2="10" '
            f'stroke="{item["color"]}" stroke-width="3" '
            f'stroke-dasharray="{item.get("dash", "")}"/>'
        )
        svg.append(
            f'<text x="{legend_x + 23}" y="14" class="legend">'
            f'{item["name"]}</text>'
        )
        legend_x += 150
    svg.append("</svg>")
    return "".join(svg)


def _error_chart(component: ComponentSkill) -> str:
    total = []
    weather = []
    model = []
    for point in component.points:
        total.append(
            None
            if point.forecast_kw is None or point.actual_kw is None
            else point.forecast_kw - point.actual_kw
        )
        weather.append(
            None
            if point.forecast_kw is None
            or point.actual_weather_kw is None
            else point.forecast_kw - point.actual_weather_kw
        )
        model.append(
            None
            if point.actual_weather_kw is None or point.actual_kw is None
            else point.actual_weather_kw - point.actual_kw
        )
    return _line_chart(
        [
            {"name": "Total", "color": "#111111", "values": total},
            {
                "name": "Weather-induced",
                "color": "#ef6c00",
                "values": weather,
            },
            {"name": "Model", "color": "#1565c0", "values": model},
        ],
        y_label=f"{component.name} error decomposition",
        unit=" kW",
    )


def _histogram_empty(component: ComponentSkill) -> bool:
    return not any(
        sum(values) > 0
        for values in component.histogram.probabilities.values()
    )


def register_forecast_skill_routes(app) -> None:
    """Attach the forecast-skill report page to *app*."""

    @app.route("/reports/forecast-skill")
    def forecast_skill_page():
        day = _parse_day(request.args.get("date"))
        try:
            skill = build_day_skill(day)
        except Exception as error:
            return render_template(
                "forecast_skill.html",
                day=day,
                error=str(error),
                skill=None,
                weather_temperature_svg="",
                weather_gti_svg="",
                solar_svg="",
                heatpump_svg="",
                solar_error_svg="",
                heatpump_error_svg="",
                solar_histogram_empty=True,
                heatpump_histogram_empty=True,
                actual_weather_slots=0,
                model_path=str(config.NET_GRID_MODEL_FILE),
                fmt=_fmt,
            )
        temperature_svg, gti_svg = _weather_charts(skill)
        return render_template(
            "forecast_skill.html",
            day=day,
            error=None,
            skill=skill,
            weather_temperature_svg=temperature_svg,
            weather_gti_svg=gti_svg,
            solar_svg=_component_chart(skill.solar),
            heatpump_svg=_component_chart(skill.heatpump),
            solar_error_svg=_error_chart(skill.solar),
            heatpump_error_svg=_error_chart(skill.heatpump),
            solar_histogram_empty=_histogram_empty(skill.solar),
            heatpump_histogram_empty=_histogram_empty(skill.heatpump),
            actual_weather_slots=sum(
                1
                for point in skill.weather
                if point.actual_temperature_c is not None
                or point.actual_gti_wm2 is not None
            ),
            model_path=skill.model_path,
            fmt=_fmt,
        )
