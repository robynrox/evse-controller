import os
from pathlib import Path
import pytest
from evse_controller.utils.paths import get_data_dir, get_log_dir, ensure_data_dirs

@pytest.fixture
def temp_dir(tmp_path):
    """Provide a temporary directory and set EVSE_DATA_DIR to point to it."""
    old_env = os.environ.get('EVSE_DATA_DIR')
    os.environ['EVSE_DATA_DIR'] = str(tmp_path)
    yield tmp_path
    if old_env is not None:
        os.environ['EVSE_DATA_DIR'] = old_env
    else:
        del os.environ['EVSE_DATA_DIR']

@pytest.fixture
def clear_env():
    """Remove EVSE_DATA_DIR from environment if it exists."""
    old_env = os.environ.get('EVSE_DATA_DIR')
    if 'EVSE_DATA_DIR' in os.environ:
        del os.environ['EVSE_DATA_DIR']
    yield
    if old_env is not None:
        os.environ['EVSE_DATA_DIR'] = old_env

def test_get_data_dir_with_env_var(temp_dir):
    """Test that get_data_dir respects EVSE_DATA_DIR environment variable."""
    result = get_data_dir()
    assert result == temp_dir
    assert isinstance(result, Path)

def test_get_data_dir_default(clear_env):
    """Test that get_data_dir defaults to project_root/data when no env var is set."""
    result = get_data_dir()
    assert result.name == "data"
    assert result.parent == Path(__file__).parent.parent
    assert isinstance(result, Path)

def test_get_log_dir(temp_dir):
    """Test that get_log_dir returns the correct logs subdirectory."""
    log_dir = get_log_dir()
    assert log_dir == temp_dir / "logs"
    assert isinstance(log_dir, Path)

def test_ensure_data_dirs_creates_directories(temp_dir):
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

def test_ensure_data_dirs_handles_existing_directories(temp_dir):
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
