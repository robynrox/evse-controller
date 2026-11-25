"""Guided first-run setup wizard."""

from __future__ import annotations

import math
from typing import Any, Optional

from flask import redirect, render_template, request, url_for

from evse_controller.price_tiers import PRESETS
from evse_controller.utils.config import config


CONTROL_MODES = {
    "preview": "Preview — heuristic controls, DP observes",
    "control_hist": "DP control — recorded load profile",
    "control_comp": "DP control — component forecast",
    "freerun": "Free run — no smart tariff control",
}

#: Octopus distribution regions used by Agile import and Agile Outgoing.
OCTOPUS_REGIONS = (
    ("A", "Eastern England"),
    ("B", "East Midlands"),
    ("C", "London"),
    ("D", "Merseyside & Northern Wales"),
    ("E", "West Midlands"),
    ("F", "North Eastern England"),
    ("G", "North Western England"),
    ("H", "Southern England"),
    ("J", "South Eastern England"),
    ("K", "Southern Wales"),
    ("L", "Southern Scotland"),
    ("M", "South Western England"),
    ("N", "North Wales (part)"),
    ("P", "Northern Ireland"),
)

PRICE_FIELDS = (
    ("import_standard", "Import standard rate", "IMPORT_STANDARD_P"),
    ("import_cheap", "Import cheap rate", "IMPORT_CHEAP_P"),
    ("import_peak", "Import peak rate", "IMPORT_PEAK_P"),
    ("export_standard", "Export standard rate", "EXPORT_STANDARD_P"),
    ("export_cheap", "Export cheap rate", "EXPORT_CHEAP_P"),
    ("export_peak", "Export peak rate", "EXPORT_PEAK_P"),
)

FLOOR_FIELDS = (
    ("hard_floor_soc", "Hard protection floor", "HARD_FLOOR_SOC"),
    ("buffer_floor_soc", "Buffer floor", "BUFFER_FLOOR_SOC"),
    ("bulk_floor_soc", "Bulk/export floor", "BULK_FLOOR_SOC"),
)


def _current_values() -> dict[str, str]:
    grid_device = str(config.SHELLY_GRID_DEVICE or "")
    grid_channel = config.SHELLY_GRID_CHANNEL
    evse_device = str(config.SHELLY_EVSE_DEVICE or "")
    evse_channel = config.SHELLY_EVSE_CHANNEL
    startup = str(config.STARTUP_MODE or "SMART").upper()
    learning = str(config.LEARNING_MODE or "preview").lower()
    if startup == "FREERUN":
        control = "freerun"
    elif learning == "control":
        control = "control_hist"
    elif learning in ("control_hist", "control_comp"):
        control = learning
    else:
        control = "preview"
    wallbox_url = str(config.WALLBOX_URL or "")
    if config.USE_WALLBOX_SIMULATOR:
        wallbox_url = "simulator"
    values = {
        "wallbox_url": wallbox_url,
        "wallbox_serial": (
            "" if config.WALLBOX_SERIAL in (None, "") else str(config.WALLBOX_SERIAL)
        ),
        "wallbox_hostname": str(config.WALLBOX_HOSTNAME or ""),
        "shelly_url": str(config.SHELLY_PRIMARY_URL or ""),
        "shelly_hostname": str(config.SHELLY_PRIMARY_HOSTNAME or ""),
        "shelly_secondary_url": str(config.SHELLY_SECONDARY_URL or ""),
        "shelly_secondary_hostname": str(config.SHELLY_SECONDARY_HOSTNAME or ""),
        "grid_monitor": (
            f"{grid_device}:{grid_channel}"
            if grid_device and grid_channel is not None
            else "primary:1"
        ),
        "evse_monitor": (
            f"{evse_device}:{evse_channel}"
            if evse_device and evse_channel is not None
            else ""
        ),
        "tariff_preset": str(config.TARIFF_PRESET or ""),
        "octopus_region": str(config.OCTOPUS_REGION or "K").upper(),
        "agile_product": str(config.OCTOPUS_AGILE_IMPORT_PRODUCT or ""),
        "max_charge_percent": str(config.MAX_CHARGE_PERCENT),
        "control_mode": control,
    }
    for key, _label, attribute in PRICE_FIELDS:
        values[key] = str(getattr(config, attribute))
    for key, _label, attribute in FLOOR_FIELDS:
        values[key] = str(getattr(config, attribute))
    return values


def _submitted_values() -> dict[str, str]:
    values = _current_values()
    for key in values:
        if key in request.form:
            values[key] = str(request.form.get(key) or "").strip()
    return values


def _parse_monitor(value: str) -> Optional[tuple[str, int]]:
    text = str(value or "").strip()
    if not text or ":" not in text:
        return None
    device, raw_channel = text.split(":", 1)
    if device not in ("primary", "secondary"):
        return None
    try:
        channel = int(raw_channel)
    except (TypeError, ValueError):
        return None
    if channel not in (1, 2):
        return None
    return device, channel


def _monitor_error(
    values: dict[str, str],
    key: str,
    label: str,
) -> Optional[str]:
    parsed = _parse_monitor(values[key])
    if parsed is None:
        return f"Choose a valid {label} assignment."
    device, _channel = parsed
    url = (
        values["shelly_url"]
        if device == "primary"
        else values["shelly_secondary_url"]
    )
    if not url:
        return (
            f"{label} is assigned to Shelly "
            f"{'1' if device == 'primary' else '2'}, but that address is blank."
        )
    return None


def _validate(
    values: dict[str, str],
) -> tuple[list[tuple[int, str]], dict[str, Any]]:
    errors: list[tuple[int, str]] = []
    parsed: dict[str, Any] = {}

    if not values["wallbox_url"]:
        errors.append((1, "Enter the Wallbox address or 'simulator'."))
    if not (values["shelly_url"] or values["shelly_secondary_url"]):
        errors.append((1, "Enter at least one Shelly address."))
    for key, label in (
        ("grid_monitor", "grid monitor"),
        ("evse_monitor", "EVSE monitor"),
    ):
        error = _monitor_error(values, key, label)
        if error:
            errors.append((1, error))

    if values["tariff_preset"] not in PRESETS:
        errors.append((2, "Choose a tariff preset."))
    region = values["octopus_region"].strip().upper()
    if region not in dict(OCTOPUS_REGIONS):
        errors.append((2, "Choose an Octopus region."))
    parsed["octopus_region"] = region
    for key, label, _attribute in PRICE_FIELDS:
        try:
            price = float(values[key])
            if not math.isfinite(price) or price < 0:
                raise ValueError
            parsed[key] = price
        except (TypeError, ValueError):
            errors.append((2, f"Enter a valid {label.lower()}."))

    try:
        max_charge = int(values["max_charge_percent"])
        if not 1 <= max_charge <= 100:
            raise ValueError
        parsed["max_charge_percent"] = max_charge
    except (TypeError, ValueError):
        errors.append((3, "Charge cap must be between 1 and 100%."))
        max_charge = None

    floors = {}
    for key, label, _attribute in FLOOR_FIELDS:
        minimum = 25 if key == "hard_floor_soc" else 0
        try:
            floor = int(values[key])
            if not minimum <= floor <= 100:
                raise ValueError
            floors[key] = floor
        except (TypeError, ValueError):
            errors.append(
                (3, f"{label} must be between {minimum} and 100%.")
            )
    if len(floors) == 3:
        if not (
            floors["hard_floor_soc"]
            <= floors["buffer_floor_soc"]
            <= floors["bulk_floor_soc"]
        ):
            errors.append(
                (3, "The floors must be ordered hard ≤ buffer ≤ bulk.")
            )
        if (
            max_charge is not None
            and floors["bulk_floor_soc"] > max_charge
        ):
            errors.append(
                (3, "The bulk floor cannot be above the charge cap.")
            )
        parsed.update(floors)

    if values["control_mode"] not in CONTROL_MODES:
        errors.append((4, "Choose a control mode."))
    parsed["control_mode"] = values["control_mode"]

    if values["wallbox_serial"]:
        try:
            parsed["wallbox_serial"] = int(values["wallbox_serial"])
        except (TypeError, ValueError):
            errors.append((1, "Wallbox serial must be numeric."))

    return errors, parsed


def _save(values: dict[str, str], parsed: dict[str, Any]) -> None:
    wallbox = values["wallbox_url"].strip()
    if wallbox.lower() in ("simulator", "simulated"):
        config.USE_WALLBOX_SIMULATOR = True
        config.WALLBOX_URL = ""
        config.WALLBOX_HOSTNAME = ""
    else:
        config.USE_WALLBOX_SIMULATOR = False
        config.WALLBOX_URL = wallbox
        config.WALLBOX_HOSTNAME = values["wallbox_hostname"]
    if "wallbox_serial" in parsed:
        config.WALLBOX_SERIAL = parsed["wallbox_serial"]
    config.SHELLY_PRIMARY_URL = values["shelly_url"]
    config.SHELLY_PRIMARY_HOSTNAME = values["shelly_hostname"]
    config.SHELLY_SECONDARY_URL = values["shelly_secondary_url"]
    config.SHELLY_SECONDARY_HOSTNAME = values["shelly_secondary_hostname"]

    grid = _parse_monitor(values["grid_monitor"])
    evse = _parse_monitor(values["evse_monitor"])
    if grid is not None:
        config.SHELLY_GRID_DEVICE, config.SHELLY_GRID_CHANNEL = grid
        config.set_channel_in_use(grid[0], grid[1], True)
    if evse is not None:
        config.SHELLY_EVSE_DEVICE, config.SHELLY_EVSE_CHANNEL = evse
        config.set_channel_in_use(evse[0], evse[1], True)
    else:
        config.SHELLY_EVSE_DEVICE = ""
        config.SHELLY_EVSE_CHANNEL = None

    config.TARIFF_PRESET = values["tariff_preset"]
    config.OCTOPUS_REGION = parsed["octopus_region"]
    config.OCTOPUS_AGILE_IMPORT_PRODUCT = values["agile_product"]
    for key, _label, attribute in PRICE_FIELDS:
        setattr(config, attribute, parsed[key])

    config.MAX_CHARGE_PERCENT = parsed["max_charge_percent"]
    for key, _label, attribute in FLOOR_FIELDS:
        setattr(config, attribute, parsed[key])

    control = parsed["control_mode"]
    if control == "freerun":
        config.STARTUP_MODE = "FREERUN"
        config.LEARNING_MODE = "off"
    else:
        config.STARTUP_MODE = "SMART"
        config.LEARNING_MODE = control
    config.save()


def register_setup_routes(app) -> None:
    @app.route("/setup", methods=["GET", "POST"])
    def setup_page():
        done = request.args.get("done") == "1"
        error = None
        initial_step = 1
        values = _current_values()
        if request.method == "POST":
            values = _submitted_values()
            errors, parsed = _validate(values)
            if errors:
                initial_step = errors[0][0]
                error = " ".join(message for _step, message in errors)
            else:
                try:
                    _save(values, parsed)
                except Exception as exc:
                    error = f"Could not save the setup: {exc}"
                else:
                    return redirect(url_for("setup_page", done="1"))
        return render_template(
            "setup.html",
            values=values,
            control_modes=CONTROL_MODES,
            price_presets=PRESETS,
            price_fields=PRICE_FIELDS,
            floor_fields=FLOOR_FIELDS,
            octopus_regions=OCTOPUS_REGIONS,
            preset_data={
                key: {
                    "import": dict(preset.default_import_prices),
                    "export": dict(preset.default_export_prices),
                }
                for key, preset in PRESETS.items()
            },
            error=error,
            initial_step=initial_step,
            done=done,
        )
