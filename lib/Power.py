class Power:
    def __init__(self, gridWatts: float, gridPf: float, evseWatts: float, evsePf: float, voltage: float, unixtime: int = -1, posEnergyJoulesCh0: float = 0, negEnergyJoulesCh0: float = 0, posEnergyJoulesCh1: float = 0, negEnergyJoulesCh1: float = 0, soc: int = 0):
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
        self.soc = soc

    def __str__(self):
        return f"Grid: {self.gridWatts}W, pf {self.gridPf}; EVSE: {self.evseWatts}W, pf {self.evsePf}; Voltage: {self.voltage}V; unixtime {self.unixtime}; SoC% {self.soc}"

    def getAccumulatedEnergy(self):
        posEnergyCh0 = round(self.posEnergyJoulesCh0 / 3600)
        posEnergyCh1 = round(self.posEnergyJoulesCh1 / 3600)
        negEnergyCh0 = round(self.negEnergyJoulesCh0 / 3600)
        negEnergyCh1 = round(self.negEnergyJoulesCh1 / 3600)
        return f"PosGrid: {posEnergyCh0}Wh; NegGrid: {negEnergyCh0}Wh; PosEVSE: {posEnergyCh1}Wh; NegEVSE: {negEnergyCh1}Wh"

    def getEnergyDelta(self, olderPower):
        posEnergyCh0 = round((self.posEnergyJoulesCh0 - olderPower.posEnergyJoulesCh0) / 3600)
        posEnergyCh1 = round((self.posEnergyJoulesCh1 - olderPower.posEnergyJoulesCh1) / 3600)
        negEnergyCh0 = round((self.negEnergyJoulesCh0 - olderPower.negEnergyJoulesCh0) / 3600)
        negEnergyCh1 = round((self.negEnergyJoulesCh1 - olderPower.negEnergyJoulesCh1) / 3600)
        return f"PosGrid: {posEnergyCh0}Wh; NegGrid: {negEnergyCh0}Wh; PosEVSE: {posEnergyCh1}Wh; NegEVSE: {negEnergyCh1}Wh"
