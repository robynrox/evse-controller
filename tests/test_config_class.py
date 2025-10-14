import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch, mock_open
import yaml

from evse_controller.utils.config import Config


@pytest.fixture(autouse=True)
def reset_config():
    """Reset the Config singleton before and after each test"""
    # Save original state
    original_testing = Config._testing
    original_config_data = Config._config_data
    original_instance = Config._instance

    # Set up testing mode with default configuration
    Config._testing = True
    Config._config_data = None
    Config._instance = None

    yield  # Run the test

    # Restore original state
    Config._testing = original_testing
    Config._config_data = original_config_data
    Config._instance = original_instance


class TestConfig:
    """Test suite for the Config class"""

    def test_singleton_pattern(self):
        """Test that Config follows the singleton pattern"""
        config1 = Config()
        config2 = Config()
        assert config1 is config2

    def test_testing_mode_initialization(self):
        """Test Config initialization in testing mode"""
        # Access the config instance which will trigger initialization
        config = Config()
        # Force initialization by accessing a property
        _ = config.WALLBOX_URL

        assert config._testing is True
        assert config._config_data is not None
        # In testing mode, the config loads from tests/test_config.yaml which has:
        # test_mode: true
        # wallbox:
        #   enabled: false
        # shelly:
        #   enabled: false
        # modbus:
        #   enabled: false
        assert "wallbox" in config._config_data
        # The test config doesn't include tariffs section as it loads test_config.yaml file
        # So we'll check if the structure matches the test file
        assert config._config_data["wallbox"]["enabled"] is False
        assert config._config_data["shelly"]["enabled"] is False
        assert config._config_data["modbus"]["enabled"] is False
        assert config._config_data["test_mode"] is True

    def test_get_method_with_dot_notation(self):
        """Test the get method with dot notation"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        # In test mode it loads test_config.yaml, which has different structure
        # Test getting nested values that exist in test_config.yaml
        value = config.get("wallbox.enabled")
        assert value is False

        # Test getting top-level values
        value = config.get("test_mode")
        assert value is True

        # Test default value when key doesn't exist
        value = config.get("non.existent.key", "default_value")
        assert value == "default_value"

    def test_ioctgo_property_getters(self):
        """Test all IOCTGO property getters"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        assert config.IOCTGO_BATTERY_CAPACITY_KWH == 59
        assert config.IOCTGO_TARGET_SOC_AT_CHEAP_START == 54
        assert config.IOCTGO_BULK_DISCHARGE_START_TIME == "16:00"
        assert config.IOCTGO_MIN_DISCHARGE_CURRENT == 3
        assert config.IOCTGO_SOC_THRESHOLD_FOR_STRATEGY == 50
        assert config.IOCTGO_GRID_IMPORT_THRESHOLD_HIGH_SOC == 0
        assert config.IOCTGO_GRID_IMPORT_THRESHOLD_LOW_SOC == 720
        assert config.IOCTGO_SMART_OCPP_OPERATION is True
        assert config.IOCTGO_OCPP_ENABLE_SOC_THRESHOLD == 30
        assert config.IOCTGO_OCPP_DISABLE_SOC_THRESHOLD == 95
        assert config.IOCTGO_OCPP_ENABLE_TIME == "23:30"
        assert config.IOCTGO_OCPP_DISABLE_TIME == "11:00"

    def test_ioctgo_property_setters(self):
        """Test all IOCTGO property setters - ensuring they properly set nested values"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        # Set new values
        config.IOCTGO_BATTERY_CAPACITY_KWH = 60
        config.IOCTGO_TARGET_SOC_AT_CHEAP_START = 60
        config.IOCTGO_BULK_DISCHARGE_START_TIME = "17:00"
        config.IOCTGO_MIN_DISCHARGE_CURRENT = 4
        config.IOCTGO_SOC_THRESHOLD_FOR_STRATEGY = 60
        config.IOCTGO_GRID_IMPORT_THRESHOLD_HIGH_SOC = 100
        config.IOCTGO_GRID_IMPORT_THRESHOLD_LOW_SOC = 800
        config.IOCTGO_SMART_OCPP_OPERATION = False
        config.IOCTGO_OCPP_ENABLE_SOC_THRESHOLD = 40
        config.IOCTGO_OCPP_DISABLE_SOC_THRESHOLD = 90
        config.IOCTGO_OCPP_ENABLE_TIME = "22:00"
        config.IOCTGO_OCPP_DISABLE_TIME = "10:00"

        # Verify values were properly set in nested structure
        assert config.IOCTGO_BATTERY_CAPACITY_KWH == 60
        assert config.IOCTGO_TARGET_SOC_AT_CHEAP_START == 60
        assert config.IOCTGO_BULK_DISCHARGE_START_TIME == "17:00"
        assert config.IOCTGO_MIN_DISCHARGE_CURRENT == 4
        assert config.IOCTGO_SOC_THRESHOLD_FOR_STRATEGY == 60
        assert config.IOCTGO_GRID_IMPORT_THRESHOLD_HIGH_SOC == 100
        assert config.IOCTGO_GRID_IMPORT_THRESHOLD_LOW_SOC == 800
        assert config.IOCTGO_SMART_OCPP_OPERATION is False
        assert config.IOCTGO_OCPP_ENABLE_SOC_THRESHOLD == 40
        assert config.IOCTGO_OCPP_DISABLE_SOC_THRESHOLD == 90
        assert config.IOCTGO_OCPP_ENABLE_TIME == "22:00"
        assert config.IOCTGO_OCPP_DISABLE_TIME == "10:00"

    def test_charging_property_getters_and_setters(self):
        """Test charging property getters and setters"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        # Test initial values
        assert config.STARTUP_STATE == "FREERUN"
        assert config.MAX_CHARGE_PERCENT == 90
        assert config.SOLAR_PERIOD_MAX_CHARGE == 80

        # Test setting new values
        config.STARTUP_STATE = "OCPP"
        config.MAX_CHARGE_PERCENT = 85
        config.SOLAR_PERIOD_MAX_CHARGE = 75

        # Verify values were properly set
        assert config.STARTUP_STATE == "OCPP"
        assert config.MAX_CHARGE_PERCENT == 85
        assert config.SOLAR_PERIOD_MAX_CHARGE == 75

    def test_wallbox_property_getters_and_setters(self):
        """Test wallbox property getters and setters"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        # In test mode it loads test_config.yaml, so test the actual values
        # The test config has "wallbox: {enabled: false}" but not the other fields
        # So they will be the default empty values
        assert config.WALLBOX_URL == ""
        assert config.WALLBOX_USERNAME == ""
        assert config.WALLBOX_PASSWORD == ""
        assert config.WALLBOX_SERIAL is None
        # The USE_WALLBOX_SIMULATOR will be the default value (False)
        # because it's not in the test config
        assert config.USE_WALLBOX_SIMULATOR is False

        # Test setting new values
        config.WALLBOX_URL = "new.test.local"
        config.WALLBOX_USERNAME = "newuser"
        config.WALLBOX_PASSWORD = "newpass"
        config.WALLBOX_SERIAL = "newserial"
        config.USE_WALLBOX_SIMULATOR = True

        # Verify values were properly set
        assert config.WALLBOX_URL == "new.test.local"
        assert config.WALLBOX_USERNAME == "newuser"
        assert config.WALLBOX_PASSWORD == "newpass"
        assert config.WALLBOX_SERIAL == "newserial"
        assert config.USE_WALLBOX_SIMULATOR is True

    def test_shelly_property_getters_and_setters(self):
        """Test shelly property getters and setters"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        # Test initial values
        assert config.SHELLY_PRIMARY_URL == ""
        assert config.SHELLY_SECONDARY_URL == ""

        # Test setting new values
        config.SHELLY_PRIMARY_URL = "http://primary-shelly.local"
        config.SHELLY_SECONDARY_URL = "http://secondary-shelly.local"

        # Verify values were properly set
        assert config.SHELLY_PRIMARY_URL == "http://primary-shelly.local"
        assert config.SHELLY_SECONDARY_URL == "http://secondary-shelly.local"

    def test_logging_property_getters_and_setters(self):
        """Test logging property getters and setters"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        # Test initial values
        assert config.FILE_LOGGING == "INFO"
        assert config.CONSOLE_LOGGING == "WARNING"

        # Test setting new values
        config.FILE_LOGGING = "DEBUG"
        config.CONSOLE_LOGGING = "ERROR"

        # Verify values were properly set
        assert config.FILE_LOGGING == "DEBUG"
        assert config.CONSOLE_LOGGING == "ERROR"

    def test_as_dict_method(self):
        """Test the as_dict method returns all configuration properly"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        config_dict = config.as_dict()

        # Check that critical sections exist
        assert "tariffs" in config_dict
        assert "ioctgo" in config_dict["tariffs"]
        assert "wallbox" in config_dict
        assert "shelly" in config_dict
        assert "charging" in config_dict
        assert "logging" in config_dict

        # Check that IOCTGO values are properly included
        assert config_dict["tariffs"]["ioctgo"]["battery_capacity_kwh"] == 59
        assert config_dict["tariffs"]["ioctgo"]["target_soc_at_cheap_start"] == 54

        # Update a value and check it appears in as_dict
        config.IOCTGO_BATTERY_CAPACITY_KWH = 70
        config_dict = config.as_dict()
        assert config_dict["tariffs"]["ioctgo"]["battery_capacity_kwh"] == 70

    def test_save_method_creates_backup(self):
        """Test the save method creates a backup and writes config to file"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        with tempfile.TemporaryDirectory() as temp_dir:
            # Patch the get_config_file function to use our temp directory
            with patch('evse_controller.utils.config.get_config_file') as mock_get_config:
                config_path = Path(temp_dir) / "config.yaml"
                mock_get_config.return_value = config_path

                # Write initial config file
                with open(config_path, 'w') as f:
                    yaml.dump({
                        "wallbox": {"url": "original.local"},
                        "tariffs": {"ioctgo": {"battery_capacity_kwh": 50}}
                    }, f)

                # Modify config and save
                config.IOCTGO_BATTERY_CAPACITY_KWH = 80
                config.WALLBOX_URL = "modified.local"
                config.save()

                # Check that backup was created
                backup_path = config_path.with_suffix('.yaml.bak')
                assert backup_path.exists()

                # Check that new config was written
                with open(config_path, 'r') as f:
                    saved_config = yaml.safe_load(f)

                assert saved_config["wallbox"]["url"] == "modified.local"
                assert saved_config["tariffs"]["ioctgo"]["battery_capacity_kwh"] == 80

                # Check that backup has original values
                with open(backup_path, 'r') as f:
                    backup_config = yaml.safe_load(f)

                assert backup_config["wallbox"]["url"] == "original.local"
                assert backup_config["tariffs"]["ioctgo"]["battery_capacity_kwh"] == 50

    def test_save_method_with_exception_restores_backup(self):
        """Test that if save fails, the backup is restored"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        with tempfile.TemporaryDirectory() as temp_dir:
            # Mock the get_config_file function to use our temp directory
            with patch('evse_controller.utils.config.get_config_file') as mock_get_config:
                config_path = Path(temp_dir) / "config.yaml"
                mock_get_config.return_value = config_path

                # Create initial config file
                with open(config_path, 'w') as f:
                    yaml.dump({"wallbox": {"url": "original.local"}}, f)

                # Mock yaml.dump to raise an exception
                with patch('yaml.dump', side_effect=Exception("Save failed")):
                    with pytest.raises(Exception, match="Save failed"):
                        config.WALLBOX_URL = "modified.local"
                        config.save()

                # Check that original config was restored
                with open(config_path, 'r') as f:
                    restored_config = yaml.safe_load(f)

                assert restored_config["wallbox"]["url"] == "original.local"

    def test_channel_methods(self):
        """Test the channel-related methods and properties"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        # Test default channel values
        assert config.get_channel_name("primary", 1) == "Channel 1"
        assert config.get_channel_abbreviation("primary", 1) == "Ch1"
        assert config.is_channel_in_use("primary", 1) is True

        # Test setting channel properties
        config.set_channel_name("primary", 1, "Solar Panel")
        config.set_channel_abbreviation("primary", 1, "SP1")
        config.set_channel_in_use("primary", 1, False)

        # Verify the changes
        assert config.get_channel_name("primary", 1) == "Solar Panel"
        assert config.get_channel_abbreviation("primary", 1) == "SP1"
        assert config.is_channel_in_use("primary", 1) is False

    def test_backward_compatibility_for_use_simulator_flag(self):
        """Test that missing use_simulator flag is handled correctly"""
        # Save original state
        original_testing = Config._testing
        original_config_data = Config._config_data
        original_instance = Config._instance

        try:
            # Create a test config without use_simulator flag
            test_config_data = {
                'wallbox': {
                    'url': 'test.local',
                    'username': 'test',
                    'password': 'test',
                    'serial': 'test'
                    # Note: no use_simulator flag
                }
            }

            # Set up the config directly with test data
            Config._testing = False  # Disable testing mode to force file loading behavior
            Config._config_data = test_config_data
            Config._instance = None  # Force new instance

            config = Config()
            # The system should default to False when use_simulator is missing but URL is present
            assert config.USE_WALLBOX_SIMULATOR is False
        finally:
            # Restore original state
            Config._testing = original_testing
            Config._config_data = original_config_data
            Config._instance = original_instance

    def test_property_consistency_after_setting(self):
        """Test that setting properties results in consistent get behavior"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        # Test IOCTGO properties
        test_values = {
            'IOCTGO_BATTERY_CAPACITY_KWH': 75,
            'IOCTGO_TARGET_SOC_AT_CHEAP_START': 65,
            'IOCTGO_MIN_DISCHARGE_CURRENT': 5,
            'IOCTGO_SOC_THRESHOLD_FOR_STRATEGY': 60
        }

        for prop_name, test_value in test_values.items():
            # Set the property
            setattr(config, prop_name, test_value)
            # Get the property and verify it matches
            assert getattr(config, prop_name) == test_value

    def test_influxdb_property_getters_and_setters(self):
        """Test influxdb property getters and setters"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        # Test initial values
        assert config.INFLUXDB_ENABLED is False
        assert config.INFLUXDB_URL == "http://localhost:8086"
        assert config.INFLUXDB_TOKEN == ""
        assert config.INFLUXDB_ORG == ""
        assert config.INFLUXDB_BUCKET == "powerlog"

        # Test setting new values
        config.INFLUXDB_ENABLED = True
        config.INFLUXDB_URL = "http://influxdb.example.com:8086"
        config.INFLUXDB_TOKEN = "my-token"
        config.INFLUXDB_ORG = "my-org"
        config.INFLUXDB_BUCKET = "my-bucket"

        # Verify values were properly set
        assert config.INFLUXDB_ENABLED is True
        assert config.INFLUXDB_URL == "http://influxdb.example.com:8086"
        assert config.INFLUXDB_TOKEN == "my-token"
        assert config.INFLUXDB_ORG == "my-org"
        assert config.INFLUXDB_BUCKET == "my-bucket"

    def test_config_file_roundtrip_ioctgo_values(self):
        """Test that IOCTGO values are properly written to and read from config file"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        # Set new values for IOCTGO properties
        config.IOCTGO_BATTERY_CAPACITY_KWH = 80
        config.IOCTGO_TARGET_SOC_AT_CHEAP_START = 70
        config.IOCTGO_BULK_DISCHARGE_START_TIME = "18:00"
        config.IOCTGO_MIN_DISCHARGE_CURRENT = 6
        config.IOCTGO_SOC_THRESHOLD_FOR_STRATEGY = 55
        config.IOCTGO_GRID_IMPORT_THRESHOLD_HIGH_SOC = 200
        config.IOCTGO_GRID_IMPORT_THRESHOLD_LOW_SOC = 850
        config.IOCTGO_SMART_OCPP_OPERATION = False
        config.IOCTGO_OCPP_ENABLE_SOC_THRESHOLD = 35
        config.IOCTGO_OCPP_DISABLE_SOC_THRESHOLD = 90
        config.IOCTGO_OCPP_ENABLE_TIME = "22:30"
        config.IOCTGO_OCPP_DISABLE_TIME = "10:30"

        with tempfile.TemporaryDirectory() as temp_dir:
            # Patch the get_config_file function to use our temp directory
            with patch('evse_controller.utils.config.get_config_file') as mock_get_config:
                config_path = Path(temp_dir) / "config.yaml"
                mock_get_config.return_value = config_path

                # Save the config to file
                config.save()

                # Check that the config file was written correctly
                assert config_path.exists()
                with open(config_path, 'r') as f:
                    saved_config = yaml.safe_load(f)

                # Verify all IOCTGO values were saved properly
                assert saved_config["tariffs"]["ioctgo"]["battery_capacity_kwh"] == 80
                assert saved_config["tariffs"]["ioctgo"]["target_soc_at_cheap_start"] == 70
                assert saved_config["tariffs"]["ioctgo"]["bulk_discharge_start_time"] == "18:00"
                assert saved_config["tariffs"]["ioctgo"]["min_discharge_current"] == 6
                assert saved_config["tariffs"]["ioctgo"]["soc_threshold_for_strategy"] == 55
                assert saved_config["tariffs"]["ioctgo"]["grid_import_threshold_high_soc"] == 200
                assert saved_config["tariffs"]["ioctgo"]["grid_import_threshold_low_soc"] == 850
                assert saved_config["tariffs"]["ioctgo"]["smart_ocpp_operation"] is False
                assert saved_config["tariffs"]["ioctgo"]["ocpp_enable_soc_threshold"] == 35
                assert saved_config["tariffs"]["ioctgo"]["ocpp_disable_soc_threshold"] == 90
                assert saved_config["tariffs"]["ioctgo"]["ocpp_enable_time"] == "22:30"
                assert saved_config["tariffs"]["ioctgo"]["ocpp_disable_time"] == "10:30"

                # Create a new Config instance to load from the file
                # Save original state and reset config singleton
                original_testing = Config._testing
                original_config_data = Config._config_data
                original_instance = Config._instance
                
                # Reset the Config singleton
                Config._testing = False
                Config._config_data = None
                Config._instance = None

                # Now create another Config instance and mock the file path
                with patch('evse_controller.utils.config.get_config_file') as mock_get_config2:
                    mock_get_config2.return_value = config_path
                    config2 = Config()
                    # Force initialization
                    _ = config2.IOCTGO_BATTERY_CAPACITY_KWH

                    # Verify that values were properly loaded from file
                    assert config2.IOCTGO_BATTERY_CAPACITY_KWH == 80
                    assert config2.IOCTGO_TARGET_SOC_AT_CHEAP_START == 70
                    assert config2.IOCTGO_BULK_DISCHARGE_START_TIME == "18:00"
                    assert config2.IOCTGO_MIN_DISCHARGE_CURRENT == 6
                    assert config2.IOCTGO_SOC_THRESHOLD_FOR_STRATEGY == 55
                    assert config2.IOCTGO_GRID_IMPORT_THRESHOLD_HIGH_SOC == 200
                    assert config2.IOCTGO_GRID_IMPORT_THRESHOLD_LOW_SOC == 850
                    assert config2.IOCTGO_SMART_OCPP_OPERATION is False
                    assert config2.IOCTGO_OCPP_ENABLE_SOC_THRESHOLD == 35
                    assert config2.IOCTGO_OCPP_DISABLE_SOC_THRESHOLD == 90
                    assert config2.IOCTGO_OCPP_ENABLE_TIME == "22:30"
                    assert config2.IOCTGO_OCPP_DISABLE_TIME == "10:30"

                # Restore original state
                Config._testing = original_testing
                Config._config_data = original_config_data
                Config._instance = original_instance

    def test_config_file_roundtrip_all_sections(self):
        """Test that values from all sections are properly written to and read from config file"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        # Set new values across different sections
        config.WALLBOX_URL = "test.evse.local"
        config.WALLBOX_USERNAME = "newuser"
        config.WALLBOX_PASSWORD = "newpass"
        config.SHELLY_PRIMARY_URL = "http://shelly1.local"
        config.SHELLY_SECONDARY_URL = "http://shelly2.local"
        config.STARTUP_STATE = "OCPP"
        config.MAX_CHARGE_PERCENT = 85
        config.IOCTGO_BATTERY_CAPACITY_KWH = 75
        config.INFLUXDB_ENABLED = True
        config.INFLUXDB_URL = "http://influx.example.com:8086"

        with tempfile.TemporaryDirectory() as temp_dir:
            # Patch the get_config_file function to use our temp directory
            with patch('evse_controller.utils.config.get_config_file') as mock_get_config:
                config_path = Path(temp_dir) / "config.yaml"
                mock_get_config.return_value = config_path

                # Save the config to file
                config.save()

                # Check that the config file was written correctly
                assert config_path.exists()
                with open(config_path, 'r') as f:
                    saved_config = yaml.safe_load(f)

                # Verify values from all sections were saved properly
                assert saved_config["wallbox"]["url"] == "test.evse.local"
                assert saved_config["wallbox"]["username"] == "newuser"
                assert saved_config["wallbox"]["password"] == "newpass"
                assert saved_config["shelly"]["primary_url"] == "http://shelly1.local"
                assert saved_config["shelly"]["secondary_url"] == "http://shelly2.local"
                assert saved_config["charging"]["startup_state"] == "OCPP"
                assert saved_config["charging"]["max_charge_percent"] == 85
                assert saved_config["tariffs"]["ioctgo"]["battery_capacity_kwh"] == 75
                assert saved_config["influxdb"]["enabled"] is True
                assert saved_config["influxdb"]["url"] == "http://influx.example.com:8086"

                # Create a new Config instance to load from the file
                # Save original state and reset config singleton
                original_testing = Config._testing
                original_config_data = Config._config_data
                original_instance = Config._instance
                
                # Reset the Config singleton
                Config._testing = False
                Config._config_data = None
                Config._instance = None

                # Now create another Config instance and mock the file path
                with patch('evse_controller.utils.config.get_config_file') as mock_get_config2:
                    mock_get_config2.return_value = config_path
                    config2 = Config()
                    # Force initialization
                    _ = config2.WALLBOX_URL

                    # Verify that values were properly loaded from file
                    assert config2.WALLBOX_URL == "test.evse.local"
                    assert config2.WALLBOX_USERNAME == "newuser"
                    assert config2.WALLBOX_PASSWORD == "newpass"
                    assert config2.SHELLY_PRIMARY_URL == "http://shelly1.local"
                    assert config2.SHELLY_SECONDARY_URL == "http://shelly2.local"
                    assert config2.STARTUP_STATE == "OCPP"
                    assert config2.MAX_CHARGE_PERCENT == 85
                    assert config2.IOCTGO_BATTERY_CAPACITY_KWH == 75
                    assert config2.INFLUXDB_ENABLED is True
                    assert config2.INFLUXDB_URL == "http://influx.example.com:8086"

                # Restore original state
                Config._testing = original_testing
                Config._config_data = original_config_data
                Config._instance = original_instance

    def test_no_duplicate_config_keys_after_setting_ioctgo_values(self):
        """Test that setting IOCTGO values doesn't create duplicate config structure"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        # Set IOCTGO values which previously caused duplication issue
        config.IOCTGO_BATTERY_CAPACITY_KWH = 80
        config.IOCTGO_TARGET_SOC_AT_CHEAP_START = 70
        config.IOCTGO_MIN_DISCHARGE_CURRENT = 6

        # Check internal structure - there should be a 'tariffs' key with 'ioctgo' inside
        # There should NOT be a key named 'tariffs.ioctgo' (with dot in the name)
        assert 'tariffs' in config._config_data
        assert 'ioctgo' in config._config_data['tariffs']
        assert 'tariffs.ioctgo' not in config._config_data  # This is the key test

        # Verify the values are correctly nested
        ioctgo_config = config._config_data['tariffs']['ioctgo']
        assert ioctgo_config['battery_capacity_kwh'] == 80
        assert ioctgo_config['target_soc_at_cheap_start'] == 70
        assert ioctgo_config['min_discharge_current'] == 6

        # Check that save and load operations don't create duplication
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch('evse_controller.utils.config.get_config_file') as mock_get_config:
                config_path = Path(temp_dir) / "config.yaml"
                mock_get_config.return_value = config_path

                config.save()

                # Load the saved config
                with open(config_path, 'r') as f:
                    saved_config = yaml.safe_load(f)

                # Verify the saved config doesn't have the duplicated structure
                assert 'tariffs' in saved_config
                assert 'ioctgo' in saved_config['tariffs']
                assert 'tariffs.ioctgo' not in saved_config  # Main test for regression

                # Verify values were saved properly
                assert saved_config['tariffs']['ioctgo']['battery_capacity_kwh'] == 80
                assert saved_config['tariffs']['ioctgo']['target_soc_at_cheap_start'] == 70
                assert saved_config['tariffs']['ioctgo']['min_discharge_current'] == 6

    def test_no_duplicate_config_keys_all_dotted_sections(self):
        """Test that all dotted sections (tariffs.ioctgo, shelly.grid, shelly.evse, wallbox.simulator) 
        don't create duplicate config structures"""
        config = Config()
        # Force initialization
        _ = config.WALLBOX_URL

        # Set values for all dotted sections
        config.IOCTGO_BATTERY_CAPACITY_KWH = 80
        config.IOCTGO_TARGET_SOC_AT_CHEAP_START = 70
        
        config.SIMULATOR_INITIAL_BATTERY_LEVEL = 65
        config.SIMULATOR_BATTERY_CAPACITY_KWH = 60
        
        config.SHELLY_GRID_DEVICE = "primary"
        config.SHELLY_GRID_CHANNEL = 2
        config.SHELLY_EVSE_DEVICE = "secondary"
        config.SHELLY_EVSE_CHANNEL = 1

        # Check internal structure - verify no dotted keys exist at root level
        dotted_keys_at_root = []
        for key in config._config_data.keys():
            if '.' in key:
                dotted_keys_at_root.append(key)
        
        assert not dotted_keys_at_root, f"Found dotted keys at root level: {dotted_keys_at_root}"

        # Verify proper nested structure exists
        assert 'tariffs' in config._config_data
        assert 'ioctgo' in config._config_data['tariffs']
        assert 'wallbox' in config._config_data
        assert 'simulator' in config._config_data['wallbox']
        assert 'shelly' in config._config_data
        assert 'grid' in config._config_data['shelly']
        assert 'evse' in config._config_data['shelly']

        # Verify specific values are correctly nested
        assert config._config_data['tariffs']['ioctgo']['battery_capacity_kwh'] == 80
        assert config._config_data['wallbox']['simulator']['initial_battery_level'] == 65
        assert config._config_data['shelly']['grid']['device'] == "primary"
        assert config._config_data['shelly']['evse']['device'] == "secondary"

        # Check that save and load operations don't create duplication
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch('evse_controller.utils.config.get_config_file') as mock_get_config:
                config_path = Path(temp_dir) / "config.yaml"
                mock_get_config.return_value = config_path

                config.save()

                # Load the saved config
                with open(config_path, 'r') as f:
                    saved_config = yaml.safe_load(f)

                # Verify the saved config doesn't have any dotted keys at root level
                saved_dotted_keys = []
                for key in saved_config.keys():
                    if '.' in key:
                        saved_dotted_keys.append(key)
                
                assert not saved_dotted_keys, f"Found dotted keys in saved config: {saved_dotted_keys}"

                # Verify proper structure is maintained
                assert 'tariffs' in saved_config and 'ioctgo' in saved_config['tariffs']
                assert 'wallbox' in saved_config and 'simulator' in saved_config['wallbox']
                assert 'shelly' in saved_config and 'grid' in saved_config['shelly']
                assert 'shelly' in saved_config and 'evse' in saved_config['shelly']

                # Verify values were saved properly
                assert saved_config['tariffs']['ioctgo']['battery_capacity_kwh'] == 80
                assert saved_config['wallbox']['simulator']['initial_battery_level'] == 65
                assert saved_config['shelly']['grid']['device'] == "primary"
                assert saved_config['shelly']['evse']['device'] == "secondary"