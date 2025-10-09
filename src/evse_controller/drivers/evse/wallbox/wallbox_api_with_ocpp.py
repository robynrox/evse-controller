"""
Extended Wallbox class with OCPP endpoint support
"""
import json
import requests
import time
from wallbox import Wallbox
from evse_controller.utils.logging_config import debug, info, warning, error
from evse_controller.utils.redaction import redact_sensitive_data


class WallboxAPIWithOCPP(Wallbox):
    """Extended Wallbox class that includes support for OCPP endpoints.
    
    This class includes a 5-minute cache for OCPP status requests to prevent
    excessive API calls and potential rate-limiting issues.
    """
    
    def __init__(self, username, password, requestGetTimeout=None, jwtTokenDrift=0):
        """
        Initialize the extended Wallbox API client
        
        Args:
            username (str): Wallbox account username
            password (str): Wallbox account password
            requestGetTimeout (float, optional): Request timeout in seconds
            jwtTokenDrift (int): Token drift in seconds for refresh timing
        """
        super().__init__(username, password, requestGetTimeout, jwtTokenDrift)
        
        # Initialize cache for OCPP status with 5-minute timeout (300 seconds)
        self._ocpp_status_cache = {}
        self._ocpp_status_cache_timestamp = {}
        self._cache_timeout = 300  # 5 minutes in seconds
        
        # Initialize cache for OCPP configuration credentials (these rarely change)
        self._ocpp_config_cache = {}
    
    def _get_cache_key(self, charger_id):
        """Generate a cache key for the given charger_id."""
        return f"ocpp_status_{charger_id}"
    
    def _is_cache_valid(self, cache_key):
        """Check if the cached data is still valid (within timeout period)."""
        if cache_key not in self._ocpp_status_cache_timestamp:
            return False
        
        import time
        current_time = time.time()
        cached_time = self._ocpp_status_cache_timestamp[cache_key]
        
        return (current_time - cached_time) < self._cache_timeout
    
    def _clear_status_cache(self, charger_id=None):
        """Clear the OCPP status cache, either for a specific charger or all chargers.
        
        The config cache is not cleared as those parameters rarely change.
        """
        if charger_id is None:
            # Clear all status cache
            self._ocpp_status_cache.clear()
            self._ocpp_status_cache_timestamp.clear()
        else:
            # Clear status cache for specific charger
            cache_key = self._get_cache_key(charger_id)
            if cache_key in self._ocpp_status_cache:
                del self._ocpp_status_cache[cache_key]
            if cache_key in self._ocpp_status_cache_timestamp:
                del self._ocpp_status_cache_timestamp[cache_key]
    
    def _get_ocpp_config_params(self, charger_id):
        """Get OCPP configuration parameters with persistent caching.
        
        This method caches the OCPP configuration parameters (address, chargePointIdentity, password)
        which rarely change, reducing the need for status API calls.
        
        Args:
            charger_id (str): The ID of the charger
            
        Returns:
            dict: The configuration parameters
        """
        cache_key = self._get_cache_key(charger_id)
        
        # Check if we have cached config parameters
        if cache_key in self._ocpp_config_cache:
            return self._ocpp_config_cache[cache_key]
        
        # Get current OCPP status to retrieve the configuration parameters
        current_status = self.get_ocpp_status(charger_id)
        
        # Extract the configuration parameters that rarely change
        config_params = {
            "address": current_status.get("address"),
            "chargePointIdentity": current_status.get("chargePointIdentity"), 
            "password": current_status.get("password")
        }
        
        # Cache the configuration parameters persistently
        self._ocpp_config_cache[cache_key] = config_params
        
        return config_params
    
    def get_ocpp_status(self, charger_id):
        """
        Get the current OCPP status for a charger
        
        Args:
            charger_id (str): The ID of the charger
            
        Returns:
            dict: The current OCPP configuration including type, address, chargePointIdentity, password, etc.
            
        Raises:
            requests.exceptions.HTTPError: If the API request fails
        """
        cache_key = self._get_cache_key(charger_id)
        
        # Check if we have a valid cached response
        if self._is_cache_valid(cache_key):
            debug(f"OCPP API: Returning cached status for charger {charger_id}")
            return self._ocpp_status_cache[cache_key]
        
        # Ensure we have a valid authentication token
        debug(f"OCPP API: Authenticating for charger {charger_id}")
        self.authenticate()
        
        try:
            debug(f"OCPP API: Making request to get OCPP status for charger {charger_id}")
            response = requests.get(
                f"{self.baseUrl}v3/chargers/{charger_id}/ocpp-configuration",
                headers=self.headers,
                timeout=self._requestGetTimeout
            )
            debug(f"OCPP API: Response status {response.status_code} for charger {charger_id}")
            
            if response.status_code == 429:
                warning(f"OCPP API: Rate limited (429) when getting status for charger {charger_id}")
                raise requests.exceptions.HTTPError(f"Rate limited: {response.status_code}")
            
            response.raise_for_status()
            result = response.json()
            debug(f"OCPP API: Successfully retrieved status for charger {charger_id}: type={result.get('type')}")
            # Log the full result with sensitive data redacted
            debug(f"OCPP API: Full status result (redacted): {redact_sensitive_data(result)}")
            
            # Cache the result
            self._ocpp_status_cache[cache_key] = result
            self._ocpp_status_cache_timestamp[cache_key] = time.time()
            
            return result
        except requests.exceptions.HTTPError as err:
            # Clear the cache if request fails to avoid using stale data
            error(f"OCPP API: HTTP error {err} when getting status for charger {charger_id}")
            self._clear_status_cache(charger_id)
            raise err
        except Exception as e:
            error(f"OCPP API: Unexpected error {e} when getting status for charger {charger_id}")
            self._clear_status_cache(charger_id)
            raise e
    
    def is_ocpp_enabled(self, charger_id):
        """
        Check if OCPP is currently enabled for a charger
        
        Args:
            charger_id (str): The ID of the charger
            
        Returns:
            bool: True if OCPP is enabled, False otherwise
            
        Raises:
            requests.exceptions.HTTPError: If the API request fails
        """
        debug(f"OCPP API: Checking if OCPP is enabled for charger {charger_id}")
        status = self.get_ocpp_status(charger_id)
        is_enabled = status.get("type") == "ocpp"
        debug(f"OCPP API: OCPP is {'enabled' if is_enabled else 'disabled'} for charger {charger_id}")
        return is_enabled
    
    def _send_ocpp_configuration(self, charger_id, address=None, charge_point_identity=None, password=None, ocpp_type="ocpp"):
        """
        Internal method to send OCPP configuration to a charger
        
        Args:
            charger_id (str): The ID of the charger
            address (str, optional): The OCPP server address
            charge_point_identity (str, optional): The charge point identity
            password (str, optional): The OCPP password
            ocpp_type (str): The OCPP type ("ocpp" to enable, "wallbox" to disable)
            
        Returns:
            dict: The response from the API
            
        Raises:
            requests.exceptions.HTTPError: If the API request fails
        """
        # Ensure we have a valid authentication token
        debug(f"OCPP API: Authenticating for OCPP configuration update for charger {charger_id}")
        self.authenticate()
        
        # Prepare the request data
        data = {
            "type": ocpp_type
        }
        
        # Only include the other parameters if they are provided (for both enable and disable)
        if address is not None:
            data["address"] = address
        if charge_point_identity is not None:
            data["chargePointIdentity"] = charge_point_identity
        if password is not None:
            data["password"] = password
            
        debug(f"OCPP API: Sending OCPP configuration for charger {charger_id}, type={ocpp_type}")
        try:
            response = requests.post(
                f"{self.baseUrl}v3/chargers/{charger_id}/ocpp-configuration",
                headers=self.headers,
                json=data,
                timeout=self._requestGetTimeout
            )
            debug(f"OCPP API: Configuration update response status {response.status_code} for charger {charger_id}")
            
            if response.status_code == 429:
                warning(f"OCPP API: Rate limited (429) when updating configuration for charger {charger_id}")
                raise requests.exceptions.HTTPError(f"Rate limited: {response.status_code}")
            
            response.raise_for_status()
            result = response.json()
            debug(f"OCPP API: Successfully updated OCPP configuration for charger {charger_id}")
            # Log the full result with sensitive data redacted
            debug(f"OCPP API: Configuration update result (redacted): {redact_sensitive_data(result)}")
            return result
        except requests.exceptions.HTTPError as err:
            error(f"OCPP API: HTTP error {err} when updating configuration for charger {charger_id}")
            raise err
        except Exception as e:
            error(f"OCPP API: Unexpected error {e} when updating configuration for charger {charger_id}")
            raise e
    
    def enable_ocpp(self, charger_id):
        """
        Enable OCPP mode for a charger
        
        Args:
            charger_id (str): The ID of the charger
            
        Returns:
            dict: The response from the API
            
        Raises:
            requests.exceptions.HTTPError: If the API request fails
        """
        info(f"OCPP API: Enabling OCPP mode for charger {charger_id}")
        # Get the OCPP configuration parameters (credentials that rarely change)
        config_params = self._get_ocpp_config_params(charger_id)
        
        # Update the type to enable OCPP
        result = self._send_ocpp_configuration(
            charger_id, 
            config_params.get("address"), 
            config_params.get("chargePointIdentity"), 
            config_params.get("password"), 
            "ocpp"
        )
        
        # Clear the status cache after successful update (config cache is kept)
        self._clear_status_cache(charger_id)
        info(f"OCPP API: Successfully enabled OCPP mode for charger {charger_id}")
        
        return result
    
    def disable_ocpp(self, charger_id):
        """
        Disable OCPP mode for a charger
        
        Args:
            charger_id (str): The ID of the charger
            
        Returns:
            dict: The response from the API
            
        Raises:
            requests.exceptions.HTTPError: If the API request fails
        """
        info(f"OCPP API: Disabling OCPP mode for charger {charger_id}")
        # Get the OCPP configuration parameters (credentials that rarely change)
        config_params = self._get_ocpp_config_params(charger_id)
        
        # Update the type to disable OCPP
        result = self._send_ocpp_configuration(
            charger_id, 
            config_params.get("address"), 
            config_params.get("chargePointIdentity"), 
            config_params.get("password"), 
            "wallbox"
        )
        
        # Clear the status cache after successful update (config cache is kept)
        self._clear_status_cache(charger_id)
        info(f"OCPP API: Successfully disabled OCPP mode for charger {charger_id}")
        
        return result

