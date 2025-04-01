from evse_controller.utils.config import Config

def test_config_testing_mode():
    config = Config.get_instance()
    assert config._testing is True
    assert config.config['wallbox']['url'] == 'test.local'