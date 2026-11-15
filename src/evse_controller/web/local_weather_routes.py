"""HTTP ingest for vendor-neutral local weather observations."""

from __future__ import annotations

import hmac
from datetime import date

from flask import jsonify, request

from evse_controller.local_weather import (
    LocalWeatherStore,
    local_weather_store,
)
from evse_controller.utils.config import config


def _authorised() -> bool:
    token = str(config.LOCAL_WEATHER_INGEST_TOKEN or "")
    if token:
        provided = (
            request.headers.get("X-Ingest-Token")
            or request.headers.get("X-Local-Weather-Token")
            or ""
        )
        if not provided:
            auth = request.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        return hmac.compare_digest(str(provided), token)
    # No token configured: allow loopback only.  A reverse proxy should set
    # the ingest token rather than exposing an unauthenticated write route.
    return request.remote_addr in ("127.0.0.1", "::1")


def register_local_weather_routes(
    app,
    store: LocalWeatherStore = local_weather_store,
) -> None:
    """Attach the local-weather ingest endpoints to *app*."""

    @app.route("/api/local-weather", methods=["POST"])
    def local_weather_ingest():
        if not config.LOCAL_WEATHER_ENABLED:
            return (
                jsonify(
                    {
                        "error": (
                            "Local weather ingest is disabled. Enable it in "
                            "the configuration first."
                        )
                    }
                ),
                503,
            )
        if not _authorised():
            return (
                jsonify({"error": "Local weather ingest token is missing or wrong"}),
                401,
            )
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({"error": "Expected a JSON observation or list"}), 400
        try:
            accepted = store.append_payload(payload, default_source="http")
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        return jsonify({"accepted": accepted}), 201

    @app.route("/api/local-weather/<day_text>")
    def local_weather_day(day_text: str):
        if not config.LOCAL_WEATHER_ENABLED:
            return jsonify({"error": "Local weather ingest is disabled"}), 503
        if not _authorised():
            return (
                jsonify({"error": "Local weather ingest token is missing or wrong"}),
                401,
            )
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            return jsonify({"error": "Expected YYYY-MM-DD"}), 400
        return jsonify(
            {
                "date": day.isoformat(),
                "observations": store.records_for_day(day),
            }
        )
