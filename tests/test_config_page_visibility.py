"""The config page hides settings that do not apply to the selected tariff."""

import copy
import re
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, render_template
import pytest

from evse_controller.price_tiers import PRESETS
from evse_controller.utils.config import config


TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "evse_controller"
    / "templates"
)


@pytest.fixture
def app():
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.add_url_rule("/setup", "setup_page", lambda: "")
    return app


def _render(app, preset, export_source=None, setup_checklist=None):
    _ = config.WALLBOX_URL
    config_dict = copy.deepcopy(config.as_dict())
    config_dict["tariffs"]["preset"] = preset
    config_dict["tariffs"]["export_source"] = export_source
    with app.test_request_context("/config"):
        return render_template(
            "config.html",
            config=config_dict,
            price_presets=PRESETS,
            setup_checklist=setup_checklist or [],
        )


def _field_value(html, field_id):
    match = re.search(
        rf'id="{re.escape(field_id)}"[^>]*value="([^"]*)"',
        html,
        re.S,
    )
    return None if match is None else match.group(1)


def test_discovery_and_connection_tests_are_on_the_config_page(app):
    body = _render(app, "iog_agile")

    assert 'id="discovery-start"' in body
    assert 'id="discovery-results"' in body
    assert 'id="discovery-networks"' in body
    assert 'data-kind="wallbox"' in body
    assert body.count('data-kind="shelly"') >= 2


def test_config_sections_have_checklist_anchors(app):
    body = _render(app, "iog_agile")

    for anchor in (
        "wallbox",
        "shelly",
        "device-assignment",
        "soc-floors",
        "tariff-prices",
        "learning-mode",
    ):
        assert f'id="{anchor}"' in body


def test_config_page_embeds_the_live_setup_checklist(app):
    item = SimpleNamespace(
        key="wallbox",
        label="Wallbox address",
        detail="Enter the Wallbox address",
        ready=False,
        anchor="#wallbox",
    )

    body = _render(
        app, "iog_agile", setup_checklist=[item]
    )

    assert 'id="config-setup-checklist"' in body
    assert 'id="setup-summary"' in body
    assert 'data-setup-key="wallbox"' in body
    assert "Enter the Wallbox address" in body
    assert 'href="#wallbox"' in body


def test_dual_address_fields_are_visible(app):
    body = _render(app, "iog_agile")

    for field in (
        "wallbox_hostname",
        "wallbox_identity",
        "shelly_primary_hostname",
        "shelly_primary_identity",
        "shelly_secondary_hostname",
        "shelly_secondary_identity",
    ):
        assert f'id="{field}"' in body
    assert 'name="wallbox[address][prefer_ip]"' in body
    assert 'name="shelly[primary_address][prefer_ip]"' in body


def test_ev_consumption_fields_are_visible(app):
    body = _render(app, "iog_agile")

    assert 'id="ev_miles_per_kwh"' in body
    assert 'id="ev_away_margin_percent"' in body
    assert "miles/kWh" in body


def test_soc_floors_are_visible_and_populated(app):
    body = _render(app, "iog_agile")

    assert "Battery and SoC floors" in body
    assert _field_value(body, "hard_floor_soc") == "25"
    assert _field_value(body, "buffer_floor_soc") == "45"
    assert _field_value(body, "bulk_floor_soc") == "50"


def test_legacy_bulk_and_grid_settings_are_gone(app):
    body = _render(app, "iog_agile")

    for field in (
        "ioctgo_enable_bulk_discharge",
        "ioctgo_bulk_discharge_start_time",
        "ioctgo_bulk_discharge_end_time",
        "ioctgo_target_soc_at_bulk_discharge_end",
        "ioctgo_soc_threshold_strategy",
        "ioctgo_grid_import_threshold_high_soc",
        "ioctgo_grid_import_threshold_low_soc",
    ):
        assert f'id="{field}"' not in body


def test_ocpp_is_only_shown_for_intelligent_go(app):
    iog = _render(app, "iog_agile")
    cosy = _render(app, "cosy")
    go = _render(app, "octopus_go")
    agile = _render(app, "agile_import")

    assert 'id="ioctgo_smart_ocpp_operation"' in iog
    assert 'id="ioctgo_smart_ocpp_operation"' not in cosy
    assert 'id="ioctgo_smart_ocpp_operation"' not in go
    assert 'id="ioctgo_smart_ocpp_operation"' not in agile


def test_cosy_solar_cap_is_cosy_only(app):
    assert 'id="solar_period_max_charge"' in _render(app, "cosy")
    assert 'id="solar_period_max_charge"' not in _render(app, "iog_agile")
    assert 'id="solar_period_max_charge"' not in _render(app, "octopus_go")


def test_agile_section_only_for_agile_tariffs(app):
    agile = _render(app, "iog_agile")
    agile_import = _render(app, "agile_import")
    cosy = _render(app, "cosy")
    agile_export = _render(app, "cosy", export_source="agile")

    assert 'id="octopus_region"' in agile
    assert 'id="octopus_agile_import_product"' not in agile
    assert 'id="octopus_agile_import_product"' in agile_import
    assert 'id="octopus_region"' not in cosy
    assert 'id="octopus_region"' in agile_export


def test_import_prices_are_hidden_for_agile_import(app):
    agile = _render(app, "agile_import")
    cosy = _render(app, "cosy")

    assert 'name="prices[import][standard]"' not in agile
    assert 'name="prices[import][cheap_windows]"' not in agile
    assert "Advanced: custom time windows" not in agile
    assert 'name="prices[import][standard]"' in cosy


def test_export_prices_follow_the_outgoing_choice(app):
    agile = _render(app, "iog_agile")
    fixed = _render(app, "iog_outgoing")
    zero = _render(app, "cosy", export_source="zero")
    none = _render(app, "cosy", export_source="none")

    assert 'name="prices[export][standard]"' not in agile
    assert 'name="prices[export][standard]"' in fixed
    assert 'name="prices[export][standard]"' not in zero
    assert 'name="prices[export][standard]"' not in none


def test_flux_has_no_outgoing_selector(app):
    body = _render(app, "flux")

    assert 'id="export_source"' not in body
    assert 'name="prices[export][standard]"' in body
