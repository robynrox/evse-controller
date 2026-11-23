"""Onboarding endpoints for network discovery and connection tests."""

from __future__ import annotations

from flask import jsonify, request

from evse_controller.discovery_service import (
    discovery_service,
    check_shelly,
    check_wallbox,
)


def register_discovery_routes(app) -> None:
    @app.route("/api/discovery/start", methods=["POST"])
    def start_device_discovery():
        """Start a background scan, or return the running scan."""
        return jsonify(discovery_service.start())

    @app.route("/api/discovery/status")
    def device_discovery_status():
        """Poll the background scan."""
        return jsonify(discovery_service.status())

    @app.route("/api/connection-test", methods=["POST"])
    def connection_test():
        """Test a Wallbox or Shelly address from the configuration page."""
        payload = request.get_json(silent=True) or {}
        kind = str(payload.get("kind") or "").strip().lower()
        value = str(payload.get("url") or "").strip()
        if kind == "wallbox":
            return jsonify(check_wallbox(value))
        if kind == "shelly":
            return jsonify(check_shelly(value))
        return jsonify({
            "ok": False,
            "message": "Unknown device type",
        }), 400
