"""Dashboard control-mode selector."""

from pathlib import Path

from flask import Flask, render_template


TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)


def _render(learning_mode="preview", label="Smart"):
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
            use_icon_buttons=True,
        )


def test_dashboard_shows_the_three_control_modes():
    body = _render()

    assert 'id="learning-mode-selector"' in body
    assert 'data-mode="preview"' in body
    assert 'data-mode="control_hist"' in body
    assert 'data-mode="control_comp"' in body
    assert "SMART" in body
    assert "LEARN HIST" in body
    assert "LEARN COMP" in body
    assert "Smart" in body


def test_control_comp_is_the_active_button():
    body = _render("control_comp", "Learn comp")

    assert 'class="mode-button active"' in body
    assert 'data-mode="control_comp"' in body
    assert "Learn comp" in body
