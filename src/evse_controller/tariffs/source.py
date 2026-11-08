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


def sessions_for_day(day: date, store=None) -> list[EnergySession]:
    """Enabled sessions that overlap the local day *day*.

    Sessions are deliberately kept out of the price rows.  Consumers that need
    a zero import price (or a reserved export slot) apply the session overlay
    themselves, so the raw supplier price remains visible.
    """
    store = session_store if store is None else store
    day_start = datetime.combine(day, time())
    day_end = day_start + timedelta(days=1)
    return [
        session
        for session in store.sessions
        if session.enabled and session.start < day_end and session.end > day_start
    ]


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
        return sessions_for_day(day, self._session_store)

    def ocpp_import_price_p(self, day: date, slot: int) -> Optional[float]:
        """Off-peak purchase price for a supplier-scheduled OCPP slot."""
        return self._tariff.ocpp_import_price_p(day, slot)


class SlotPriceBookSource:
    """The offline fallback source used while no tariff is active.

    The daily report runs in SMART, FREERUN, PAUSE and OCPP alike.  When a
    tariff is stopped there is still a price book to value the slot: the fixed
    import band and the Agile Outgoing rate cache the report has always used.
    This adapter exposes that book through the same :class:`TariffSource`
    contract, so the report has one pricing path whether a tariff is running or
    not.

    The import band is deliberately *raw*: a free-import session is a separate
    session row, and the consumer applies it.  That keeps the fallback
    equivalent to a tariff source, where the supplier price and the session
    overlay are also distinct.
    """

    name = "SlotPriceBook"
    dynamic_import = False
    # The export side is the fetched Agile Outgoing series.
    dynamic_export = True

    def __init__(self, book=None, session_store=None):
        if book is None:
            # Imported lazily so this module does not depend on the report.
            from evse_controller.slot_prices import price_book

            book = price_book
        self._book = book
        self._session_store = session_store

    def rates_for_day(self, day: date) -> list[SlotRate]:
        rates = []
        for slot in range(SLOTS_PER_DAY):
            start = slot_start(day, slot)
            rates.append(
                SlotRate(
                    start=start,
                    end=start + timedelta(minutes=SLOT_MINUTES),
                    import_p=self._book.raw_import_price_p(day, slot),
                    export_p=self._book.export_price_p(day, slot),
                )
            )
        return rates

    def sessions_for_day(self, day: date) -> list[EnergySession]:
        return sessions_for_day(day, self._session_store)

    def ocpp_import_price_p(self, day: date, slot: int) -> Optional[float]:
        """The price book has no supplier-scheduled rate of its own."""
        return None
