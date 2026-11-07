"""Receding-horizon battery planner.

The planner consumes a normalised price/load/session series and returns the
optimal half-hourly Wallbox actions.  The live tariff remains the actuator and
fallback until the planner has been backtested and validated.

The first implementation is a pure-Python dynamic program over half-hour
slots.  It deliberately has no third-party solver dependency: the state space
is small, the efficiency model is a measured lookup, and a DP over successor
SoC states keeps the integer Wallbox current and the 4 A/5 A efficiency steps
inside the cost model.
"""
