"""Guided first-run setup wizard."""

from pathlib import Path

from flask import Flask
import pytest

from evse_controller.utils.config import config as app_config
from evse_controller.web import setup_routes


TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)


@pytest.fixture
def isolated_config(monkeypatch):
    """Point the application's config singleton at a fresh test config.

    ``Config`` is a singleton and the web routes import the module-level
    instance.  Reset that instance's data rather than constructing another
    ``Config`` so the routes and the assertions see the same object.
    """
    had_data = "_config_data" in app_config.__dict__
    original = app_config.__dict__.get("_config_data")
    app_config.__dict__["_config_data"] = None
    _ = app_config.WALLBOX_URL
    monkeypatch.setattr(app_config, "save", lambda: None)
    try:
        yield app_config
    finally:
        if had_data:
            app_config.__dict__["_config_data"] = original
        else:
            app_config.__dict__.pop("_config_data", None)


def _app():
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.secret_key = "test"
    app.add_url_rule("/", "index", lambda: "")
    app.add_url_rule(
        "/ev-readiness", "ev_readiness_page", lambda: ""
    )
    app.add_url_rule("/config", "config_page", lambda: "")
    setup_routes.register_setup_routes(app)
    return app


def _form(**overrides):
    values = {
        "wallbox_url": "192.168.1.180",
        "wallbox_serial": "123456",
        "shelly_url": "192.168.1.118",
        "shelly_secondary_url": "192.168.1.186",
        "grid_monitor": "primary:1",
        "evse_monitor": "primary:2",
        "tariff_preset": "iog_agile",
        "octopus_region": "K",
        "agile_product": "AGILE-24-10-01",
        "import_standard": "30",
        "import_cheap": "10",
        "import_peak": "30",
        "export_standard": "8",
        "export_cheap": "8",
        "export_peak": "12",
        "max_charge_percent": "90",
        "hard_floor_soc": "25",
        "buffer_floor_soc": "45",
        "bulk_floor_soc": "50",
        "control_mode": "preview",
    }
    values.update(overrides)
    return values


def test_setup_page_renders_the_steps(isolated_config):
    app = _app()

    with app.test_client() as client:
        response = client.get("/setup")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "First-run setup" in body
    assert "1 · Hardware" in body
    assert "2 · Tariff" in body
    assert "3 · Battery" in body
    assert "4 · Control" in body
    assert "5 · Review" in body
    assert 'id="wallbox_url"' in body
    assert 'id="tariff_preset"' in body
    assert '<select id="octopus_region"' in body
    assert "Southern Wales (K)" in body
    assert "Import prices (p/kWh)" in body
    assert "Export prices (p/kWh)" in body


def test_setup_page_saves_the_essentials(isolated_config):
    app = _app()

    with app.test_client() as client:
        response = client.post("/setup", data=_form())

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/setup?done=1")
    assert isolated_config.WALLBOX_URL == "192.168.1.180"
    assert isolated_config.USE_WALLBOX_SIMULATOR is False
    assert isolated_config.WALLBOX_SERIAL == 123456
    assert isolated_config.SHELLY_PRIMARY_URL == "192.168.1.118"
    assert isolated_config.SHELLY_SECONDARY_URL == "192.168.1.186"
    assert isolated_config.SHELLY_GRID_DEVICE == "primary"
    assert isolated_config.SHELLY_GRID_CHANNEL == 1
    assert isolated_config.SHELLY_EVSE_DEVICE == "primary"
    assert isolated_config.SHELLY_EVSE_CHANNEL == 2
    assert isolated_config.is_channel_in_use("primary", 2) is True
    assert isolated_config.TARIFF_PRESET == "iog_agile"
    assert isolated_config.OCTOPUS_REGION == "K"
    assert isolated_config.IMPORT_CHEAP_P == 10
    assert isolated_config.EXPORT_STANDARD_P == 8
    assert isolated_config.MAX_CHARGE_PERCENT == 90
    assert isolated_config.HARD_FLOOR_SOC == 25
    assert isolated_config.BUFFER_FLOOR_SOC == 45
    assert isolated_config.BULK_FLOOR_SOC == 50
    assert isolated_config.STARTUP_MODE == "SMART"
    assert isolated_config.LEARNING_MODE == "preview"


def test_setup_page_supports_freerun(isolated_config):
    app = _app()

    with app.test_client() as client:
        client.post("/setup", data=_form(control_mode="freerun"))

    assert isolated_config.STARTUP_MODE == "FREERUN"
    assert isolated_config.LEARNING_MODE == "off"


def test_setup_page_rejects_bad_floor_order(isolated_config):
    app = _app()
    before = isolated_config.HARD_FLOOR_SOC

    with app.test_client() as client:
        response = client.post(
            "/setup", data=_form(hard_floor_soc="60")
        )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "hard ≤ buffer ≤ bulk" in body
    assert isolated_config.HARD_FLOOR_SOC == before


def test_setup_page_rejects_a_hard_floor_below_the_minimum(isolated_config):
    app = _app()

    with app.test_client() as client:
        response = client.post(
            "/setup", data=_form(hard_floor_soc="20")
        )

    assert response.status_code == 200
    assert "Hard protection floor must be between 25 and 100%" in (
        response.get_data(as_text=True)
    )


def test_setup_page_rejects_a_missing_wallbox(isolated_config):
    app = _app()
    isolated_config.WALLBOX_URL = "10.0.0.5"

    with app.test_client() as client:
        response = client.post(
            "/setup", data=_form(wallbox_url="")
        )

    assert response.status_code == 200
    assert "Wallbox address" in response.get_data(as_text=True)
    assert isolated_config.WALLBOX_URL == "10.0.0.5"


def test_setup_page_rejects_an_unknown_region(isolated_config):
    app = _app()

    with app.test_client() as client:
        response = client.post(
            "/setup", data=_form(octopus_region="Z")
        )

    assert response.status_code == 200
    assert "Choose an Octopus region." in response.get_data(as_text=True)


def test_setup_page_uses_the_simulator(isolated_config):
    app = _app()

    with app.test_client() as client:
        response = client.post(
            "/setup", data=_form(wallbox_url="simulator")
        )
        page = client.get("/setup")

    assert response.status_code == 302
    assert isolated_config.USE_WALLBOX_SIMULATOR is True
    assert isolated_config.WALLBOX_URL == ""
    assert 'value="simulator"' in page.get_data(as_text=True)


def test_setup_page_requires_an_evse_monitor(isolated_config):
    app = _app()
    isolated_config.SHELLY_EVSE_DEVICE = "primary"
    isolated_config.SHELLY_EVSE_CHANNEL = 2

    with app.test_client() as client:
        response = client.post("/setup", data=_form(evse_monitor=""))

    assert response.status_code == 200
    assert "EVSE monitor" in response.get_data(as_text=True)
    assert isolated_config.SHELLY_EVSE_DEVICE == "primary"
    assert isolated_config.SHELLY_EVSE_CHANNEL == 2


def test_setup_page_shows_the_completion_page(isolated_config):
    app = _app()

    with app.test_client() as client:
        response = client.get("/setup?done=1")

    body = response.get_data(as_text=True)
    assert "Setup saved" in body
    assert "Go to dashboard" in body
