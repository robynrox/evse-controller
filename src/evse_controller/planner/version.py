"""Version identifier for the Learning mode planner.

Bump :data:`SOLVER_VERSION` whenever a change can alter the chosen action or
its cost, even when the configuration is unchanged.  Recorded runs store the
value, so a later regression can be tied to the planner generation that
produced it and the first run of a new generation is easy to find.
"""

#: Date-based identifier for the current planner generation.
SOLVER_VERSION = "dp-2026.11.12"
