"""Tests for the dashboard history store."""

from collections import deque

from evse_controller.drivers.EvseController import EvseController
from evse_controller.history_store import HistoryStore, resolution_for


def _channels(value):
    return {"primary": {"channel1": value, "channel2": None}}


def test_window_resolutions_keep_the_point_count_sensible():
    assert resolution_for(300) == 1
    assert resolution_for(900) == 3
    assert resolution_for(3600) == 10
    assert resolution_for(86400) == 360


def test_recent_query_uses_raw_samples_and_preserves_gaps():
    store = HistoryStore()
    store.record(100.0, _channels(100))
    store.record(110.0, _channels(200))
    store.record(150.0, _channels(300))

    result = store.query(300, now=160.0)

    assert result["window_s"] == 300
    assert result["resolution_s"] == 1
    assert [entry["timestamp"] for entry in result["entries"]] == [
        100.0, 110.0, 150.0
    ]
    assert result["entries"][0]["channels"]["primary"]["channel1"] == 100
    assert result["entries"][1]["channels"]["primary"]["channel2"] is None


def test_large_window_downsamples_with_mean_min_and_max():
    store = HistoryStore()
    for index in range(30):
        store.record(
            float(index),
            {"primary": {"channel1": float(index)}},
        )

    result = store.query(300, now=29.0)

    # 300 s at the 5-minute resolution is one-second buckets, so the test
    # exercises the 3 s bucket by asking for a 15-minute window.
    longer = store.query(900, now=29.0)
    assert longer["resolution_s"] == 3
    assert len(longer["entries"]) < 30
    first = longer["entries"][0]
    assert first["channels"]["primary"]["channel1"] == 1.0
    assert first["min"]["primary"]["channel1"] == 0.0
    assert first["max"]["primary"]["channel1"] == 2.0
    assert result["resolution_s"] == 1


def test_incremental_since_includes_the_current_partial_bucket():
    store = HistoryStore()
    store.record(100.0, _channels(100))
    store.record(110.0, _channels(200))

    result = store.query(300, since=110.0, now=120.0)

    assert [entry["timestamp"] for entry in result["entries"]] == [110.0]
    assert result["entries"][0]["channels"]["primary"]["channel1"] == 200


def test_long_window_uses_the_coarse_buffer_and_includes_current_bucket():
    store = HistoryStore(recent_seconds=60, bucket_seconds=10)
    for index in range(12):
        store.record(
            float(index * 10),
            {"primary": {"channel1": float(index * 10)}},
        )

    result = store.query(120, now=115.0)

    assert result["resolution_s"] == 1
    assert [entry["timestamp"] for entry in result["entries"]] == [
        float(value) for value in range(0, 120, 10)
    ]
    assert result["entries"][-1]["channels"]["primary"]["channel1"] == 110


def test_out_of_order_samples_are_ignored():
    store = HistoryStore()
    store.record(200.0, _channels(200))
    store.record(100.0, _channels(100))

    result = store.query(300, now=210.0)

    assert [entry["timestamp"] for entry in result["entries"]] == [200.0]
    assert result["entries"][0]["channels"]["primary"]["channel1"] == 200


def test_controller_default_history_stays_backwards_compatible():
    controller = object.__new__(EvseController)
    controller.history = {
        "entries": deque([{"timestamp": 1.0, "channels": _channels(10)}]),
        "channel_metadata": {"channels": {}},
    }
    controller.history_store = HistoryStore()
    controller._update_channel_metadata = lambda: None

    result = controller.getHistory()

    assert result["entries"] == [
        {"timestamp": 1.0, "channels": _channels(10)}
    ]


def test_controller_windowed_history_uses_the_store():
    import time

    controller = object.__new__(EvseController)
    controller.history = {"entries": deque(), "channel_metadata": {"channels": {}}}
    controller.history_store = HistoryStore()
    controller._update_channel_metadata = lambda: None
    now = int(time.time())
    controller.history_store.record(now - 10, _channels(10))
    controller.history_store.record(now, _channels(20))

    result = controller.getHistory(window_s=300, since=now)

    assert result["window_s"] == 300
    assert result["resolution_s"] == 1
    assert [entry["timestamp"] for entry in result["entries"]] == [float(now)]
    assert result["channel_metadata"] == {"channels": {}}
