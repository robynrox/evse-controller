# Recurring EV readiness schedule

**Status:** design/backlog. Not implemented. One-off readiness targets remain
the current behaviour.

## Why

A one-off target is right for a trip, but daily use needs a timer: for example,
weekdays at 07:30, target 80%, hold for two hours, off at weekends and while on
holiday. The useful mental model is a basic heating timer rather than a
calendar application.

The important safety behaviour is the automatic release. A user who sets a
target and then goes on holiday should not come back to a car that has been
held at the readiness floor indefinitely.

## Desired behaviour

A recurring schedule should provide:

- a target SoC;
- a ready-by time in local wall-clock time;
- selected days of the week;
- an optional hold/cancel grace period after the target is reached;
- optional paused/active date ranges for holidays or periods when the timer is
  not needed;
- an enabled/disabled switch.

The schedule should **materialise a one-off readiness target** for each
occurrence. The existing planner, policy map and readiness charge/floor rules
then work unchanged. The schedule is a layer above the current target store,
not a second control path.

### Automatic release

After the target has been reached, the generated target should be completed
automatically after a configured grace period. This is the holiday protection.

Open question: if the target is reached early, should the grace period start
immediately or only at the ready-by time? The safer default is:

```text
cancel_at = max(ready_by, target_reached_at + hold_minutes)
```

so the car is not released before the departure time merely because it charged
quickly. The hold can therefore begin at `ready_by` if that is preferred; the
choice should be explicit in the UI.

If the target is never reached, a separate fallback can cancel it after
`ready_by + cancel_after_ready_by_minutes`. This is optional because cancelling
a still-short target also stops the controller trying to meet it. A sensible
default is to keep trying until the car is unplugged, unless the user asks for
a give-up timeout.

### Paused periods

The schedule should support a paused-until date, or explicit active-from /
active-until dates. While paused:

- no new targets are materialised;
- an already-materialised target for a paused day is cancelled;
- the schedule remains visible and can be resumed without re-entering it.

A manual one-off target should take precedence over the schedule. Manual
complete/cancel must win and must not be immediately recreated by the timer.

## Timing

For a morning ready-by time, the planner needs the previous evening's cheap
slots. The target therefore needs to exist before midnight, not at 00:00 on the
ready-by day.

The schedule should either:

- materialise the next occurrence at a configurable lead time (for example
  16:00 or 20:00 the previous day), or
- materialise it when the moving planner horizon first reaches the occurrence.

Use an idempotency key such as `(schedule_id, occurrence_date)` so repeated
control ticks cannot create duplicate targets.

"Target reached" should use the estimated SoC with a small tolerance and, if
practical, confirmation across more than one sample. It should not fire on a
single noisy reading.

The existing unplug/completion rules still apply. An unplug after the ready-by
time completes the target as it does today.

## Proposed data model

```text
RecurringReadinessSchedule
  id
  name
  enabled
  days_of_week          # mon..sun
  ready_by              # local time
  target_soc_percent
  hold_minutes_after_target
  cancel_after_ready_by_minutes   # optional give-up fallback
  active_from           # optional date
  active_until          # optional date
  created_at
  updated_at
```

Materialised targets should record their source, for example:

```text
source          # "manual" or "schedule"
schedule_id
occurrence_date
```

That lets auto-cancel, the dashboard and the API distinguish a timer-generated
target from a one-off target.

Storage: extend `data/state/ev_readiness.json` with a top-level `schedules`
key, keeping the existing one-off target shape compatible. A separate file is
also possible; the single file keeps the readiness feature together.

## UI and API

`/ev-readiness` should gain a weekly schedule section:

- a table of days, ready-by time, target and hold time;
- add/edit/delete and enable/disable;
- pause until a date, with the next occurrence and "paused until" shown;
- the currently active generated target, its source and its auto-release time;
- manual one-off targets on the same page, with clear precedence.

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
- **Car away:** no target, or complete per the existing readiness rules.

## Tests

- weekly evaluation with enabled/disabled days;
- paused and active date ranges;
- materialisation before midnight and idempotency across repeated ticks;
- auto-cancel after the grace period, and never before the ready-by time;
- optional give-up timeout for a target that is never reached;
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
