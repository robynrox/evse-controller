"""Command-line interface for EVSE device discovery."""
import asyncio
import logging
import sys
from typing import List
import json
import socket
from .discovery import DeviceDiscovery, DeviceInfo

def setup_logging():
    """Configure logging for the application."""
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    logging.getLogger("pymodbus").setLevel(logging.WARNING)

def get_hostname(ip: str) -> str:
    """Get hostname for IP address with timeout."""
    try:
        return socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, socket.timeout):
        return "Unknown"

async def main():
    """Main entry point for the discovery tool."""
    setup_logging()
    logger = logging.getLogger(__name__)
    
    logger.info("Starting EVSE device discovery...")
    
    discovery = DeviceDiscovery()
    try:
        devices = await discovery.discover_devices()
        
        if not devices:
            logger.info("No devices found.")
            return
        
        # Print detailed results
        print("\nDiscovered Devices:")
        print("-" * 50)
        for device in devices:
            print(f"\nDevice: {device.name}")
            print(f"Type: {device.device_type}")
            print(f"IP: {device.ip}")
            print(f"Verified: {device.verified}")
            if device.details:
                print("Details:")
                print(json.dumps(device.details, indent=2))
            print("-" * 50)
        
        # Print concise summary with hostnames
        print("\nSummary:")
        print("-" * 50)
        print(f"{'Type':<15} {'IP Address':<15} {'Hostname':<30}")
        print("-" * 50)
        for device in devices:
            hostname = get_hostname(device.ip)
            print(f"{device.device_type:<15} {device.ip:<15} {hostname:<30}")
        print("-" * 50)
        
        # Print hostname usage tip
        print("\nTip: For DHCP networks, consider using hostnames instead of IP addresses")
        print("     in your configuration to avoid issues with IP address changes.")
        
    except KeyboardInterrupt:
        logger.info("Discovery interrupted by user.")
    except Exception as e:
        logger.error(f"Error during discovery: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(main())
