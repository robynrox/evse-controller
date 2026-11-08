"""Web routes for the DP policy map."""

from datetime import date, datetime
from pathlib import Path

from flask import Flask
import pytest

from evse_controller.planner.history import PlannerHistory
from evse_controller.tariffs.octopus.battery_efficiency import (
    BatteryEfficiencyModel,
)
from evse_controller.web.policy_report_routes import (
    register_policy_report_routes,
)


DAY = date(2026, 11, 8)
TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)


def _inputs(count=4):
    return [
        {
            "slot": index,
            "start": f"2026-11-08T{index // 2:02d}:{(index % 2) * 30:02d}:00",
            "import_p": 10.0,
            "export_p": 50.0 if index == 1 else 0.0,
            "load_kw": 0.5,
            "session": None,
            "forced": None,
        }
        for index in range(count)
    ]


def _run(*, with_params=True):
    run = {
        "ts": "2026-11-08T00:00:00",
        "day": DAY.isoformat(),
        "slot": 0,
        "reported_soc": 55,
        "estimate_soc": 55.0,
        "target_soc": 50.0,
        "forecast_days": 14,
        "source": "test",
        "success": True,
        "relaxed": False,
        "final_soc": 50.0,
        "total_cost_p": 0.0,
        "actions": [
            {
                "slot": 0,
                "kind": "idle",
                "current_a": 0.0,
                "commanded_current_a": 0,
                "ac_power_kw": 0.0,
                "soc_before": 55.0,
                "soc_after": 55.0,
                "cost_p": 0.0,
                "session": None,
            }
        ],
        "inputs": _inputs(),
    }
    if with_params:
        run["planner"] = {
            "capacity_kwh": 48.292,
            "soc_step_percent": 0.5,
            "min_current_a": 3.0,
            "charge_cap_percent": 90.0,
            "protection_floor_percent": 25.0,
            "target_soc_percent": 50.0,
            "degradation_p_per_kwh": 0.0,
            "max_charge_current_a": 16.0,
            "max_discharge_current_a": 16.0,
            "efficiency_model": BatteryEfficiencyModel(
                capacity_kwh=48.292
            ).as_dict(),
        }
    return run


@pytest.fixture
def history(tmp_path):
    store = PlannerHistory(tmp_path / "history")
    store.record_run(DAY, _run())
    store.record_soc_sample(
        DAY,
        {
            "ts": "2026-11-08T00:05:00",
            "slot": 0,
            "estimate": 55.0,
            "reported": 55,
        },
    )
    return store


@pytest.fixture
def client(history):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_policy_report_routes(app, history=history)
    app.config.update(TESTING=True)
    return app.test_client()


def test_page_renders_policy_map_and_tables(client):
    response = client.get(f"/reports/policy?date={DAY.isoformat()}")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "DP policy map" in body
    assert 'class="policy-map"' in body
    assert "policy bands" in body
    assert "Why this action?" in body
    assert "Option" in body
    assert "data-chart-zoom" in body
    assert "soc-grid-line" in body
    assert "policy-marginal" in body
    assert "predates planner-parameter capture" not in body


def test_heatmap_puts_high_soc_at_the_top(client):
    import re

    body = client.get(
        f"/reports/policy?date={DAY.isoformat()}"
    ).get_data(as_text=True)
    labels = {
        int(percent): float(y)
        for y, percent in re.findall(
            r'<text x="[^"]*" y="([0-9.]+)" text-anchor="end" '
            r'class="axis">(\d+)%</text>',
            body,
        )
    }

    assert labels[90] < labels[80] < labels[70] < labels[30]
    assert 85 not in labels


def test_half_step_view_labels_every_five_percent(client):
    import re

    body = client.get(
        f"/reports/policy?date={DAY.isoformat()}&step=0.5"
    ).get_data(as_text=True)
    labels = {
        int(percent)
        for _, percent in re.findall(
            r'<text x="[^"]*" y="([0-9.]+)" text-anchor="end" '
            r'class="axis">(\d+)%</text>',
            body,
        )
    }

    assert {85, 80, 75, 30, 25}.issubset(labels)


def test_page_supports_resolution_and_view_switch(client):
    response = client.get(
        f"/reports/policy?date={DAY.isoformat()}&step=0.5&view=advantage"
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Decision margin" in body
    assert "0.5%" in body


def test_page_warns_when_planner_params_are_missing(tmp_path):
    store = PlannerHistory(tmp_path / "old")
    store.record_run(DAY, _run(with_params=False))
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_policy_report_routes(app, history=store)
    app.config.update(TESTING=True)

    response = app.test_client().get(f"/reports/policy?date={DAY.isoformat()}")

    assert response.status_code == 200
    assert "predates planner-parameter capture" in response.get_data(
        as_text=True
    )


def test_page_warns_when_the_run_had_no_load_forecast(tmp_path):
    run = _run(with_params=True)
    run["forecast_days"] = 0
    for item in run["inputs"]:
        item["load_kw"] = 0.0
    run["planner"]["load_fallback_kw"] = 0.72
    store = PlannerHistory(tmp_path / "no-load")
    store.record_run(DAY, run)
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_policy_report_routes(app, history=store)
    app.config.update(TESTING=True)

    response = app.test_client().get(f"/reports/policy?date={DAY.isoformat()}")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "no house-load forecast" in body
    assert "0.72 kW fallback" in body


def test_empty_history_shows_an_explanation(tmp_path):
    store = PlannerHistory(tmp_path / "empty")
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_policy_report_routes(app, history=store)
    app.config.update(TESTING=True)

    response = app.test_client().get(f"/reports/policy?date={DAY.isoformat()}")

    assert response.status_code == 200
    assert "No shadow plans are recorded" in response.get_data(as_text=True)


def test_invalid_date_is_a_404(client):
    assert client.get("/reports/policy?date=not-a-date").status_code == 404
