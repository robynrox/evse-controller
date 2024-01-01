class Power:
    def __init__(self, gridWatts: float, solarWatts: float, voltage: float):
        self.gridWatts = gridWatts
        self.solarWatts = solarWatts
        self.voltage = voltage
    
    def __str__(self):
        return f"Grid: {self.gridWatts} W; Solar: {self.solarWatts} W; Voltage: {self.voltage} V"
