class Power:
    def __init__(self, gridWatts: float, gridPf: float, evseWatts: float, evsePf: float, voltage: float, unixtime: int = -1, posEnergyJoulesCh0: float = 0, negEnergyJoulesCh0: float = 0, posEnergyJoulesCh1: float = 0, negEnergyJoulesCh1: float = 0):
        self.gridWatts = gridWatts
        self.gridPf = gridPf
        self.evseWatts = evseWatts
        self.evsePf = evsePf
        self.voltage = voltage
        self.unixtime = unixtime
        self.posEnergyJoulesCh0 = posEnergyJoulesCh0
        self.negEnergyJoulesCh0 = negEnergyJoulesCh0
        self.posEnergyJoulesCh1 = posEnergyJoulesCh1
        self.negEnergyJoulesCh1 = negEnergyJoulesCh1
    
    def __str__(self):
        return f"Grid: {self.gridWatts}W, pf {self.gridPf}; EVSE: {self.evseWatts}W, pf {self.evsePf}; Voltage: {self.voltage}V; unixtime {self.unixtime}"
