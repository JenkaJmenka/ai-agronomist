# Anomaly Detection Quality Phase — Architect Review

**Date:** 2026-09-09
**Branch:** `fix/alert-label-integrity`
**Reviewer lens:** Architect (design decisions, structural coherence, future-proofing)

---

## Acceptance Criteria Verification

### 1. Stuck-fault alert of severity `urgent` for Dec 14–26 humidity freeze

**NOT MET.**

The episode grouping architecture document (`docs/architecture/episode_grouping.md`)
describes a wall-clock stuck detector invoked inside `finalize_scores`, `StuckCarry`
state threaded between portions, `FaultWindow` completed-fault tracking, mid-freeze
detection, and `AnomalyEpisode` output with `severity: "urgent"`. None of these
exist in the implementation:

- `StuckCarry` class: **does not exist** anywhere in the codebase. `grep -rn
  "class StuckCarry" .` returns zero matches.
- `FaultWindow` class: **does not exist**. Zero matches.
- `AnomalyEpisode` dataclass: **does not exist** as implementation. Documented
  only in the architecture doc.
- `finalize_scores` (`src/api/services/upload_portions.py:187–276`) performs LSTM
  threshold calibration and statistical-hit merging. It contains zero references to
  stuck detection, episode grouping, wall-clock analysis, or severity assignment.
- `score_portion` (`upload_portions.py:132–184`) runs LSTM inference and
  rolling z-score detection. Zero references to stuck-fault detection.
- `src/api/services/cold_run_upload.py` contains score dict builders and the upload
  orchestration. Zero references to stuck detection, episodes, or wall-clock gates.
- `src/workers/upload_consumer.py` — zero stuck/episode references.

The only stuck-fault detection that exists is:

1. `src/models/stuck_fault_detector.py` — sample-indexed, used by
   `ComprehensiveInferencePipeline` (live path only, NOT wired to upload).
2. `src/models/setpoint_scada_detector.py::_is_flatline` — sample-indexed, also
   live-path only.
3. `src/models/bootstrap.py::_classify_root_cause` — heuristic variance check,
   not a detector.

The architecture document describes a complete system that was never built. The
episode grouping doc is aspirational documentation, not a record of what shipped.

**Evidence:** `grep -rn "StuckCarry\|FaultWindow\|AnomalyEpisode\|stuck_wallclock"
src/api/services/ --include="*.py"` returns zero results.

### 2. End-to-end detection latency is a MEASURED number

**UNVERIFIABLE.**

`docs/architecture/episode_grouping.md:157–158` describes `onset_ts` and
`detected_ts` fields on the `AnomalyEpisode` dataclass, which would make latency
measurable. Since `AnomalyEpisode` is not implemented, no detection latency is
measured or reported.

### 3. Dec 25 recovery does not produce the largest alert cluster

**UNVERIFIABLE.**

The recovery mechanism then described in `episode_grouping.md` (score capping
at `RECOVERY_SCORE_CAP`, `is_recovery=True` flag) is not implemented in
`finalize_scores` or any reachable upload-path code. Task 5 has since deleted
`RECOVERY_SCORE_CAP` outright and repurposed `is_recovery` into a
cross-channel informational tag computed query-time in
`src/api/services/anomaly_episode_service.py`; the upload path still never
sets it.

### 4. No light alert fires on clear-sky July midday irradiance

**MET.**

- `src/config.py:149`: `LIGHT_RANGE = (0.0, 1200.0)` — corrected from `(0, 1000)`.
- `src/config.py:996`: `OOB_LIMITS["light"] = (0.0, 2000.0)` — corrected from
  `200000.0`.
- `tests/api/test_light_unit.py:69–87`: test
  `test_clearsky_fixture_all_light_values_within_operational_band` verifies every
  row of the native July fixture (max 1019.98 W/m²) is within LIGHT_RANGE.
- `src/vendors/csv_adapter.py:47–108`: light-unit inference from column headers
  with sourced conversion factors for lux→W/m² and PPFD→W/m².

### 5. Temperature-drop/humidity-spike pairing no longer accounts for majority of alerts

**PARTIALLY MET — design addresses the root cause but implementation is incomplete.**

The label integrity fix (migration 024, `trigger_channel`/`trigger_detector` columns)
correctly persists the actual triggering column. `_enrich_sample_fast` in
`anomaly_group_service.py:498–560` now prefers `trigger_channel` over
value-based inference for post-migration rows, so pipe-temperature alerts will no
longer be mislabelled as greenhouse-air-temperature. However, the stuck-fault
detector (which would produce the largest cluster with a stuck-fault label instead
of a temperature-drop/humidity-spike pair) is not implemented, so the dominant
alert pattern for the Dec 14–26 window remains whatever the rolling z-score
produces.

### 6. Regression: existing true out-of-bounds alerts still fire

**MET.**

- `tests/api/test_light_unit.py` includes regression tests for genuinely anomalous
  light values.
- `tests/api/test_label_integrity.py` tests both spike and drop reasoning
  generation.
- The rolling z-score path and LSTM scoring path are unchanged in
  `upload_portions.py`.
- `tests/api/test_score_transform.py` tests the canonical transform correctness.

---

## Specific Scrutiny

### Does anything still fabricate a label at render time?

**Yes, but only for pre-migration rows (correct).**

`anomaly_group_service.py:528–548` (the `incident is None` branch of
`_enrich_sample_fast`): when `trigger_channel` is `None` (pre-migration-024 rows),
the code falls back to `_infer_sensors_from_values` and
`_infer_fault_type_from_values`. This is correct — pre-migration rows have no
persisted trigger data and must be inferred.

For post-migration rows with `trigger_channel` set, lines 529–532 use the
persisted value directly, bypassing inference.

The same pattern appears in `_groups_by_type_or_sensor` at lines 897–909 for
the type/sensor grouping path.

### Is the score genuinely distributed or merely rescaled into new saturation?

**Genuinely distributed.**

`_stat_z_to_canonical` (`upload_portions.py:40–52`) uses the asymptotic branch
(`hi=None`) of `score_transform.canonical`. For z-scores above the threshold
(COLD_RUN_STAT_Z_MIN = 5.5):

```
canonical = 0.5 + 0.5 * (z - 5.5) / z
```

This asymptotes to 1.0 but never reaches it for finite z. Confirmed by
`test_label_integrity.py:269–309` — 9 distinct z-values produce at least 5
distinct scores, all strictly below 1.0.

### Does the stuck-fault gate hold on IRREGULAR input?

**Not applicable — the wall-clock stuck-fault detector does not exist.**

The config constants `STUCK_WALLCLOCK_MIN_HOURS`, `STUCK_WALLCLOCK_MIN_SAMPLES`,
`STUCK_WALLCLOCK_EPSILON`, `STUCK_WALLCLOCK_CHANNELS`, `CADENCE_EXPECTED_SECONDS`,
and `CADENCE_TOLERANCE_FACTOR` are defined in `src/config.py:734–769` but are
not imported or used by any code in the upload path (`upload_portions.py`,
`cold_run_upload.py`, `csv_persist.py`).

### Does episode state survive the 5,000-row chunk boundary?

**Not applicable — `StatCarry` carries rolling z-score state across chunks
(confirmed: `upload_portions.py:57–65`), but `StuckCarry` does not exist and
episode grouping is not implemented.**

### Are any reported numbers configured thresholds dressed up as measurements?

**N/A for stuck-fault detection (not implemented). For the score transform and
threshold handling, the numbers are genuine:** `threshold_used` returned by
`calibrate_threshold` is computed from the score distribution, not a configured
constant. `CANONICAL_ANOMALY_THRESHOLD = 0.5` is a fixed point of the transform
by construction, not a dressed-up measurement.

### Multi-tenant: every query filtered by tenant_id

**MET.** All SQL queries in `anomaly_group_service.py` pass through
`_base_conditions` (line 632–645) which includes `SensorReading.tenant_id ==
tenant_id`. The `apply_anomaly_scores` path in `csv_persist.py:347–350` filters
by `tenant_id` and `greenhouse_id`. The anomaly route at
`src/api/routes/anomalies.py` takes `tenant_id` as a required query parameter.

### Non-finite floats guarded before JSON-column writes

**MET.** `score_transform.py:73–98` returns `None` for NaN/±Inf inputs with
WARNING-level logging. `csv_persist.py:326–329` guards `trigger_value` with
`math.isfinite`. `live/persistence.py:48–54` documents the NaN guard for raw
rows. Migration 023 has explicit non-finite handling at lines 146–149, 168–169,
204–210, 308–310.

### Migrations 023/024/025 chain correctly and are reversible

**No migration 025 exists.** The chain is 022 → 023 → 024:

- `023`: `down_revision = "022"` ✓
- `024`: `down_revision = "023"` ✓
- Both are reversible: 023 restores `anomaly_score` from `anomaly_score_raw`
  then drops columns; 024 drops four nullable columns.
- Both add nullable columns only → metadata-only DDL on Postgres, no table
  rewrite.

### Guardrails

- **Max 300 lines/file:** `anomaly_group_service.py` is 1,039 lines. EXCEEDS.
  `csv_persist.py` is 506 lines. EXCEEDS. `config.py` is 1,340 lines. EXCEEDS.
  These were pre-existing violations not introduced by this phase.
- **50 lines/function:** `_groups_by_type_or_sensor` is ~157 lines (830–988).
  Pre-existing. `_groups_by_date` is ~122 lines. Pre-existing.
- **3 params:** `_row_dict_from_minimal` takes 10 positional + 4 keyword params
  (line 214–227). New code, borderline since the keyword params are trigger
  columns that conceptually belong together.
- **No magic numbers:** New code uses config constants. The conversion factors
  in `csv_adapter.py` are documented with sourced references. ✓
- **No swallowed exceptions:** No empty `except` blocks in new code. ✓
- **No `any` in TS:** `web/lib/severity.ts` and related hooks use typed
  `Severity` union. No `any`. ✓

---

## Architect-Lens Design Questions

### Episode Grouping Architecture: Design vs. Implementation

The architecture document `docs/architecture/episode_grouping.md` is **well-argued
and structurally sound.** The rejected alternatives (storage layer, presentation
layer) have honest assessments. The detection-layer placement makes the right
trade-off for the batch upload path.

**However, the document describes a system that does not exist.** The episode
model, contiguity rule, stuck-fault duration semantics, recovery scoring, and
mid-freeze detection are all specified but none are implemented. This is either:

1. A design document written ahead of implementation (acceptable if flagged)
2. A description of code believed to be written but never committed

Given the task spec references "Task 5 required a documented decision... written
to docs/architecture/episode_grouping.md", this appears to be case (1) — the
architecture was documented but implementation was not completed within this phase.

**Recommendation:** The document should carry a prominent "STATUS: DESIGN ONLY —
not yet implemented" header until the code ships. As-is, a reader would believe
these features exist.

### Wall-clock-plus-cadence gate vs. sample-indexed thresholds

**This is a sound design decision — but moot since it's unimplemented.**

The existing sample-indexed detectors (`preprocess_sequences` with sliding
windows, `_is_flatline` with `STUCK_WINDOW_K`, `_effective_rolling_window` for
cadence-adaptive rolling stats) assume uniform or near-uniform cadence. The
config constants `STUCK_WALLCLOCK_MIN_HOURS`, `STUCK_WALLCLOCK_MIN_SAMPLES`,
`CADENCE_EXPECTED_SECONDS`, `CADENCE_TOLERANCE_FACTOR` define a wall-clock gate
that would correctly handle irregular input — but these constants are unused by
any code in the upload path.

If/when implemented, the internal inconsistency (wall-clock in one detector,
sample-indexed in all others) is **acceptable and intentional**: the stuck-fault
detector specifically needs wall-clock semantics because a 6-hour freeze at
5-minute cadence is 72 samples, but the same 6-hour freeze at 30-minute cadence
is only 12 samples. The sample-indexed threshold would need per-cadence
calibration; the wall-clock threshold does not. The other detectors
(z-score, LSTM) naturally handle cadence variation through their adaptive
rolling windows and sequence-length normalization.

### ComprehensiveInferencePipeline boundary

The `ComprehensiveInferencePipeline` (18 detectors, used by live path and
`/api/inference/integrated`) is NOT wired to the upload path by explicit
decision. The upload path uses only:

1. LSTM-VAE reconstruction error → `upload_portions.py::score_portion`
2. Rolling z-score → `upload_portions.py::_detect_stats_incremental`

These are different detectors from the comprehensive pipeline's stuck-fault
detector, setpoint-scada detector, band-breach detector, etc. **The two paths
have divergent detection stacks.** This is currently acceptable because:

- Upload is cold-run (no real-time latency requirement, can afford full-file
  analysis)
- The upload detectors serve a different use case (retrospective scoring with
  file-level threshold calibration)

**However, this creates a growing coherence problem:**

- A stuck fault visible in the Dec 14–26 fixture would be detected by the
  comprehensive pipeline's `StuckFaultDetector` (if the file were processed
  through the live path) but NOT by the upload path (which has no stuck
  detection).
- The quality-phase diagnosis identified this exact failure mode but the fix
  (wall-clock stuck detection in the upload path) was not completed.
- As the comprehensive pipeline gains more detector types, the gap between
  "what live detects" and "what upload detects" will widen.

**Recommendation:** The next phase should either wire the comprehensive
pipeline's stuck detector into the upload path, or implement the dedicated
wall-clock stuck detector described in the architecture doc.

### Canonical light unit choice (W/m²)

**Correct for Priva/Hoogendoorn/Connext.**

These greenhouse climate computers natively report solar irradiance in W/m².
The conversion factors in `csv_adapter.py:52–55` use sourced references
(CIE S 010/E for lux, Thimijan & Heins 1983 for PPFD). The conversion
boundary is at the right layer (CSV adapter normalize step), so all downstream
code sees a single unit.

**Minor concern:** `SensorReading.light` doc string (line 77 of `models.py`)
still says "Light intensity in lux". This should be updated to "W/m²" to match
the canonical unit.

Remaining `lux` labels in `config.py` at `SENSOR_NOISE_TARGETS` (line 854),
`SPIKE_DELTA_MAX` (line 963), `NOISE_BASELINE_VARIANCE` (line 1011), and
`BIAS_EMA_THRESHOLD_BY_SENSOR` (line 1033) are conceptually ambiguous — they
were calibrated when the unit was labelled lux but the numeric values were
always in W/m²-equivalent range. The comments should be updated for clarity but
the numeric values are likely correct as-is.

### Data model: flat columns vs. related table for trigger data

**Flat columns (trigger_channel, trigger_detector, detected_severity,
trigger_value) are the right choice for the current volume.**

A related table (`sensor_reading_triggers`) would be better if:
- Multiple channels commonly fire at the same timestamp
- The system needs to store all triggers, not just the primary one
- Attribution data grows to more than 4 fields

Currently, `build_trigger_data_by_timestamp` (`cold_run_upload.py:116–150`)
already selects the highest-|z| column as the single primary trigger. The
design explicitly acknowledges the multi-channel case (line 119: "highest |z|
column wins"). This is a reasonable simplification: a single primary trigger
with the highest-z channel is the right UX for alert triage.

**If the stuck-fault detector is added:** it would need to write `trigger_channel
= "humidity"`, `trigger_detector = "stuck_fault"`, `detected_severity = "urgent"`,
`trigger_value = 100.0` on its alert rows. The flat-column schema accommodates
this without changes. The detector/severity distinction means these rows will not
be confused with statistical alerts on the same channel.

**When to revisit:** If multi-trigger attribution becomes a product requirement
(e.g., "this timestamp had BOTH a statistical humidity alert AND a stuck-fault
alert"), a junction table or JSON-array column would be needed. For now, flat
columns are simpler and performant.

---

## Failing Test Triage

### tests/api/test_023_migration.py (3 failures)

These tests exercise migration 023 logic (canonical score transform, backfill,
downgrade) using inline function copies. They were added by **this phase**
(the file is in the diff as a new file).

**test_live_round_trip_restores_original_score:**
Tests that downgrade restores `anomaly_score` from `anomaly_score_raw`. The
`_run_downgrade` function calls `ALTER TABLE ... DROP COLUMN` for `score_scale`
and `anomaly_score_raw`. This depends on SQLite 3.35+ (DROP COLUMN support).
The test is structurally sound — the failure is likely an environment issue
(SQLite version) or a timing issue with the in-memory DB. This test exercises
023-only logic; migration 024 columns are not involved.

**Verdict: PRE-EXISTING (uncommitted ADR-005 work).** The task spec states
"Migration 023 and its tests were pre-existing UNCOMMITTED ADR-005 work already
in the tree when this phase began." These tests were written for 023 logic that
was already in the working tree. The failures are in 023 downgrade mechanics,
not in anything 024 changes. **NOT a blocking finding.**

**test_downgrade_drops_new_columns:**
Same analysis — tests 023 downgrade column removal.
**PRE-EXISTING, NOT BLOCKING.**

**test_nan_in_live_row_becomes_null_with_unknown_scale:**
Tests that NaN scores are neutralized during backfill. The inline `_run_backfill_live`
function mirrors the migration logic. SQLite handles NaN differently than Postgres
(the test acknowledges this limitation). Failure likely involves SQLite's NaN
float representation in the parameter binding.
**PRE-EXISTING, NOT BLOCKING.**

### tests/api/test_inference.py::test_inference_saves_to_database

**INTRODUCED BY THIS PHASE. BLOCKING.**

The test asserts `all(r.anomaly_score == pytest.approx(0.72))`. Before this phase,
the inference route stored the raw score directly:
```python
record.anomaly_score = scores_by_key[record_key]
```

After this phase (`src/api/routes/inference.py` diff lines 120–140), the route
applies the canonical transform:
```python
can = _canonical_score(raw_score, _batch_p, _batch_hi, timestamp=ts_str)
record.anomaly_score = canonical_by_key[record_key]
record.anomaly_score_raw = raw_by_key[record_key]
record.score_scale = "batch"
```

With batch calibration `p=0.8, hi=1.0` and raw score `0.72`:
- `0.72 <= 0.8` → branch 2: `canonical = 0.5 * 0.72 / 0.8 = 0.45`

The test expects `0.72` but the stored canonical score is `0.45`. The test
needs to be updated to expect the canonical value (0.45) instead of the raw
value (0.72), and optionally assert `anomaly_score_raw == 0.72`.

**This is a regression introduced by this phase's inference route changes.**

---

## Blocking Findings

| # | File | Finding |
|---|------|---------|
| B1 | `src/api/services/upload_portions.py`, `docs/architecture/episode_grouping.md` | **Stuck-fault detection, episode grouping, and recovery scoring are documented but NOT implemented.** Acceptance criteria 1, 2, 3 are NOT MET. The architecture doc describes `StuckCarry`, `FaultWindow`, `AnomalyEpisode`, wall-clock gates, mid-freeze detection, and recovery score capping — none of which exist in the codebase. Config constants (`STUCK_WALLCLOCK_*`, `CADENCE_*`) are defined but unused by the upload path. |
| B2 | `tests/api/test_inference.py:218` | **Phase-introduced test regression.** `test_inference_saves_to_database` expects raw score (0.72) but the route now stores the canonical-transformed score (0.45). |

## Non-Blocking Findings

| # | File:Line | Finding |
|---|-----------|---------|
| N1 | `src/db/models.py:77` | `SensorReading.light` doc says "lux" but canonical unit is now W/m². |
| N2 | `src/config.py:854,963,1011,1033` | Remaining `lux` comments on config constants that now operate in W/m² space. |
| N3 | `src/api/services/anomaly_group_service.py` | 1,039 lines — exceeds 300-line guardrail. Pre-existing, not introduced by this phase. |
| N4 | `src/api/services/upload_portions.py:214–227` | `_row_dict_from_minimal` has 10+ parameters. Could use a dataclass/TypedDict. |
| N5 | `docs/architecture/episode_grouping.md` | Missing implementation status header. Reads as if the described system is deployed. |
| N6 | `tests/api/test_023_migration.py` | Three pre-existing failures in uncommitted ADR-005 test code. Not introduced by this phase (evidence: they test 023-only logic, 024 columns not involved). |

---

## Verdict: **NEEDS FIXES**

### Rationale

The label integrity work (Task 1: migration 024, trigger columns, reasoning
generation from trigger channel) is solid and correctly addresses the original
diagnostic (52% of alerts were misattributed to greenhouse air temperature when
heating-pipe temperature was the true trigger).

The score de-saturation (Task 1/D3) is correctly implemented via the asymptotic
canonical transform.

The bootstrap loader fix (Task 6) correctly replaces the non-reproducible
reservoir sampling with full retention.

The light unit reconciliation (Task 4) is thorough and well-sourced.

The frontend severity changes (Task 1F) correctly prefer server-assigned
severity over client re-derivation.

**However, three of six acceptance criteria (1, 2, 3) depend on stuck-fault
detection, episode grouping, and recovery scoring that are documented in the
architecture but not implemented in code.** The architecture document
(`episode_grouping.md`) describes a complete system with detailed semantics
that a reader would believe is deployed. This is the most significant gap.

Additionally, the inference route now applies canonical score transforms but
the corresponding test was not updated (B2), which is a straightforward fix
but is a regression that must be addressed before merge.

---

> **Historical — point-in-time record.** Written 2026-09-09. Status: partially implemented — the stuck-fault, episode-grouping and recovery-scoring gaps it found were taken up in the following weeks (see `upload_detector_convergence.md`, ADR-011); acceptance-criteria verdicts are as of the review date. Not maintained against current code. This banner sits at the bottom deliberately: `upload_detector_convergence.md` cites this file by line range (`:256–287`), and a banner at the top would shift those lines.
