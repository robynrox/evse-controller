"""
Extended Wallbox class with OCPP endpoint support
"""
import json
import requests
from wallbox import Wallbox


class WallboxAPIWithOCPP(Wallbox):
    """Extended Wallbox class that includes support for OCPP endpoints"""
    
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
    
    def _clear_cache(self, charger_id=None):
        """Clear the OCPP status cache, either for a specific charger or all chargers."""
        if charger_id is None:
            # Clear all cache
            self._ocpp_status_cache.clear()
            self._ocpp_status_cache_timestamp.clear()
        else:
            # Clear cache for specific charger
            cache_key = self._get_cache_key(charger_id)
            if cache_key in self._ocpp_status_cache:
                del self._ocpp_status_cache[cache_key]
            if cache_key in self._ocpp_status_cache_timestamp:
                del self._ocpp_status_cache_timestamp[cache_key]
    
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
            return self._ocpp_status_cache[cache_key]
        
        # Ensure we have a valid authentication token
        self.authenticate()
        
        try:
            response = requests.get(
                f"{self.baseUrl}v3/chargers/{charger_id}/ocpp-configuration",
                headers=self.headers,
                timeout=self._requestGetTimeout
            )
            response.raise_for_status()
            result = response.json()
            
            # Cache the result
            import time
            self._ocpp_status_cache[cache_key] = result
            self._ocpp_status_cache_timestamp[cache_key] = time.time()
            
            return result
        except requests.exceptions.HTTPError as err:
            # Clear the cache if request fails to avoid using stale data
            self._clear_cache(charger_id)
            raise err
    
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
        status = self.get_ocpp_status(charger_id)
        return status.get("type") == "ocpp"
    
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
            
        try:
            response = requests.post(
                f"{self.baseUrl}v3/chargers/{charger_id}/ocpp-configuration",
                headers=self.headers,
                json=data,
                timeout=self._requestGetTimeout
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as err:
            raise err
    
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
        # Get current OCPP status
        current_status = self.get_ocpp_status(charger_id)
        
        # Check if OCPP is already enabled
        if current_status.get("type") == "ocpp":
            # OCPP is already enabled, no need to make API call
            return {"message": "OCPP was already enabled, no action needed", "status": "unchanged"}
        
        # Use the current configuration but change the type to "ocpp" to enable
        result = self._send_ocpp_configuration(
            charger_id, 
            current_status.get("address"), 
            current_status.get("chargePointIdentity"), 
            current_status.get("password"), 
            "ocpp"
        )
        
        # Clear the cache after successful update
        self._clear_cache(charger_id)
        
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
        # Get current OCPP status
        current_status = self.get_ocpp_status(charger_id)
        
        # Check if OCPP is already disabled
        if current_status.get("type") != "ocpp":
            # OCPP is already disabled, no need to make API call
            return {"message": "OCPP was already disabled, no action needed", "status": "unchanged"}
        
        # Use the current configuration but change the type to "wallbox" to disable
        result = self._send_ocpp_configuration(
            charger_id, 
            current_status.get("address"), 
            current_status.get("chargePointIdentity"), 
            current_status.get("password"), 
            "wallbox"
        )
        
        # Clear the cache after successful update
        self._clear_cache(charger_id)
        
        return result
    
    