# Episode Grouping — Architecture Decision

## Problem

Anomaly alerts are emitted one per (timestamp, column) sample. A single
continuous event (e.g. the 163 h humidity freeze recovery on 2026-12-25)
produces 35+ individual alert rows. There is no start, end, duration, or
episode identity anywhere in the pipeline.

## Decision: grouping at the detection layer

Episode grouping runs inside `finalize_scores` (the detection layer), not
at the storage or presentation layer.

## Alternatives rejected

### Storage layer (episode_id column + migration)

- **Pro:** survives pagination, enables direct SQL queries by episode.
- **Con:** requires an alembic migration adding `episode_id` to
  `sensor_readings` (coupling anomaly identity to the telemetry table).
  Backfill is non-trivial. Episode boundaries can shift when detection
  thresholds are recalibrated — a stored episode_id becomes stale.
- **Con:** the CSV upload path already produces a
  `UploadInferenceResponse` that is consumed by the API and the worker;
  threading episode_id through persist_readings_chunk adds cross-cutting
  complexity for a feature that is naturally a derived view.

### Presentation layer (re-derive on every request)

- **Pro:** zero migration, flexible — clients can re-derive with
  different gap thresholds.
- **Con:** re-derives episodes on every request using the full
  `high_anomalies` list. This is O(N log N) per request.
- **Con:** pagination breaks episode identity: an episode spanning two
  pages is split into two partial episodes.
- **Con:** the server-side `/api/anomalies/groups` endpoint groups by
  date/type/sensor today — adding contiguity-based grouping at the
  presentation layer would require the client to do the work (unreliable)
  or the server to re-derive from the DB (slow for large windows).

### Why detection layer wins

- **Per-sample scores are preserved.** `anomaly_scores` (one float per
  sequence) stays intact for the timeline chart; episodes are a parallel
  view of the sparse `high_anomalies` list.
- **Computed once.** The episode list is built at detection time and
  travels with the response. No re-derivation, no pagination issues.
- **No migration.** Episodes are a view over existing data structures.
- **Bounded memory.** The grouper processes the already-accumulated
  `high_anomalies` list in `finalize_scores` — O(N_alerts) not
  O(N_rows). State is O(active_channels), not O(file_length).
- **Multi-tenant safe.** Each upload produces its own episode list in
  its own response — no shared mutable state.

## Episode model

```
AnomalyEpisode:
  channel:          str       # triggering sensor/column
  detector:         str       # stuck_fault, statistical, lstm, etc.
  start_ts:         ISO str   # first alert timestamp
  end_ts:           ISO str   # last alert timestamp
  duration_seconds: float     # see "Stuck-fault duration semantics"
  peak_score:       float     # max alert score in the episode
  sample_count:     int       # number of alerts in the episode
  severity:         str       # worst severity in the episode
  is_recovery:      bool      # post-fault transition, not independent
  is_ongoing:       bool      # freeze still open at detection time
  onset_ts:         str|null  # stuck_fault only: freeze onset
  detected_ts:      str|null  # stuck_fault only: when detection fired
  frozen_value:     float|null
```

## Contiguity rule

Two consecutive alerts on the same **(channel, detector)** pair belong to
the same episode when the gap between their timestamps is <=
`EPISODE_GAP_SECONDS` (default: 600 s = 2x the expected 5-min cadence).
Alerts on different channels OR different detectors are always separate
episodes, even at the same timestamp.

### Why group by (channel, detector) — Task 5b fix

Before Task 5b, episodes were grouped by channel only. A `stuck_fault`
alert and a `statistical` alert on the same channel at a similar
timestamp would merge into one episode, with the detector label inherited
from whichever alert sorted first. This caused the main 163 h humidity
freeze to be absorbed into a `statistical` cluster — losing the fault's
identity, severity, and duration.

Grouping by (channel, detector) ensures each detector's events are
tracked independently. A `stuck_fault` episode can never be merged with
a `statistical` episode, even if they overlap in time on the same
channel.

## Stuck-fault duration semantics (Task 5b)

A stuck_fault alert fires when the wall-clock gate is first crossed
(typically at STUCK_WALLCLOCK_MIN_HOURS ≈ 6 h). At that instant the
freeze is still ongoing — the alert's `z_score` field carries the
elapsed duration *at detection time*, not the final duration.

### Completed faults (freeze ended in the data)

When the frozen value changes (i.e. the sensor recovers), a
`FaultWindow` is recorded in `StuckCarry.completed_faults` with the
exact onset and end timestamps. The episode grouper looks up each
stuck_fault alert's `(channel, onset_ts)` in the completed-fault index:

- **Match found:** `duration_seconds = end_dt - onset_dt` (the full
  frozen span). `is_ongoing = False`.

Example: the 2026-12-18T13:00 humidity freeze ends at 2026-12-25T08:55.
The episode reports `duration_seconds ≈ 589,500` (163.75 h),
`is_ongoing = False`.

### Open faults (freeze still active at end of file)

If no matching `FaultWindow` exists — the freeze was still open when
the file ended and the alert came from `flush()` — the episode uses
`z_score * 3600` (the gate-crossing duration in seconds) as a **lower
bound**:

- `duration_seconds = z_score * 3600` (≈ 21,600 s for a 6 h gate)
- `is_ongoing = True`

Consumers that display ongoing episodes should indicate the duration is
provisional (e.g. "≥ 6.0 h (ongoing)").

### Why not update in place?

The episode list is part of the response object, which is immutable
after `finalize_scores` returns. A live-monitoring path would need a
separate mechanism (e.g. an SSE update) to push a revised duration when
the freeze ends. That is out of scope for the cold-run upload path.

## Recovery scoring (Task 3)

When a channel exits a detected stuck fault:

1. **Baseline cleanup:** the fault-period values are NaN-ed out in the
   statistical carry so the rolling z-score baseline is not contaminated.
2. **Score cap:** recovery episodes (`is_recovery=True`) have their
   peak score capped at `RECOVERY_SCORE_CAP` (default: 0.4, below the
   canonical anomaly threshold). The stuck fault episode (score 1.0)
   always outranks the recovery.
3. **Visibility:** recovery episodes are still reported to the operator
   (they indicate the sensor came back) — they are just not scored as
   the largest anomaly in the run.

## Stuck-fault mid-freeze detection (Task 2b)

The wall-clock stuck detector now evaluates the gate on every row while
the freeze is still open. The alert fires as soon as
`STUCK_WALLCLOCK_MIN_HOURS` is crossed, not when the freeze ends. Each
alert carries both `onset_ts` (when the freeze began) and `detected_ts`
(when detection fired) so detection latency is measurable and testable.
