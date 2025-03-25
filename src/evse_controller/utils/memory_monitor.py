import os
import psutil
import threading
import time
from datetime import datetime
from evse_controller.utils.logging_config import info

def get_memory_usage():
    """Get current memory usage of the process"""
    process = psutil.Process(os.getpid())
    memory_info = process.memory_info()
    return {
        'rss': memory_info.rss / 1024 / 1024,  # RSS in MB
        'vms': memory_info.vms / 1024 / 1024,  # VMS in MB
        'percent': process.memory_percent()
    }

class MemoryMonitor(threading.Thread):
    def __init__(self, interval=3600):  # Default: 1 hour
        threading.Thread.__init__(self, name="MemoryMonitor")
        self.daemon = True
        self.interval = interval
        self.running = True

    def run(self):
        while self.running:
            mem = get_memory_usage()
            info(f"Memory usage - RSS: {mem['rss']:.1f}MB, VMS: {mem['vms']:.1f}MB, Percent: {mem['percent']:.1f}%")
            time.sleep(self.interval)

    def stop(self):
        self.running = False