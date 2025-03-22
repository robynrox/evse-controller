class Power:
    def __init__(self, ch1Watts: float = 0, ch1Pf: float = 0, ch2Watts: float = 0, ch2Pf: float = 0, voltage: float = 0, unixtime: int = -1, posEnergyJoulesCh0: float = 0, negEnergyJoulesCh0: float = 0, posEnergyJoulesCh1: float = 0, negEnergyJoulesCh1: float = 0, soc: int = 0):
        self.ch1Watts = ch1Watts
        self.ch1Pf = ch1Pf
        self.ch2Watts = ch2Watts
        self.ch2Pf = ch2Pf
        self.voltage = voltage
        self.unixtime = unixtime
        self.posEnergyJoulesCh0 = posEnergyJoulesCh0
        self.negEnergyJoulesCh0 = negEnergyJoulesCh0
        self.posEnergyJoulesCh1 = posEnergyJoulesCh1
        self.negEnergyJoulesCh1 = negEnergyJoulesCh1
        self.soc = soc

    def __str__(self):
        return f"Ch1: {self.ch1Watts}W, pf {self.ch1Pf}; Ch2: {self.ch2Watts}W, pf {self.ch2Pf}; Voltage: {self.voltage}V; unixtime {self.unixtime}; SoC% {self.soc}"

    def getAccumulatedEnergy(self):
        posEnergyCh0 = round(self.posEnergyJoulesCh0 / 3600)
        posEnergyCh1 = round(self.posEnergyJoulesCh1 / 3600)
        negEnergyCh0 = round(self.negEnergyJoulesCh0 / 3600)
        negEnergyCh1 = round(self.negEnergyJoulesCh1 / 3600)
        return f"PosCh1: {posEnergyCh0}Wh; NegCh1: {negEnergyCh0}Wh; PosCh2: {posEnergyCh1}Wh; NegCh2: {negEnergyCh1}Wh"

    def getEnergyDelta(self, olderPower):
        posEnergyCh0 = round((self.posEnergyJoulesCh0 - olderPower.posEnergyJoulesCh0) / 3600)
        posEnergyCh1 = round((self.posEnergyJoulesCh1 - olderPower.posEnergyJoulesCh1) / 3600)
        negEnergyCh0 = round((self.negEnergyJoulesCh0 - olderPower.negEnergyJoulesCh0) / 3600)
        negEnergyCh1 = round((self.negEnergyJoulesCh1 - olderPower.negEnergyJoulesCh1) / 3600)
        return f"PosCh1: {posEnergyCh0}Wh; NegCh1: {negEnergyCh0}Wh; PosCh2: {posEnergyCh1}Wh; NegCh2: {negEnergyCh1}Wh"

    def getHomeWatts(self):
        return self.ch1Watts - self.ch2Watts
