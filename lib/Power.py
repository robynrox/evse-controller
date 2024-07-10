class Power:
    def __init__(self, gridWatts: float, gridPf: float, solarWatts: float, solarPf: float, voltage: float):
        self.gridWatts = gridWatts
        self.gridPf = gridPf
        self.solarWatts = solarWatts
        self.solarPf = solarPf
        self.voltage = voltage
    
    def __str__(self):
        return f"Grid: {self.gridWatts} W, pf {self.gridPf}; EVSE: {self.solarWatts} W, pf {self.solarPf}; Voltage: {self.voltage} V"
