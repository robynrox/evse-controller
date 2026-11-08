"""Three-tier import/export prices with optional half-hour windows.

The model is deliberately small: every side has a ``standard`` fallback plus a
``cheap`` and a ``peak`` tier.  Cheap and peak may each have any number of
windows, including windows that cross midnight.  Standard has no windows.

Presets carry the window structure, because that is what changes between
supplier products.  A normal install chooses a preset and edits only the six
prices.  Advanced users may override the window lists.

This module has no hardware or tariff imports so validation can be tested on
its own and reused by the web form before anything is saved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

TIERS = ("standard", "cheap", "peak")
SIDES = ("import", "export")
EXPORT_SOURCES = ("agile", "tiers", "none")
DAY_MINUTES = 24 * 60


def parse_clock(text: str) -> int:
    """Parse ``HH:MM`` into minutes since midnight.

    ``24:00`` is accepted as the end of the day.  Anything else outside the
    day is a configuration error the user needs to see, not a silent clamp.
    """
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time {text!r}; use HH:MM")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        raise ValueError(f"Invalid time {text!r}; use HH:MM") from None
    if not (0 <= hour <= 24) or not (0 <= minute < 60):
        raise ValueError(f"Invalid time {text!r}; use HH:MM")
    if hour == 24 and minute != 0:
        raise ValueError(f"Invalid time {text!r}; 24:00 is the latest end")
    return hour * 60 + minute


@dataclass(frozen=True)
class PriceWindow:
    """One half-open ``[start, end)`` window, wrap-aware."""

    start: int
    end: int
    text: str

    @classmethod
    def from_text(cls, text: str) -> "PriceWindow":
        raw = str(text).strip()
        if "-" not in raw:
            raise ValueError(
                f"Invalid window {text!r}; use HH:MM-HH:MM (e.g. 23:30-05:30)"
            )
        start_text, end_text = raw.split("-", 1)
        start = parse_clock(start_text)
        end = parse_clock(end_text)
        if start == end:
            raise ValueError(
                f"Invalid window {text!r}; start and end must differ"
            )
        return cls(start=start, end=end, text=f"{start_text.strip()}-{end_text.strip()}")

    def covers(self, day_minute: int) -> bool:
        minute = day_minute % DAY_MINUTES
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end

    def _intervals(self) -> tuple[tuple[int, int], ...]:
        if self.start < self.end:
            return ((self.start, self.end),)
        return ((self.start, DAY_MINUTES), (0, self.end))

    def overlaps(self, other: "PriceWindow") -> bool:
        for start_a, end_a in self._intervals():
            for start_b, end_b in other._intervals():
                if start_a < end_b and start_b < end_a:
                    return True
        return False

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.text


def _windows(values: Optional[Iterable[str]]) -> tuple[PriceWindow, ...]:
    if not values:
        return ()
    if isinstance(values, str):
        values = [values]
    return tuple(PriceWindow.from_text(value) for value in values)


def _validate_side(side: str, windows: Mapping[str, tuple[PriceWindow, ...]]):
    if windows.get("standard"):
        raise ValueError(
            f"{side.title()} standard tier cannot have windows; it is the "
            "fallback"
        )
    cheap = windows.get("cheap", ())
    peak = windows.get("peak", ())
    for cheap_window in cheap:
        for peak_window in peak:
            if cheap_window.overlaps(peak_window):
                raise ValueError(
                    f"{side.title()} cheap window {cheap_window} overlaps "
                    f"peak window {peak_window}"
                )


@dataclass(frozen=True)
class PriceTierTable:
    """Resolved prices and windows for one side of the meter."""

    import_prices: Mapping[str, float]
    export_prices: Mapping[str, float]
    import_windows: Mapping[str, tuple[PriceWindow, ...]]
    export_windows: Mapping[str, tuple[PriceWindow, ...]]
    export_source: str = "tiers"

    def __post_init__(self):
        if self.export_source not in EXPORT_SOURCES:
            raise ValueError(
                f"Unknown export source {self.export_source!r}; choose "
                f"{', '.join(EXPORT_SOURCES)}"
            )
        for side, prices in (
            ("import", self.import_prices),
            ("export", self.export_prices),
        ):
            for tier in TIERS:
                value = prices.get(tier)
                if value is None:
                    raise ValueError(f"{side.title()} {tier} price is missing")
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"{side.title()} {tier} price must be a number"
                    ) from None
                if number < 0:
                    raise ValueError(
                        f"{side.title()} {tier} price cannot be negative"
                    )
        _validate_side("import", self.import_windows)
        _validate_side("export", self.export_windows)

    @staticmethod
    def _lookup(
        prices: Mapping[str, float],
        windows: Mapping[str, tuple[PriceWindow, ...]],
        day_minute: int,
    ) -> float:
        # Cheap is checked before peak; validation guarantees no overlap.
        for tier in ("cheap", "peak"):
            for window in windows.get(tier, ()):
                if window.covers(day_minute):
                    return float(prices[tier])
        return float(prices["standard"])

    def import_price_p(self, day_minute: int) -> float:
        return self._lookup(
            self.import_prices, self.import_windows, day_minute
        )

    def import_price_with(self, prices: Mapping[str, float], day_minute: int) -> float:
        """Look up a slot using current prices with this table's windows."""
        return self._lookup(prices, self.import_windows, day_minute)

    def export_price_p(self, day_minute: int) -> Optional[float]:
        if self.export_source == "none":
            return None
        return self._lookup(
            self.export_prices, self.export_windows, day_minute
        )

    def export_price_with(
        self, prices: Mapping[str, float], day_minute: int
    ) -> Optional[float]:
        """Look up a slot using current prices with this table's windows."""
        if self.export_source == "none":
            return None
        return self._lookup(prices, self.export_windows, day_minute)

    @property
    def dynamic_export(self) -> bool:
        return self.export_source == "agile"


@dataclass(frozen=True)
class PricePreset:
    """The window structure and starter prices for a supplier product."""

    key: str
    label: str
    import_windows: Mapping[str, tuple[str, ...]]
    export_windows: Mapping[str, tuple[str, ...]]
    default_import_prices: Mapping[str, float]
    default_export_prices: Mapping[str, float]
    export_source: str = "tiers"


PRESETS: dict[str, PricePreset] = {
    "iog_agile": PricePreset(
        key="iog_agile",
        label="Intelligent Octopus Go + Agile Outgoing",
        import_windows={"cheap": ("23:30-05:30",), "peak": ()},
        export_windows={"cheap": (), "peak": ()},
        default_import_prices={"standard": 30.0, "cheap": 10.0, "peak": 30.0},
        default_export_prices={"standard": 8.0, "cheap": 8.0, "peak": 12.0},
        export_source="agile",
    ),
    "iog_outgoing": PricePreset(
        key="iog_outgoing",
        label="Intelligent Octopus Go + Outgoing Octopus",
        import_windows={"cheap": ("23:30-05:30",), "peak": ()},
        export_windows={"cheap": (), "peak": ()},
        default_import_prices={"standard": 30.0, "cheap": 10.0, "peak": 30.0},
        default_export_prices={"standard": 12.0, "cheap": 12.0, "peak": 12.0},
        export_source="tiers",
    ),
    "iog_prime": PricePreset(
        key="iog_prime",
        label="Intelligent Octopus Go + Prime Outgoing",
        import_windows={"cheap": ("23:30-05:30",), "peak": ()},
        export_windows={"cheap": (), "peak": ("16:00-19:00",)},
        default_import_prices={"standard": 30.0, "cheap": 10.0, "peak": 30.0},
        default_export_prices={"standard": 9.0, "cheap": 9.0, "peak": 16.0},
        export_source="tiers",
    ),
    "iog_no_export": PricePreset(
        key="iog_no_export",
        label="Intelligent Octopus Go, no export tariff",
        import_windows={"cheap": ("23:30-05:30",), "peak": ()},
        export_windows={"cheap": (), "peak": ()},
        default_import_prices={"standard": 30.0, "cheap": 10.0, "peak": 30.0},
        default_export_prices={"standard": 0.0, "cheap": 0.0, "peak": 0.0},
        export_source="none",
    ),
    "octopus_go": PricePreset(
        key="octopus_go",
        label="Octopus Go",
        import_windows={"cheap": ("00:30-05:30",), "peak": ()},
        export_windows={"cheap": (), "peak": ()},
        default_import_prices={"standard": 30.0, "cheap": 10.0, "peak": 30.0},
        default_export_prices={"standard": 8.0, "cheap": 8.0, "peak": 12.0},
        export_source="tiers",
    ),
    "cosy": PricePreset(
        key="cosy",
        label="Cosy Octopus",
        import_windows={
            "cheap": ("04:00-07:00", "13:00-16:00", "22:00-24:00"),
            "peak": ("16:00-19:00",),
        },
        export_windows={"cheap": (), "peak": ()},
        default_import_prices={"standard": 26.22, "cheap": 12.86, "peak": 39.32},
        default_export_prices={"standard": 15.0, "cheap": 15.0, "peak": 15.0},
        export_source="tiers",
    ),
    "flux": PricePreset(
        key="flux",
        label="Octopus Flux",
        import_windows={"cheap": ("02:00-05:00",), "peak": ("16:00-19:00",)},
        export_windows={"cheap": ("02:00-05:00",), "peak": ("16:00-19:00",)},
        default_import_prices={"standard": 24.85, "cheap": 14.91, "peak": 34.79},
        default_export_prices={"standard": 13.26, "cheap": 4.69, "peak": 26.42},
        export_source="tiers",
    ),
}


def build_price_table(
    preset: str,
    import_prices: Mapping[str, float],
    export_prices: Mapping[str, float],
    *,
    import_windows: Optional[Mapping[str, Optional[Iterable[str]]]] = None,
    export_windows: Optional[Mapping[str, Optional[Iterable[str]]]] = None,
    export_source: Optional[str] = None,
) -> PriceTierTable:
    """Resolve a preset plus optional window overrides into a table.

    A window key omitted or set to ``None`` uses the preset.  An explicit
    empty list removes that tier's windows, which lets a user keep a preset's
    prices but simplify its structure.
    """
    if preset not in PRESETS:
        raise ValueError(
            f"Unknown tariff preset {preset!r}; choose from "
            f"{', '.join(sorted(PRESETS))}"
        )
    definition = PRESETS[preset]

    def resolve(
        side: str,
        preset_windows: Mapping[str, tuple[str, ...]],
        overrides: Optional[Mapping[str, Optional[Iterable[str]]]],
    ) -> Mapping[str, tuple[PriceWindow, ...]]:
        resolved = {}
        overrides = overrides or {}
        for tier in TIERS:
            if tier == "standard":
                resolved[tier] = ()
            elif tier in overrides and overrides[tier] is not None:
                resolved[tier] = _windows(overrides[tier])
            else:
                resolved[tier] = _windows(preset_windows.get(tier, ()))
        return resolved

    return PriceTierTable(
        import_prices=import_prices,
        export_prices=export_prices,
        import_windows=resolve(
            "import", definition.import_windows, import_windows
        ),
        export_windows=resolve(
            "export", definition.export_windows, export_windows
        ),
        export_source=export_source or definition.export_source,
    )
