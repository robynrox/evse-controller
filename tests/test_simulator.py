import pytest
import time
from evse_controller.drivers.evse.simulator.simulator_thread import SimulatedWallboxThread
from evse_controller.drivers.evse.async_interface import EvseState, EvseCommandData, EvseCommand

@pytest.fixture
def simulator():
    """Create a simulator instance for testing."""
    # Configure with fast simulation speed for quicker tests
    sim = SimulatedWallboxThread(
        initial_battery_level=50,
        battery_capacity_kwh=10,
        simulation_speed=3600,  # 1 hour per second
        heartbeat_interval=0.1  # Fast heartbeat for tests
    )
    sim.start()
    yield sim
    sim.stop()
    
    # Reset the singleton for other tests
    SimulatedWallboxThread._instance = None

def test_simulator_initialization(simulator):
    """Test that the simulator initializes correctly."""
    state = simulator.get_state()
    assert state.evse_state == EvseState.PAUSED
    assert state.current == 0
    assert state.battery_level == 50
    assert state.power_watts == 0.0

def test_simulator_charging(simulator):
    """Test that the simulator charges correctly."""
    # Set charging current to 16A
    command = EvseCommandData(command=EvseCommand.SET_CURRENT, value=16)
    simulator.send_command(command)
    
    # Wait for the command to be processed
    time.sleep(0.2)
    
    # Check initial state
    state = simulator.get_state()
    assert state.evse_state == EvseState.CHARGING
    assert state.current == 16
    assert state.power_watts > 0
    
    # Wait for battery level to increase (1 second = 1 hour at 3600x speed)
    initial_level = state.battery_level
    time.sleep(1)
    
    # Check that battery level increased
    state = simulator.get_state()
    assert state.battery_level > initial_level

def test_simulator_discharging(simulator):
    """Test that the simulator discharges correctly."""
    # Set discharging current to -10A
    command = EvseCommandData(command=EvseCommand.SET_CURRENT, value=-10)
    simulator.send_command(command)
    
    # Wait for the command to be processed
    time.sleep(0.2)
    
    # Check initial state
    state = simulator.get_state()
    assert state.evse_state == EvseState.DISCHARGING
    assert state.current == -10
    assert state.power_watts > 0
    
    # Wait for battery level to decrease (1 second = 1 hour at 3600x speed)
    initial_level = state.battery_level
    time.sleep(1)
    
    # Check that battery level decreased
    state = simulator.get_state()
    assert state.battery_level < initial_level

def test_simulator_full_battery(simulator):
    """Test that the simulator stops charging when battery is full."""
    # Set initial battery level close to full
    simulator.state.battery_level = 96
    
    # Set charging current
    command = EvseCommandData(command=EvseCommand.SET_CURRENT, value=16)
    simulator.send_command(command)
    
    # Wait for battery to charge to full
    time.sleep(1)
    
    # Check that charging stopped
    state = simulator.get_state()
    assert state.evse_state == EvseState.PAUSED
    assert state.battery_level >= 97  # Full threshold is 97%

def test_simulator_empty_battery(simulator):
    """Test that the simulator stops discharging when battery is empty."""
    # Set initial battery level close to empty
    simulator.state.battery_level = 6
    
    # Set discharging current
    command = EvseCommandData(command=EvseCommand.SET_CURRENT, value=-10)
    simulator.send_command(command)
    
    # Wait for battery to discharge to empty
    time.sleep(1)
    
    # Check that discharging stopped
    state = simulator.get_state()
    assert state.evse_state == EvseState.PAUSED
    assert state.battery_level <= 5  # Empty threshold is 5%
