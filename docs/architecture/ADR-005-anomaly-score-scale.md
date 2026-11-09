# ADR-005 — `anomaly_score` scale semantics

**Status:** Accepted — implemented; `score_scale` is a live column on `sensor_readings` (`src/db/models.py:100`, added by migration `023_canonical_anomaly_score`).
**Date:** 2026-08-01
**Author:** architect
**Supersedes:** nothing. **Related:** ADR-004 (deterministic attribution), `docs/architecture/live-monitoring-review.md`
**History:** Renamed into the ADR series from `docs/architecture/anomaly_score_scale_decision.md` on 2026-11-09; body unchanged.

---

## 1. Problem

`SensorReading.anomaly_score` is written by four producers on three mutually
incompatible numeric scales, and read through a single `threshold` query
parameter. The same number means different things depending on which code path
wrote the row.

| Producer | Site | Density | Scale | Own cutoff |
|---|---|---|---|---|
| Live SCADA scorer | `src/live/persistence.py:139` | dense (every row) | raw LSTM-VAE reconstruction error; noise floor ~0.017, demo peaks ~0.36 | `LIVE_ANOMALY_THRESHOLD = 0.05` |
| Cold-run CSV upload | `src/api/services/csv_persist.py:310` via `cold_run_upload.build_anomaly_scores_by_timestamp` | **sparse** — only flagged rows get a score, normal rows stay `NULL` | raw detector error, calibrated per file at P99.9 | per-file, not persisted |
| Batch / fused pipeline | `src/api/routes/inference.py:131`, `comprehensive_pipeline` | dense | fused detector probability in [0,1] | `ANOMALY_THRESHOLD = 0.8` |
| Synthetic generator | `src/api/services/synthetic_anomalies.py:214-216`, `src/db/seeds.py:36,49` | dense | fabricated: anomalies 0.60–0.99, filler `random.uniform(0.02, 0.28)` | none — it is ground truth, not a detector |

The read path has exactly one knob: `SensorReading.anomaly_score >= threshold`
(`anomaly_group_service.py:510`, `readings_service.py:201,230`), fed by
`web/lib/severity.ts:6` `DEFAULT_SCORE_THRESHOLD = 0.15`.

**The observed failure.** 0.15 was tuned against the *live* scale. Applied to
synthetic rows, it sits inside the generator's *normal* band (0.02–0.28), so
fabricated filler is served as anomalies: 484 of 500 exported rows. The 16 real
ones (score ≥ 0.60) are correct. There is no threshold value that is correct for
both cohorts — 0.15 floods on synthetic, 0.8 returns empty on live (live scores
top out at 0.36 and can never reach 0.8).

Note this is not only a scale problem. It is also a **density** problem: the
upload path already establishes the convention that an unset score means "not
flagged," and the generator breaks it by scoring every row.

---

## 2. Decision

### 2.1 One canonical scale, normalised at write time

**`anomaly_score` is a calibrated exceedance ratio in `[0, 1]` where `0.5` is
the producing detector's own decision point and `1.0` is that detector's
saturation point.**

A single transform, parameterised per producer by a `(p, hi)` calibration pair —
`p` = that producer's decision threshold, `hi` = its saturation value
(`None` for unbounded scales):

```
canonical(raw, p, hi):
    raw < 0                  ->  0.0        # clamp
    raw <= p                 ->  0.5 * raw / p
    raw >  p, hi is not None ->  0.5 + 0.5 * (raw - p) / (hi - p)
    raw >  p, hi is None     ->  0.5 + 0.5 * (raw - p) / raw
```

Continuous at `raw = p`, strictly monotone, bounded in `[0, 1]`, and invertible
given `(p, hi)`. The unbounded branch is asymptotic to 1.0 and never reaches it,
so the existing `Query(..., ge=0.0, le=1.0)` validation stays valid unchanged.

Applied to the real evidence:

| Producer | raw | canonical | reading |
|---|---|---|---|
| live (p=0.05, hi=None) | 0.017 noise floor | **0.17** | below cutoff |
| live | 0.05 | **0.50** | exactly at cutoff |
| live | 0.08 compound episode | **0.69** | fires |
| live | 0.36 demo peak | **0.93** | strong |
| batch (p=0.8, hi=1.0) | 0.28 | **0.175** | below cutoff |
| batch | 0.8 | **0.50** | exactly at cutoff |
| batch | 0.99 | **0.97** | strong |

One threshold — `0.5` — is now simultaneously correct for both, and the numbers
either side of it are comparable *across producers* because both are expressed as
"distance past my own cutoff," not in detector-native units.

### 2.2 Ground-truth producers do not get the transform

The synthetic generator and `seeds.py` run **no detector**. They have no `raw`
and no `p`, so squashing their fabricated numbers is meaningless. They emit
canonical values **directly**, from a config band:

```
SYNTHETIC_ANOMALY_SCORE_RANGE  = (0.75, 0.99)   # injected faults
SYNTHETIC_CONTEXT_SCORE_RANGE  = (0.05, 0.20)   # background filler
```

Filler now lands *below* live's own measured noise floor (0.17), which is the
correct ordering: a fabricated normal row must never outrank a real measured one.
The per-type score spread currently in `_TYPE_PROFILES` (lines 29–39) may stay as
intra-band variation, but must be re-expressed inside `SYNTHETIC_ANOMALY_SCORE_RANGE`.

Side effect worth naming: `gradual_drift` currently emits `(0.60, 0.90)` on the
batch scale, i.e. partly *below* `ANOMALY_THRESHOLD = 0.8` — the generator has
been emitting "real anomalies" that its own scale calls normal. Moving to an
explicit canonical band removes that class of bug permanently.

### 2.3 Raw values are preserved, not discarded

Two new nullable columns on `sensor_readings`:

- `anomaly_score_raw FLOAT NULL` — the producer's native value, verbatim.
- `score_scale VARCHAR(16) NULL` — provenance label: `live | upload | batch | synthetic | legacy | unknown`.

`score_scale` is **audit and re-derivation metadata only**. It is explicitly
*not* a query-time threshold selector — see §6.1. Its purpose:

- When a retrain moves the live noise floor, you bump `p` in config and
  recompute `anomaly_score` from `anomaly_score_raw`. Pure recompute, no
  re-inference over history. Without the raw column that recalibration would
  require re-running the model over every stored row.
- It makes the migration reversible.

All calibration pairs live in one config block, next to the thresholds they
mirror:

```
SCORE_SCALE_CALIBRATION = {
    "live":   (LIVE_ANOMALY_THRESHOLD, None),   # unbounded recon error
    "batch":  (ANOMALY_THRESHOLD,      1.0),    # fused probability
    "upload": (<per-file calibrated p>, None),  # supplied per call
}
CANONICAL_ANOMALY_THRESHOLD = 0.5
```

The upload path already computes its per-file threshold; it must now pass it into
the transform instead of persisting the raw error. That is the only producer
whose `p` is not a constant.

### 2.4 Non-finite guard is mandatory at the transform boundary

`canonical()` returns `None` (→ `NULL`) for any non-finite `raw`, non-finite `p`,
or `p <= 0`, and logs with tenant/greenhouse/timestamp context. Reuse the existing
`sanitize_channels` pattern from `src/live/persistence.py` — do not write a second
sanitizer.

Postgres `float8` accepts `NaN`/`Infinity` where `jsonb` does not, so a bad value
in the column will not fail on write but *will* 500 the moment it reaches a JSON
response body or a `RawReading.data` blob. Guarding at the transform is the only
place that catches both.

---

## 3. Answers to the specific questions

### Q1 — Normalise at write time, or record scale per row?

**Normalise at write time.** Rejected alternative and why, in §6.1. Short form:
a per-row scale forces the threshold predicate into a disjunction
(`(scale='live' AND s>=t_live) OR (scale='batch' AND s>=t_batch)`), which
destroys the `ix_sensor_readings_score_desc` index walk that migration 022 was
created for — the documented 38× local / ~9.8 s production regression. And it
makes a single FE slider unrenderable for a mixed window, which is a real case:
a tenant can inject synthetic anomalies into a greenhouse that is also streaming
live.

### Q2 — How should `/api/anomalies` choose its threshold?

Make `threshold` **optional with a server-side default**, and echo the effective
value in the response.

```
threshold: Optional[float] = Query(default=None, ge=0.0, le=1.0)
...
effective_threshold = threshold if threshold is not None else CANONICAL_ANOMALY_THRESHOLD
```

Both routes (`anomalies.py:56` and `:102`) change identically. Response gains
`"threshold": effective_threshold` and `"score_scale_version": 2`.

Under §2.1 there is nothing for the server to *infer* — one scale means one
default works for a synthetic tenant, a live tenant, and a tenant with both.
The parameter survives only as an operator sensitivity dial, which preserves the
`web/AGENTS.md` "URL is the state" convention: a bookmarked
`?threshold=0.62` still reproduces its exact view.

**Rejected: server-side auto-detection** ("sniff the rows, pick a scale"). It is
non-deterministic, its answer changes as data lands, and it is invisible to the
operator. It also breaks `@cache(expire=REDIS_CACHE_EXPIRE_SECONDS)`, which keys
on query parameters — a server-inferred threshold that shifts under a stable key
serves stale, silently-wrong results.

Making the parameter optional is also what de-risks the release ordering (§5.3):
the FE can simply stop sending a threshold rather than having to send a new one
at a coordinated instant.

### Q3 — What does `web/lib/severity.ts` `DEFAULT_SCORE_THRESHOLD` become?

**`0.5`, and yes it stays a single constant.** That is the whole point of §2.1 —
if it needed to be more than one constant, the canonicalisation failed.

It stops being a hand-tuned magic number and becomes a statement of the scale's
definition. The doc comment must change too; the current one
(`"Matches the model's calibrated cutoff scale (scores top out ~0.2)"`) is a
description of the live raw scale and will be actively misleading after this
change. Replacement: `/** Canonical scale: 0.5 == the producing detector's own decision point. */`

Four call sites move for free because they all import the constant:
`useAnomalies.ts:57`, `ThresholdsView.tsx:34,36`, `MonitoringJumpLink.tsx:28`,
`MonitoringView.tsx:135`.

Preferred refinement: the FE reads the server-echoed `threshold` from the
response when present and keeps the constant only as the pre-first-paint default.
Keeping the constant avoids an extra round-trip and a loading state on cold load.

**Follow-on, explicitly out of scope here:** a fixed canonical scale finally makes
*fixed severity bands* possible in `getSeverity` (e.g. 0.5–0.7 warning,
0.7–0.9 lstm, ≥0.9 critical) instead of deriving severity from
`detector`/`agreement` flags that are usually absent. That is a separate change,
needs no migration, and should not be bundled into this one.

### Q4 — Correct baseline for sensor attribution (cause D)

**Per-sensor z-score against the tenant's diurnal profile (hour-of-day mean and
standard deviation), with an explicit fallback ladder.**

The surviving expression — at `anomaly_group_service.py:285` and `:346` after
backend-engineer's cause-C/E fix landed — is

```
z = abs(f - mid) / (span / 2.0)        # mid = config range midpoint
```

and it is wrong in **two** independent ways, which matters because fixing only
the obvious one leaves the bug in place:

1. **Wrong centre.** Light's config midpoint is 500 lux; the generator's mean
   light is 200 (`synthetic_anomalies.py:48`). Normal light is scored as a large
   deviation by construction.
2. **Wrong scale — this is the load-bearing defect.** Dividing by half-range
   makes the value a fraction of *how wide the sensor's legal band is*, not of
   *how much that sensor normally varies*. Light's half-range is 500; temperature's
   is 5. Light will win the `max()` tiebreak at `:352` for almost any reading
   simply because its legitimate range is two orders of magnitude wider. That is
   exactly the observed 450-of-484 light attribution. Re-centring on 200 without
   changing the denominator leaves light comparing ~300/500 against temperature's
   ~0.6/5 — still incomparable.

Dividing by σ makes the quantity dimensionless and genuinely comparable across
sensors. That is the fix.

**Cause C's fix did not address this.** Backend-engineer correctly deleted the
unreachable `accuracy_decline` branch from `_infer_fault_type_from_values`, but the
half-span denominator survives in `_infer_sensors_from_values` (`:346`) — which is
the function that actually *picks the attributed sensor*. The 450-of-484 light
attribution is still live and this remains open work.

Fallback ladder, most to least specific:

1. Tenant diurnal profile for the row's hour → `z = |v - mean_h| / std_h`
2. Static per-sensor expected `(mean, std)` from config (new constants)
3. Current range-midpoint behaviour — retained only so the function is total

**Cost, honestly stated:**

- `src/api/services/diurnal_profile_service.py` **already exists** and already
  persists per-tenant hourly `(mean, std)` into
  `LearningState.state_json["diurnal_profiles"]`. Infrastructure cost is near
  zero; this is wiring, not new machinery.
- `anomaly_group_service` is currently pure and stateless per row. It becomes
  tenant-aware. Mitigate by fetching the profile **once per request in the route**
  and threading it down as a parameter — the identical pattern already used for
  `incidents=incidents` (`anomalies.py:75`, `:165`). One extra query per request,
  zero per-row cost, and it sits inside the existing response cache.
- Cold start: a tenant below `DIURNAL_PROFILE_MIN_ROWS` has no profile and falls
  to rung 2. Synthetic injection writes real `SensorReading` rows, so a demo
  tenant builds its own profile as it is used.
- **Determinism regression:** attribution for a given historical row can change
  as the profile accumulates. Acceptable — this is a diagnostic hint on a
  *fallback* path, and the pipeline's own detectors already have this property.
  It must not be relied on for anything audited.

**Rejected: excluding wide-range sensors from the tiebreak.** It hides the defect
rather than fixing it, and it makes genuine light failures — `light_failure` is a
first-class injected type — permanently unattributable.

**Rejected: rolling distribution stats computed at query time.** Correct, but it
is a window function over the full history per request on the exact path that was
just optimised from 33 s to under 1 s. The diurnal profile is the same statistic,
precomputed.

**Direction of travel:** this value-based inference is a *fallback*. The right
long-term answer is that rows carry attribution from the producer that computed
it (ADR-004). This fallback should shrink over time, not accumulate features.

### Q5 — Should the synthetic generator persist `Incident` rows?

**No. Reading `SensorReading.anomaly_type` is the correct architectural
boundary.** Clear recommendation, one option only.

`Incident` is the *output record of the detection pipeline*. It carries
`fault_type`, `root_cause`, `root_cause_details`, and confidence, and it is the
substrate of the feedback and active-learning loop: operators mark incidents
true/false positive, `feedback_validator` updates per-operator EMA reliability,
and `ACTIVE_LEARNING_*` thresholds trigger retraining off that evidence.

Writing forged `Incident` rows from a generator that already knows ground truth
injects fabricated detections into that evidence base. Operators would grade
them, reliability scores would move, and retraining decisions would be made on
detections the model never made. That is a data-integrity failure with a long
blast radius and no clean rollback.

The generator's job is to produce **input**, not output. If the real ~20-fault-type
classifier in `comprehensive_pipeline` should participate, the correct move is to
run the injected rows *through the pipeline*, which then writes `Incident` rows on
its normal path with genuine provenance. That is a separate, larger piece of work
("score-on-inject"), should be gated behind an explicit request flag, and should
not be smuggled in as a side effect of injection.

Meanwhile `anomaly_type` is exactly right for the labelling job: it is the ground
truth of what was injected, it is already persisted on every synthetic row
(`models.py:84`), and it is already the deletion key
(`synthetic_anomalies.py:273`). Precedence for `fault_type` resolution:

```
incident.fault_type            (real detection)      — highest
SensorReading.anomaly_type     (injected ground truth)
_infer_fault_type_from_values  (heuristic)           — last resort
```

**One coupling to flag for whoever implements the passthrough:** `anomaly_type`
holds *generator type names* (`temperature_spike`, `co2_surge`, `light_failure`),
not fault-taxonomy keys (`spike_fault`, `stuck_fault`). A mapping table is
required. `FAULT_LABELS` in `web/lib/severity.ts:8-15` knows only six keys;
unmapped generator types will render as raw identifiers in the UI. The mapping
belongs on the backend, with the FE label record extended to match — that is FE
work and needs to ship in the same release train (§5.3).

### Q6 — Migration and back-compat

See §5.

---

## 4. What the FE user actually sees

Before: threshold slider at 0.15, 500 "anomalies," 484 of them normal readings,
light attributed on 450 of them, "Unknown / unclassified" on all 484.

After: slider defaults to 0.5, 16 anomalies, all real, each labelled with its
injected fault type, attributed against the tenant's own diurnal baseline.
Dragging the slider below 0.5 is now a meaningful "show me sub-threshold
activity" action instead of an accident.

---

## 5. Migration

### 5.1 Existing prod rows partition cleanly

Every existing row is classifiable with no guessing:

| Predicate | Cohort | Historical `(p, hi)` |
|---|---|---|
| `source = 'live'` | live | `(0.05, None)` |
| `anomaly_type IS NOT NULL` | synthetic | n/a — ground truth |
| both `NULL`, `anomaly_score IS NOT NULL` | upload (sparse) | per-file, **not recoverable** |

The batch/fused cohort overlaps the third bucket and cannot be separated from
upload rows retrospectively; both are handled by the same rule below, which is
safe because both are sparse-or-above-cutoff by construction.

### 5.2 Alembic 023 — steps

1. **Add columns** `anomaly_score_raw FLOAT NULL`, `score_scale VARCHAR(16) NULL`.
   Both nullable with no default → metadata-only DDL on Postgres, no table
   rewrite, no long lock.

2. **Backfill in batches**, keyed on `score_scale IS NULL` so the job is
   resumable and idempotent. Commit per batch — a single transaction over the
   full table risks lock escalation and WAL blowup.

3. **Per-cohort transform:**
   - `anomaly_score_raw := anomaly_score` for every row first.
   - **live:** `anomaly_score := canonical(raw, 0.05, None)`, `score_scale := 'live'`.
   - **upload/batch:** the per-file `p` is gone, but sparseness guarantees every
     stored score was already above its own cutoff. Use a one-time rank-preserving
     map into the upper half: `0.5 + 0.5 * raw / (1 + raw)`. Monotone, bounded,
     everything lands in `(0.5, 1.0)`, intra-file ordering preserved, and every
     previously-flagged row stays flagged. `score_scale := 'legacy'` — deliberately
     *not* `'upload'`, so these approximated rows are distinguishable from
     correctly-calibrated ones written after the change. Going forward the upload
     path knows `p` and uses the real transform.
   - **synthetic:** **delete these rows.** They are test data by definition, and
     transforming fabricated scores into a calibrated scale lends them credibility
     they do not have. The delete path already exists and is proven safe
     (`anomaly_type IS NOT NULL`, `synthetic_anomalies.py:270-274`). Escape hatch
     for anyone with a demo tenant they want to keep: map filler → 0.10,
     injected → 0.85, `score_scale := 'synthetic'`.

4. **Non-finite guard in the migration itself.** Exclude non-finite values from
   the transform explicitly — `WHERE anomaly_score = anomaly_score` (NaN filter)
   plus `<> 'Infinity'::float8` / `<> '-Infinity'::float8` — and set those rows to
   `anomaly_score = NULL, score_scale = 'unknown'`. Postgres `float8` accepts
   these, so they may already be present. Feeding `NaN` through the transform
   propagates it into a column that is then serialised into a JSON response body,
   which is where it 500s. This must also be tested against Postgres, not just
   SQLite — SQLite's laxer JSON handling means a SQLite-only test cannot catch it
   (same class of gap as the `utc_date` construct documented at
   `anomaly_group_service.py:453-476`).

5. **Reindex.** `ix_sensor_readings_score_desc` (migration 022) is a partial index
   `WHERE anomaly_score IS NOT NULL`. Rewriting every value bloats it and leaves
   the planner with stale statistics — precisely the index the ~9.8 s production
   regression depended on. After backfill: `CREATE INDEX CONCURRENTLY` a
   replacement, then drop and rename, followed by `ANALYZE`. Do **not** plain
   `REINDEX` on a live table. This is the single largest operational risk in the
   migration and should be scheduled, not fired ad hoc.

6. **Downgrade** = restore `anomaly_score := anomaly_score_raw` where
   `anomaly_score_raw IS NOT NULL`, then drop both columns. The raw column is
   what makes this reversible; that is its main justification beyond
   recalibration.

### 5.3 Coordinated release — REQUIRED, and the ordering is not symmetric

**This change cannot ship backend-only or frontend-only.** The two orderings have
very different consequences:

- **Backend first (correct).** Backend canonicalises while the FE still sends
  `0.15`. Under the canonical scale 0.15 under-filters: it shows sub-cutoff noise.
  Degraded, visibly noisy, *not* an outage — and no worse than today.
- **Frontend first (must not happen).** FE sends `0.5` while the backend is still
  on native scales. Live rows top out at 0.36 raw, so a 0.5 threshold returns
  **zero rows** for every live tenant. A silently empty dashboard with no error —
  the worst possible failure mode.

**Ship order:**

1. Backend release: migration 023 + canonical transform at all write sites +
   `threshold` made optional with the `CANONICAL_ANOMALY_THRESHOLD` default +
   effective threshold echoed in the response. Backward compatible with the
   current FE.
2. Frontend release: `DEFAULT_SCORE_THRESHOLD = 0.5`, stop sending an explicit
   threshold, extend `FAULT_LABELS` with the generator-type mappings from Q5.

Making the parameter optional in step 1 is what removes the ordering hazard from
step 2 entirely — the FE stops sending a value rather than having to switch to a
new one at a coordinated instant.

**Other back-compat surfaces:**

- **Bookmarked and shared URLs** carrying `?threshold=0.15` (and `?saved=` on the
  Thresholds page) will under-filter after the change. There is no server-side
  stored threshold to migrate — `ThresholdsView.tsx:36` reads `saved` from the URL,
  not from persistence — so the blast radius is bookmarks only. Acceptable; the
  echoed threshold in the response makes the state visible to the operator.
- **`web/features/monitoring/api/live.ts:46`** validates a `threshold` field in
  the SSE payload with Zod. If the live stream emits its raw threshold, it must
  emit the canonical one after this change or the two surfaces will disagree
  about what "flagged" means mid-session.
- **Test fixtures** hardcode both scales — `flaggedWindow.test.ts:65` uses 0.5,
  `live.test.ts:34` uses 0.15. These now mean the same thing and both need
  intent-revealing updates rather than mechanical find-replace.

---

## 6. Rejected alternatives

### 6.1 Per-row `score_scale` with query-time threshold resolution

The main alternative, rejected on three grounds:

1. **It destroys the index walk.** The threshold predicate becomes a disjunction
   over scales. Postgres cannot satisfy an ordered index scan across a per-scale
   `OR`; it falls back to bitmap-or plus top-N heapsort — the exact plan that
   migration 022 was written to eliminate after it caused ~9.8 s production
   response times. Paying that back to solve a semantics problem is a bad trade.
2. **The FE slider becomes unrenderable.** A window can legitimately mix live and
   synthetic rows. One slider cannot express two thresholds, and showing two
   sliders exposes an internal producer taxonomy to an operator who has no reason
   to know it exists.
3. **It makes the coupling permanent.** Every future reader — the Streamlit
   dashboard, exports, the metrics job, anything not yet written — would have to
   re-implement scale resolution correctly or silently reproduce this bug.
   Normalising at write time makes correct reads the default and the only option.

The provenance value is real, which is why `score_scale` is kept in §2.3 — as
audit metadata, not as query semantics.

### 6.2 Canonicalising onto the existing batch scale (threshold 0.8)

`0.8` is one producer's constant with no meaning to the others. It also leaves
the `[0, 0.8)` band undefined across producers, which is the current bug in a new
costume.

### 6.3 Canonicalising onto the live raw scale (threshold 0.05)

Worse. Raw reconstruction error is unbounded (`le=1.0` validation would have to
go), model-version dependent, and **every retrain silently changes what every
stored historical score means**. The synthetic generator runs no LSTM at all, so
there is nothing for it to produce a reconstruction error from.

### 6.4 Percentile/rank-based canonical score

`score = within-tenant percentile` is perfectly comparable by construction, and
tempting. Rejected: it requires a global sort per write, re-ranks all history on
every insert, and — fatally — a completely healthy tenant would have exactly 1%
of its normal rows flagged at the 99th percentile, by definition. A calm
greenhouse must be able to report zero anomalies.

### 6.5 Dropping `anomaly_score` and computing at read time

Loses the index entirely and makes read cost unbounded in window size. The path
this ADR touches was optimised from 33 s to under 1 s specifically by pushing
work to write time; this would reverse that.

### 6.6 Setting synthetic filler rows to `NULL` instead of a low canonical band

Genuinely attractive — it matches the existing sparse-upload convention
(`cold_run_upload.py:44-46`) and makes the bug structurally impossible for *any*
threshold value. Rejected because the FE score timeline is fed by the same
windowed payload as the tables (`useAnomalies.ts` docstring), and a synthetic
demo tenant would render 16 isolated points instead of a continuous trace — a
visible regression on the primary demo surface. Safety here comes from the
canonical scale plus a fixed threshold constant, not from absent data.

If the score timeline is ever decoupled from this payload, revisit — `NULL`
filler is the structurally stronger answer.

---

## 7. Open items for implementers

- Causes C and E **landed 2026-08-01** while this ADR was being written. Line
  references above are against the post-fix tree. Two consequences:
  - The generator-type → fault-taxonomy mapping requested in Q5 already exists as
    `_ANOMALY_TYPE_TO_FAULT_TYPE` (`anomaly_group_service.py:80`), and
    `"synthetic_context" → None` is exactly right. No new `FAULT_LABELS` keys were
    introduced, so the Q5 mapping work is **done** and the §5.3 FE coupling for it
    drops out of the release train.
  - Cause C removed the dead branch but **not** the defective denominator — Q4 is
    untouched by it and remains fully open. The two changes no longer collide.
- Q4 (attribution baseline) and this ADR's canonicalisation are independent and can
  ship in either order or in parallel. Q4 needs no migration.
- `SCORE_SCALE_CALIBRATION` must be the only place any `(p, hi)` pair is written.
  If a second copy appears, the recalibration property in §2.3 is lost.
