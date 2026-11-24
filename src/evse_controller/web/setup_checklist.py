"""Dashboard quick-start checklist for first-run configuration."""

from __future__ import annotations

from dataclasses import dataclass

from evse_controller.price_tiers import PRESETS


@dataclass(frozen=True)
class SetupItem:
    key: str
    label: str
    detail: str
    ready: bool
    blocking: bool
    anchor: str


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _device_label(device: str) -> str:
    return {
        "primary": "Shelly 1",
        "secondary": "Shelly 2",
    }.get(device, device or "No device")


def _device_url(config, device: str) -> str:
    if device == "primary":
        return _text(getattr(config, "SHELLY_PRIMARY_URL", ""))
    if device == "secondary":
        return _text(getattr(config, "SHELLY_SECONDARY_URL", ""))
    return ""


def _channel(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _monitor_item(
    config,
    key: str,
    label: str,
    device_attr: str,
    channel_attr: str,
) -> SetupItem:
    device = _text(getattr(config, device_attr, ""))
    channel = _channel(getattr(config, channel_attr, None))
    url = _device_url(config, device)
    ready = bool(device and channel in (1, 2) and url)
    if ready:
        try:
            ready = bool(config.is_channel_in_use(device, channel))
        except Exception:
            ready = False
    detail = "Not assigned"
    if device:
        detail = _device_label(device)
        if channel in (1, 2):
            detail += f" · Ch {channel}"
        else:
            detail += " · no channel"
        if not url:
            detail += " · no address"
    return SetupItem(
        key=key,
        label=label,
        detail=detail,
        ready=ready,
        blocking=True,
        anchor="#device-assignment",
    )


def _price_text(value) -> str:
    try:
        return f"{float(value):.1f}p"
    except (TypeError, ValueError):
        return "unknown"


def build_setup_checklist(config) -> list[SetupItem]:
    """Return the dashboard's first-run items.

    Missing hardware blocks the card.  The tariff, floor and control entries
    are normally review links, but an invalid value also keeps the card
    visible so a broken configuration cannot be hidden.
    """
    wallbox_url = _text(getattr(config, "WALLBOX_URL", ""))
    simulator = bool(
        getattr(config, "USE_WALLBOX_SIMULATOR", False)
    ) or wallbox_url.lower() in ("simulator", "simulated")
    wallbox_ready = bool(wallbox_url) or simulator
    wallbox_detail = "Simulator (testing)" if simulator else (
        wallbox_url or "No Wallbox address yet"
    )

    primary = _text(getattr(config, "SHELLY_PRIMARY_URL", ""))
    secondary = _text(getattr(config, "SHELLY_SECONDARY_URL", ""))
    if primary and secondary:
        shelly_detail = "Shelly 1 and Shelly 2 configured"
    elif primary:
        shelly_detail = "Shelly 1 configured"
    elif secondary:
        shelly_detail = "Shelly 2 configured"
    else:
        shelly_detail = "No Shelly address yet"

    preset_key = _text(getattr(config, "TARIFF_PRESET", ""))
    preset = PRESETS.get(preset_key)
    import_p = getattr(config, "IMPORT_STANDARD_P", None)
    export_p = getattr(config, "EXPORT_STANDARD_P", None)
    tariff_ready = (
        preset is not None
        and import_p is not None
        and export_p is not None
    )
    tariff_detail = preset.label if preset is not None else "No tariff preset"
    if import_p is not None and export_p is not None:
        tariff_detail += (
            f" · import {_price_text(import_p)}"
            f" · export {_price_text(export_p)}"
        )

    hard = getattr(config, "HARD_FLOOR_SOC", 25)
    buffer = getattr(config, "BUFFER_FLOOR_SOC", 45)
    bulk = getattr(config, "BULK_FLOOR_SOC", 50)
    try:
        hard_f, buffer_f, bulk_f = (
            float(hard), float(buffer), float(bulk)
        )
        floors_ready = (
            0.0 <= hard_f <= buffer_f <= bulk_f <= 100.0
        )
        floors_detail = (
            f"Hard {hard_f:.0f}% · buffer {buffer_f:.0f}%"
            f" · bulk {bulk_f:.0f}%"
        )
    except (TypeError, ValueError):
        floors_ready = False
        floors_detail = "Invalid floor values"

    mode = _text(getattr(config, "LEARNING_MODE", "preview")).lower()
    mode_labels = {
        "preview": "Preview (observing)",
        "control_hist": "DP control — recorded load",
        "control_comp": "DP control — component forecast",
        "off": "Off",
    }
    control_ready = mode in mode_labels
    control_detail = mode_labels.get(mode, mode or "Not set")

    return [
        SetupItem(
            "wallbox",
            "Wallbox address",
            wallbox_detail,
            wallbox_ready,
            True,
            "#wallbox",
        ),
        SetupItem(
            "shelly",
            "Shelly address",
            shelly_detail,
            bool(primary or secondary),
            True,
            "#shelly",
        ),
        _monitor_item(
            config,
            "grid",
            "Grid power monitor",
            "SHELLY_GRID_DEVICE",
            "SHELLY_GRID_CHANNEL",
        ),
        _monitor_item(
            config,
            "evse",
            "EVSE power monitor",
            "SHELLY_EVSE_DEVICE",
            "SHELLY_EVSE_CHANNEL",
        ),
        SetupItem(
            "tariff",
            "Tariff and prices",
            tariff_detail,
            tariff_ready,
            not tariff_ready,
            "#tariff-prices",
        ),
        SetupItem(
            "floors",
            "Battery and SoC floors",
            floors_detail,
            floors_ready,
            not floors_ready,
            "#soc-floors",
        ),
        SetupItem(
            "control",
            "Control mode",
            control_detail,
            control_ready,
            not control_ready,
            "#learning-mode",
        ),
    ]


def setup_is_incomplete(items: list[SetupItem]) -> bool:
    """True when a blocking hardware item is still missing."""
    return any(item.blocking and not item.ready for item in items)
