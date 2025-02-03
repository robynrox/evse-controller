import requests
import json
from os import environ
from datetime import datetime, timezone

class OctopusAPI:
    def __init__(self, api_key, account_number):
        self.api_key = api_key
        self.account_number = account_number
        self.base_url = "https://api.octopus.energy/v1/"

    def get_current_tariff(self):
        try:
            response = requests.get(
                f"{self.base_url}accounts/{self.account_number}/",
                auth=(self.api_key, ''),
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error fetching tariff data: {e}")
            return None

def get_active_tariffs(data, target_date=None):
    if target_date is None:
        target_date = datetime.now(timezone.utc)
    
    active_tariffs = []
    data = data['properties'][0]
    
    # Iterate through electricity meter points
    for electric in data['electricity_meter_points']:
        for agreement in electric['agreements']:
            tariff_code = agreement.get('tariff_code', '')  # Get the entire tariff code string
            
            valid_from = datetime.fromisoformat(agreement['valid_from'])
            valid_to = datetime.fromisoformat(agreement['valid_to']) if agreement.get('valid_to') else None
            
            if valid_to is not None:
                if target_date >= valid_from and target_date <= valid_to:
                    active_tariffs.append(tariff_code)
            else:
                if target_date >= valid_from:
                    active_tariffs.append(tariff_code)
    
    # Iterate through gas meter points
    for gas in data['gas_meter_points']:
        for agreement in gas['agreements']:
            tariff_code = agreement.get('tariff_code', '')  # Get the entire tariff code string
            
            valid_from = datetime.fromisoformat(agreement['valid_from'])
            valid_to = datetime.fromisoformat(agreement.get('valid_to')) if agreement.get('valid_to') else None
            
            if valid_to is not None:
                if target_date >= valid_from and target_date <= valid_to:
                    active_tariffs.append(tariff_code)
            else:
                if target_date >= valid_from:
                    active_tariffs.append(tariff_code)
    
    return active_tariffs

def test():
    # Retrieve API key and account number from environment variables
    api_key = environ.get('OCTOPUS_API_KEY')
    account_number = environ.get('OCTOPUS_ACCOUNT_NUMBER')

    if not api_key or not account_number:
        print("Error: Missing Octopus credentials")
        return

    octopus = OctopusAPI(api_key, account_number)
    tariff_data = octopus.get_current_tariff()
    
    if tariff_data:
        # Pretty-print the JSON data
        pretty_json = json.dumps(tariff_data, indent=4, sort_keys=True)
        print(pretty_json)

        # Print a list of currently active tariffs
        print(get_active_tariffs(tariff_data))
    else:
        print("Failed to fetch tariff data")

if __name__ == "__main__":
    test()
