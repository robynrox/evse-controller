"""
Asynchronous OCPP Manager for handling OCPP state discovery and management
with rate limiting and retry logic.
"""
import threading
import queue
import time
import random
import traceback
from enum import Enum
from typing import Dict, Any, Optional

from evse_controller.drivers.evse.event_bus import EventBus, EventType
from evse_controller.utils.config import config
from evse_controller.drivers.evse.wallbox.wallbox_api_with_ocpp import WallboxAPIWithOCPP
from evse_controller.utils.logging_config import debug, info, warning, error


class OCPPCommand(Enum):
    """Commands that can be sent to the OCPP manager"""
    GET_STATE = "get_state"
    SET_ENABLED = "set_enabled"
    SET_DISABLED = "set_disabled"


class OCPPManager:
    """
    Asynchronous manager for OCPP state discovery and management.
    Handles rate limiting, retries with exponential backoff, and uses event bus
    for state change notifications.
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized'):
            return
            
        self.state = None  # Current known OCPP state (True=enabled, False=disabled, None=unknown)
        self.event_bus = EventBus()
        self.request_queue = queue.Queue()
        self.retry_queue = queue.PriorityQueue()  # (retry_time, job)
        self._stop_event = threading.Event()
        self._worker_thread = None
        self._retry_thread = None
        self._api_client = None
        self._initialized = True
        
        # Configuration for retries - more reasonable values
        self.max_retries = 10  # More retries for persistent rate limiting
        self.base_delay = 5  # seconds
        self.max_delay = 60  # 1 minute maximum backoff to reduce wait times

    def start(self):
        """Start the OCPP manager worker threads"""
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._worker_thread = threading.Thread(target=self._process_requests, daemon=True)
            self._worker_thread.start()
        
        if self._retry_thread is None or not self._retry_thread.is_alive():
            self._retry_thread = threading.Thread(target=self._process_retries, daemon=True)
            self._retry_thread.start()

    def stop(self):
        """Stop the OCPP manager worker threads"""
        self._stop_event.set()
        # Add sentinel values to wake up threads
        self.request_queue.put(None)
        
    def get_state(self) -> Optional[bool]:
        """Get the current known OCPP state"""
        return self.state

    def request_state_discovery(self):
        """Asynchronously request current OCPP state"""
        job = {
            'command': OCPPCommand.GET_STATE,
            'timestamp': time.time(),
            'attempts': 0
        }
        self.request_queue.put(job)

    def set_state(self, target_state: bool):
        """Request to set OCPP to a specific state"""
        command = OCPPCommand.SET_ENABLED if target_state else OCPPCommand.SET_DISABLED
        job = {
            'command': command,
            'target_state': target_state,
            'timestamp': time.time(),
            'attempts': 0
        }
        self.request_queue.put(job)

    def _has_credentials(self) -> bool:
        """Check if required credentials are available"""
        return all([
            config.WALLBOX_USERNAME,
            config.WALLBOX_PASSWORD,
            config.WALLBOX_SERIAL
        ])

    def _get_api_client(self):
        """Get or create API client with credentials"""
        if not self._has_credentials():
            return None
            
        if self._api_client is None:
            try:
                self._api_client = WallboxAPIWithOCPP(
                    config.WALLBOX_USERNAME,
                    config.WALLBOX_PASSWORD
                )
            except Exception as e:
                error(f"OCPP Manager: Failed to create API client: {e}")
                return None
                
        return self._api_client

    def _execute_request(self, job: Dict[str, Any]):
        """Execute a single OCPP request"""
        try:
            debug(f"OCPP Manager: Starting execution of job: {job['command'].value}")
            api_client = self._get_api_client()
            if not api_client:
                debug("OCPP Manager: No API client available, assuming OCPP is off")
                # If no credentials, assume OCPP is off
                return {'status_code': None, 'successful': True, 'ocpp_enabled': False}
                
            debug(f"OCPP Manager: Executing request: {job['command'].value}")
                
            if job['command'] == OCPPCommand.GET_STATE:
                debug(f"OCPP Manager: Calling is_ocpp_enabled for serial {config.WALLBOX_SERIAL}")
                ocpp_enabled = api_client.is_ocpp_enabled(config.WALLBOX_SERIAL)
                debug(f"OCPP Manager: is_ocpp_enabled returned: {ocpp_enabled}")
                return {'status_code': 200, 'successful': True, 'ocpp_enabled': ocpp_enabled}
                
            elif job['command'] == OCPPCommand.SET_ENABLED:
                debug(f"OCPP Manager: Calling enable_ocpp for serial {config.WALLBOX_SERIAL}")
                try:
                    result = api_client.enable_ocpp(config.WALLBOX_SERIAL)
                    debug(f"OCPP Manager: enable_ocpp returned: {result}")
                    debug(f"OCPP Manager: enable_ocpp result type: {type(result)}")
                    # Check if result indicates success
                    if isinstance(result, dict):
                        # Look for success indicators in the response
                        success_indicators = [
                            'type' in result and result['type'] == 'ocpp',
                            'success' in result and result['success'] is True,
                            'status' in result and result['status'] == 'ok'
                        ]
                        is_success = any(success_indicators) or len(result) > 0  # Non-empty dict usually means success
                        debug(f"OCPP Manager: enable_ocpp interpreted as success: {is_success}")
                    else:
                        is_success = result is not None
                        debug(f"OCPP Manager: enable_ocpp non-dict result interpreted as success: {is_success}")
                    return {'status_code': 200, 'successful': True, 'ocpp_enabled': True, 'raw_result': result}
                except Exception as enable_err:
                    debug(f"OCPP Manager: Exception in enable_ocpp: {enable_err}")
                    debug(f"OCPP Manager: Exception type: {type(enable_err).__name__}")
                    raise enable_err  # Re-raise to be caught by outer except
                
            elif job['command'] == OCPPCommand.SET_DISABLED:
                debug(f"OCPP Manager: Calling disable_ocpp for serial {config.WALLBOX_SERIAL}")
                try:
                    result = api_client.disable_ocpp(config.WALLBOX_SERIAL)
                    debug(f"OCPP Manager: disable_ocpp returned: {result}")
                    debug(f"OCPP Manager: disable_ocpp result type: {type(result)}")
                    # Check if result indicates success
                    if isinstance(result, dict):
                        # Look for success indicators in the response
                        success_indicators = [
                            'type' in result and result['type'] == 'wallbox',
                            'success' in result and result['success'] is True,
                            'status' in result and result['status'] == 'ok'
                        ]
                        is_success = any(success_indicators) or len(result) > 0  # Non-empty dict usually means success
                        debug(f"OCPP Manager: disable_ocpp interpreted as success: {is_success}")
                    else:
                        is_success = result is not None
                        debug(f"OCPP Manager: disable_ocpp non-dict result interpreted as success: {is_success}")
                    return {'status_code': 200, 'successful': True, 'ocpp_enabled': False, 'raw_result': result}
                except Exception as disable_err:
                    debug(f"OCPP Manager: Exception in disable_ocpp: {disable_err}")
                    debug(f"OCPP Manager: Exception type: {type(disable_err).__name__}")
                    raise disable_err  # Re-raise to be caught by outer except
                
        except Exception as e:
            error_str = str(e).lower()
            debug(f"OCPP Manager: Exception during request execution: {e}")
            debug(f"OCPP Manager: Exception type: {type(e).__name__}")
            debug(f"OCPP Manager: Exception traceback: {traceback.format_exc()}")
            # Check if it's a rate limit error (429)
            if "429" in error_str or "rate" in error_str or "limit" in error_str:
                debug(f"OCPP Manager: Detected rate limit error: {e}")
                return {'status_code': 429, 'successful': False, 'error': str(e)}
            else:
                debug(f"OCPP Manager: Other error during request: {e}")
                return {'status_code': None, 'successful': False, 'error': str(e)}

    def _calculate_backoff_delay(self, attempt_count: int) -> float:
        """Calculate delay with exponential backoff and jitter"""
        # Exponential backoff: base_delay * (2^(attempt-1))
        delay = min(self.base_delay * (2 ** (attempt_count-1)), self.max_delay)
        # Add jitter to smooth out thundering herd
        jitter = random.uniform(0.8, 1.2)
        # Ensure the result doesn't exceed the max_delay even with jitter
        return min(delay * jitter, self.max_delay)

    def _process_requests(self):
        """Main processing loop for handling OCPP requests"""
        while not self._stop_event.is_set():
            try:
                # Get job from queue with timeout to allow checking stop_event
                try:
                    job = self.request_queue.get(timeout=0.5)  # Shorter timeout
                except queue.Empty:
                    # Sleep briefly when queue is empty to reduce CPU usage
                    if not self._stop_event.is_set():
                        time.sleep(0.1)  # Small sleep to yield CPU
                    continue  # Continue loop to check for stop_event or new jobs
                    
                # Check if it's a sentinel value to stop
                if job is None:
                    break
                    
                result = self._execute_request(job)
                
                if result['successful']:
                    # Handle successful response
                    self._handle_successful_response(job, result)
                else:
                    # Handle failure with retry logic
                    job['attempts'] += 1
                    if job['attempts'] < self.max_retries:
                        delay = self._calculate_backoff_delay(job['attempts'])
                        retry_time = time.time() + delay
                        # Add to retry queue with priority based on retry time
                        self.retry_queue.put((retry_time, job))
                        info(f"OCPP Manager: Request failed, scheduled retry in {delay:.1f}s (attempt {job['attempts']}/{self.max_retries})")
                    else:
                        # Max retries reached - log error and publish current state if available
                        error(f"OCPP Manager: Request failed after {self.max_retries} attempts: {result.get('error', 'Unknown error')}")
                        self._handle_persistent_error(job)
                        
            except Exception as e:
                error(f"OCPP Manager: Error processing request: {e}")
                # Sleep longer on unexpected errors
                if not self._stop_event.is_set():
                    time.sleep(1)

    def _process_retries(self):
        """Process retry queue"""
        while not self._stop_event.is_set():
            try:
                if not self.retry_queue.empty():
                    retry_time, job = self.retry_queue.queue[0]
                    if time.time() >= retry_time:
                        # Pop the job from the queue and put it back to the main queue
                        self.retry_queue.get()  # Remove the job
                        self.request_queue.put(job)
                    else:
                        # Sleep longer when no retries are pending to reduce CPU usage
                        time.sleep(0.5)  # 500ms sleep when nothing to do
                else:
                    # Sleep longer when queue is empty to reduce CPU usage
                    time.sleep(1.0)  # 1 second sleep when queue is empty
            except Exception as e:
                error(f"OCPP Manager: Error in retry processing: {e}")
                time.sleep(2)  # Longer sleep on error

    def _handle_successful_response(self, job: Dict[str, Any], result: Dict[str, Any]):
        """Handle successful API response"""
        if job['command'] == OCPPCommand.GET_STATE:
            new_state = result.get('ocpp_enabled', False)
            old_state = self.state
            self.state = new_state
            
            # Publish appropriate state event
            event_type = EventType.OCPP_ENABLED if new_state else EventType.OCPP_DISABLED
            self.event_bus.publish(event_type, time.time())
            debug(f"OCPP Manager: State discovery - OCPP is {'enabled' if new_state else 'disabled'}")

        elif job['command'] in [OCPPCommand.SET_ENABLED, OCPPCommand.SET_DISABLED]:
            target_state = job.get('target_state', self.state)
            old_state = self.state
            self.state = target_state
            
            # Publish state change event
            event_type = EventType.OCPP_ENABLED if target_state else EventType.OCPP_DISABLED
            self.event_bus.publish(event_type, time.time())
            info(f"OCPP Manager: State changed - OCPP is now {'enabled' if target_state else 'disabled'}")

    def _handle_persistent_error(self, job: Dict[str, Any]):
        """Handle error when max retries are exceeded"""
        # If we can't determine the state, assume it's unchanged but log the issue
        if job['command'] == OCPPCommand.GET_STATE:
            warning(f"OCPP Manager: Could not determine OCPP state after retries, keeping current state: {self.state}")
        else:
            # For enable/disable commands, we can't confirm the state change
            event_type = EventType.OCPP_ENABLED if self.state else EventType.OCPP_DISABLED
            self.event_bus.publish(event_type, time.time())
            warning(f"OCPP Manager: Could not {job['command'].value.replace('set_', '')} OCPP after retries")
            
    def initialize(self):
        """Initialize the OCPP manager and start threads"""
        self.start()
        # If we have credentials, request initial state discovery
        if self._has_credentials():
            self.request_state_discovery()
        else:
            # If no credentials, assume OCPP is off
            self.state = False
            self.event_bus.publish(EventType.OCPP_DISABLED, time.time())
            warning("OCPP Manager: Missing credentials, assuming OCPP is disabled")


# Global instance for easy access
ocpp_manager = OCPPManager()