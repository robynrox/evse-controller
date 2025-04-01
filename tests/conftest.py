import pytest
from evse_controller.utils.config import Config

@pytest.fixture(autouse=True)
def setup_test_config():
    """Automatically set up test configuration for all tests"""
    Config.set_testing(True)
    yield
    Config.set_testing(False)