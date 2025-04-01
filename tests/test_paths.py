import os
from pathlib import Path
import pytest
from evse_controller.utils.paths import get_data_dir, get_log_dir, ensure_data_dirs, get_config_file

@pytest.fixture
def temp_dir(tmp_path):
    """Provide a temporary directory and set EVSE_DATA_DIR to point to it."""
    old_env = os.environ.get('EVSE_DATA_DIR')
    real_data_dir = Path(__file__).parent.parent / "data"
    
    # Ensure we're not using the real data directory
    assert tmp_path != real_data_dir, "Temporary path matches real data directory!"
    
    os.environ['EVSE_DATA_DIR'] = str(tmp_path)
    print(f"Setting EVSE_DATA_DIR to {tmp_path}")
    
    yield tmp_path
    
    # Extra safety: ensure we didn't write to real data directory
    if real_data_dir.exists():
        config_file = real_data_dir / "config" / "config.yaml"
        if config_file.exists() and config_file.stat().st_size == 0:
            raise RuntimeError(f"Real config file at {config_file} was emptied during test!")
    
    if old_env is not None:
        print(f"Restoring EVSE_DATA_DIR to {old_env}")
        os.environ['EVSE_DATA_DIR'] = old_env
    else:
        print("Removing EVSE_DATA_DIR from environment")
        os.environ.pop('EVSE_DATA_DIR', None)  # Using pop with default value instead of del

@pytest.fixture
def clear_env():
    """Remove EVSE_DATA_DIR from environment if it exists."""
    old_env = os.environ.get('EVSE_DATA_DIR')
    if 'EVSE_DATA_DIR' in os.environ:
        del os.environ['EVSE_DATA_DIR']
    yield
    if old_env is not None:
        os.environ['EVSE_DATA_DIR'] = old_env

@pytest.fixture
def temp_cwd(tmp_path):
    """Temporarily change the current working directory."""
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(old_cwd)

def test_get_data_dir_with_env_var(temp_dir, temp_cwd):
    """Test that get_data_dir respects EVSE_DATA_DIR environment variable."""
    result = get_data_dir()
    assert result == temp_dir
    assert isinstance(result, Path)

def test_get_data_dir_default(clear_env, temp_cwd):
    """Test that get_data_dir defaults to project_root/data when no env var is set."""
    result = get_data_dir()
    assert result.name == "data"
    assert result.parent == Path(__file__).parent.parent
    assert isinstance(result, Path)

def test_get_log_dir(temp_dir, temp_cwd):
    """Test that get_log_dir returns the correct logs subdirectory."""
    log_dir = get_log_dir()
    assert log_dir == temp_dir / "logs"
    assert isinstance(log_dir, Path)

def test_ensure_data_dirs_creates_directories(temp_dir, temp_cwd):
    """Test that ensure_data_dirs creates all required directories."""
    # First verify directories don't exist
    assert not (temp_dir / "config").exists()
    assert not (temp_dir / "logs").exists()
    assert not (temp_dir / "state").exists()
    
    # Create directories
    ensure_data_dirs()
    
    # Verify directories were created
    assert (temp_dir / "config").is_dir()
    assert (temp_dir / "logs").is_dir()
    assert (temp_dir / "state").is_dir()

def test_ensure_data_dirs_handles_existing_directories(temp_dir, temp_cwd):
    """Test that ensure_data_dirs works even if directories already exist."""
    # Create directories first
    (temp_dir / "config").mkdir()
    (temp_dir / "logs").mkdir()
    (temp_dir / "state").mkdir()
    
    # Should not raise any exceptions
    ensure_data_dirs()
    
    # Verify directories still exist
    assert (temp_dir / "config").is_dir()
    assert (temp_dir / "logs").is_dir()
    assert (temp_dir / "state").is_dir()

def test_get_config_file_data_dir_exists(temp_dir, temp_cwd):
    """Test that get_config_file returns data dir config when it exists."""
    data_config = get_data_dir() / "config" / "config.yaml"
    data_config.parent.mkdir(parents=True, exist_ok=True)
    data_config.write_text("data config")
    
    # Create a config in temporary cwd
    Path("config.yaml").write_text("cwd config")
    
    result = get_config_file()
    assert result == data_config
    assert result.read_text() == "data config"
    
    # Clean up not strictly necessary as tmp_path will be removed
    Path("config.yaml").unlink()

def test_get_config_file_copies_from_cwd(temp_dir, temp_cwd):
    """Test that get_config_file copies from cwd when data dir config doesn't exist."""
    cwd_config = Path("config.yaml")
    cwd_config.write_text("cwd config")
    
    result = get_config_file()
    expected_path = get_data_dir() / "config" / "config.yaml"
    
    assert result == expected_path
    assert result.read_text() == "cwd config"
    
    # Clean up not strictly necessary as tmp_path will be removed
    cwd_config.unlink()

def test_get_config_file_no_config_exists(temp_dir, temp_cwd):
    """Test that get_config_file exits when no config exists."""
    with pytest.raises(SystemExit) as exc_info:
        get_config_file()
    assert exc_info.value.code == 1

def test_get_config_file_no_config_exists_required(temp_dir, temp_cwd):
    """Test that get_config_file exits when no config exists and require_exists=True."""
    with pytest.raises(SystemExit) as exc_info:
        get_config_file(require_exists=True)
    assert exc_info.value.code == 1

def test_get_config_file_no_config_exists_not_required(temp_dir, temp_cwd):
    """Test that get_config_file returns expected path when require_exists=False."""
    result = get_config_file(require_exists=False)
    expected_path = get_data_dir() / "config" / "config.yaml"
    assert result == expected_path
    assert not result.exists()  # Confirms file wasn't created

def test_data_dir_environment_handling(temp_dir, temp_cwd):
    """Verify that environment variable handling doesn't affect real config."""
    # Save current state
    real_data_dir = Path(__file__).parent.parent / "data"
    real_config = real_data_dir / "config" / "config.yaml"
    original_content = real_config.read_text() if real_config.exists() else None
    
    # Do some operations
    test_config = temp_dir / "config" / "config.yaml"
    test_config.parent.mkdir(parents=True, exist_ok=True)
    test_config.write_text("test config")
    
    # Verify we're using test directory
    assert get_data_dir() == temp_dir
    
    # After test
    if original_content is not None:
        assert real_config.read_text() == original_content, "Real config was modified!"
