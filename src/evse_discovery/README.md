# EVSE Device Discovery

A proof of concept tool for discovering Wallbox and Shelly devices on your local network.

## Features

- Automatic discovery of Wallbox Quasar devices
- Automatic discovery of Shelly EM devices
- Multiple discovery methods (mDNS, network scanning)
- Provides both IP addresses and hostnames for stable DHCP configurations
- Typically completes scanning in about 1 minute

## Installation

```bash
# Clone the repository
git clone https://github.com/robynrox/evse-discovery.git
cd evse-discovery

# Install dependencies using Poetry
poetry install

# Or using pip
pip install -r requirements.txt
```

## Usage

```bash
# Start the discovery process
python -m evse_discovery
```

The tool will scan your local network and provide a summary of discovered devices. Use the hostnames from the summary in your configuration files when possible, as they're more stable than IP addresses in DHCP environments.

## Network Compatibility

This tool has been tested on typical home networks (192.168.1.x). If you encounter issues with different network configurations, please open an issue on GitHub with details about your network setup.

## Quick Start

On Linux/MacOS:
```bash
./discover.sh
```

On Windows (PowerShell):
```powershell
.\discover.ps1
```

For manual installation and usage, see below.
