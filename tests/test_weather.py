"""Optional Open-Meteo client: request shape, parsing and cache fallback."""

from datetime import date, datetime, timedelta

import pytest

from evse_controller.utils.config import config
from evse_controller.weather import (
    OPEN_METEO_ARCHIVE_URL,
    OPEN_METEO_HISTORICAL_FORECAST_URL,
    OPEN_METEO_URL,
    OpenMeteoArchiveClient,
    OpenMeteoClient,
    OpenMeteoHistoricalForecastClient,
    WeatherNotConfigured,
    parse_weather_payload,
    snapshot_from_payload,
    snapshot_to_payload,
)


# Test-only geometry.  Production has no weather defaults; these values are
# injected with monkeypatch and never written to the real config.
DAY = date(2026, 11, 7)
FETCHED = datetime(2026, 11, 7, 12, 0)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        return FakeResponse(self.payload)


def payload():
    return {
        "latitude": 51.5,
        "longitude": -3.2,
        "timezone": "Europe/London",
        "model": "best_match",
        "hourly": {
            "time": [
                "2026-11-07T10:00",
                "2026-11-07T11:00",
                "2026-11-08T00:00",
            ],
            "temperature_2m": [10.0, 10.5, 6.0],
            "shortwave_radiation": [120.0, 180.0, 0.0],
            "direct_normal_irradiance": [80.0, 100.0, 0.0],
            "diffuse_radiation": [40.0, 60.0, 0.0],
            "global_tilted_irradiance": [150.0, 210.0, 0.0],
            "cloud_cover": [25.0, 20.0, 80.0],
        },
    }


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(config, "WEATHER_LATITUDE", 51.5)
    monkeypatch.setattr(config, "WEATHER_LONGITUDE", -3.2)
    monkeypatch.setattr(config, "WEATHER_PANEL_AZIMUTH", 180.0)
    monkeypatch.setattr(config, "WEATHER_PANEL_TILT", 35.0)
    yield config


def test_fetch_parses_and_caches(configured, tmp_path):
    session = FakeSession(payload())
    client = OpenMeteoClient(
        session=session, cache_file=tmp_path / "weather.json"
    )

    snapshot = client.fetch(at=FETCHED)

    assert len(snapshot.hours) == 3
    assert snapshot.hours[0].temperature_c == pytest.approx(10.0)
    assert snapshot.hours[1].tilted_irradiance_wm2 == pytest.approx(210.0)
    assert snapshot.hours_for_day(DAY)[0].cloud_cover_pct == pytest.approx(25)

    cached = client.load_cached()
    assert cached is not None
    assert cached.hours[1].shortwave_radiation_wm2 == pytest.approx(180.0)
    assert cached.panel_tilt == pytest.approx(35.0)
    assert cached.panel_azimuth == pytest.approx(180.0)


def test_request_uses_open_meteo_south_zero_azimuth(configured, tmp_path):
    session = FakeSession(payload())
    client = OpenMeteoClient(
        session=session, cache_file=tmp_path / "weather.json"
    )

    client.fetch(at=FETCHED)

    _, params, _ = session.calls[0]
    assert session.calls[0][0] == OPEN_METEO_URL
    assert params["latitude"] == pytest.approx(51.5)
    assert params["longitude"] == pytest.approx(-3.2)
    assert params["tilt"] == pytest.approx(35.0)
    # Compass south (180°) becomes Open-Meteo's 0° south.
    assert params["azimuth"] == pytest.approx(0.0)
    assert "global_tilted_irradiance" in params["hourly"]


def test_compass_azimuth_is_converted(configured, tmp_path):
    session = FakeSession(payload())
    client = OpenMeteoClient(
        session=session, cache_file=tmp_path / "weather.json"
    )

    for compass, expected in (
        (0.0, -180.0),
        (90.0, -90.0),
        (180.0, 0.0),
        (270.0, 90.0),
    ):
        configured.WEATHER_PANEL_AZIMUTH = compass
        client.fetch(at=FETCHED)
        assert session.calls[-1][1]["azimuth"] == pytest.approx(expected)


def test_latest_rejects_a_stale_snapshot(configured, tmp_path):
    session = FakeSession(payload())
    client = OpenMeteoClient(
        session=session, cache_file=tmp_path / "weather.json"
    )
    client.fetch(at=FETCHED)

    assert client.latest(
        max_age_hours=1.0, now=FETCHED + timedelta(minutes=30)
    ) is not None
    assert client.latest(
        max_age_hours=1.0, now=FETCHED + timedelta(hours=2)
    ) is None
    assert client.load_cached() is not None


def test_unconfigured_client_refuses_to_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WEATHER_LATITUDE", None)
    monkeypatch.setattr(config, "WEATHER_LONGITUDE", None)
    monkeypatch.setattr(config, "WEATHER_PANEL_AZIMUTH", None)
    monkeypatch.setattr(config, "WEATHER_PANEL_TILT", None)
    client = OpenMeteoClient(
        session=FakeSession(payload()),
        cache_file=tmp_path / "weather.json",
    )

    assert not client.configured
    with pytest.raises(WeatherNotConfigured):
        client.fetch()
    assert not (tmp_path / "weather.json").exists()


def test_archive_client_chunks_and_deduplicates(configured):
    session = FakeSession(payload())
    client = OpenMeteoArchiveClient(session=session)

    snapshot = client.fetch_range(
        date(2026, 11, 7), date(2026, 11, 9), chunk_days=2
    )

    assert len(session.calls) == 2
    assert session.calls[0][0] == OPEN_METEO_ARCHIVE_URL
    assert session.calls[0][1]["start_date"] == "2026-11-07"
    assert session.calls[0][1]["end_date"] == "2026-11-08"
    assert session.calls[1][1]["start_date"] == "2026-11-09"
    assert session.calls[1][1]["end_date"] == "2026-11-09"
    assert session.calls[0][1]["tilt"] == pytest.approx(35.0)
    assert session.calls[0][1]["azimuth"] == pytest.approx(0.0)
    assert snapshot.source == "open-meteo-archive"
    assert len(snapshot.hours) == 3


def test_snapshot_payload_round_trip(configured):
    snapshot = parse_weather_payload(payload(), source="archive")

    restored = snapshot_from_payload(snapshot_to_payload(snapshot))

    assert restored.source == "archive"
    assert restored.hours[1].tilted_irradiance_wm2 == pytest.approx(210.0)
    assert restored.panel_tilt == pytest.approx(35.0)


def test_historical_forecast_client_uses_its_own_endpoint(configured):
    session = FakeSession(payload())
    client = OpenMeteoHistoricalForecastClient(session=session)

    snapshot = client.fetch_range(
        date(2026, 11, 7), date(2026, 11, 7)
    )

    assert session.calls[0][0] == OPEN_METEO_HISTORICAL_FORECAST_URL
    assert snapshot.source == "open-meteo-historical-forecast"
