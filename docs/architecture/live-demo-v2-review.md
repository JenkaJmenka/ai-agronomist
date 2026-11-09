# Architecture Review — Live Demo v2 Increment (PRs #36–#38)

> **Historical — point-in-time record.** Written 2026-07-20. Status: record only — a read-only review of the diff `2b5630d..333dbc1`; its findings were addressed in follow-up PRs and the live stack has changed substantially since (see ADR-009). Not maintained against current code.

**Reviewer:** architect · **Date:** 2026-07-20 · **Scope:** diff `2b5630d..HEAD`
(commits `ed09208`/`eeaf021` demo v2, `6c2b5a9`/`b65ce89` compound episodes,
`d57902a`/`333dbc1` wall-clock tick fix). Read-only review; no code modified.

---

## Part 1 — Conformance / consistency review

### Verdict: **NEEDS FIXES** (1 blocking finding, F1)

The backend pipeline is clean: isolation, layering, and config discipline are all
verified. The blocking finding is on the FE side — a channel-discovery sampling
bug that hides the increment's headline feature (new sensor tiles) in the most
common real-world session shape (page load with existing persisted history).

### 1.1 New-channel flow isolation (stream → merge → SSE → FE) — VERIFIED ✅

Traced end-to-end:

- **Source:** `mock_fertigation` (priority 30, 2× interval) emits
  `soil_moisture`/`root_zone_temp` via `FertigationSignalState`
  (`src/live/sources/fertigation.py`) — bounded, finite, clamped to
  `CHANNEL_BOUNDS` in `_clamp_and_sanitize`.
- **Merge:** `ChannelMerger` is channel-name-agnostic; extras merge like any
  channel with provenance in `event.sources`. Per-scope
  `grace_seconds = max(default, max poll interval)` (Phase 7 fix) covers the 2×
  fertigation cadence — slow-channel buckets are not dropped; extras simply
  appear in every other bucket, which the FE tolerates (`getLatestValue` scans
  for the newest numeric value per channel).
- **ML isolation:** `LiveScorer._extract_vector` (`src/live/scoring.py:156`)
  builds the input vector strictly from `CHANNEL_ORDER = SENSOR_NAMES[:INPUT_DIM]`
  (`temperature, humidity, co2, light`); extra keys in `event.channels` are
  ignored on ingest, forward-fill, and staleness tracking. QA's
  `test_vpd_wiring.py` empirically confirms identical scores with/without
  extras. Demo compound episodes filter to `ML_CHANNELS` (`mock.py:381`), so
  they can never touch fertigation channels.
- **Persistence isolation:** `_event_to_row` (`src/live/persistence.py:73`)
  maps only the 4 canonical columns; extras are silently dropped (the Part 2
  gap, by design for this increment) and the incomplete-row policy keys on
  `_CANONICAL_CHANNELS` only — a bucket with extras but missing CO2 is still
  correctly skipped. `test_persistence_extra_channels.py` covers this.
- **SSE → FE:** `LiveReadingEvent.channels` carries extras through the hub
  unchanged; `toSensorRow` spreads them into `SensorRow`; `discoverChannels`
  picks them up dynamically — **but see F1**.

### 1.2 Derived-channel placement (`_attach_derived_vpd`) — RIGHT LAYER, MINOR MOVE RECOMMENDED

Assessment of the three candidate layers:

| Layer | Verdict | Reasoning |
|---|---|---|
| **Merger** | ✗ | The merger is transport/semantics-agnostic (correctly has no channel knowledge, no hub import). Adding VPD there couples time-alignment machinery to channel physics and breaks its single responsibility. |
| **FE** | ✗ | VPD would exist only in the browser: not persisted, not available to future alerting/setpoint checks server-side, and the Magnus formula would be duplicated Python/TS with drift risk. FE already correctly treats vpd as just another discovered channel with a `derived` provenance badge. |
| **Runtime callback** (current) | ✓ | Enrichment belongs after merge, before scoring/publish/persist — exactly where `on_reading` sits. One computation, one wire value; SSE, persistence, and scoring all see the same event. |

**Coupling/testability:** `compute_vpd` is a pure function in `derived.py`
(17 unit tests, good). The **attachment** function, however, lives in
`runtime.py` — runtime now knows channel physics exists. Recommend (non-blocking,
next touch): move `_attach_derived_vpd` into `src/live/derived.py` as a public
`attach_derived_channels(event) -> None` and have the runtime call that single
entry point. When a second derived channel arrives (e.g. dew point), the
runtime callback stays one line and derived.py owns the registry. Note also
that in-place mutation of a validated Pydantic model bypasses the
finite-validator — acceptable today only because `compute_vpd` guarantees a
finite return; the docstring of `attach_derived_channels` should state that
invariant explicitly.

### 1.3 Demo-mode design + threshold calibration — WORKS, BUT THE DEFAULT IS A TRAP (F2)

The compound-episode design is sound: multi-channel same-direction offsets are
the right way to defeat a model that learned inverse couplings, magnitudes are
config-driven (`MOCK_SCADA_DEMO_SPIKE_SIGMA` etc., all env-overridable, no
magic numbers), values stay clamped/finite, and the empirical margin
(min 0.22 across seeds vs 0.05 threshold) is comfortable. The wall-clock tick
fix (`d57902a`) is present in `stream()` — the tick counter now advances in
both clock modes, closing the prod/test divergence that shipped broken.

**The problem:** `LIVE_ANOMALY_THRESHOLD` defaults to `ANOMALY_THRESHOLD`
(**0.8**, the batch/fused-detector scale), while the live path operates at a
noise floor of ~0.017 and demo peaks of ~0.36. Any deployment that forgets
`LIVE_ANOMALY_THRESHOLD=0.05` gets a silently dead live-anomaly feature — no
error, no log, just nothing ever fires. Prod works today only because the env
var is pinned. **Fix now (one line):** give the live path its own calibrated
default (`os.environ.get("LIVE_ANOMALY_THRESHOLD", "0.05")`) with a comment
explaining why it must not inherit the batch threshold.

**The deeper issue (flagged, future work):** live scores are raw LSTM-VAE
reconstruction errors — their scale is *detector-relative*. Every retrain,
EWC update, or per-tenant checkpoint swap silently changes the score
distribution, and any absolute threshold (0.05 included) drifts out of
calibration. Recommended normalization path, in two phases:

- **Phase A — registration-time calibration (per detector):** when a model is
  activated, run it over its validation windows and store score quantiles
  (`{q50, q95, q99, q999}`) in `ModelVersion.metrics_json`. `LiveScorer`
  reads them at detector resolution (it already re-resolves every 300 s) and
  sets `threshold = q99` (env-overridable multiplier). Threshold then tracks
  the detector automatically across retrains.
- **Phase B — rolling-baseline percentile (per scope, runtime):** in
  `LiveScorer`, keep `self._score_baseline: deque(maxlen=N)` (N ≈ 1800 ≈ 1 h
  at 2 s cadence). Per score, compute `pct = rank(score, baseline) / len(baseline)`
  and trigger on `pct ≥ 0.995` **and** `score ≥ 3 × median(baseline)` (the
  absolute-floor guard prevents firing in ultra-quiet windows where the 99.5th
  percentile is still normal noise). Two hygiene rules: (1) do **not** append
  scores to the baseline while `self._in_episode` is true — otherwise a long
  anomaly poisons its own baseline and self-suppresses; (2) warm-up: fall back
  to the Phase-A absolute threshold until the baseline holds ≥ 300 samples.
  Emit both `score` and `score_pct` in `LiveAnomalyEvent` so the FE can show a
  scale-free severity. State is in-memory (acceptable: single-worker
  constraint already applies to the live runtime); persisting baseline
  quantiles to `CustomerModelConfig` on shutdown is an optional later step.

### 1.4 BE↔FE contract parity — CONSISTENT, ONE TUNING NOTE (F4)

Field-by-field, `CHANNEL_BOUNDS` (`src/live/events.py`, hard physical clamps)
vs `CHANNEL_META` (`web/.../channelMeta.ts`, optimal/alarm ranges). These are
*different semantics* (bounds ⊃ optimal), which is correct; parity check is
units + containment + mock-signal fit:

| Channel | BE bounds | BE unit | FE optimal | FE unit | Contained | Mock signal fit |
|---|---|---|---|---|---|---|
| temperature | (0, 50) | °C | [20, 28] | °C | ✓ | ✓ base 22.5–27.5 ±0.5σ |
| humidity | (0, 100) | % | [55, 75] | % | ✓ | ⚠ base 57.5–72.5 ± coupling (±1.5) ± 1.0σ — can graze 55 |
| co2 | (0, 1000) | ppm | [350, 500] | ppm | ✓ | ⚠ midday dip bottoms at ~350 ± 5σ — ~half of midday ticks < 350 → routine "warning" flicker with no anomaly (F4) |
| light | (0, 2000) | lux | [0, 1000] | lux | ✓ | ✓ peak 400 ± 50σ; below-min never alarmed (night rule, correctly special-cased) |
| soil_moisture | (0, 100) | % | [60, 85] | % | ✓ | ✓ sawtooth 62→85 (mock floor 55 reachable only via episode — correct) |
| root_zone_temp | (0, 45) | °C | [18, 24] | °C | ✓ | ✓ 21 ± 2 ± 0.15σ |
| vpd | (0, 4) | kPa | [0.5, 1.5] | kPa | ✓ | ✓ ~0.75 (night) – ~1.3 (midday) |

**Setpoint semantics vs status rule:** setpoints are an FE-only construct
(localStorage per tenant/gh, zod-validated on read — good) that *override* the
`optimal` defaults; `getChannelStatus` widens the range by 10 % of span for
warning-vs-critical. Semantics are coherent: tiles and the single-channel chart
band both read the same effective `setpoints` object, so display and alarm
logic can't diverge. Two notes: the light below-min exemption is keyed on the
hard-coded string `"light"` rather than a meta flag (`alarmBelowMin: false`
would be cleaner and survive a second light-like channel); and setpoints on a
derived channel (vpd) are semantically "alarm ranges", not controllable
setpoints — harmless, but the panel title "Optimal setpoints" mildly
overpromises for that row.

### 1.5 Prod/test mode divergence hunt

- **Pinned clock vs wall clock:** the class of bug that bit this increment
  (`_tick_count` frozen in wall-clock mode → demo episodes never fired in
  prod) is fixed in-diff and commented at the mutation site. Verified: demo
  scheduling is tick-based and ticks now advance in both modes; the
  seconds→ticks conversion in `_schedule_demo_episode` uses the same
  `poll_interval_seconds` in both modes. No residual clock-mode divergence
  found in the demo path.
- **Demo flag defaults:** `MOCK_SCADA_DEMO_MODE` defaults **True** in config,
  but `MockScadaSource` reads `params.get("demo_mode", False)` — tests that
  construct `SourceConfig` directly get demo **off** while prod (via
  `build_mock_sources_for_scope`) gets demo **on**. QA covers demo explicitly,
  so this is acceptable, but it is exactly the shape of divergence to watch.
  Related hygiene flag (F6): with `MOCK_SCADA_ENABLED`, `MOCK_SCADA_DEMO_MODE`,
  and `LIVE_PERSIST_ENABLED` all defaulting True, **synthetic forced anomalies
  are persisted into `sensor_readings` with scores** on any default-config
  deployment. If that table ever feeds retraining/online learning for a real
  tenant, demo data contaminates training. Document that
  `MOCK_SCADA_DEMO_MODE`/`MOCK_SCADA_ENABLED` must be off for real-tenant
  scopes, or scope mock persistence to demo tenants only.
- **localStorage SSR (F3):** `useSetpoints` initializes state with
  `useState(() => loadOverrides(storageKey))`. On the server this returns `{}`;
  during client hydration the initializer reads real localStorage. When a user
  has saved overrides, the hydration render differs from server HTML (tile
  status classes, panel input values) → React hydration mismatch. Fix: initialize
  to `{}` and rely on the already-present `useEffect(... [storageKey])` to load
  overrides post-mount (it already runs on mount; the fix is one line).
- **FE staleness / e2e:** e2e mocks all BFF routes per convention — no live
  divergence introduced. `STALE_THRESHOLD_MS = 30 s = 2× heartbeat` is
  consistent with `LIVE_HEARTBEAT_SECONDS`.

### Findings table

| ID | Severity | Location | Issue | Recommendation |
|---|---|---|---|---|
| F1 | **HIGH (blocking)** | `web/features/monitoring/api/readings.ts:75` (`discoverChannels`) | Channel discovery samples only the **first 50** rows of the chronologically-ascending merged array. In live mode, historical rows (canonical-4 only, since extras aren't persisted) sort before live rows — so after ~100 s of persisted history, `soil_moisture`/`root_zone_temp`/`vpd` are **never discovered**: no tiles, no chart series, no setpoint rows. The increment's headline feature disappears on any page load with existing history. | Sample the **newest** 50 rows (`rows.slice(-50)`) — new channels only exist in recent data by construction — or scan all rows with an early-exit set. One-line fix + unit test with 100 canonical rows followed by extras-bearing rows. Part 2 persistence reduces but does not remove the exposure (channels added later will always hit this again). |
| F2 | MEDIUM | `src/config.py:975` | `LIVE_ANOMALY_THRESHOLD` defaults to batch `ANOMALY_THRESHOLD` (0.8) — 16× the live operating point (noise ~0.017, demo peaks ~0.36). Without the prod env override the live anomaly feature is silently dead. | Own calibrated default `"0.05"` for the live path (§1.3). Longer term: detector-relative normalization — registration-time quantiles in `ModelVersion.metrics_json` (Phase A), rolling-baseline percentile in `LiveScorer` (Phase B), per the concrete sketch in §1.3. |
| F3 | MEDIUM | `web/features/monitoring/hooks/useSetpoints.ts:95` | `useState` initializer reads localStorage → server/client hydration mismatch whenever overrides exist. | Initialize to `{}`; the existing `useEffect` on `[storageKey]` already loads overrides on mount. |
| F4 | LOW | `web/.../channelMeta.ts:34` vs `src/config.py` CO2 signal | Mock CO2 midday dip bottoms exactly at the FE optimal min (350) → routine warning flicker on the CO2 tile with no anomaly present, diluting the status colours' meaning during demos. Humidity can similarly graze its 55 floor via temp coupling. | Co-design: either widen FE co2 optimal to [330, 520] (and humidity to [53, 77]) or lift the mock CO2 dip floor. Prefer the FE change — mock ranges are shared with training data. |
| F5 | LOW | `src/live/sources/mock.py:291` | `_NOISE_STD.get(channel, 1.0)` fallback means random episodes on fertigation channels use absolute-unit magnitudes: a root_zone_temp "5–8σ" spike is really 33–53σ (its noise std is 0.15). Cosmetic realism only. | Add `soil_moisture`/`root_zone_temp` entries to `_NOISE_STD` (source the values from `fertigation.py` constants to keep config in one place). |
| F6 | LOW | `src/config.py` demo/persist defaults | Default-on demo mode + default-on persistence writes forced synthetic anomalies into `sensor_readings` — training-data contamination risk if a real tenant's history feeds retraining. | Document the constraint in `docs/live_scada_connectors.md` §8; consider tagging live-persisted demo rows via the existing `anomaly_type` column (e.g. `"demo_compound"`) so training pipelines can exclude them. |
| F7 | INFO | `src/live/runtime.py:60` | `_attach_derived_vpd` lives in runtime.py — runtime carries channel-physics knowledge; in-place mutation bypasses Pydantic finite validation (safe only via `compute_vpd`'s guarantee). | Move to `src/live/derived.py` as `attach_derived_channels(event)`; document the finite-return invariant. Not urgent; do on next touch of either file. |

Blocking: **F1** (1). F2 is one line and should ride along but is not blocking
(prod env pins the value today).

---

## Part 2 — Extra-channel persistence design

### Problem restatement

`sensor_readings` has fixed columns for the canonical 4. `soil_moisture`,
`root_zone_temp`, `vpd` exist only in the live stream (Redis replay window,
`LIVE_HUB_*`): on page reload the historical fetch returns canonical-only rows
and extra-channel history is gone (and feeds F1 above).

### Options evaluated

**(a) Write extra channels as `RawReading` rows from `PersistenceBatcher` — RECOMMENDED (with one critical shape decision, below).**

- Schema change: **none.** `raw_readings` already has JSON `data`, `source`,
  and the exact conflict key we need:
  `uq_raw_readings_tenant_greenhouse_timestamp (tenant_id, greenhouse_id, timestamp)`
  (verified in `src/db/models.py:407-415`). The dialect-aware
  `ON CONFLICT DO NOTHING` pattern already exists in `persistence.py`.
- Read path: **already built.** `fetch_merged_readings`
  (`readings_service.py:132`) merges `RawReading` rows with canonical anomaly
  scores, and the FE discovers channels dynamically from row keys — extras
  surface with zero read-path changes. This is precisely the path the CSV
  upload flow uses today; live persistence simply adopts the same dual-table
  contract.
- **Critical shape decision — write the FULL channel dict, not extras-only.**
  `merge_anomaly_scores_into_readings` attaches *only* `anomaly_score` from
  the canonical table onto raw rows; when `raw_df` is non-empty, the merged
  output rows are the **raw rows**. If we wrote raw rows containing only
  extras, the monitoring view for live data would return rows *without*
  temperature/humidity/co2/light — breaking the main chart. Therefore the raw
  row's `data` must be the complete `event.channels` (canonical + extras +
  vpd), mirroring upload-path full fidelity.

**(b) JSON `extra_channels` column on `SensorReading`** — rejected. Requires an
Alembic migration on prod Postgres, touches every consumer of
`_canonical_records_to_rows` to unpack the JSON, and creates a second,
different channel-extension mechanism alongside the `RawReading` one the read
path already merges. More moving parts for the same outcome as (a).

**(c) Dedicated narrow table `(tenant_id, greenhouse_id, channel, ts, value)`** —
rejected for this increment. Cleanest long-term for arbitrary high-cardinality
channels, but: new model + migration, a pivot in the read path (new query +
merge logic), 3× the row count of (a) (~130 k rows/day/scope at 3 extras), and
the FE gains nothing it doesn't get from (a). Revisit only if channel count
grows past ~15 or per-channel retention policies are needed.

### VPD: persist or recompute on read? — **PERSIST.**

Persist the vpd value that was streamed. Rationale: (1) the wire value and the
historical value must be identical — recompute-on-read reintroduces a second
implementation site (read layer) for the Magnus formula and diverges the moment
either changes; (2) recompute requires both temp and humidity present in the
same row — a bucket where vpd was computed live but a canonical channel later
fails the row's completeness path would lose it; (3) storage cost is ~15 bytes
per row — negligible; (4) it is already in `event.channels` at persist time, so
persisting is the *simpler* code. The FE already renders provenance from
`CHANNEL_META.vpd.provenance = "derived"` — no change needed.

### Implementation spec for backend-engineer (no further design decisions needed)

1. **`src/live/persistence.py`** — add alongside `_event_to_row`:

   - `_event_to_raw_row(event: LiveReadingEvent) -> Optional[Dict[str, Any]]`:
     returns `None` if `sanitize_channels(event.channels)` is empty; else
     `{"tenant_id": ..., "greenhouse_id": ..., "timestamp": event.timestamp,
     "source": "live", "data": sanitize_channels(event.channels)}`.
     **NaN guard is mandatory here** — `data` is a JSON column and the known
     SQLite/Postgres NaN gap means a non-finite float that passes locally will
     500 on prod Postgres. `sanitize_channels` (strips, does not 0-fill) is the
     correct guard; do NOT use the `.get(k, 0.0)` fallback pattern for the JSON
     dict.
   - No completeness requirement for raw rows (unlike `_event_to_row`):
     `RawReading` has no non-nullable channel columns; partial buckets are
     valid full-fidelity records. The canonical skip-incomplete policy is
     unchanged.
   - In `_flush()`: build both row lists in the same drain pass; execute two
     statements in the **same session/transaction** (canonical insert as
     today, then `insert(RawReading).values(raw_rows).on_conflict_do_nothing(
     index_elements=["tenant_id", "greenhouse_id", "timestamp"])`). On
     exception the existing whole-batch requeue path covers both tables
     (idempotent thanks to both unique constraints).
   - Module docstring: extend the persistence-policy section with the
     dual-table contract and the "full dict, not extras-only" rationale.

2. **Conflict semantics note:** if a tenant uploads a CSV whose timestamps
   collide with live raw rows, first-writer-wins (DO NOTHING) — same behavior
   the canonical table has today. Acceptable; document in the docstring.

3. **Write amplification (measured against 1 reading/2 s/scope):**
   43,200 events/day/scope → 43.2 k rows/day added to `raw_readings`
   (in addition to the existing 43.2 k/day in `sensor_readings`). At ~7
   channels the JSON payload is ~200 B/row → ~8.6 MB/day/scope. Flushes stay
   batched (`LIVE_PERSIST_FLUSH_SECONDS = 10` → ~5 rows/table/flush, one
   round-trip each). No index changes needed
   (`ix_raw_readings_tenant_timestamp` exists). Retention/downsampling is
   explicitly out of scope; flag for a future increment when a scope passes
   ~90 days (~4 M rows).

4. **Read-path verification (no code change expected, must be tested):**
   after this lands, `fetch_merged_readings` returns raw-backed rows for live
   data. Two behaviors to pin with tests:
   - Extras appear in `/api/readings/*` responses and FE discovery
     (after the F1 fix, from the newest rows).
   - **Transition seam:** live rows persisted *before* this increment exist
     only in `sensor_readings`; once raw rows exist for a scope, the merge
     starts from `raw_df` and canonical-only timestamps drop out of the merged
     window. For demo scopes this history is disposable — the accepted
     mitigation is a one-time optional backfill script (INSERT INTO
     raw_readings SELECT ... FROM sensor_readings with the 4 canonical
     channels as `data`, `source='live-backfill'`, ON CONFLICT DO NOTHING).
     Ship the backfill as a standalone script under `scripts/`; do not run it
     automatically.

5. **Tests (qa-engineer follow-up):** raw row written with full channel dict
   incl. vpd; extras-only bucket persists raw but skips canonical; empty/
   all-NaN channels → no raw row; duplicate timestamp no-op on both tables;
   both-table write in one flush survives a mid-flush failure (requeue);
   `fetch_merged_readings` surfaces soil_moisture/root_zone_temp/vpd with
   scores attached; non-finite value never reaches the JSON column
   (Postgres-representative test via the existing NaN-gap guard tests).

6. **Explicitly out of scope:** no Alembic migration, no new models, no
   changes to `readings_service.py`, no FE changes beyond the F1 fix (which is
   Part 1's finding, not part of this design).

---

*Prepared by the architect agent. Companion signal:
`tasks/done/live-p10-architect.json`.*

Other half of this co-review: [live-demo-v2-review-techlead.md](live-demo-v2-review-techlead.md).
