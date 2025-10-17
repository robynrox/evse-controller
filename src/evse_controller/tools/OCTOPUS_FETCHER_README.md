# Octopus Intelligent Tariff Information Fetcher

This script allows you to fetch information from the Octopus Intelligent tariff system using the GraphQL API.

## Prerequisites

You need:
- An Octopus Energy API key (obtainable from your account dashboard at https://octopus.energy/dashboard/developer/)
- Your Octopus Energy account ID (the string starting with `A-` displayed near the top of your Octopus account page)

## Dependencies

The script requires the following Python packages:
- `gql[aiohttp]` - For GraphQL client functionality
- `aiohttp` - For asynchronous HTTP requests

## Installation

To install the required dependencies:
```bash
pip install gql[aiohttp]
```

Note: This might install a newer version of `aiohttp` which could conflict with the main project's dependencies.

## Usage

### Command Line Arguments

```bash
python octopus_intelligent_fetcher.py --api-key <your_api_key> --account-id <your_account_id>
```

Optional arguments:
- `--device-only`: Fetch only device information
- `--dispatches-only`: Fetch only planned dispatches
- `--help`: Show help message

### Environment Variables

You can also provide credentials via environment variables:

```bash
export OCTOPUS_API_KEY="your_api_key"
export OCTOPUS_ACCOUNT_ID="your_account_id"
python octopus_intelligent_fetcher.py
```

### Examples

1. Fetch all combined data (default):
   ```bash
   python octopus_intelligent_fetcher.py --api-key sk_live_blah --account-id A-blah
   ```

2. Fetch only device information:
   ```bash
   python octopus_intelligent_fetcher.py --api-key sk_live_blah --account-id A-blah --device-only
   ```

3. Fetch only planned dispatches:
   ```bash
   python octopus_intelligent_fetcher.py --api-key sk_live_blah --account-id A-blah --dispatches-only
   ```

## Output

The script outputs JSON data containing the requested information:
- Vehicle charging preferences (target times and SOC)
- Device information (vehicle and charger details)
- Planned dispatches (scheduled charging times)
- Completed dispatches (recent charging sessions)

## Planned Dispatches Format

The output includes planned dispatches in the following format:
- `startDtUtc`: UTC start time of the charging slot
- `endDtUtc`: UTC end time of the charging slot  
- `chargeKwh`: Expected energy (kWh) to be charged during this slot (negative values indicate energy consumption)
- `meta.source`: Type of charge (`smart-charge` for automatic intelligent charging, `bump-charge` for manual boost charge)
- `meta.location`: Location information

This information can be used to automatically switch between OCPP mode and Intelligent Octopus mode based on planned charging slots.