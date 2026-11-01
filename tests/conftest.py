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
