"""Rolling dashboard history with time-window queries.

The dashboard needs detail for the last few minutes and a coarse reflection of
the last day, without shipping every sample to the browser on every poll.  This
store keeps a one-hour raw buffer and a 24-hour 10 s bucket buffer, then
aggregates a query into a resolution that keeps each trace to a few hundred
points.

A bucket is only emitted when it contains at least one sample.  Gaps are
therefore real gaps, not invented zeroes, which lets the chart show a comms
outage at its true position on a fixed time axis.
"""

from __future__ import annotations

from collections import deque
import math
import time
from typing import Any, Optional

DEFAULT_WINDOW_SECONDS = 300
MAX_WINDOW_SECONDS = 86400
RECENT_SECONDS = 3600
LONG_BUCKET_SECONDS = 10
RETENTION_SECONDS = 86400

#: ``(maximum window seconds, bucket seconds)``.  Each window produces roughly
#: 240–360 points, enough to show the shape without sending a dense series.
WINDOW_RESOLUTIONS: tuple[tuple[int, int], ...] = (
    (300, 1),
    (900, 3),
    (1800, 5),
    (3600, 10),
    (7200, 30),
    (10800, 45),
    (14400, 60),
    (21600, 90),
    (28800, 120),
    (36000, 150),
    (43200, 180),
    (54000, 225),
    (64800, 270),
    (86400, 360),
)


def resolution_for(window_seconds: int) -> int:
    """Bucket size for a requested time window."""
    for limit, resolution in WINDOW_RESOLUTIONS:
        if window_seconds <= limit:
            return resolution
    return WINDOW_RESOLUTIONS[-1][1]


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_channels(channels: dict) -> dict:
    """Copy a channel tree with numeric values, preserving gaps as ``None``."""
    result: dict[str, dict[str, Optional[float]]] = {}
    for device, device_values in (channels or {}).items():
        if not isinstance(device_values, dict):
            continue
        result[device] = {
            channel: _as_float(value)
            for channel, value in device_values.items()
        }
    return result


class HistoryStore:
    """A two-resolution ring of dashboard power samples."""

    def __init__(
        self,
        *,
        recent_seconds: int = RECENT_SECONDS,
        bucket_seconds: int = LONG_BUCKET_SECONDS,
        retention_seconds: int = RETENTION_SECONDS,
    ) -> None:
        if recent_seconds <= 0 or bucket_seconds <= 0:
            raise ValueError("history windows must be positive")
        self.recent_seconds = int(recent_seconds)
        self.bucket_seconds = int(bucket_seconds)
        self.retention_seconds = int(retention_seconds)
        self._recent: deque[dict] = deque()
        self._long: deque[dict] = deque()
        self._current_start: Optional[int] = None
        self._current: dict[str, dict[str, dict[str, float]]] = {}
        self._last_timestamp: Optional[float] = None

    # -- recording ------------------------------------------------------

    def record(self, timestamp: float, channels: dict) -> None:
        """Append one grid-monitor sample to both resolutions."""
        ts = float(timestamp)
        if self._last_timestamp is not None and ts < self._last_timestamp:
            # Updates are chronological in normal operation.  Ignore a stale
            # out-of-order sample rather than corrupting the current bucket.
            return
        clean = _clean_channels(channels)
        self._recent.append({"timestamp": ts, "channels": clean})
        self._last_timestamp = ts
        self._trim_recent(ts)

        bucket_start = int(ts // self.bucket_seconds) * self.bucket_seconds
        if self._current_start is None:
            self._current_start = bucket_start
        elif bucket_start != self._current_start:
            self._flush_current()
            self._current_start = bucket_start
        self._accumulate(clean)
        self._trim_long(ts)

    def _accumulate(self, channels: dict) -> None:
        for device, values in channels.items():
            target = self._current.setdefault(device, {})
            for channel, value in values.items():
                if value is None:
                    continue
                entry = target.setdefault(
                    channel,
                    {"sum": 0.0, "count": 0, "min": value, "max": value},
                )
                entry["sum"] += value
                entry["count"] += 1
                entry["min"] = min(entry["min"], value)
                entry["max"] = max(entry["max"], value)

    def _flush_current(self) -> None:
        if self._current_start is None or not self._current:
            self._current = {}
            return
        channels: dict[str, dict[str, Optional[float]]] = {}
        mins: dict[str, dict[str, float]] = {}
        maxs: dict[str, dict[str, float]] = {}
        counts: dict[str, dict[str, int]] = {}
        for device, values in self._current.items():
            channels[device] = {}
            mins[device] = {}
            maxs[device] = {}
            counts[device] = {}
            for channel, entry in values.items():
                count = int(entry["count"])
                if count <= 0:
                    channels[device][channel] = None
                    continue
                channels[device][channel] = entry["sum"] / count
                mins[device][channel] = entry["min"]
                maxs[device][channel] = entry["max"]
                counts[device][channel] = count
        self._long.append(
            {
                "timestamp": float(self._current_start),
                "channels": channels,
                "min": mins,
                "max": maxs,
                "counts": counts,
            }
        )
        self._current = {}

    def _trim_recent(self, now: float) -> None:
        cutoff = now - self.recent_seconds
        while self._recent and self._recent[0]["timestamp"] < cutoff:
            self._recent.popleft()

    def _trim_long(self, now: float) -> None:
        cutoff = now - self.retention_seconds
        while self._long and self._long[0]["timestamp"] < cutoff:
            self._long.popleft()

    # -- querying -------------------------------------------------------

    def query(
        self,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        *,
        since: Optional[float] = None,
        now: Optional[float] = None,
    ) -> dict:
        """Return downsampled entries for the requested window.

        ``since`` includes the bucket containing that timestamp so the client
        can replace a partial final bucket rather than appending a duplicate.
        """
        window = max(1, min(int(window_seconds), MAX_WINDOW_SECONDS))
        current_time = time.time() if now is None else float(now)
        window_start = current_time - window
        resolution = resolution_for(window)
        start = window_start
        if since is not None:
            start = max(window_start, float(since) - resolution)

        if window <= self.recent_seconds:
            source = list(self._recent)
            source_is_long = False
            current_bucket = None
        else:
            source = list(self._long)
            source_is_long = True
            current_bucket = self._current_payload()

        buckets: dict[int, dict] = {}
        for entry in source:
            timestamp = float(entry.get("timestamp", 0.0))
            if timestamp < start:
                continue
            self._merge_entry(
                buckets, entry, resolution, raw=not source_is_long
            )

        if current_bucket is not None and current_bucket["timestamp"] >= start:
            self._merge_entry(buckets, current_bucket, resolution, raw=False)

        entries = [buckets[key] for key in sorted(buckets)]
        return {
            "window_s": window,
            "resolution_s": resolution,
            "from": window_start,
            "to": current_time,
            "last_sample": self._last_timestamp,
            "entries": entries,
        }

    def _current_payload(self) -> Optional[dict]:
        if self._current_start is None or not self._current:
            return None
        channels: dict[str, dict[str, Optional[float]]] = {}
        mins: dict[str, dict[str, float]] = {}
        maxs: dict[str, dict[str, float]] = {}
        counts: dict[str, dict[str, int]] = {}
        for device, values in self._current.items():
            channels[device] = {}
            mins[device] = {}
            maxs[device] = {}
            counts[device] = {}
            for channel, entry in values.items():
                count = int(entry["count"])
                if count <= 0:
                    channels[device][channel] = None
                    continue
                channels[device][channel] = entry["sum"] / count
                mins[device][channel] = entry["min"]
                maxs[device][channel] = entry["max"]
                counts[device][channel] = count
        return {
            "timestamp": float(self._current_start),
            "channels": channels,
            "min": mins,
            "max": maxs,
            "counts": counts,
        }

    def _merge_entry(
        self,
        buckets: dict[int, dict],
        entry: dict,
        resolution: int,
        *,
        raw: bool,
    ) -> None:
        timestamp = float(entry.get("timestamp", 0.0))
        bucket_start = int(timestamp // resolution) * resolution
        target = buckets.setdefault(
            bucket_start,
            {
                "timestamp": float(bucket_start),
                "channels": {},
                "min": {},
                "max": {},
                "counts": {},
            },
        )
        channels = entry.get("channels") or {}
        mins = entry.get("min") or {}
        maxs = entry.get("max") or {}
        counts = entry.get("counts") or {}
        for device, values in channels.items():
            if not isinstance(values, dict):
                continue
            target_channels = target["channels"].setdefault(device, {})
            target_mins = target["min"].setdefault(device, {})
            target_maxs = target["max"].setdefault(device, {})
            target_counts = target["counts"].setdefault(device, {})
            for channel, value in values.items():
                number = _as_float(value)
                if number is None:
                    target_channels.setdefault(channel, None)
                    continue
                weight = 1
                if not raw:
                    weight = int(
                        (counts.get(device) or {}).get(channel, 1)
                    )
                previous = target_channels.get(channel)
                previous_count = int(target_counts.get(channel, 0))
                if previous is None:
                    target_channels[channel] = number
                else:
                    target_channels[channel] = (
                        previous * previous_count + number * weight
                    ) / (previous_count + weight)
                target_counts[channel] = previous_count + weight
                low = (mins.get(device) or {}).get(channel, number)
                high = (maxs.get(device) or {}).get(channel, number)
                target_mins[channel] = min(
                    target_mins.get(channel, low), low
                )
                target_maxs[channel] = max(
                    target_maxs.get(channel, high), high
                )

    # -- introspection --------------------------------------------------

    def __len__(self) -> int:
        return len(self._recent)
