"""The normalised tariff interface the planner and report consume.

A tariff class currently does three jobs: it prices a day, it knows about
supplier session windows, and it contains a control heuristic with its own
dashboard.  The first two are what the DP needs, and they are the same shape
for every tariff:

* 48 half-hour rows per local day;
* import and export prices in p/kWh;
* ``None`` when a price is genuinely unknown, never zero;
* session windows expressed as half-hour boundaries.

:class:`TariffSourceAdapter` presents an existing :class:`Tariff` through that
interface without changing any control behaviour.  It is deliberately a thin
adapter: the golden tests compare its output with the tariff's own
``get_import_rate`` / ``get_export_rate`` methods, so a later config-driven
source can be proven equivalent before the old classes are retired.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional, Protocol, runtime_checkable

from evse_controller.energy_sessions import EnergySession, session_store
from evse_controller.tariffs.base import Tariff


SLOT_MINUTES = 30
SLOTS_PER_DAY = 48


def slot_start(day: date, slot: int) -> datetime:
    """Start of a half-hour slot as a naive local datetime.

    The local day is always 48 rows, including clock-change days, which is why
    this is naive: a fixed 30-minute step with UTC offsets would run backwards
    on a spring-forward morning.
    """
    return datetime.combine(day, time()) + timedelta(minutes=SLOT_MINUTES * slot)


@dataclass(frozen=True)
class SlotRate:
    """One half-hour of prices, in p/kWh.

    ``import_p`` and ``export_p`` are ``None`` when the tariff does not know
    the price for that slot.  Consumers must show that as unknown, not as a
    free slot.
    """

    start: datetime
    end: datetime
    import_p: Optional[float]
    export_p: Optional[float]


@runtime_checkable
class TariffSource(Protocol):
    """Read-only price and session series for a local calendar day."""

    name: str
    dynamic_import: bool
    dynamic_export: bool

    def rates_for_day(self, day: date) -> list[SlotRate]:
        """Return 48 rows, ordered by slot, for *day*."""
        ...

    def sessions_for_day(self, day: date) -> list[EnergySession]:
        """Return enabled supplier sessions that overlap *day*."""
        ...


class TariffSourceAdapter:
    """Adapt a :class:`Tariff` to the :class:`TariffSource` interface.

    This is a compatibility bridge, not the destination.  It lets the planner
    and the report read a stable contract while the individual tariff classes
    are still in charge of control.
    """

    def __init__(
        self,
        tariff: Tariff,
        session_store=None,
        name: Optional[str] = None,
    ):
        self._tariff = tariff
        self._session_store = session_store
        self._name = name or type(tariff).__name__

    @property
    def name(self) -> str:
        return self._name

    @property
    def dynamic_import(self) -> bool:
        return bool(getattr(self._tariff, "dynamic_import", False))

    @property
    def dynamic_export(self) -> bool:
        return bool(getattr(self._tariff, "dynamic_export", False))

    @property
    def tariff(self) -> Tariff:
        """The wrapped tariff, for code that still needs control methods."""
        return self._tariff

    def rates_for_day(self, day: date) -> list[SlotRate]:
        rates = []
        for slot in range(SLOTS_PER_DAY):
            start = slot_start(day, slot)
            rates.append(
                SlotRate(
                    start=start,
                    end=start + timedelta(minutes=SLOT_MINUTES),
                    import_p=self._tariff.import_price_p(day, slot),
                    export_p=self._tariff.export_price_p(day, slot),
                )
            )
        return rates

    def sessions_for_day(self, day: date) -> list[EnergySession]:
        store = (
            self._session_store
            if self._session_store is not None
            else session_store
        )
        day_start = datetime.combine(day, time())
        day_end = day_start + timedelta(days=1)
        return [
            session
            for session in store.sessions
            if session.enabled and session.start < day_end and session.end > day_start
        ]
