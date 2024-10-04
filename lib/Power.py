class Power:
    def __init__(self, gridWatts: float, gridPf: float, evseWatts: float, evsePf: float, voltage: float):
        self.gridWatts = gridWatts
        self.gridPf = gridPf
        self.evseWatts = evseWatts
        self.evsePf = evsePf
        self.voltage = voltage
    
    def __str__(self):
        return f"Grid: {self.gridWatts} W, pf {self.gridPf}; EVSE: {self.evseWatts} W, pf {self.evsePf}; Voltage: {self.voltage} V"
