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
    _fmt_action_current,
    _fmt_gate,
    _hedge_note,
    _sample_lookup,
    register_policy_report_routes,
)


DAY = date(2026, 11, 8)
TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)


def test_sample_lookup_does_not_collide_across_midnight():
    lookup = _sample_lookup(
        [
            {
                "ts": "2026-11-15T07:00:00",
                "slot": 14,
                "estimate": 80.0,
            },
            {
                "ts": "2026-11-16T07:00:00",
                "slot": 14,
                "estimate": 20.0,
            },
        ]
    )

    assert lookup[(date(2026, 11, 15), 14)] == 80.0
    assert lookup[(date(2026, 11, 16), 14)] == 20.0


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


def test_follow_arcs_show_the_starting_current_only():
    assert (
        _fmt_action_current(
            "follow_house_conservative", 7.5, 3.0, 12.0
        )
        == "≥3 A"
    )
    assert (
        _fmt_action_current(
            "follow_house_aggressive", 7.5, 3.0, 16.0
        )
        == "≥3 A"
    )
    assert (
        _fmt_action_current("follow_solar", 5.0, 4.0, 9.0)
        == "≥4 A"
    )
    assert (
        _fmt_action_current(
            "follow_bidirectional_conservative", 0.0, -3.0, 6.0
        )
        == "-3…+6 A"
    )
    assert _fmt_action_current("charge", 7.0, 7.0, 7.0) == "7 A"


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
    assert "max-width: none" in body
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


def test_fmt_gate_distinguishes_absent_and_zero_gates():
    assert _fmt_gate(None) == "—"
    assert _fmt_gate(0) == "any import"
    assert _fmt_gate(480) == "480 W"


def test_hedge_note_reports_the_assumed_direction_probability():
    from evse_controller.planner.dp import PlannerSlot

    slot = PlannerSlot(
        start=datetime(2026, 6, 21, 12, 0),
        import_p=20.0,
        export_p=5.0,
        load_hedge=((-0.72, 0.0167), (0.5, 0.0167)),
    )

    note = _hedge_note(slot)

    assert "no surplus or import sample" in note
    assert "1.7% each" in note
    assert _hedge_note(None) == ""


def test_options_table_labels_discharge_follow_and_shows_gate(tmp_path):
    run = _run(with_params=True)
    for item in run["inputs"]:
        item["load_samples"] = [[0.5, 1.0]]
        item["load_source"] = "profile"
        item["follow_aggressive_gate_w"] = 240.0
        item["follow_aggressive_floor_percent"] = 50.0
        item["follow_hybrid_floors"] = [[5.0, None, 480.0]]
    store = PlannerHistory(tmp_path / "hybrid")
    store.record_run(DAY, run)
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_policy_report_routes(app, history=store)
    app.config.update(TESTING=True)

    response = app.test_client().get(
        f"/reports/policy?date={DAY.isoformat()}&slot=0&soc=60"
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "<th>Gate</th>" in body
    assert "discharge follow" in body
    assert "480 W" in body
    assert "follow house floor" not in body


def test_page_shows_follow_modes_and_their_gates(tmp_path):
    run = _run(with_params=True)
    run["forecast_days"] = 1
    for item in run["inputs"]:
        item["load_samples"] = [[0.1, 0.5], [0.8, 0.5]]
        item["load_source"] = "profile"
        item["follow_conservative_gate_w"] = 0.0
        item["follow_aggressive_gate_w"] = 0.0
        item["follow_conservative_floor_percent"] = 45.0
        item["follow_aggressive_floor_percent"] = 50.0
    store = PlannerHistory(tmp_path / "follow")
    store.record_run(DAY, run)
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_policy_report_routes(app, history=store)
    app.config.update(TESTING=True)

    response = app.test_client().get(
        f"/reports/policy?date={DAY.isoformat()}&slot=0&soc=60"
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Follow gates for this slot" in body
    assert "follow house conservative" in body
    assert "follow house aggressive" in body
    assert "Starts at house import" in body


def test_page_shows_config_limits_only_when_there_is_no_target(tmp_path):
    run = _run(with_params=True)
    run["target_soc"] = None
    run["planner"]["target_soc_percent"] = None
    run["planner"]["discharge_floor_percent"] = 40.0
    store = PlannerHistory(tmp_path / "no-target")
    store.record_run(DAY, run)
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_policy_report_routes(app, history=store)
    app.config.update(TESTING=True)

    response = app.test_client().get(f"/reports/policy?date={DAY.isoformat()}")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "config limits only" in body
    assert "export floor 40.0%" in body


def test_page_labels_a_prior_as_a_prior(tmp_path):
    run = _run(with_params=True)
    run["forecast_days"] = 0
    for item in run["inputs"]:
        item["load_samples"] = [[0.0125 + 0.025 * i, 1 / 16] for i in range(16)]
        item["load_source"] = "prior"
        item["load_kw"] = 0.2
    store = PlannerHistory(tmp_path / "prior")
    store.record_run(DAY, run)
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_policy_report_routes(app, history=store)
    app.config.update(TESTING=True)

    response = app.test_client().get(f"/reports/policy?date={DAY.isoformat()}")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "0–400 W prior" in body


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
    assert "no grid-load forecast" in body
    assert "0.72 kW fallback" in body


def test_empty_history_shows_an_explanation(tmp_path):
    store = PlannerHistory(tmp_path / "empty")
    app = Flask(__name__, template_folder=str(TEMPLATES))
    register_policy_report_routes(app, history=store)
    app.config.update(TESTING=True)

    response = app.test_client().get(f"/reports/policy?date={DAY.isoformat()}")

    assert response.status_code == 200
    assert "No Learning mode preview plans are recorded" in response.get_data(
        as_text=True
    )


def test_invalid_date_is_a_404(client):
    assert client.get("/reports/policy?date=not-a-date").status_code == 404
