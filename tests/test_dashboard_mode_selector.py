"""Dashboard control-mode selector."""

from pathlib import Path
from types import SimpleNamespace

from flask import Flask, render_template


TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)


def _render(
    learning_mode="preview",
    label="Heuristic",
    live_load_spike_override=True,
    setup_checklist=None,
    setup_incomplete=False,
):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.config.update(SERVER_NAME="localhost", PREFERRED_URL_SCHEME="http")
    for name in (
        "backtest_report_page",
        "component_report_today",
        "config_page",
        "daily_report_today",
        "data_quality_page",
        "decision_page",
        "export_report_page",
        "ev_readiness_page",
        "plan_report_today",
        "policy_report_page",
        "schedule_page",
        "sessions_page",
        "value_report_page",
    ):
        app.add_url_rule("/" + name, name, lambda: "")
    with app.test_request_context("/"):
        return render_template(
            "index.html",
            scheduled_events=[],
            current_state="SMART",
            wallbox_reset_available=False,
            learning_mode=learning_mode,
            learning_mode_label=label,
            learning_preview="Would: Charge 11A",
            live_load_spike_override=live_load_spike_override,
            setup_checklist=setup_checklist or [],
            setup_incomplete=setup_incomplete,
            ev_target="No target",
            use_icon_buttons=True,
        )


def test_dashboard_shows_the_three_control_modes():
    body = _render()

    assert 'id="learning-mode-selector"' in body
    assert 'data-mode="preview"' in body
    assert 'data-mode="control_hist"' in body
    assert 'data-mode="control_comp"' in body
    assert "HEURISTIC" in body
    assert "LEARN HISTORY" in body
    assert "LEARN FORECAST" in body
    assert "Heuristic" in body


def test_control_comp_is_the_active_button():
    body = _render("control_comp", "Learn forecast")

    assert 'class="mode-button active"' in body
    assert 'data-mode="control_comp"' in body
    assert "Learn forecast" in body


def test_dashboard_shows_the_idle_load_override_toggle():
    body = _render()

    assert 'id="idle-load-override"' in body
    assert "Idle load override" in body
    markup = body.split('id="idle-load-override"', 1)[1].split(">", 1)[0]
    assert "checked" in markup


def test_idle_load_override_toggle_reflects_the_setting():
    body = _render(live_load_spike_override=False)

    assert 'id="idle-load-override"' in body
    markup = body.split('id="idle-load-override"', 1)[1].split(">", 1)[0]
    assert "checked" not in markup


def test_setup_checklist_shows_missing_items():
    items = [
        SimpleNamespace(
            label="Wallbox address",
            detail="No address yet",
            ready=False,
            anchor="#wallbox",
        )
    ]

    body = _render(
        setup_checklist=items, setup_incomplete=True
    )

    assert "Finish setting up your controller" in body
    assert "Wallbox address" in body
    assert "No address yet" in body
    assert "#wallbox" in body


def test_setup_checklist_is_hidden_when_complete():
    body = _render(setup_checklist=[], setup_incomplete=False)

    assert "Finish setting up your controller" not in body
