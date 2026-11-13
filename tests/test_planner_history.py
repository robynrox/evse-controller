"""Append-only planner and estimator history."""

from datetime import date

from evse_controller.planner.history import PlannerHistory


DAY = date(2026, 11, 7)


def test_runs_and_samples_round_trip(tmp_path):
    history = PlannerHistory(tmp_path)

    history.record_run(
        DAY,
        {
            "ts": "2026-11-07T12:00:00",
            "slot": 24,
            "soc": 60.0,
            "actions": [{"slot": 24, "kind": "idle"}],
        },
    )
    history.record_soc_sample(
        DAY,
        {
            "ts": "2026-11-07T12:00:00",
            "estimate": 59.8,
            "variance": 1.2,
            "reported": 60,
        },
    )
    history.record_decision(
        DAY,
        {
            "ts": "2026-11-07T12:00:00",
            "slot": 24,
            "mode": "Idle — battery protection",
        },
    )

    runs = history.runs_for_day(DAY)
    samples = history.soc_samples_for_day(DAY)
    decisions = history.decisions_for_day(DAY)

    assert len(runs) == 1
    assert runs[0]["slot"] == 24
    assert runs[0]["actions"][0]["kind"] == "idle"
    assert len(samples) == 1
    assert samples[0]["estimate"] == 59.8
    assert decisions == [
        {
            "ts": "2026-11-07T12:00:00",
            "slot": 24,
            "mode": "Idle — battery protection",
        }
    ]


def test_bad_lines_are_ignored(tmp_path):
    history = PlannerHistory(tmp_path)
    history.record_run(DAY, {"slot": 1})
    path = tmp_path / f"{DAY.isoformat()}.runs.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n\n")

    runs = history.runs_for_day(DAY)

    assert runs == [{"slot": 1}]


def test_available_days_are_newest_first(tmp_path):
    history = PlannerHistory(tmp_path)
    history.record_run(DAY, {"slot": 1})
    history.record_run(date(2026, 11, 6), {"slot": 1})

    assert history.available_days() == [DAY, date(2026, 11, 6)]


def test_set_directory_redirects_the_store(tmp_path):
    history = PlannerHistory()
    history.set_directory(tmp_path)
    history.record_run(DAY, {"slot": 2})

    assert (tmp_path / f"{DAY.isoformat()}.runs.jsonl").exists()


def test_vehicle_events_round_trip(tmp_path):
    history = PlannerHistory(tmp_path)

    history.record_vehicle_event(
        DAY,
        {
            "ts": "2026-11-07T01:00:00",
            "event": "disconnect",
            "slot": 2,
            "soc": 60.0,
        },
    )
    history.record_vehicle_event(
        DAY,
        {
            "ts": "2026-11-07T02:30:00",
            "event": "reconnect",
            "slot": 5,
            "soc_before": 60.0,
            "soc": 54.0,
            "soc_delta": -6.0,
        },
    )

    events = history.vehicle_events_for_day(DAY)

    assert [event["event"] for event in events] == [
        "disconnect",
        "reconnect",
    ]
    assert events[1]["soc_delta"] == -6.0
