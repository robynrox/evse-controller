import pytest
from evse_controller.utils.config import Config
from evse_controller.configure import DEFAULT_CONFIG

@pytest.fixture(autouse=True)
def reset_config():
    """Reset the Config singleton after each test"""
    original_testing = Config._testing
    original_config_data = Config._config_data
    original_instance = Config._instance

    yield

    # Reset the singleton to its original state
    Config._testing = original_testing
    Config._config_data = original_config_data
    Config._instance = original_instance

def test_default_config_has_channel_structure():
    """Test that the default config includes the new channel structure"""
    # This test will fail initially and pass once we implement the changes
    assert "channels" in DEFAULT_CONFIG["shelly"]

    # Check primary device channels
    assert "primary" in DEFAULT_CONFIG["shelly"]["channels"]
    assert "channel1" in DEFAULT_CONFIG["shelly"]["channels"]["primary"]
    assert "channel2" in DEFAULT_CONFIG["shelly"]["channels"]["primary"]

    # Check channel properties
    channel1 = DEFAULT_CONFIG["shelly"]["channels"]["primary"]["channel1"]
    assert "name" in channel1
    assert "abbreviation" in channel1
    assert "in_use" in channel1

    # Check secondary device channels
    assert "secondary" in DEFAULT_CONFIG["shelly"]["channels"]

def test_backward_compatibility():
    """Test that old config files without channel info are handled correctly"""
    # Create an old-style config file
    old_config = {
        "shelly": {
            "primary_url": "http://192.168.1.100",
            "secondary_url": "http://192.168.1.101",
            "grid": {
                "device": "primary",
                "channel": 1
            },
            "evse": {
                "device": "secondary",
                "channel": 1
            }
        }
    }

    # Set up Config to use our test config directly
    Config._testing = True
    Config._config_data = old_config
    Config._instance = None

    # Create a new config instance
    test_config = Config()

    # Test that the old config is loaded correctly
    assert test_config.SHELLY_PRIMARY_URL == "http://192.168.1.100"
    assert test_config.SHELLY_GRID_DEVICE == "primary"

    # Test that channel properties are accessible with defaults
    # These properties will be implemented as part of the solution
    assert hasattr(test_config, "get_channel_name")
    assert test_config.get_channel_name("primary", 1) == "Channel 1"  # Default name
    assert test_config.get_channel_abbreviation("primary", 1) == "Ch1"  # Default abbreviation
    assert test_config.is_channel_in_use("primary", 1) == True  # Default to True for backward compatibility

def test_save_and_load_channel_config():
    """Test saving and loading a config with channel information"""
    # Create a config with channel information
    test_config = {
        "shelly": {
            "primary_url": "http://192.168.1.100",
            "secondary_url": "",
            "channels": {
                "primary": {
                    "channel1": {
                        "name": "Grid Import/Export",
                        "abbreviation": "Grid",
                        "in_use": True
                    },
                    "channel2": {
                        "name": "Heat Pump",
                        "abbreviation": "HP",
                        "in_use": False
                    }
                }
            },
            "grid": {
                "device": "primary",
                "channel": 1
            }
        }
    }

    # Set up Config to use our test config directly
    Config._testing = True
    Config._config_data = test_config
    Config._instance = None

    # Create a new config instance
    test_config_obj = Config()

    # Test that channel properties are loaded correctly
    assert test_config_obj.get_channel_name("primary", 1) == "Grid Import/Export"
    assert test_config_obj.get_channel_abbreviation("primary", 1) == "Grid"
    assert test_config_obj.is_channel_in_use("primary", 1) == True
    assert test_config_obj.is_channel_in_use("primary", 2) == False

def test_set_channel_properties():
    """Test setting channel properties"""
    # Set up a test config
    Config._testing = True
    Config._config_data = None
    Config._instance = None

    # Create a new config instance
    test_config = Config()
    test_config._ensure_initialized()

    # Set channel properties
    test_config.set_channel_name("primary", 1, "Test Channel")
    test_config.set_channel_abbreviation("primary", 1, "TC")
    test_config.set_channel_in_use("primary", 1, False)

    # Test that properties were set correctly
    assert test_config.get_channel_name("primary", 1) == "Test Channel"
    assert test_config.get_channel_abbreviation("primary", 1) == "TC"
    assert test_config.is_channel_in_use("primary", 1) == False
