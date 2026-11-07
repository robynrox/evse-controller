import pytest
from evse_controller.utils.config import Config

def pytest_configure(config):
    """Called before test collection - set up test configuration"""
    Config.set_testing(True)

def pytest_unconfigure(config):
    """Called after all tests are done"""
    Config.set_testing(False)

@pytest.fixture(autouse=True)
def setup_test_config():
    """Per-test configuration if needed"""
    yield


@pytest.fixture(autouse=True)
def isolated_energy_sessions(tmp_path):
    """Keep real supplier sessions out of the shared store during tests.

    The store is a process-wide singleton so the web UI and the tariff see the
    same windows.  Without this, a session added on the live system would
    change the tariff decisions under test.
    """
    from evse_controller.energy_sessions import session_store

    original_path = session_store.path
    original_sessions = session_store.sessions
    session_store.path = tmp_path / "sessions.json"
    session_store.sessions = []
    try:
        yield session_store
    finally:
        session_store.path = original_path
        session_store.sessions = original_sessions


@pytest.fixture(autouse=True)
def isolated_tariff_state(tmp_path, monkeypatch):
    """Keep a live tariff state file out of unit tests.

    The tariff restores its stored-energy ledger on construction.  Without
    this, running the controller on the same machine and then running the
    tests would silently change the ledger and savings the tests start with.
    """
    from evse_controller.tariffs.octopus.ioctgo_with_agile_outgoing import (
        IOctGoWithAgileOutgoingTariff,
    )

    monkeypatch.setattr(
        IOctGoWithAgileOutgoingTariff,
        "STATE_PATH",
        tmp_path / "ioctgo_agile_outgoing_state.json",
    )
    yield


@pytest.fixture(autouse=True)
def isolated_daily_report(tmp_path):
    """Keep the per-day energy log out of the live data directory.

    The log is a process-wide singleton because the controller, the web UI
    and the API must see the same day.  Tests point it at a temporary file and
    reset its in-memory day between cases.
    """
    from evse_controller.daily_report import daily_log

    daily_log.reset(
        tmp_path / "daily_reports",
        profile_directory=tmp_path / "load_profile",
    )
    try:
        yield daily_log
    finally:
        daily_log.reset()
        daily_log.set_directory(None)
        daily_log.set_profile_directory(None)


@pytest.fixture(autouse=True)
def isolated_price_book(tmp_path, monkeypatch):
    """Keep the Agile rate cache local and never hit the network in tests."""
    from evse_controller import slot_prices

    slot_prices.price_book.reset(tmp_path / "agile_rates")
    monkeypatch.setattr(
        slot_prices, "fetch_day_rates", lambda *args, **kwargs: []
    )
    try:
        yield slot_prices.price_book
    finally:
        slot_prices.price_book.reset()
        slot_prices.price_book.set_directory(None)
