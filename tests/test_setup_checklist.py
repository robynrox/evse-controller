"""Dashboard quick-start checklist."""

from types import SimpleNamespace

from evse_controller.web.setup_checklist import (
    build_setup_checklist,
    setup_is_incomplete,
)


def _config(**overrides):
    values = {
        "WALLBOX_URL": "",
        "USE_WALLBOX_SIMULATOR": False,
        "SHELLY_PRIMARY_URL": "",
        "SHELLY_SECONDARY_URL": "",
        "SHELLY_GRID_DEVICE": "primary",
        "SHELLY_GRID_CHANNEL": 1,
        "SHELLY_EVSE_DEVICE": "",
        "SHELLY_EVSE_CHANNEL": None,
        "TARIFF_PRESET": "iog_agile",
        "IMPORT_STANDARD_P": 30.0,
        "EXPORT_STANDARD_P": 8.0,
        "HARD_FLOOR_SOC": 25,
        "BUFFER_FLOOR_SOC": 45,
        "BULK_FLOOR_SOC": 50,
        "LEARNING_MODE": "preview",
    }
    values.update(overrides)
    config = SimpleNamespace(**values)
    config.is_channel_in_use = lambda device, channel: True
    return config


def _items(config):
    return {item.key: item for item in build_setup_checklist(config)}


def test_blank_config_reports_the_blocking_items():
    items = _items(_config())

    assert items["wallbox"].ready is False
    assert items["shelly"].ready is False
    assert items["grid"].ready is False
    assert items["evse"].ready is False
    assert setup_is_incomplete(list(items.values())) is True


def test_simulator_counts_as_a_configured_wallbox():
    items = _items(_config(WALLBOX_URL="simulator"))

    assert items["wallbox"].ready is True
    assert "Simulator" in items["wallbox"].detail


def test_complete_hardware_hides_the_checklist():
    items = _items(
        _config(
            WALLBOX_URL="192.168.1.180",
            SHELLY_PRIMARY_URL="192.168.1.118",
            SHELLY_SECONDARY_URL="192.168.1.186",
            SHELLY_EVSE_DEVICE="primary",
            SHELLY_EVSE_CHANNEL=2,
        )
    )

    assert items["wallbox"].ready is True
    assert items["shelly"].ready is True
    assert items["grid"].ready is True
    assert items["evse"].ready is True
    assert setup_is_incomplete(list(items.values())) is False


def test_unused_channel_is_not_ready():
    config = _config(
        WALLBOX_URL="192.168.1.180",
        SHELLY_PRIMARY_URL="192.168.1.118",
    )
    config.is_channel_in_use = lambda device, channel: False

    items = _items(config)

    assert items["grid"].ready is False
    assert setup_is_incomplete(list(items.values())) is True


def test_secondary_assignment_needs_the_secondary_address():
    items = _items(
        _config(
            WALLBOX_URL="192.168.1.180",
            SHELLY_PRIMARY_URL="192.168.1.118",
            SHELLY_GRID_DEVICE="secondary",
            SHELLY_GRID_CHANNEL=1,
        )
    )

    assert items["grid"].ready is False
    assert "no address" in items["grid"].detail


def test_invalid_floors_and_preset_keep_the_checklist_visible():
    items = _items(
        _config(
            WALLBOX_URL="192.168.1.180",
            SHELLY_PRIMARY_URL="192.168.1.118",
            SHELLY_EVSE_DEVICE="primary",
            SHELLY_EVSE_CHANNEL=2,
            HARD_FLOOR_SOC=60,
            BUFFER_FLOOR_SOC=45,
            TARIFF_PRESET="not-a-preset",
        )
    )

    assert items["floors"].ready is False
    assert items["tariff"].ready is False
    assert setup_is_incomplete(list(items.values())) is True


def test_tariff_and_control_details_are_friendly():
    items = _items(_config())

    assert "Intelligent Octopus Go" in items["tariff"].detail
    assert "import 30.0p" in items["tariff"].detail
    assert "export 8.0p" in items["tariff"].detail
    assert items["control"].detail == "Preview (observing)"
