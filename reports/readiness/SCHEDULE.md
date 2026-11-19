# Recurring EV readiness schedule

**Status:** design/backlog. Not implemented. One-off readiness targets remain
the current behaviour.

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

This is deliberate holiday protection: the timer always ends. If a user wants
"keep trying until the target is reached, however long that takes", that
should be a separate option, not the default.

A manual complete/cancel should still win and should not be recreated by the
timer for that occurrence.

### Unplugging

The existing readiness rules still apply. An unplug after the ready-by time
completes the target as it does today. An unplug before the occurrence is
materialised means there is nothing to charge; the schedule should not create
a target for a car that is away unless that is useful for the next connection.
This needs a decision when the feature is implemented.

## Paused periods

The schedule should support a paused-until date, or explicit active-from /
active-until dates. While paused:

- no new targets are materialised;
- an already-materialised target for a paused day is cancelled;
- the schedule remains visible and can be resumed without re-entering it.

Manual one-off targets take precedence over the schedule.

## Timing

For a morning ready-by time, the planner needs the previous evening's cheap
slots. The target therefore needs to exist before midnight, not at 00:00 on
the ready-by day.

The schedule should either:

- materialise the next occurrence at a configurable lead time (for example
  16:00 or 20:00 the previous day), or
- materialise it when the moving planner horizon first reaches the occurrence.

Use an idempotency key such as `(schedule_id, occurrence_date)` so repeated
control ticks cannot create duplicate targets.

The target SoC still matters for planning and for the shortfall report, but
the release is purely time-based.

## Proposed data model

```text
RecurringReadinessSchedule
  id
  name
  enabled
  days_of_week                  # mon..sun
  ready_by                      # local time
  target_soc_percent
  hold_minutes_after_ready_by
  active_from                   # optional date
  active_until                  # optional date
  created_at
  updated_at
```

Optional extension for a single grouped rule:

```text
ready_by_by_day                 # e.g. {mon: 07:00, ..., fri: 08:00}
```

Materialised targets should record their source and planned release:

```text
source                          # "manual" or "schedule"
schedule_id
occurrence_date
release_at                      # ready_by + hold
```

That lets auto-release, the dashboard and the API distinguish a timer-generated
target from a one-off target.

Storage: extend `data/state/ev_readiness.json` with a top-level `schedules`
key, keeping the existing one-off target shape compatible. A separate file is
also possible; the single file keeps the readiness feature together.

## UI and API

`/ev-readiness` should gain a weekly schedule section:

- a table of days, ready-by time, target, hold and release time;
- add/edit/delete and enable/disable;
- pause until a date, with the next occurrence and "paused until" shown;
- the currently active generated target, its source and its release time;
- manual one-off targets on the same page, with clear precedence.

A friendly summary should be generated from the rules, for example:

> 60% by 07:00 Mon-Thu and 08:00 Fri, held until 09:00 / 10:00.

API endpoints should mirror the page: list, create, update, delete, pause and
resume. The existing one-off target endpoints stay.

## Edge cases

- **DST:** the rest of the controller uses naive local wall-clock time. A
  ready-by time may be skipped or repeated; define the behaviour and test it.
- **Midnight:** anchor an occurrence to the ready-by date, not the date the
  target was materialised.
- **Overlapping schedules:** keep one active target. Warn on overlap or define
  a deterministic precedence rather than silently creating two.
- **Holiday pause:** cancel a materialised target when the pause begins.
- **Manual cancel during the hold:** do not recreate until the next occurrence.
- **Target above the charge cap:** clamp as today and show the warning.
- **No price horizon:** keep the existing checkpoint-target behaviour.
- **Missed target:** complete at the release time with a shortfall warning.
- **Car away:** no target, or complete per the existing readiness rules.

## Tests

- the Mon-Thu 07:00 / Fri 08:00 example produces the right occurrences;
- the floor is retained until `ready-by + hold`, even if the target SoC is
  reached early;
- a missed target completes at the release time with a shortfall;
- weekly evaluation with enabled/disabled days;
- paused and active date ranges;
- materialisation before midnight and idempotency across repeated ticks;
- manual target precedence and manual cancel;
- unplug/completion interaction;
- API and page CRUD.

## Non-goals

- a full calendar/exception UI;
- geofencing or automatic trip detection;
- multiple cars;
- changing the DP or the planner's target semantics.

The schedule generates the same target the user can create manually today. The
planner should not need to know that a timer produced it.
