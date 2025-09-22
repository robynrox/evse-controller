"""
Test file for wallbox_api_with_ocpp.py
"""
import sys
import os

# Add the project root to the path so we can import our modules
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from src.evse_controller.drivers.evse.wallbox.wallbox_api_with_ocpp import WallboxAPIWithOCPP

# Example usage:
# wallbox_client = WallboxAPIWithOCPP("username", "password")
# response = wallbox_client.get_ocpp_status("12345")
# response = wallbox_client.enable_ocpp("12345")
# response = wallbox_client.disable_ocpp("12345")
# print(response)

print("WallboxAPIWithOCPP class imported successfully!")
print("Available methods:")
print("- get_ocpp_status(charger_id)")
print("- enable_ocpp(charger_id)")
print("- disable_ocpp(charger_id)")