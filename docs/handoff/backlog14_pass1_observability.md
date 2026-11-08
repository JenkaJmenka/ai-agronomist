# BACKLOG-14 — Pass-1 observability interface

**Owner of the producing code:** ml-engineer (`src/models/`, `src/config.py`).
**Consumer:** backend-engineer — plumb these into `metrics_json` in
`src/api/services/cold_start_training.py`. Then qa-engineer for coverage.

**Branch:** `fix/pass1-mad-bimodal-channels`. Written 2026-11-08 under the
cross-agent exception for `docs/handoff/`: this is a contract, not working notes.

---

## 0. Units — one rule, no exceptions

**Every `*_fraction` field on every type below is a fraction in `[0, 1]`.
Nothing in this contract is a percentage.** Format at the presentation edge if
a percentage is wanted; do not store one.

Every fraction is published alongside its numerator **and** its denominator,
because the denominator is where this went wrong before: a 44.6% cull of real
readings in `data/bootstrapped_clean_csvtest_20260712.csv` surfaced as 3.84%
because it had been divided by a 91.4%-NaN frame.

---

## 1. New fields on `BootstrapPass1Result` (`src/models/bootstrap.py`)

All four are **defaulted**, so every existing constructor — including
`tests/api/test_bootstrap_route.py::_make_pass1_result` — keeps working
unchanged.

| field | type | meaning |
|---|---|---|
| `mad_channels` | `tuple[MadChannelReport, ...]` | One report per channel the MAD scrub was asked about, in request order. Empty when the frame was empty. |
| `if_flag_fraction` | `float` | IsolationForest flag rate over the **whole frame** (`if_flags.mean()`). Fit and predict now run on the same population, so there is exactly one IF rate — see §3. |
| `discard_stats` | `Pass1DiscardStats` | Union discard against both denominators, plus the guard status. §4. |
| `warnings` | `tuple[Pass1Warning, ...]` | Structured `(code, message)` pairs, **returned** rather than only logged. Order: per-channel MAD warnings, then the discard warning. **Was `tuple[str, ...]`** — see §8. |

Four derived read-only properties are provided so a consumer never has to walk
`mad_channels` by hand:

| property | type | denominator |
|---|---|---|
| `mad_channel_cull_fractions` | `dict[str, float]` | that channel's finite values |
| `mad_channel_cull_rows` | `dict[str, int]` | — (absolute count) |
| `mad_channel_finite_rows` | `dict[str, int]` | — (the denominator above) |
| `mad_skipped_channels` | `dict[str, str]` | channel → skip reason (§2) |

A channel that was skipped appears in `mad_channel_cull_fractions` with `0.0`
and in `mad_skipped_channels` with its reason. Both dicts are safe to serialize
directly: keys are channel names, values are JSON scalars.

### Suggested `metrics_json` shape

Not binding — backend-engineer owns the key names — but this is the shape the
fields were designed for:

```json
{
  "pass1_clean_rows": 100075,
  "pass1_anomaly_rows": 4846,
  "pass1_mad_cull_fractions": {"temperature": 0.0, "humidity": 0.0135911781,
                               "co2": 0.0, "light": 0.0},
  "pass1_mad_cull_rows":      {"temperature": 0, "humidity": 1426,
                               "co2": 0, "light": 0},
  "pass1_mad_finite_rows":    {"temperature": 104921, "humidity": 104921,
                               "co2": 104921, "light": 104921},
  "pass1_mad_skipped_channels": {"co2": "modal_share_above_limit",
                                 "light": "excluded_channel"},
  "pass1_if_flag_fraction": 0.0400015249,
  "pass1_discard": {
    "frame_rows": 104921, "frame_discarded": 4846,
    "frame_discard_fraction": 0.0461871,
    "data_rows": 104921, "data_discarded": 4846,
    "data_discard_fraction": 0.0461871,
    "status": "ok"
  },
  "pass1_warnings": []
}
```

**Guard non-finite floats before writing.** `metrics_json` is a JSON column and
production is Postgres, which rejects `NaN` where SQLite accepts it. The
fraction fields are always finite, but `MadChannelReport.median`, `.mad` and
`.modal_share` are **NaN for a skipped channel** — if you choose to carry them,
coerce to `null` first.

---

## 2. `MadChannelReport` (`src/models/mad_scrub_report.py`)

Frozen dataclass, one per channel.

| field | type | notes |
|---|---|---|
| `column` | `str` | channel name |
| `is_skipped` | `bool` | `True` = not evaluated, contributes no flags |
| `skip_reason` | `str \| None` | `None` iff `is_skipped is False` |
| `median` | `float` | whole-channel median of finite values; **NaN if skipped before measurement** |
| `mad` | `float` | median absolute deviation; **NaN if skipped before measurement** |
| `modal_share` | `float` | fraction of finite values taken by the single commonest value; **NaN if skipped before measurement** |
| `finite_rows` | `int` | non-NaN values in the channel |
| `cull_rows` | `int` | rows this channel flagged |
| `cull_fraction` | `float` | `cull_rows / finite_rows`, `0.0` when `finite_rows == 0` |

`skip_reason` is one of these five string constants, exported from the same
module. Treat the set as closed and match on the constants, not the literals.

**The five reasons are not independent alternatives — they are checked in a
fixed precedence order, and only the first match is reported.** A reviewer's
point, recorded here rather than left implicit: a DB row's `skip_reason`
answers "which check fired first", not "which checks apply" — a channel that
is both zero-spread and nominally over the modal-share limit is reported as
zero-spread (`mad_at_or_below_min_spread`), never `modal_share_above_limit`,
because the spread check runs first and returns before modal share is even
evaluated for that channel.

| order | constant | value | when | checked in |
|---|---|---|---|---|
| 0 | `SKIP_REASON_MISSING` | `"column_absent"` | requested column not present in the frame | `compute_mad_scrub` (before per-column evaluation) |
| 1 | `SKIP_REASON_EXCLUDED` | `"excluded_channel"` | named in `BOOTSTRAP_MAD_EXCLUDED_CHANNELS` | `_skip_reason`, first check |
| 2 | `SKIP_REASON_NO_FINITE` | `"no_finite_values"` | channel is entirely NaN | `_skip_reason`, second check |
| 3 | `SKIP_REASON_NO_SPREAD` | `"mad_at_or_below_min_spread"` | MAD ≤ `BOOTSTRAP_MAD_MIN_SPREAD` | `_skip_reason`, third check — **tested before modal share, and the order is load-bearing** (see below) |
| 4 | `SKIP_REASON_MODAL_SHARE` | `"modal_share_above_limit"` | modal share > `BOOTSTRAP_MAD_MAX_MODAL_SHARE`, reached only when spread is non-degenerate | `_skip_reason`, last check |

**Why the order changed (review finding, fixed in this branch):** `mad == 0`
implies at least half the channel's finite values sit exactly at the median,
so its modal share is always ≥ ~0.5 — above the 0.40 limit. With modal share
tested first, `SKIP_REASON_NO_SPREAD` was unreachable in production, and a
genuinely *constant* sensor was reported as `modal_share_above_limit` —
"bimodal" — which is false, and was being persisted into `metrics_json` per
model. `co2` is the live case: the CSV adapter fills it with the constant
400.0 whenever the vendor file carries no real CO2 channel. Testing spread
first makes the reported reason true, and makes `mad_at_or_below_min_spread`
a usable zero-variance signal for BACKLOG-21 (stuck-sensor detection) that
`modal_share_above_limit` could not be, having been contaminated with every
constant channel in the corpus.

Measured on the three fixture corpora, real `ColdStartBootstrap.run()`, same
inputs before and after — **only the reported reason changes; `cull_rows` is
0 → 0 on every row below**, because this is a label fix, not a behavior
change: a skipped channel never culls (`cull_rows` is set to 0 whenever
`_skip_reason` returns non-``None``, before the z-score mask is even
computed), and every channel below has `mad == 0.0`, so it was already
`is_skipped=True` — just under the wrong reason — both before and after.

| corpus | channel | before | after |
|---|---|---|---|
| tenant_1 | `co2` | `modal_share_above_limit` | `mad_at_or_below_min_spread` |
| `native_july_clearsky.csv` | `co2` | `modal_share_above_limit` | `mad_at_or_below_min_spread` |
| `native_dec_stuck_fault.csv` | `humidity` | `modal_share_above_limit` | `mad_at_or_below_min_spread` |
| `native_dec_stuck_fault.csv` | `co2` | `modal_share_above_limit` | `mad_at_or_below_min_spread` |

`light` stays `excluded_channel` on all three corpora in both columns — the
exclusion check runs first (order 1) and returns before spread or modal share
are evaluated at all, so reordering those two later checks cannot touch it.
`modal_share_above_limit` is still reachable, and now means what it says:
genuinely multi-modal *with real spread* (`mad > BOOTSTRAP_MAD_MIN_SPREAD`) —
not a constant sensor masquerading as one.

`MadScrubResult` (same module) carries `flags: pd.Series`, `channels` and
`warnings`, with `cull_fractions` / `cull_counts` / `finite_counts` /
`skipped_channels` convenience properties. `compute_mad_flags()` keeps its old
signature and still returns just the flag Series.

---

## 3. IsolationForest — fit and predict now run on the same population

`_compute_if_flags(df)` lost its `exclude_mask` parameter.

Between `892bcd3` (2026-07-24) and this change the forest was **fitted** on
non-MAD-flagged rows and then `.predict()`ed over the whole frame.
`contamination` calibrates the decision boundary to the distribution the forest
saw, so applying it to a different population voids that calibration. Measured
on the tenant_1 corpus before the change: 4.00% of the fit subset flagged — the
contamination asked for — but **98.08% of the rows MAD had already culled**. A
single MAD misfire therefore reached the operator as two independent detectors
agreeing.

The removed comment read `# Spec: MAD-flagged rows are excluded from IF *fit*,
then OR-ed in.` **There is no such spec item.** Verified three ways:

1. The plan the comment refers to is `docs/architecture/scada-config-engine-plan.md`.
   Its Pass-1 MAD row (line 55) says only *"add it as an additional Pass-1
   stage; keep the physics flags"*, and its implementation item (lines 217-220)
   says *"add global-median robust z-score stage … **before** IsolationForest;
   new flags surfaced in `BootstrapPass1Result` (`mad_flags`); physics + IF
   stages retained."* "Before" is stage order. Neither line mentions a fit
   exclusion; `grep -i exclud` over the whole plan returns one unrelated hit
   about live-vs-synthetic row selection.
2. `892bcd3`, which introduced both `mad_scrub.py` and the `exclude_mask`,
   touched **no `.md` file at all**.
3. The inline comment that went with it — `# Score the full (non-excluded)
   frame so MAD-culled rows stay MAD-only.` — is self-refuting: scoring the
   full frame is precisely what stops those rows being MAD-only.

**A procedural note worth acting on separately:** that plan file is **not in the
repository**. `git check-ignore -v` reports `.gitignore:45: docs/architecture/*`,
so `scada-config-engine-plan.md` (and `cold_start_bootstrap.md`, and others)
exist only in the main checkout's working tree. A code comment citing a spec
nobody else can open is why this took a round of archaeology. Flagging for the
orchestrator, which owns `.gitignore`; no change made here.

`docs/architecture/` is architect's, so the deviation from the plan's line-55
item is **not** recorded there. This section is that record until an owner is
assigned. The deviation is narrow: the MAD stage still exists, still runs before
IsolationForest, and still surfaces `mad_flags` — exactly what the plan asked
for. What changed is which *channels* it evaluates (§5) and that it no longer
feeds IF's fit population.

---

## 4. `Pass1DiscardStats` and the guard (`src/models/pass1_discard_guard.py`)

| field | type | notes |
|---|---|---|
| `frame_rows` | `int` | rows in the resampled frame |
| `frame_discarded` | `int` | `(physics \| mad \| if).sum()` |
| `frame_discard_fraction` | `float` | `frame_discarded / frame_rows` |
| `data_rows` | `int` | rows with ≥ 1 finite sensor value — **the threshold denominator** |
| `data_discarded` | `int` | discarded rows among those |
| `data_discard_fraction` | `float` | `data_discarded / data_rows` |
| `status` | `str` | `"ok"` \| `"warn"` \| `"fail"` |
| `warnings` | `tuple[Pass1Warning, ...]` | empty for `"ok"`; exactly one warning otherwise |

Plus `is_failing: bool` and `summary() -> str` (one line carrying both
numerators and both denominators).

Status is decided on `data_discard_fraction`, never on the frame fraction:

| status | condition | constant | default |
|---|---|---|---|
| `"ok"` | `< warn` | — | — |
| `"warn"` | `>= warn` and `< fail` | `BOOTSTRAP_CULL_WARN_FRACTION` | `0.15` |
| `"fail"` | `>= fail` | `BOOTSTRAP_CULL_FAIL_FRACTION` | `0.25` |

Both bounds are **inclusive at the threshold** (`>=`), matching the brief's
"warn at ≥15%, fail at ≥25%".

A per-channel warning is additionally emitted when any channel's
`cull_fraction` exceeds `BOOTSTRAP_CHANNEL_CULL_WARN_FRACTION` (`0.10`). That
constant replaces the hardcoded `_CULL_WARN_FRACTION` in `mad_scrub.py`, which
reached only the log and fired on every tenant_1 run from July to October
unnoticed. Its message no longer advises raising `BOOTSTRAP_MAD_Z_THRESHOLD` —
that advice was measurably wrong (§5).

### Failure is a stop, not a log

`ColdStartBootstrap.run()` raises **`ExcessivePass1DiscardError`** on
`status == "fail"`, after persisting the clean/anomaly CSVs so the diagnostics
survive.

`ExcessivePass1DiscardError` subclasses `ValueError`. **No API change is
required**: `bootstrap.InsufficientDataError` *is* `ValueError`
(`src/models/bootstrap.py:53`), so `src/api/routes/bootstrap.py:115-116` keeps
mapping it to HTTP 400. A distinct class was chosen over reusing the alias so a
caller can tell "too much was thrown away" from "too little was left" — e.g. to
word the two 400s differently. Both are re-exported from `src.models.bootstrap`.

Ordering inside `run()`: persist CSVs → `BOOTSTRAP_MIN_CLEAN_ROWS` check →
discard guard. The min-clean check stays first so small-file behaviour is
unchanged.

---

## 5. The MAD channel guards (for anyone reading a `skip_reason` in the DB)

Two independent guards, **neither of which subsumes the other**:

1. `BOOTSTRAP_MAD_EXCLUDED_CHANNELS` — default `{"light"}`, comma-separated env
   override. `light` is structurally bimodal. When its night zeros sit near half
   the window, every zero deviates from the median by exactly the median, so
   `MAD == median` identically and the cull boundary collapses to
   `median · (1 + z/0.6745)` = 6.18903 · median. On the tenant_1 corpus that is
   8.3552 W/m², and it culled 49,741 rows (47.41%).
2. `BOOTSTRAP_MAD_MAX_MODAL_SHARE` (0.40) and `BOOTSTRAP_MAD_MIN_SPREAD`
   (`1e-9`) — generic degeneracy. `light` is 44.0% zeros on the tenant_1
   corpus, so guard 2 alone would catch it there; it is 30.6% zeros on
   `tests/fixtures/native_july_clearsky.csv`, where only guard 1 does.

`BOOTSTRAP_MAD_MIN_SPREAD = 1e-9` is chosen between two measurable floors:
sensor values arrive as decimal text with at most four fractional digits, so
the smallest MAD a genuinely varying channel can show is `1e-4`, while float64
round-off at greenhouse magnitudes is ~`1e-13`. It subsumes the exact `MAD == 0`
case.

**`MAD == 0` was already safe before this change, and still is.**
`mad_z_scores` has had an explicit early return (`out[finite] = 0.0`) since
`892bcd3`; there was never a division by zero and such a channel never
contributed culls. `tests/models/test_mad_scrub.py::test_mad_z_scores_zero_when_mad_is_zero`
pins it, and that contract is **unchanged** — the condition only widened from
`mad == 0.0` to `mad <= min_spread`, which is a superset. The guard is kept in
`mad_z_scores` as well as in `compute_mad_scrub` so a direct caller of the
low-level function cannot divide by zero.

**Raising `BOOTSTRAP_MAD_Z_THRESHOLD` cannot fix a bimodal channel**, which is
why the old warning text was replaced. Because MAD scales *with* the median, a
full-sun 1000 W/m² row scores only z ≈ 499. Sweep on the tenant_1 corpus, MAD
stage only, both channel guards disabled:

| z threshold | MAD cull fraction | `light` culls |
|---|---|---|
| 3.5 | 48.46% | 49,741 |
| 5 | 46.63% | 48,912 |
| 10 | 44.25% | 46,424 |
| 100 | 24.04% | 25,220 |
| 1000 | 0.00% | 0 |

**These differ from the figures in the task brief** (z>5 → 48.77%, z>10 →
46.48%, z>100 → 27.08%, z>1000 → 4.00%), and the difference reconciles: the
brief's sweep measured the **union** discard, which carries IsolationForest's
~4% on top, so its z>1000 row reads 4.00% where the MAD stage alone reads 0.00%.
Same phenomenon, different numerator. The conclusion is unaffected and in fact
slightly stronger: no threshold between 3.5 and 100 brings the MAD stage below
24%.

---

## 6. Measured effect — all three corpora, real `ColdStartBootstrap.run()`

Fractions are of the frame; all three corpora are 100% rows-carrying-data, so
the frame and data denominators coincide and both are reported identically by
`discard_stats`.

| corpus | rows | union before | union after | hour-12 survival before → after |
|---|---|---|---|---|
| tenant_1 `2026_1d_english_clean_fixed.csv` | 104,921 | 50.52% (53,004) | **4.62%** (4,846) | 0.00% → **84.47%** |
| `native_july_clearsky.csv` | 2,016 | 4.02% (81) | 4.02% (81) | 91.67% → 91.67% |
| `native_dec_stuck_fault.csv` | 3,387 | 4.43% (150) | 4.10% (139) | 77.78% → 79.86% |

Per-channel MAD culls, tenant_1, before → after:
`light` 49,741 → **0** (skipped), `humidity` 1,426 → **1,426** (unchanged —
legitimate culls preserved), `temperature` 0 → 0, `co2` 0 → 0 (skipped, was
already contributing nothing because MAD = 0).
`native_dec_stuck_fault`: `temperature` 15 → **15**, all others 0 → 0.

**No legitimate cull was removed on any corpus.** Every channel the generic
rule newly skips (`co2` on all three, `humidity` on `native_dec`) had MAD = 0
and was already contributing zero culls.

### The hour-12 gate is NOT met — 84.47%, requirement > 90%

Reported as measured, not tuned. The whole shortfall is IsolationForest; MAD-only
hour-12 survival is **98.72%**. IF's flag rate by hour on tenant_1 rises from
0.3-0.6% at night to 10.55% (h11), **15.53% (h12)**, 19.45% (h13), 16.57%
(h14) — i.e. its fixed 4% contamination budget is spent almost entirely on
bright midday rows. The cause is the same one as the MAD bug one layer down:
`light` is an IF input feature, and diurnal/seasonal light is the dominant
variance direction in the corpus, so the forest learns "bright" = "rare".
Flagged rows have median light 734 W/m² against 1 W/m² for clean rows.

**Measured candidate fix, deliberately NOT applied — it is outside the five
approved changes and needs the user's decision.** Dropping `light` from the IF
feature list:

| IF feature set | total flagged | hour-12 flag rate | hour-12 survival (IF only) |
|---|---|---|---|
| `[temperature, humidity, co2, light]` (shipped) | 4,197 (4.00%) | 15.53% | 84.47% |
| `[temperature, humidity, co2]` | 4,196 (4.00%) | 4.00% | 96.00% |
| `[temperature, humidity]` | 4,116 (3.92%) | 3.36% | 96.64% |

The global rate is unchanged at 4.00% either way — contamination pins it — so
this redistributes which 4% is flagged rather than reducing the count. The cost
is that IF would no longer see light at all, so a light-sensor fault would have
to be caught by the dedicated detectors instead. Decide before Phase 2 if the
hour-12 gate is to be met.

### BACKLOG-15 moved — not by anything added here

`P(pass-1 flag | humidity frozen at 100)` on tenant_1, with "frozen" meaning a
row inside a run of ≥ `STUCK_WINDOW_K` (10) identical readings — 8,389 rows:
**32.80% → 5.33%**. All 5.33% is IsolationForest; MAD contributes 0.00% after
the change (27.23% before). **No flatline rule was added**; the fall is just the
union discard falling. BACKLOG-15 remains open and untouched.

Note the figure contradicts the brief, which cited 1.17% for this quantity. The
32.80% "before" was produced by a replication of the pre-fix algorithm that
reproduces the known pre-fix figures exactly (MAD 48.46%, IF 49.59%, union
50.52% = 53,004/104,921 — production's numbers), so the replication is sound.
The 1.17% likely measures a different population or a post-Pass-2 detector
output rather than Pass-1 flags.

---

## 7. What Phase 2 must not assume

- **`warnings` can be non-empty on a successful run.** `status == "warn"` returns
  normally. Persist them; do not treat a non-empty tuple as failure.
- **`mad_channels` order follows the requested columns**, currently
  `_SENSOR_COLS = ["temperature", "humidity", "co2", "light"]`. Key by
  `column`; do not index positionally.
- **`if_flag_fraction` is of the frame, not of IF's input rows.** Rows with any
  NaN sensor are dropped before fitting, so on a NaN-heavy upload this fraction
  is lower than the rate among rows the forest actually scored.
- **A `"fail"` never reaches a `BootstrapPass1Result`** — it raises. Anything
  reading `discard_stats.status` off a returned result sees only `ok`/`warn`.

---

## 8. Warning codes — the closed set (added 2026-11-08, review finding)

`warnings` changed from `tuple[str, ...]` to `tuple[Pass1Warning, ...]`.
**This is a breaking change for the serializer and for `web/openapi.json`.**

Why: `pass1_warnings` reaches the tenant through `POST /api/v1/bootstrap/run`
and is published in `web/openapi.json`, and the free-text form named our
internal environment variables to the customer — it told them to "add it to
`BOOTSTRAP_MAD_EXCLUDED_CHANNELS`". A warning is now two things with two
audiences.

`Pass1Warning` lives in **`src/models/pass1_warning.py`**:

| field | type | notes |
|---|---|---|
| `code` | `str` | Stable, machine-readable, safe to branch on and to store. Always a member of `PASS1_WARNING_CODES`. |
| `message` | `str` | Tenant-safe prose. **No environment variable names, no internal constant / module / field names.** Safe in an HTTP body. |

Plus `as_dict() -> dict[str, str]` returning `{"code": ..., "message": ...}`.
**Serialize with `[w.as_dict() for w in result.warnings]`** — a bare
`list(result.warnings)` now holds dataclasses and will raise `TypeError` in
`json.dumps`.

### The closed set

Match on the module constants, never on the literals. All three are exercised
and verified free of internal names.

| constant | value | when |
|---|---|---|
| `WARNING_CODE_DISCARD_HIGH` | `"discard_fraction_high"` | union discard at/above the warn limit; result still returned |
| `WARNING_CODE_DISCARD_EXCESSIVE` | `"discard_fraction_excessive"` | at/above the fail limit; training stopped, raised as `ExcessivePass1DiscardError` |
| `WARNING_CODE_CHANNEL_CULL_HIGH` | `"channel_cull_fraction_high"` | one channel over its per-channel cull share; **emitted once per offending channel, so this code can repeat in one result** |

`PASS1_WARNING_CODES` is the frozenset of all three. If a code is added, it is
added there in the same edit.

### The remediation text moved to the log, it was not deleted

A reviewer praised those messages, so they are intact — just relocated. Each
producer (`mad_scrub_warnings._channel_cull_warning`,
`pass1_discard_guard._with_warning`) logs the full engineering detail at
WARNING level, naming `BOOTSTRAP_MAD_EXCLUDED_CHANNELS`,
`BOOTSTRAP_MAD_Z_THRESHOLD`, `BOOTSTRAP_CULL_WARN_FRACTION` /
`BOOTSTRAP_CULL_FAIL_FRACTION` and printing the `mad`/`median` pair that proves
raising the z threshold cannot help a bimodal channel. **Nothing API-visible
carries any of it.** `ColdStartBootstrap.run` additionally logs one line with
the codes, so a run can be tied to the warnings it returned.

### The fail message is also the HTTP 400 body

`ColdStartBootstrap.run` raises `ExcessivePass1DiscardError(warning.message)`
and `src/api/routes/bootstrap.py` passes `str(exc)` straight to the caller, so
the fail message had to be tenant-safe as well — not only the returned
warnings. Raise with `.message`, never with the code and never with the log
text.
