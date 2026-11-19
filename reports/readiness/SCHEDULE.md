# Recurring EV readiness schedule

**Status:** implemented. The one-off readiness target remains available, and
the weekly schedule below generates the same kind of target automatically.

## Why

A one-off target is right for a trip, but daily use needs a timer. The useful
mental model is a basic heating timer rather than a calendar application.

The important safety behaviour is the automatic release. A user who sets a
target and then goes on holiday should not come back to a car that has been
held at the readiness floor indefinitely.

## The behaviour in plain language

A schedule should express something like:

> Make sure the car has at least 60% SoC at 07:00 each day from Monday to
> Thursday and 08:00 on Friday. Retain at least 60% SoC for two hours after
> the time indicated.

That means:

- charge towards **60% by the ready-by time**;
- keep **60% as a floor** from the ready-by time until
  `ready-by + hold`;
- release the target automatically at the end of the hold, then return to
  normal operation.

For the example:

| Day | Ready by | Hold | Floor released |
|---|---|---|---|
| Monday-Thursday | 07:00 | 2 h | 09:00 |
| Friday | 08:00 | 2 h | 10:00 |
| Saturday-Sunday | no occurrence | - | - |

The target time can differ by day. The simplest way to represent the example
is **two rules**:

1. Monday-Thursday, ready-by 07:00, target 60%, hold 120 minutes.
2. Friday, ready-by 08:00, target 60%, hold 120 minutes.

A single rule with an optional per-day ready-by override would also work, but
multiple rules are simpler to store, edit and test. The UI can group them so
the user still writes one sentence.

## Automatic release

The release is based on **the ready-by time**, not on when the target SoC was
reached:

```text
release_at = ready_by + hold_minutes_after_ready_by
```

So:

- if the car reaches 60% early, the floor still holds until 09:00 (or 10:00 on
  Friday);
- if the car has not reached 60% by 07:00, the controller keeps trying during
  the hold window;
- at the release time the occurrence is completed automatically, even if the
  target was not reached. The shortfall is reported rather than leaving the
  target active indefinitely.

This is deliberate holiday protection: the timer always ends rather than
leaving the target active indefinitely. A manual complete/cancel suppresses
the current occurrence and the scheduler does not recreate it.

### Unplugging

The existing readiness rules still apply. An unplug after the ready-by time
completes the target as it does today. The scheduler itself does not know
whether the car is connected; it materialises the occurrence as usual, and the
tariff's normal readiness charge/floor rules decide what to do. A future
refinement could suppress an occurrence while the car is away.

## Paused periods

The schedule has a **pause through** date. While paused:

- no new targets are materialised;
- an already-materialised scheduled target is cancelled;
- the schedule remains visible and can be resumed without re-entering it.

Each event also has its own enabled switch and can be edited or deleted.
Disabling or deleting an event releases a target it already materialised.

Manual one-off targets take precedence over the schedule.

## Timing

For a morning ready-by time, the planner needs the previous evening's cheap
slots. The target therefore needs to exist before midnight, not at 00:00 on
the ready-by day.

The scheduler uses a fixed **24-hour lead window**. It materialises the next
occurrence as soon as it is within 24 hours, and not before. Repeated ticks
are idempotent: an active target for the same occurrence is reused, and a
target is only created when no target is active. A manual target owns the
slot until it is completed or cancelled.

The target SoC still matters for planning and for the shortfall report, but
the release is purely time-based.

## Data model

```text
RecurringReadinessEvent
  id
  days                          # mon..sun
  ready_time                    # local time, on :00 or :30
  target_soc_percent
  hold_minutes                  # 0..24h in 30-minute steps
  enabled
  created_at
  updated_at
```

Materialised targets record their source and planned release:

```text
source                          # "manual" or "schedule"
schedule_id
release_at                      # ready_by + hold
completion_reason               # "released", "schedule disabled", etc.
completed_soc_percent           # optional shortfall record
```

That lets auto-release and the page distinguish a timer-generated target from
a one-off target.

Storage: `data/state/ev_readiness_schedule.json` holds the events, the
paused-through date and skipped occurrences. One-off targets remain in
`data/state/ev_readiness.json`.

An optional future extension is a single grouped rule with a
`ready_by_by_day` map, so the UI can express the Mon–Thu/Friday example as one
row. The current implementation uses two event rows, which is simpler to edit
and test.

## UI and API

`/ev-readiness` has a weekly schedule section:

- a table of days, target time, target SoC, hold and release time;
- add/edit/delete and enable/disable;
- pause through a date, with the pause shown on the page;
- the currently active generated target, its source and its release time;
- manual one-off targets on the same page, with clear precedence.

A friendly summary is generated from the rules, for example:

> 60% by 07:00 Mon–Thu and 08:00 Fri, held until 09:00 / 10:00.

The page handles the schedule. REST/MQTT endpoints for the schedule are not
implemented yet; they are a natural follow-up if external controllers need to
edit it.

## Edge cases

- **DST:** the rest of the controller uses naive local wall-clock time. A
  ready-by time may be skipped or repeated; define the behaviour and test it.
- **Midnight:** anchor an occurrence to the ready-by date, not the date the
  target was materialised.
- **Overlapping schedules:** keep one active target. The scheduler chooses the
  earliest upcoming occurrence deterministically; the page does not warn
  about overlap yet.
- **Holiday pause:** the pause-through date cancels a materialised target.
- **Manual cancel during the hold:** the occurrence is skipped and is not
  recreated; the next occurrence still runs.
- **Target above the charge cap:** clamp as today and show the warning.
- **No price horizon:** keep the existing checkpoint-target behaviour.
- **Missed target:** complete at the release time with the shortfall recorded.
- **Car away:** the scheduler creates the occurrence as usual; the existing
  readiness charge/floor rules handle the unplugged car.

## Tests

- the Mon–Thu 07:00 / Fri 08:00 example produces the right occurrences;
- the floor is retained until `ready-by + hold`, even if the target SoC is
  reached early;
- a missed target completes at the release time with the shortfall recorded;
- weekly evaluation with enabled/disabled days;
- pause-through dates, per-event enable/disable and per-occurrence skip;
- the 24-hour lead window and idempotency across repeated ticks;
- manual target precedence and manual cancel;
- page CRUD for schedule events and pause/resume;
- the existing readiness unplug/completion tests still pass.

## Non-goals

- a full calendar/exception UI;
- geofencing or automatic trip detection;
- multiple cars;
- changing the DP or the planner's target semantics.

The schedule generates the same target the user can create manually today. The
planner should not need to know that a timer produced it.
