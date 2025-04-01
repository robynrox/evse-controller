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
