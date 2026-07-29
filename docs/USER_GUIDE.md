# AI Agronomist — User Guide

A complete walkthrough of the operator interface: every page, every control, and
the workflows that connect them.

**Related docs:** [Anomaly Catalog](ANOMALY_CATALOG.md) · [README](../README.md) · [Framework spec](anomaly_framework_spec.md)

---

## Contents

**Getting started**
1. [What this system does](#1-what-this-system-does)
2. [Starting the application](#2-starting-the-application)
3. [Concepts you need first](#3-concepts-you-need-first)
4. [The shell: sidebar, tenant, theme](#4-the-shell-sidebar-tenant-theme)
5. [Shared controls on every data page](#5-shared-controls-on-every-data-page)

**Page by page**
6. [Home](#6-home)
7. [Monitoring](#7-monitoring)
8. [Climate Data](#8-climate-data)
9. [Gas Monitoring](#9-gas-monitoring)
10. [Anomaly Detection](#10-anomaly-detection)
11. [Thresholds](#11-thresholds)
12. [System Metrics](#12-system-metrics)
13. [Configuration](#13-configuration)
14. [Upload & Operations](#14-upload--operations)
15. [Job Monitoring](#15-job-monitoring)
16. [Operator Feedback](#16-operator-feedback)

**Workflows**
17. [Uploading data](#17-uploading-data)
18. [Training models](#18-training-models)
19. [First-run checklist](#19-first-run-checklist)
20. [Troubleshooting](#20-troubleshooting)

---

# 1. What this system does

AI Agronomist watches greenhouse sensor telemetry and tells you when something is
wrong — distinguishing *sensor faults* (the instrument is lying) from *climate
faults* (the greenhouse is genuinely out of band) from *network faults* (the data
never arrived).

It works in four layers:

1. **Deterministic rule detectors** — closed-form maths on the raw signal
   (rolling MAD, CUSUM, variance ratios, Pearson correlation, psychrometrics).
2. **Crop band checks** — comparison against day/night setpoints you configure.
3. **A learned LSTM-VAE** — a sequence autoencoder trained on *your* clean
   history, which catches shapes no rule anticipated.
4. **An ensemble vote** — four detectors weighted together, with agreement
   counting.

On top of detection sits a diagnosis stack: pattern classification, root-cause
analysis, confidence scoring, and a recommendation engine — plus incident memory
(find similar past cases) and active learning (your verdicts retrain the model).

Every anomaly type is documented in the [Anomaly Catalog](ANOMALY_CATALOG.md).

---

# 2. Starting the application

## Option A — Docker Compose (everything at once)

```bash
docker compose up -d
```

This starts PostgreSQL, RabbitMQ, Redis, the FastAPI API, the background worker,
and the legacy Streamlit dashboard. The Next.js frontend is run separately (see
below).

| Service | URL |
|---------|-----|
| API | http://localhost:8000 |
| API docs (OpenAPI) | http://localhost:8000/docs |
| RabbitMQ management | http://localhost:15672 |
| Streamlit (legacy) | http://localhost:8501 |

## Option B — local development

**Backend:**

```bash
python3 -m venv venv && source venv/bin/activate   # Python 3.11
pip install -r requirements.txt

export DATABASE_URL=sqlite+aiosqlite:///./greenhouse.db
alembic upgrade head

python -m uvicorn src.api.main:app --reload
```

**Worker** (required for uploads that persist or train — without it, jobs sit in
`preparing` forever):

```bash
python -m src.workers.main
```

**Frontend** (from `web/`, pnpm 9.15.9 via corepack):

```bash
cd web
corepack pnpm install
API_URL=http://localhost:8000 corepack pnpm dev
```

Open **http://localhost:3000**.

> The browser never talks to FastAPI directly. Every request goes through the
> Next.js **BFF** proxy at `/api/bff/*`, which injects the `X-API-Key` header
> server-side. This is why `API_URL` and `API_KEY` are server environment
> variables and must never be exposed as `NEXT_PUBLIC_*`.

## Database migrations

Run `alembic upgrade head` **before or simultaneously with** the application
deploy. The running app errors on a missing column if migrations lag.

```bash
alembic upgrade head
alembic heads     # must show a single head
```

> **Only the `alembic/` tree is authoritative.** `src/db/migrations/` is a dead
> legacy tree containing a single initial-schema stub — do not run it.

---

# 3. Concepts you need first

## Tenant and greenhouse

Everything is scoped to a **tenant** (a customer) and a **greenhouse** within it.
Readings, incidents, models, jobs, and configuration are all filtered by this
pair. Selecting the wrong scope is the single most common reason a page looks
empty.

Both live in the URL, so a link always carries the scope with it.

## The URL is the state

Tenant, greenhouse, time window, threshold, channel selection and live mode are
all query-string parameters. Consequences worth knowing:

- **Reload reproduces the exact view.** No hidden state.
- **Links are shareable.** Send someone a URL and they see what you see.
- **Browser back/forward work** as navigation through view states.

| Parameter | Meaning |
|-----------|---------|
| `tenant` | Tenant id (default `tenant_1`) |
| `gh` | Greenhouse id (default `gh_A`) |
| `from` / `to` | Time window bounds (ISO timestamps) |
| `threshold` | Anomaly score cutoff (default `0.15`) |
| `live` | Live streaming on/off |
| `channel` | Focused sensor channel |
| `refresh` | Auto-refresh interval in seconds |

## Anomaly score and threshold

Each reading can carry an **anomaly score**. The **threshold** decides what counts
as an anomaly. The default is **0.15**, matching the model's calibrated cutoff
scale where scores top out around 0.2.

The threshold is **shared across Monitoring, Anomaly Detection and Thresholds** —
it is one URL parameter, so changing it on one page changes it everywhere. That
is deliberate: you should never be looking at two pages with different definitions
of "anomaly".

## Severity colours

| Chip | Meaning |
|------|---------|
| **Normal** | Below threshold |
| **Warning** | Above threshold, one statistical detector |
| **LSTM / drift** | Above threshold, flagged by an LSTM or drift detector |
| **Critical** | Above threshold **and** multiple detectors agree |

Critical is not "a higher score" — it is *agreement*. Two independent detectors
concurring is a far stronger signal than one detector scoring high, which is why
it gets its own colour.

## Downsampling

Long windows return more points than a chart can draw. The backend downsamples
using LTTB (largest-triangle-three-buckets), which preserves visual shape.

**Anomalies are preserved through downsampling** — flagged points are never
dropped. The header shows *"· downsampled (anomalies preserved)"* when this is
active.

However: **never count anomalies from chart rows.** Downsampled rows drop
unflagged points unevenly per window. Anomaly counts always come from the
dedicated flagged-rows endpoint, which is why the count in the Anomaly Detection
header can read 2,649 while the chart holds 35 points. Both are correct.

---

# 4. The shell: sidebar, tenant, theme

The left sidebar is present on every page.

## Navigation

```
Home

ACTIVE MONITORING
  Monitoring          — all sensor channels, live streaming
  Climate Data        — temperature & humidity only
  Gas Monitoring      — ethylene, NOx, CO and friends

DETECTION
  Anomaly Detection   — flagged readings, grouping, diagnosis
  Thresholds          — tune the cutoff before committing
  System Metrics      — pipeline health

OPERATIONS
  Configuration       — crop setpoints, seasons, CO₂ flag
  Upload & Operations — CSV upload, models, test data
  Job Monitoring      — background job progress

FEEDBACK
  Operator Feedback   — label incidents, drive retraining
```

Navigation links **preserve the query string**, so moving between pages keeps
your tenant, greenhouse and time window.

## Tenant selector

At the bottom of the sidebar. Two dropdowns:

- **Tenant** — `tenant_1`, `tenant_2`, `tenant_3`
- **Greenhouse** — `gh_A`, `gh_B`, `gh_C`

Changing either updates the URL and refetches every query on the page.

## Theme toggle

Top-right of the sidebar header. Light/dark; the choice persists.

---

# 5. Shared controls on every data page

Monitoring, Climate, Gas, Anomaly Detection and Thresholds share one toolbar in
the page header.

## Setpoints panel

Per-channel min/max display bands, used for tile colouring and chart reference
lines. Stored in **browser localStorage** — these are *your view preferences*,
not system configuration.

- Only channels with known metadata get editable rows.
- Validation runs **on blur**, not per keystroke, so typing `-` or `3.` mid-entry
  does not flash an error.
- **Reset** restores defaults.

> Not to be confused with **Configuration → Product-specific setpoints**, which
> are server-side crop bands that actually drive detection. The Setpoints panel
> only changes what you see; the Configuration page changes what is detected.

## Live toggle

Starts a Server-Sent Events stream of readings for the current scope. The button
shows a pulsing green dot while active.

What happens when you turn it on:

1. The time window snaps to **wall-clock now** (not the newest stored reading),
   so incoming ticks land inside the visible lookback.
2. The current period preset is re-applied against that new anchor.
3. If the scope was muted by a previous *Clear all data*, it is explicitly
   resumed.

> Historical CSV data often sits far from wall-clock now. Outside live mode,
> presets anchor to the **newest stored reading** so "Last 15 min" is not empty
> on a dataset that ends in December. In live mode they anchor to **now**,
> because otherwise a future-dated historical maximum would clip every incoming
> tick out of view. This is the one place where the two anchoring rules differ,
> and it is the reason the chart sometimes jumps when you toggle Live.

## Period selector

| Preset | Lookback |
|--------|----------|
| Last 15 min | 0.25 h |
| Last 30 min | 0.5 h |
| Last hour | 1 h |
| Last 12 hours | 12 h |
| Last 24 hours | 24 h (default) |
| Last 7 days | 168 h |
| Last 1 month | 720 h |
| Last 6 months | 4368 h |
| **Custom** | Explicit start/end datetime pickers |

Selecting **Custom** opens datetime fields. If Custom is already selected,
choosing it again reopens the pickers.

## Refresh interval

Auto-refetch cadence: 10 s, 30 s (default), 1 min, 5 min.

## Chart interaction

On any time-series chart:

- **Drag horizontally** to select a span → narrows the URL window. Every table
  and chart on the page follows, and the Period control switches to Custom.
- **Scroll** to zoom.
- **Reset button** returns to the full window.

Because selection writes to the URL, a zoom is shareable and survives reload.

---

# 6. Home

`/`

A scaffold and reference page, not an operational dashboard.

| Card | Contents |
|------|----------|
| **Severity semantics** | The four severity chips as a legend |
| **Window controls** | The URL-driven date range control, standalone |
| **Chart pipeline** | A uPlot rendering demo |

Useful for confirming the frontend is alive and for checking colour rendering in
your theme. Day-to-day work starts on **Monitoring**.

---

# 7. Monitoring

`/monitoring` — the main operational view. All discovered sensor channels,
historical and live.

## Header

Page title, then the active window (e.g. *"2026-07-01 00:00 → 2026-07-02
00:00"*), with *· downsampled* appended when the backend reduced the point count.
Shared controls on the right.

## Sensor tiles

One tile per channel showing the latest value, unit and label.

- **Left border and value colour** reflect status against your setpoints:
  green (ok), amber (warning), red (critical).
- In live mode a tile **flashes** on each new value.
- When the stream goes stale the tiles **dim**, so a frozen feed is visible
  rather than silently showing old numbers as if they were current.

## Sensor channels card

A toggle chip per discovered channel. Turning channels off removes them from the
chart and table.

Visibility is a small state machine: it remembers your manual selection per
tenant/greenhouse for the session, but a **jump from an anomaly** (see below)
temporarily focuses the affected channels so you land on the right lines.

Channel discovery is dynamic — it reads whatever columns your data actually
contains, merging historical and live rows. A channel present only in the live
buffer still appears.

## Live anomaly strip

Visible only while Live is on and anomalies have arrived. A compact ribbon of
recent live detections above the chart, so you notice an event without watching
the chart continuously.

## Time series chart

Multi-channel uPlot chart.

- Anomaly windows are painted as **bands** behind the lines.
- Bands only paint when the visible channel selection makes them meaningful — if
  you have hidden the affected channel, painting its band would be misleading.
- Detector-attributed sensors per timestamp come from two sources: enrichment for
  historical rows and the live SSE stream for current ones.
- In live mode, when the lookback is mostly empty (a demo CSV far from wall-clock
  now), the chart auto-fits the X axis to the actual data span instead of drawing
  a flat desert with a blip on the right edge.

## Readings table

Newest first, row count in the card title.

- Virtualised above 100 rows.
- Anomalous rows are highlighted, with affected sensors marked.
- **Clicking a row** narrows the chart window to that timestamp. Modifier-click
  and shift-click extend the selection into a range.
- Values are carried forward for display only — a live tick that lacks a column
  shows the last known value rather than `—`. The **chart never uses
  carry-forward**, because it would draw long flat polylines across the empty
  lookback and crush the real live activity at the right edge.

## Empty state

> *"No sensor readings in the selected window. Widen the range or pick another
> tenant/greenhouse."*

In live mode: *"Waiting for live data — streaming started."*

---

# 8. Climate Data

`/climate`

Monitoring, filtered to channels whose names contain `temp` or `humid`
(substring match, so `temperature`, `air_temp`, `relative_humidity` all appear).

Identical controls and behaviour to Monitoring. Use it when temperature/humidity
noise from other channels is getting in the way.

---

# 9. Gas Monitoring

`/gas`

Monitoring, filtered to the gas channels — but with **exact** name matching, not
substring:

`c2h4` (ethylene) · `gas_co2` · `no` · `no2` · `nox` · `co`

> The exact match is deliberate. With substring matching the climate `co2`
> channel would leak onto this page, and climate CO₂ (enrichment control) and gas
> CO₂ (combustion product) are different measurements with different failure
> modes. Mixing them would be actively misleading.

Gas anomaly scoring is separate from the climate pipeline — see
[Gas anomalies](ANOMALY_CATALOG.md#part-5--gas-anomalies). Ethylene is the
channel to watch: it causes flower abortion at low ppb and usually indicates
incomplete combustion in a CO₂ burner.

---

# 10. Anomaly Detection

`/anomalies` — where you investigate what was flagged.

## Header

Title, active window, *"· downsampled (anomalies preserved)"* when applicable.

## Control bar

Three elements in one row:

**Threshold slider** — the anomaly cutoff. Shared with Monitoring and Thresholds
via the URL.

**View switcher** — *Graphical view* / *Table view*.

**Count readout** — e.g. *"2,649 anomalies ≥ 0.150 in 41,280 points"*.

> The count comes from a dedicated exact-count endpoint, not from the chart rows.
> While that request is in flight the count shows `—` rather than a wrong number:
> displaying the downsampled row count would be off by orders of magnitude and
> would also route the grouping logic down the wrong path.

## Graphical view

### Anomaly score timeline

Score over time with the threshold drawn as a reference line. Drag-select to
narrow the window — the tables below follow.

### Top anomalies

Highest-scoring rows, capped at **50**. Columns:

| Column | Contents |
|--------|----------|
| Timestamp | When |
| Score | Numeric score with severity chip |
| Severity | Normal / Warning / LSTM-drift / Critical |
| Fault type | Human label (e.g. "Spike fault", "Calibration drift") |
| Affected sensors | Which channels |
| Reasoning | Which algorithm fired and why |
| Jump link | → Monitoring, focused on this timestamp and these channels |

Fault type, reasoning and affected sensors arrive from a separate enrichment
request. While it loads those cells show skeletons rather than `—`, so an empty
cell always means "no data" and never "still loading".

**Export CSV** downloads the current table snapshot.

### All anomalies

Every flagged row, newest first, paginated. Same columns.

### The jump link

Takes you to Monitoring with the time window centred on that anomaly and the
affected channels focused. This is the fastest path from *"something was flagged"*
to *"here is what the sensors were doing".*

## Table view — grouped anomalies

Aggregates instead of listing. The grouping strategy switches automatically at
**500 flagged rows**:

| Count | Mode | Group by |
|-------|------|----------|
| < 500 | Client-side | Date, Severity |
| ≥ 500 | Server-side | Date, Fault type, Sensor |

Each group shows its key, the row count, the peak score, and expandable samples
with values and reasoning.

> The switch is not cosmetic. Below 500 rows the client has the complete flagged
> set and can group it exactly. Above 500 the payload is capped, so grouping
> client-side would silently group a *subset* and report wrong counts. The page
> waits for the exact count before committing to either path — which is why you
> may briefly see a loading state instead of an immediate render.

---

# 11. Thresholds

`/thresholds` — a what-if page. Change the cutoff and see the consequence
*before* committing.

## Threshold card

The slider, plus the score distribution for the current window:

> *Score range in window: 0.021 → 0.198 · p90 0.142 · p95 0.161*

The percentiles are the useful part: if p95 is 0.161, a threshold of 0.15 will
flag roughly the top 6 % of readings.

## Comparison tiles

| Tile | Meaning |
|------|---------|
| **Points in window** | Total readings |
| **Points with scores** | Readings that carry a score (unscored rows can never be flagged) |
| **Flagged at saved** | Count at the currently saved threshold |
| **Flagged at threshold** | Count at the previewed threshold, with a **+N / −N vs saved** chip |

## How to use it

1. Pick a representative window — ideally one containing a known real event.
2. Move the slider and watch the delta chip.
3. Cross-check on Anomaly Detection: does the real event survive? Do the
   borderline rows look like noise?
4. Settle on a value.

Raising the threshold reduces alarm fatigue but risks missing slow-onset faults
like calibration drift, whose scores are inherently modest. Lowering it catches
those but floods the queue. The percentiles tell you where the mass of your
distribution actually sits, which is more informative than either extreme.

---

# 12. System Metrics

`/metrics` — pipeline observability. Auto-refreshes every 30 s. Fixed 24-hour
window.

## Detections (last 24 h)

| Metric | Meaning |
|--------|---------|
| **Total detections** | Anomalies raised |
| **True positives** | Confirmed by operators on the Feedback page |
| **False positives** | Rejected by operators |
| **FP rate** | False positives / labelled total |
| **Avg confidence** | Mean confidence score |

True/false positive counts only move when operators label incidents. If they are
both zero, nobody is using the Feedback page — and without labels, active
learning never triggers.

## Pattern distribution

Horizontal bars showing which pattern types dominate (drift, spike, dependency,
lag, saturation, cyclic, cascade — see the
[pattern reference](ANOMALY_CATALOG.md#pattern-classification-what-kind-of-anomaly)).

The shape of this distribution is diagnostic of the *system*, not just the
greenhouse. Everything landing in one bucket usually means a threshold is
mistuned or one sensor is dominating the flagged set.

The two cards query independently, so one failing does not blank the other.

---

# 13. Configuration

`/configuration` — server-side operational configuration for the selected
tenant/greenhouse. **This page changes what gets detected.**

Every change is staged in a local draft; nothing is sent until you confirm. The
save button reads **Save changes** when dirty and **Saved** when clean, and
requires a two-step confirm.

## Crop profile

Select the active crop: **tomato**, **cucumber**, **sweet pepper**, or
**strawberry** (from the agronomic setpoint table).

Changing the crop **loads that profile's day/night bands** into the setpoints
editor below, replacing what is there. The card shows the config source
(tenant-specific override vs default).

## Day / night window

Two fields: **Day starts** and **Day ends**, as hours **UTC** (0–23).

This drives which band — day or night — the crop band detectors apply. Getting
the timezone wrong here is a common source of spurious band breaches: night
setpoints applied during the day will flag constantly.

## CO₂ enrichment

A single checkbox: **CO₂ enrichment equipment installed**.

- **Off** (default) — `co2_band_low` findings are **suppressed**. Ambient outdoor
  CO₂ is ~420 ppm while crop lower bounds start at 600–800 ppm, so a greenhouse
  without enrichment is permanently below band. Reporting it would fire on every
  reading.
- **On** — enables *"CO₂ enrichment underperforming"* detection.

`co2_band_high` (ventilation failure, respiration build-up) is **always**
reported, regardless of this flag.

## Product-specific setpoints

A table of day/night min/max bands per channel:

| Channel | Day min | Day max | Night min | Night max |
|---------|---------|---------|-----------|-----------|

If night values are left blank they inherit the day band (the fields show the day
value as placeholder).

These bands drive all eight
[crop band breach anomalies](ANOMALY_CATALOG.md#part-2--crop-setpoint-band-breaches).
Set them to what your crop actually needs at its current stage, not to the
widest range that avoids alarms — a band tuned to silence is a band that detects
nothing.

## Seasonal mapping

Map each of the twelve months to **Summer** or **Winter**.

Seasonal statistical baselines (mean, σ, median) are computed from historical
telemetry in the mapped months. This lets detectors use different expectations
for a July night and a January night without you maintaining two configurations.

Relevant constants: `SEASONAL_MIN_SAMPLES` (500), `SEASONAL_SCALE_MIN` (0.5),
`SEASONAL_SCALE_MAX` (2.0).

## Saving

The confirmation warns: *"Saving replaces operational setpoints and the season map
for this greenhouse. Detector sensitivity will use the new baselines on the next
evaluation."* Changes apply to future evaluations; historical incidents are not
re-scored.

---

# 14. Upload & Operations

`/operations` — data in, models managed, test data, destructive cleanup.

Four cards.

## Card 1 — Upload sensor data

See [Uploading data](#17-uploading-data) for the full workflow. In brief:

1. Choose a CSV file. Client-side validation runs immediately.
2. Choose what happens after upload:
   - **Find anomalies (no persistence)** — synchronous analysis, nothing stored.
   - **Ingest data** — stores readings; queues a background job.
   - **Ingest data + train model** — stores readings and forces model training.
3. Click **Upload**, then **Confirm upload** in the summary dialog.

Analyze-only returns inline: *"N sequences processed · M anomalies detected"*
with a link to Anomaly Detection. Persist/train returns a job id with a link to
Job Monitoring.

## Card 2 — Model registry

Lists every trained model version for the tenant.

| Element | Meaning |
|---------|---------|
| **Active model** | Currently deployed model id and type |
| Version rows | Model id, type, `active` chip, creation timestamp |
| **Activate** | Promote a version to active |
| **Delete** | Two-step confirm; cannot be undone |

Empty state: *"No trained models for this tenant — the next cold-run upload will
create one."*

If no model is active: *"No active deployment — cold-run will use the global
baseline."* That is a warning worth acting on. The global baseline is generic;
detection quality is materially worse than a tenant-trained model.

## Card 3 — Inject test anomalies

Generates synthetic readings with pre-scored anomaly spikes, for demos and QA.

| Control | Range |
|---------|-------|
| Anomaly type | Random, temperature spike/drop, humidity spike/drop, CO₂ surge, light failure, multi-sensor, gradual drift |
| Anomaly count | 1–50 |
| Background readings | 0–500, in steps of 10 |

Injected rows are **tagged**, so the removal control below deletes them (all, or
by type) without touching real sensor data.

> Always clean up before handing an environment to a customer. In the charts,
> injected rows are indistinguishable from real ones.

## Card 4 — Manage stored data

Two destructive operations, both two-step.

**Remove live demo data** — deletes Go Live / mock SCADA readings only. CSV
uploads and injected test rows survive. The live feed is muted until you turn
Live back on.

**Clear all data** — permanently deletes all sensor readings, synthetic rows and
related incidents for the selected tenant **and greenhouse**. Cannot be undone.

> Check the tenant/greenhouse in the sidebar before either. The confirmation
> dialog names the scope — read it.

After a wipe, live mode is disabled and every cached query is invalidated. The
scope stays *muted* until you explicitly turn Live on, which is what stops a
lingering SSE reconnect from immediately re-seeding the data you just deleted.

---

# 15. Job Monitoring

`/jobs` — background jobs for the current tenant/greenhouse. Active jobs stream
live; the list refreshes every 5 s.

## Job card

| Element | Meaning |
|---------|---------|
| Job id | Monospace identifier |
| Job type | e.g. `upload_csv` |
| Status chip | See below |
| Progress bar | For in-flight jobs |
| Progress text | `12/40 portions · 30.0%` |
| **Terminate** | Cancel an in-flight job |

## Statuses

| Status | Colour | Meaning |
|--------|--------|---------|
| `preparing` | in-flight | File stored, cold-start training may be running |
| `running` | in-flight | Processing portions |
| `completed` | green | Finished successfully |
| `partially_failed` | amber | Finished, but something did not land |
| `failed` | red | Did not complete |

While `total_portions` is still unknown the progress text reads *"Preparing
(cold-start training can take a few minutes)…"*. That is expected: portion count
is only known after the prepare phase finishes, and prepare includes training
when the tenant has no model.

## Transport

Progress arrives over SSE. If the stream is unavailable the card falls back to
polling and appends *"· live stream unavailable, polling"*. Job status is
**never** served from cache — a stale job status is worse than a slow one.

## Failures

- `N portions failed during execution` — partial data loss; check the error.
- `Error: …` — the terminal error message.

`partially_failed` specifically means the job processed data but wrote **zero
rows** to storage despite persistence being requested. It exists so that failure
cannot be silent: an earlier version reported "455 anomalies detected" while the
database stayed empty.

---

# 16. Operator Feedback

`/feedback` — label incidents so the model learns from you. This is the loop that
turns detection into *improving* detection.

## Learning progress

A progress bar toward the next retrain trigger:

> *67 % toward next retrain · 14 high-quality labels · 6 more needed · last
> retrain 2026-07-15*

Retraining triggers when enough high-quality labels accumulate. The gating
constants (`src/config.py`):

| Constant | Default | Meaning |
|----------|---------|---------|
| `ACTIVE_LEARNING_MIN_QUALITY_LABELS` | 20 | Labels needed |
| `ACTIVE_LEARNING_OPERATOR_ACCURACY_THRESHOLD` | 0.85 | Operator reliability required for "high quality" |
| `ACTIVE_LEARNING_MIN_PATTERN_TYPES` | 2 | Distinct pattern types needed |
| `ACTIVE_LEARNING_MIN_RETRAIN_INTERVAL_HOURS` | 4 | Minimum gap between retrains |
| `ACTIVE_LEARNING_FEEDBACK_WINDOW_DAYS` | 30 | Age window for counting labels |

> The **two pattern types** requirement matters: 20 labels all confirming
> temperature spikes teaches the model about temperature spikes and nothing else.
> Diversity in the label set is what makes retraining generalise.

## Review queue

Open incidents, newest first. Each row shows detection time, score chip, fault
type label, and a one-line description (hint summary, reasoning, or
`pattern on sensors`).

Click **Label incident** to expand the verdict form.

## Verdict form

| Field | Notes |
|-------|-------|
| **Verdict** | *True anomaly* or *False positive* — required |
| **Operator ID** | Defaults to `operator_1`; required |
| **Confidence** | Slider 0–1, default 0.85 |
| **Pattern observed** | Optional free text |
| **Root cause** | Optional free text |

Submission is **optimistic** — the row disappears immediately. If the server
rejects it the change is rolled back and you see *"Verdict failed and was rolled
back: …"*.

On success: *"Verdict recorded (accepted) · 67 % toward retrain"*, plus
*"· retraining triggered"* when the verdict crossed the threshold.

## Why operator ID matters

Each operator accumulates a **reliability score** (`ACTIVE_LEARNING_OPERATOR_ALPHA`
0.2, default 0.5, minimum 5 samples before it is trusted). Labels from operators
below `ACTIVE_LEARNING_OPERATOR_ACCURACY_THRESHOLD` count less toward retraining.

Sharing one operator id across a team defeats this: the system can no longer tell
a careful labeller from a careless one, and one person's mistakes drag everyone's
weight down. Use real, distinct ids.

## Empty state

> *"No open incidents to review. Incidents are created when comprehensive
> inference runs on uploaded data."*

If this persists, no data has been analysed through the comprehensive pipeline —
upload a CSV in analyze or persist mode.

---

# 17. Uploading data

## CSV format

### Required columns

The adapter maps your column names automatically using keyword scoring — you do
**not** need to rename anything. Highest-scoring match wins per canonical target.

| Canonical | Recognised patterns (highest score first) |
|-----------|-------------------------------------------|
| `timestamp` | `timestamp` · `time` · `date` (excluding `update`, `created`) |
| `temperature` | `greenhouse temp` · `compartment temp` · `air temp` · `temperature` · `temp`; falls back to plant or outdoor temperature |
| `humidity` | `relative humidity` · `humidity %` · `humidity` · `rh` (all excluding `deficit`) |

`timestamp`, `temperature` and `humidity` are **required**. Rows where either
sensor is NaN are dropped.

### Optional columns

| Canonical | Recognised patterns | Default if absent |
|-----------|--------------------|--------------------|
| `co2` | `co2` · `carbon dioxide` · `co₂` | **400.0 ppm** |
| `light` | `light intensity` · `light w/m` · `lux` · `light` · `illuminance` / `par` / `ppfd` | **500.0 lux** |
| `measured_hd` | `humidity deficit` · `hd (g…)` · `hd` | — |

Substituted defaults are midpoints, not zeros — a zero light default would look
like permanent darkness to every detector.

### Columns that unlock extra detectors

Present them and more anomaly types become available; omit them and those
detectors stay inert:

| Column family | Unlocks |
|---------------|---------|
| Measured humidity deficit | [Thermodynamic contradiction](ANOMALY_CATALOG.md#8-thermodynamic-contradiction-psychrometric) |
| Heating setpoint | [Under-heating](ANOMALY_CATALOG.md#under-heating), [Over-heating](ANOMALY_CATALOG.md#over-heating) |
| Plant / canopy temperature | [Thermal divergence](ANOMALY_CATALOG.md#thermal-divergence) |
| Outdoor temperature, solar radiation | Over-heating's solar exclusion |
| Gas channels (`c2h4`, `no`, `nox`, …) | [Gas anomalies](ANOMALY_CATALOG.md#part-5--gas-anomalies) |

### Format auto-detection

Detected from the first 4 KB:

- **Encoding** — UTF-8, with or without BOM.
- **Delimiter** — `,` or `;`, whichever appears more in the header.
- **Decimal separator** — `.` or `,` (European CSVs with `;` and `1,5` work).

Malformed lines are skipped rather than failing the upload.

### Example

```csv
timestamp,greenhouse_temp,relative_humidity,co2,light_intensity
2026-07-01T00:00:00,21.4,68.2,412,0
2026-07-01T00:05:00,21.3,68.5,415,0
2026-07-01T00:10:00,21.5,68.1,410,2
```

## Pre-upload validation

Runs in the browser the moment you select a file — no round trip.

**Blocking errors:**

| Error | Cause |
|-------|-------|
| *The file is empty.* | Zero bytes |
| *File is too large (N MB). Maximum allowed size is 50 MB.* | Over the cap |
| *The CSV has no data rows (only a header or is completely empty).* | Header only |
| *CSV format error: no sample row matches the header column count.* | Structural mismatch throughout |

**Warnings** (upload still proceeds; the server skips bad rows the same way):

> *"Some rows have inconsistent column counts and will be skipped during
> processing. CSV format error (around line 847): this usually means unquoted
> commas in a text field, mixed comma/semicolon delimiters, or extra columns on
> some rows. Re-export as UTF-8 CSV with comma separators and quote text fields,
> or fix the row and try again."*

Validation samples the first 1 MB and up to 2,000 data rows — enough to catch
systematic problems without reading a 50 MB file in the browser.

## The three upload modes

### Find anomalies (no persistence)

- **Endpoint:** `POST /api/inference/upload` — **synchronous**
- **Stores:** nothing
- **Returns:** sequences processed, anomalies detected, threshold used

Use for a quick assessment of a new dataset. Results appear on Anomaly Detection,
but nothing persists.

> Because it runs inside the HTTP request, this path is only suitable for
> modestly sized files — a large CSV will hit a request timeout. The legacy
> Streamlit dashboard enforced a hard cap of `UPLOAD_SYNC_MAX_ROWS` (20,000
> rows) here; the Next.js path does not, so use one of the queued modes for
> anything large.

> Even in analyze mode, if the tenant has **no active model**, cold-start training
> runs opportunistically so the analysis is not done against a generic baseline.
> Opportunistic means: if it fails, the upload proceeds against the global
> baseline and reports a skip reason rather than erroring.

### Ingest data

- **Endpoint:** `POST /api/inference/upload-async` — **202 Accepted**, queued
- **Stores:** sensor readings + raw readings (all columns)
- **Returns:** job id; follow on Job Monitoring

The file is stored durably, then a prepare message is published to RabbitMQ. The
worker trains (if needed), splits the data into portions
(`UPLOAD_PORTION_ROWS` = 5,000) and processes them.

Cold-start training still happens automatically when the tenant has no active
model — but here failure is **not** opportunistic in the same way, since the whole
point is to build the tenant's data foundation.

### Ingest data + train model

Same as above, but **forces** training even when an active model exists. Training
failures propagate as job errors rather than falling back to the baseline.

Use when you have materially more or better data than the current model saw.

## Step by step

1. **Upload & Operations** → check the tenant/greenhouse in the sidebar.
2. Select your CSV. Validation runs; read any warnings.
3. Pick a mode.
4. Click **Upload**. A summary dialog appears with filename, size, scope and
   mode.
5. Click **Confirm upload**.
6. Analyze mode → results inline. Persist/train → go to **Job Monitoring**.

## Why uploads are portioned

A large CSV cannot be processed inside an HTTP request without hitting timeouts.
The flow is:

```
Browser → API: store file durably (artifact store), create job, publish prepare → 202
Worker  → prepare: train if needed, split into portions, publish each
Worker  → portions: score, persist, update progress
Browser ← SSE: live progress on /jobs
```

The file crosses the API→worker boundary through the artifact store (a database
blob keyed `uploads/<job_id>`), not the filesystem — containers in a typical
deployment share no disk. A redeploy mid-upload no longer loses the work: the job
resumes from its durable state.

---

# 18. Training models

## What gets trained

An **LSTM-VAE** per tenant:

| Parameter | Value |
|-----------|-------|
| Input dimensions | 4 (temperature, humidity, CO₂, light) |
| Sequence length | 20 timesteps |
| Hidden dimensions | [64, 32] |
| Latent dimension | 8 |
| Dropout | 0.2 |
| β (KL weight) | 0.1 |
| Loss | MSE + β·KL |

## The two-pass cold-start bootstrap

The problem: to train on *clean* data you must first know which data is clean —
and you have no model yet. The solution is to clean the data with methods that
need no training, then train on the result.

### Pass 1 — clean the data

Flags rows that fail either check:

- **Psychrometric plausibility** — temperature/humidity/humidity-deficit
  combinations that violate physics (bounded by `BOOTSTRAP_PHYSICS_HD_MAX`).
- **Isolation Forest** — multivariate outliers at
  `BOOTSTRAP_IF_CONTAMINATION` = 0.04.
- **MAD scrub** — spike removal at `BOOTSTRAP_MAD_Z_THRESHOLD`.

Output: a clean CSV and a suspected-anomalies CSV.

| Constant | Default | Meaning |
|----------|---------|---------|
| `BOOTSTRAP_MIN_CLEAN_ROWS` | 100 | Minimum clean rows to proceed |
| `BOOTSTRAP_MIN_IF_ROWS` | 10 | Minimum rows for Isolation Forest |
| `BOOTSTRAP_RESAMPLE_INTERVAL` | `5min` | Resampling cadence |
| `BOOTSTRAP_GAS_NULL_THRESHOLD` | 0.95 | Null fraction above which a gas column is dropped |

### Pass 2 — train and score

Trains a fresh LSTM-VAE on the clean rows (10 epochs, batch 256, lr 1e-3, capped
at `BOOTSTRAP_MAX_TRAIN_SEQUENCES`), then scores **every** row — clean and
anomalous alike. The detection threshold is set at
`BOOTSTRAP_THRESHOLD_PERCENTILE` (99.0) of the resulting score distribution.

Scoring the anomalous rows too is the point: it validates that the model actually
separates them from the clean set.

The checkpoint is persisted to the artifact store and registered as the tenant's
active deployment.

## When training happens

| Situation | Result |
|-----------|--------|
| No active model, any upload mode | Cold-start training runs automatically |
| Active model exists, mode = *Ingest data* | Skipped (`active_model_exists`) |
| Mode = *Ingest data + train model* | Always trains |
| Not enough clean data | Skipped (`insufficient_data`); baseline used |
| Training error, not forced | Skipped (`training_error`); baseline used |
| Training error, forced | Job fails with the error |

## Training from the CLI

```bash
python scripts/train_lstm_vae.py
```

Trains on synthetic or configured data using the `TRAINING_CONFIG` values —
`NUM_EPOCHS` 100, `BATCH_SIZE` 32, `LEARNING_RATE` 1e-3, `WEIGHT_DECAY` 1e-5,
`EARLY_STOPPING_PATIENCE` 10, `VALIDATION_SPLIT` 0.2.

## Training via API

```bash
curl -X POST http://localhost:8000/api/models/{tenant_id}/train
curl http://localhost:8000/api/models/{tenant_id}/training-status
```

## Managing versions

On **Upload & Operations → Model registry**, or via API:

| Endpoint | Purpose |
|----------|---------|
| `GET /api/models/{tenant}` | Active model |
| `GET /api/models/{tenant}/versions` | All versions |
| `POST /api/models/{tenant}/activate` | Promote a version |
| `POST /api/models/{tenant}/rollback` | Roll back |
| `DELETE /api/models/{tenant}/versions/{id}` | Delete one |

**Always keep the previous version** until the new one has run against real
traffic. Rollback is the cheapest recovery; retraining is not.

## Retraining from operator feedback

Once enough high-quality labels accumulate (see
[Operator Feedback](#16-operator-feedback)), retraining triggers automatically.
It is guarded:

- **Promote/rollback** — a new model is only promoted if it improves by at least
  `ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD` (0.05). Otherwise it is rolled back.
- **Catastrophic forgetting protection** — EWC (`EWC_LAMBDA` 0.4) and SI
  (`SI_LAMBDA` 0.4) penalise moving weights that mattered for earlier data.
- **Replay buffer** — `REPLAY_BUFFER_CAPACITY` 1,000 normal sequences plus
  `ANOMALY_BUFFER_CAPACITY` 100 anomalous ones are replayed during retraining.
- **Threshold movement cap** — `ACTIVE_LEARNING_MAX_THRESHOLD_DELTA` 0.15 bounds
  how far one retrain can shift the cutoff.

Together these prevent the failure mode where a model retrained on recent
feedback becomes excellent at last week's fault and blind to everything else.

## Online learning

Continuous incremental adaptation, separate from full retraining:

```bash
curl -X POST http://localhost:8000/api/learning/observe \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"tenant_1","greenhouse_id":"gh_A","sequences":[[...]]}'
```

`ONLINE_LEARNING_RATE` 1e-4, `ONLINE_UPDATE_INTERVAL` 50, `ONLINE_BATCH_SIZE` 32,
gradient clipping at 1.0.

Drift is tracked at `DRIFT_WINDOW_SIZE` 100 with `DRIFT_THRESHOLD` 0.3; check it
with `GET /api/learning/drift`.

---

# 19. First-run checklist

Setting up a new tenant, in order:

1. **Select the tenant and greenhouse** in the sidebar. Everything below is
   scoped to this pair.
2. **Configure the crop** — Configuration → pick the crop, set the day/night
   window in **UTC**, set the CO₂ enrichment flag, review the setpoint bands,
   map the seasons. Save.
   *Do this before uploading.* Band breaches are evaluated against whatever is
   configured at evaluation time.
3. **Upload historical data** — Upload & Operations → *Ingest data + train
   model*. Give it as much clean history as you have; the model is only as good
   as its training window.
4. **Watch the job** — Job Monitoring until `completed`. Prepare includes
   training and can take several minutes.
5. **Verify the model** — Upload & Operations → Model registry should show an
   active model.
6. **Check the data landed** — Monitoring, period *Last 7 days* or wider.
   Presets anchor to the newest stored reading, so historical uploads appear
   without hunting for the right dates.
7. **Tune the threshold** — Thresholds. Look at p90/p95, preview the flagged
   delta, settle on a value.
8. **Review anomalies** — Anomaly Detection. Use grouped Table view to see what
   dominates before drilling into individual rows.
9. **Start labelling** — Operator Feedback, with real per-person operator ids.
10. **Watch pipeline health** — System Metrics, once labels exist.

---

# 20. Troubleshooting

## "No sensor readings in the selected window"

In likelihood order:

1. **Wrong tenant/greenhouse.** Check the sidebar. This is the most common cause.
2. **Window does not cover the data.** Presets anchor to the newest stored
   reading, but a custom window can still land outside it. Try *Last 6 months*.
3. **Nothing was persisted.** *Find anomalies (no persistence)* stores nothing by
   design. Check Job Monitoring for a persist job.
4. **The job failed.** Check Job Monitoring.

## Job stuck in "preparing"

- **The worker is not running.** Start it (`python -m src.workers.main`). The API
  accepts uploads and queues them whether or not a worker exists — nothing
  processes them.
- **RabbitMQ is down.** Check http://localhost:15672.
- **Cold-start training is running.** Legitimate, and can take minutes on a large
  CSV. The card says so.

Stale prepare jobs are swept after `STALE_PREPARE_MINUTES` (45).

## Job "partially_failed"

Data was processed but zero rows reached storage despite persistence being
requested. Check the error on the card. Usually a database constraint or
connection problem — read the message rather than retrying blindly.

## Live mode shows nothing

- The chart auto-fits when the lookback is mostly empty, but give the stream a
  few seconds to produce ticks.
- After *Clear all data* the scope is **muted**. Toggling Live resumes it
  explicitly — that is the intended path back.
- Check `GET /api/live/status` for the source state.

## Anomaly count looks wrong

It is probably right. The header count comes from an exact server-side count over
the full window; the chart rows are downsampled. A count of 2,649 against 35
visible points is normal and expected. Never count chart rows.

## Everything is flagged / nothing is flagged

Go to **Thresholds** and look at the score distribution. If p95 is 0.161 and your
threshold is 0.02, everything flags. If it is 0.5, nothing does. Set the threshold
from the distribution, not from a number you liked elsewhere.

Also check the model: with no active model, cold-run uses the global baseline and
scores are poorly calibrated for your greenhouse.

## Band breaches firing constantly

- **Day/night window is in the wrong timezone.** It is **UTC**. Night setpoints
  applied during the day will breach continuously.
- **Wrong crop profile**, or bands from a different growth stage.
- **CO₂ low breaches** — turn the enrichment flag off if you have no enrichment
  equipment.

## Upload rejected

Read the specific message — validation names the failure. Most common: the file
is over 50 MB, or it has header-only content. For inconsistent-column warnings,
re-export as UTF-8 CSV with comma separators and quoted text fields.

## API unreachable from the UI

The BFF returns `{"detail": "Upstream API unreachable"}` with status 502. Check
that FastAPI is running and that `API_URL` in the frontend environment points at
it.

## Reference

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Liveness |
| `GET /health/db` | Database connectivity |
| `GET /api/live/status` | Live source state |
| `GET /api/jobs` | Job list |
| `GET /docs` | Interactive OpenAPI docs |
