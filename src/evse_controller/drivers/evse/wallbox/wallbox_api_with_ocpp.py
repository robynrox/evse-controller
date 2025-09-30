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
        # Ensure we have a valid authentication token
        self.authenticate()
        
        try:
            response = requests.get(
                f"{self.baseUrl}v3/chargers/{charger_id}/ocpp-configuration",
                headers=self.headers,
                timeout=self._requestGetTimeout
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as err:
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
        # Get current OCPP status to retrieve the required parameters
        current_status = self.get_ocpp_status(charger_id)
        
        # Use the current configuration but change the type to "ocpp" to enable
        return self._send_ocpp_configuration(
            charger_id, 
            current_status.get("address"), 
            current_status.get("chargePointIdentity"), 
            current_status.get("password"), 
            "ocpp"
        )
    
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
        # Get current OCPP status to retrieve the required parameters
        current_status = self.get_ocpp_status(charger_id)
        
        # Use the current configuration but change the type to "wallbox" to disable
        return self._send_ocpp_configuration(
            charger_id, 
            current_status.get("address"), 
            current_status.get("chargePointIdentity"), 
            current_status.get("password"), 
            "wallbox"
        )
    
    