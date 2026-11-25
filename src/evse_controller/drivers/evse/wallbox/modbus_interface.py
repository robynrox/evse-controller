from abc import ABC, abstractmethod
from typing import Optional, List

class ModbusClientInterface(ABC):
    @abstractmethod
    def open(self) -> bool:
        """Open the connection"""
        pass

    @abstractmethod
    def close(self) -> bool:
        """Close the connection"""
        pass

    @abstractmethod
    def is_open(self) -> bool:
        """Check if connection is open"""
        pass

    @abstractmethod
    def read_holding_registers(self, reg_addr: int, reg_nb: int = 1) -> Optional[List[int]]:
        """Read holding registers"""
        pass

    @abstractmethod
    def write_single_register(self, reg_addr: int, reg_value: int) -> Optional[bool]:
        """Write single register"""
        pass

class ModbusClientWrapper(ModbusClientInterface):
    """Wrapper for pyModbusTCP.client.ModbusClient"""
    def __init__(self, host: str, auto_open: bool = True, timeout: int = 2):
        from pyModbusTCP.client import ModbusClient
        self._auto_open = auto_open
        self._timeout = timeout
        self._host = host
        self._client = ModbusClient(host=host, auto_open=auto_open, timeout=timeout)

    def set_host(self, host: str) -> None:
        """Reconnect the client to a newly verified address."""
        from pyModbusTCP.client import ModbusClient
        try:
            self._client.close()
        except Exception:
            pass
        self._host = host
        self._client = ModbusClient(
            host=host,
            auto_open=self._auto_open,
            timeout=self._timeout,
        )

    def open(self) -> bool:
        return self._client.open()

    def close(self) -> bool:
        return self._client.close()

    def is_open(self) -> bool:
        return self._client.is_open

    def read_holding_registers(self, reg_addr: int, reg_nb: int = 1) -> Optional[List[int]]:
        return self._client.read_holding_registers(reg_addr, reg_nb)

    def write_single_register(self, reg_addr: int, reg_value: int) -> Optional[bool]:
        return self._client.write_single_register(reg_addr, reg_value)