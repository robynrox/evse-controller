"""Device discovery implementation for EVSE devices."""
from typing import Dict, List, Optional
import asyncio
import logging
from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf, ServiceStateChange
import netifaces
import ipaddress
import requests
from pymodbus.client import ModbusTcpClient
import aiohttp

logger = logging.getLogger(__name__)

class DeviceInfo:
    """Information about a discovered device."""
    def __init__(self, ip: str, device_type: str, name: str):
        self.ip = ip
        self.device_type = device_type
        self.name = name
        self.verified = False
        self.details: Dict[str, any] = {}

class DeviceDiscovery:
    """Main discovery class for EVSE devices."""
    def __init__(self):
        self.devices: Dict[str, DeviceInfo] = {}
        self._zeroconf = Zeroconf()
    
    async def discover_devices(self) -> List[DeviceInfo]:
        """Run all discovery methods and return found devices."""
        # Run different discovery methods concurrently
        await asyncio.gather(
            self._discover_mdns(),
            self._scan_network()
        )
        return list(self.devices.values())

    async def _discover_mdns(self):
        """Discover devices using mDNS."""
        # Create a ServiceBrowser for Shelly devices
        browser = ServiceBrowser(
            self._zeroconf,
            "_shelly._tcp.local.",
            handlers=[self._handle_shelly_mdns]
        )
        
        # Wait a bit for responses
        await asyncio.sleep(3)
        
        # Clean up
        self._zeroconf.close()

    def _handle_shelly_mdns(self, zeroconf, service_type, name, state_change):
        """Handle mDNS discovery of Shelly device."""
        if state_change == ServiceStateChange.Added:
            info = zeroconf.get_service_info(service_type, name)
            if info:
                ip = info.parsed_addresses()[0]
                device = DeviceInfo(ip, "Shelly EM", f"Shelly EM at {ip}")
                device.verified = True
                self.devices[ip] = device
                logger.info(f"Found Shelly device via mDNS at {ip}")

    async def _scan_network(self):
        """Scan local networks for devices."""
        networks = self._get_local_networks()
        
        for network in networks:
            logger.info(f"Scanning network: {network}")
            
            # Skip first address (network address) and last address (broadcast)
            # Also skip .1 which is typically the gateway
            hosts = [ip for ip in network.hosts() 
                    if ip.packed[-1] not in (0, 1, 255)]
            
            # Create tasks for all hosts
            tasks = []
            for ip in hosts:
                ip_str = str(ip)
                # Check for Shelly first (faster HTTP check)
                tasks.append(self._check_shelly(ip_str))
                # Only proceed with Modbus check if no Shelly was found at this IP
                if ip_str not in self.devices:
                    tasks.append(self._check_wallbox(ip_str))
            
            # Run scans in batches to avoid overwhelming the network
            batch_size = 50
            for i in range(0, len(tasks), batch_size):
                batch = tasks[i:i + batch_size]
                await asyncio.gather(*batch)
                # Small delay between batches
                await asyncio.sleep(0.1)

    def _get_local_networks(self) -> List[ipaddress.IPv4Network]:
        """Get list of local networks to scan."""
        networks = []
        for interface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(interface)
            if netifaces.AF_INET in addrs:
                for addr in addrs[netifaces.AF_INET]:
                    ip = addr['addr']
                    if ip.startswith('127.'):  # Skip localhost
                        continue
                    if 'netmask' in addr:
                        network = ipaddress.IPv4Network(
                            f"{ip}/{addr['netmask']}", 
                            strict=False
                        )
                        networks.append(network)
                        logger.info(f"Found network: {network} on interface {interface}")
        return networks

    async def _check_wallbox(self, ip: str):
        """Check if IP belongs to a Wallbox device."""
        # Common Wallbox registers to check
        registers = [
            (0x51, 1),  # Common status register
            (0x300, 1), # Another common register
        ]
        
        try:
            client = ModbusTcpClient(ip, port=502, timeout=1)
            
            for address, count in registers:
                logger.info(f"Trying register {hex(address)} at {ip}")
                try:
                    result = await asyncio.to_thread(
                        client.read_holding_registers,
                        address=address,
                        count=count
                    )
                    
                    if result and not result.isError():
                        logger.info(f"Success reading register {hex(address)} at {ip}: {result.registers}")
                        device = DeviceInfo(ip, "Wallbox", f"Wallbox at {ip}")
                        device.verified = True
                        device.details["modbus_port"] = 502
                        device.details["register"] = hex(address)
                        device.details["value"] = result.registers[0]
                        self.devices[ip] = device
                        client.close()
                        return
                        
                except Exception as e:
                    logger.info(f"Failed to connect for register {hex(address)}")
                    
            client.close()
            
        except Exception as e:
            logger.debug(f"Error checking Wallbox at {ip}: {str(e)}")

    async def _check_shelly(self, ip: str):
        """Check if IP belongs to a Shelly device."""
        url = f"http://{ip}/status"  # Define url at the start of the method
        try:
            logger.debug(f"Checking Shelly at {ip}")  # Log IP instead of url
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=2) as response:
                    if response.status == 200:
                        data = await response.json()
                        if "emeters" in data:
                            device = DeviceInfo(ip, "Shelly EM", f"Shelly EM at {ip}")
                            device.verified = True
                            device.details = data
                            self.devices[ip] = device
                            logger.info(f"Found Shelly EM device at {ip}")
        except aiohttp.ClientError as e:
            logger.debug(f"No Shelly device at {ip}: {str(e)}")
        except Exception as e:
            logger.debug(f"Error checking Shelly at {ip}: {str(e)}")

    async def close(self):
        """Clean up resources."""
        if self._zeroconf:
            self._zeroconf.close()
