# ADR-013: Drift-watch alert policy (plan item C3)

**Status:** Proposed
**Date:** 2026-11-23
**Scope:** When a drift-watch episode may become an operator alert, and in what form.
**Related:** `src/models/drift_watch.py`, `src/api/services/drift_watch_service.py`,
`src/api/routes/drift_watch.py`, `anomaly_episodes` (migration 026),
`docs/handoff/handover-2026-11-15.md` §0 item 3 and §14.

## 1. Context

Drift watch runs a two-sided CUSUM on weather-removed hourly residuals. It runs at the end of
each CSV upload (`run_drift_watch_shadow`, both upload paths) and by `backfill_drift_watch`. It
does not run on the live path. Today it is shadow only: episodes go to `drift_watch_episodes`
and the Drift Watch page. No episode reaches an operator alert.

Facts from the code that this decision depends on:

- **Calibration rests on one episode.** `calibrate()` fits on the first 8 weeks and picks `h`
  by binary search on the next 8 weeks (`GAP_WEEKS = 0`), that is 1.84 months. One false
  episode is 0.54/month. The `temp_paired` figure of 0.55/month is therefore one episode. A
  Poisson count of 1 is consistent with a true rate anywhere from about 0.01 to 3/month.
  The calibration rate is not evidence that the input meets the budget.
- **The calibration window is in-sample and one season.** It is also where the self-test
  injects faults. `k` and `h` are frozen after that. The rolling refit adapts only the Ridge
  model and `(mu, sigma)`.
- **The first scoring after calibration reaches back into the calibration data.** On that
  call `last_processed_ts` is `None`, so `update()` scores the whole context frame
  (`BASELINE_WEEKS + 2` = 10 weeks before `upload_end`). That frame overlaps the fit and
  calibration windows. Some "year view" episodes can therefore be in-sample.
- **The refit overwrites the record of the calibration window.** `_rolling_refit` rewrites
  `fit_window_start/end` in `calibration_json`. After the first refit, the stored data cannot
  tell where out-of-sample data starts.
- **The stored offset is one hour's residual.** `current_offset_native = z * sigma` at the
  alarm hour. The upsert overwrites `offset_estimate` with the latest alarm. The range
  0.59–7.60 °C in the year view is a set of single-hour values, not sustained offsets.
- **Episode IDs are not stable.** `episode_id = f"{input_id}_{seq}"`. Backfill deletes all
  episodes and starts `seq` again. A label or alert keyed on `episode_id` breaks on the next
  backfill.
- **An episode end does not mean the fault is gone.** The refit drops only hours with
  `|z| > ELEVATED_Z_CEIL` (3.0). A steady offset below 3 sigma enters the trailing baseline,
  and after about 8 weeks the model absorbs it. The episode then ends without a repair.
- **A paired-probe residual cannot say which probe drifted.** `temp_paired` is
  `temperature − plant_temperature` minus a covariate model.

Measured in production (tenant_1/gh_A, 2026 year, backfill 2026-11-23): `temp_paired` armed,
14 episodes in the year view (about 1.2/month over 12 months), one ongoing since 2026-11-12 at
−2.97 °C. `rh_virtual` shadow (ceiling hit, 1.66/month), 12 episodes. `hd_physics` derived,
0 episodes. Production gave 26 episodes, a local run of the same CSV gave 25. The 12-month
denominator is too generous: the input cannot arm before 16 weeks of history. If the data
starts in January, about 8.3 months are out-of-sample. If all 14 episodes are out-of-sample,
the rate is about 1.7/month. This was not measured here (no shell); see §8 step 9.

## 2. Decision summary

1. Alert eligibility needs **armed AND out-of-sample evidence**: at least 12 weeks of scored
   data after the calibration window, and an alert-eligible episode count in the trailing
   13 weeks at or below budget. The calibration-window rate alone is never enough.
2. Every unlabelled episode counts as a false alarm. Only an operator "confirmed drift" label
   takes an episode out of the count.
3. Alert state is separate from calibration status, with hysteresis. Breaching the upper limit
   suspends alerts automatically. Re-enabling is manual.
4. One alert per episode, `severity = warning` (advisory), never urgent, written to
   `anomaly_episodes` with `detector = 'drift_watch'`.
5. An alert needs a sustained offset of at least **2 × min_shift**, measured by a robust
   estimator, not the single-hour value.
6. `stuck_fault` suppresses a drift alert on any channel that the input reads. Upload-path
   `calibration_drift` hits are linked to the alert, not suppressed.
7. Global flag default OFF. Enable per input, per scope. tenant_1 stays off until the steps
   in §7 are done.

## 3. Gate for an input to alert

An input's `alert_state` is one of `off`, `eligible`, `enabled`, `suspended`. It is a new
column, separate from `status` (`armed`/`shadow`/`derived`), because `shadow` already means
"calibration constraints not met". Reusing it would hide why an input is silent.

**Out-of-sample (OOS) data** is data strictly after `calibration_window_end`. That is a new,
immutable field on `CalibrationResult`. The refit must not change it. Episodes with
`first_detected_ts <= calibration_window_end` are in-sample. They are shown on the page but
never counted, in either direction.

**OOS false-alarm count**, over the trailing `RATE_WINDOW_WEEKS = 13` (about 3 months), counts
new episodes that:
- pass the alert filters (§5 minimum offset, §6 suppression), because that is what an
  operator would see; and
- do not carry the label `confirmed_drift`.

Unlabelled episodes count as false alarms. That makes the figure an upper bound on the true
false-alarm rate. It is the only label-free estimate that cannot be too optimistic. The raw
count (all new episodes, no filters) is also shown, for diagnosis only.

| Transition | Rule |
|---|---|
| `off` → `eligible` | `status == armed`, at least `MIN_OOS_WEEKS = 12` weeks of OOS scored data, and an OOS count of **≤ 3** in the trailing 13 weeks (≈ 1.0/month, the budget) |
| `eligible` → `enabled` | Manual, per scope and input (admin endpoint). Never automatic |
| `enabled` → `suspended` | Automatic, when the trailing 13-week count is **≥ 7**. Also when `status` stops being `armed` |
| `suspended` → `eligible` | Automatic, when the count comes back to ≤ 3. Re-enabling stays manual |

Why 7: at a true rate equal to the budget (mean 3 per 13 weeks), P(count ≥ 7) ≈ 3.4% per
window, so a healthy input is rarely suspended by chance. P(count ≥ 6) ≈ 8.4% was judged too
jumpy. The gap between 3 and 7 is hysteresis, so the state does not flap. Every transition is
written with a reason and a timestamp. All four numbers are `src/config.py` constants.

On suspension the detector keeps running and episodes keep being stored. Only the alert writer
stops. The input is **not** recalibrated automatically: a new `h` fitted on recent data can
absorb a real drift, and the cause (season, a model regime break, a real fault) needs a person.

**Rejected:**
- *Calibration-window rate only* (today's arming rule). One episode in 1.84 months, in-sample,
  one season. 0.55 → about 1.7 out-of-sample on tenant_1 is the example.
- *A held-out replay period as the only gate.* It is useful once, before enabling (the backfill
  year is such a replay), but it goes stale. The rolling OOS count keeps measuring.
- *Count only operator-marked false alarms.* Silence would then look like zero false alarms.
  Operators do not label most alerts.
- *Auto-demote by setting `status = shadow`.* That mixes calibration meaning with alert policy.
  It also would be undone by a recalibration.

## 4. Alert form

**Granularity.** One alert per drift episode, keyed by
`(tenant_id, greenhouse_id, input_id, direction, first_detected_ts)`. `first_detected_ts`
depends only on the data, so the key survives a backfill. `episode_id` does not (§1).
Continuation events (`is_new = False`) update the same alert: `end_ts`, offset, `is_ongoing`.
A refit never creates an alert. A new alert in the same direction needs a new episode, so
`DRIFT_WATCH_QUIET_HOURS` (72 h) of silence first. That is the dedup rule. No second timer is
added.

**Recency.** An alert is written only if the episode's `last_confirmed_ts` is within
`ALERT_RECENCY_DAYS = 14` of the newest scored hour. Uploading a year of history raises
historical episodes on the page, not twelve alerts at once. `backfill_drift_watch` never writes
alerts.

**Storage.** Upsert into `anomaly_episodes` with `detector = 'drift_watch'`,
`fault_type = 'calibration_drift'`, `channel = target_channel`, `onset_ts` = the episode's first
onset estimate, `detected_ts = first_detected_ts`, `trigger_value` = robust offset,
`is_ongoing` while confirmations continue. `release_ts` stays NULL: the one-way check
constraint already allows a closed episode with no release. When an episode ends, the UI says
"no longer detected", not "resolved", because the refit can absorb a steady offset (§1).

**Severity.** Always `warning` (advisory). Never `critical`/urgent. Reasons: the evidence is
hourly means with a delay of hours to days; an offset is a measurement fault, not a plant
emergency; and the fast, large faults (frozen sensor, out of bounds, band breach) already have
urgent detectors. Large offsets sort first, but they do not escalate.

**What the operator sees:**
- Channel in plain words, and for paired probes both probes: "Greenhouse air temperature and
  plant temperature disagree by 2.9 °C. One of the two probes may have drifted." Do not name
  one probe as the faulty one; the residual cannot tell.
- For a virtual sensor: "RH reads 6 %RH below what temperature, CO₂ and light predict."
- Direction (reads high or low compared to the reference), robust offset with units to 0.1,
  estimated onset, first detected, last confirmed, ongoing or not.
- The monitor's expected false-alarm rate (budget and current OOS figure), so the operator can
  weigh the alert.
- Two actions: **Confirmed drift** and **False alarm**, with an optional note.

**Feedback.** Labels go in a new table `drift_watch_feedback`, keyed by the same natural key,
with `label`, `note`, `labelled_at`, `labelled_by`. Backfill never deletes it. `false_alarm`
and no label both count in §3; `confirmed_drift` does not. The existing `operator_feedback`
table is not reused: it has no `greenhouse_id` and is keyed by an LSTM `sequence_id`.

## 5. Minimum offset for alerting

An alert needs `|robust offset| >= DRIFT_WATCH_ALERT_MIN_OFFSET_MULT × min_shift`, with the
multiplier at **2.0**: 1.0 °C, 4.0 %RH, 0.6 g/m³.

The robust offset is the median of `(residual − mu)` over `[onset_estimate, last_confirmed]`,
computed in `DriftWatch.update()` and carried on `DriftEpisode`. The single-hour value stays
for diagnosis.

Reasons: `min_shift` (0.5 °C) is a detection target, chosen so the self-test can prove
sensitivity. It is not an action threshold. A 0.5 °C disagreement is within the usual
tolerance of greenhouse probes, and an operator cannot act on it. 2× is the middle self-test
level, so detection is tested there. It is also below the upload EMA bias limit (2 °C, 5 %RH),
so drift watch still adds value under that detector. Episodes below the limit stay visible on
the Drift Watch page.

**Rejected:** 1× (alerts that cannot be acted on, and the 0.59 °C case); 4× (2 °C overlaps the
existing bias detector, so drift watch would add little); thresholds on the single-hour offset
(noisy, and the stored value is just the last alarm hour).

The year-view range cannot decide how many of the 14 episodes pass, because those values are
single-hour values. Recompute with the robust estimator first (§8 step 9).

## 6. Interaction with other detectors

Precedence on a channel: `stuck_fault` > `out_of_bounds` / `humidity_fault` > `drift_watch`.

- **stuck_fault suppresses.** If a `stuck_fault` row in `anomaly_episodes` overlaps the
  episode span on *any* channel the input reads (target, reference probe, predictors), no drift
  alert is written. The episode records `suppressed_by`. A frozen sensor makes a residual offset
  trivially. The December humidity freeze is the example: `stuck_fault` caught it, and
  `rh_virtual` would have reported the same event a second time.
- **Upload `calibration_drift` and bias hits are linked, not suppressed.** They are row-level,
  on 20-sample windows, already shipped, and part of the accuracy gate. The drift alert lists
  them as "also seen by the upload level-shift check". Suppressing them would change shipped
  behaviour and the composite score for no gain.
- **Drift inputs on the same channel and direction** with overlapping spans become one alert.
  The input with the larger robust offset is primary.
- **Cascade.** When an alert is open on a channel that another input uses as a predictor
  (temperature in `rh_virtual`), the second input's alert says "may be caused by the open
  temperature drift". It is not suppressed, because both can be true.

## 7. Rollout

- `DRIFT_WATCH_ALERTS_ENABLED` (env, default **False**) is the global kill switch. With it off,
  no `alert_state` leaves `off` and no `drift_watch` row reaches `anomaly_episodes`.
- Enable per `(tenant, greenhouse, input)` only from `eligible` (§3), by an admin action.
- Before any scope is enabled: the 25 vs 26 difference is explained (§9), and the robust-offset
  and suppression rules have been replayed on that scope's history and reviewed.

**tenant_1 / gh_A** stays `off`. 1.2/month over 12 months is above budget, and the honest OOS
figure may be about 1.7. Before it can move:
1. Measure the OOS denominator and split the 14 episodes into in-sample and OOS.
2. Replay with robust offset, the 2× limit and stuck suppression. Count alert-eligible OOS
   episodes in each trailing 13-week window.
3. Review the episodes with the grower and record labels, the ongoing −2.97 °C one first. A
   physical probe check now is the cheapest real label available.
4. If the trailing count stays above 3, the fix is the detector, not the policy. Look at a
   longer, multi-season calibration window for `h` (one episode in 8 weeks is not a measurement).
   That is ml-engineer work under a separate item.

`rh_virtual` cannot become eligible while it is `shadow`. It waits for the drift-RH model item.

## 8. Implementation plan

| # | Owner | Work |
|---|---|---|
| 1 | ml-engineer | `src/config.py`: `DRIFT_WATCH_ALERTS_ENABLED` (False), `..._ALERT_MIN_OFFSET_MULT` (2.0), `..._ALERT_MIN_OOS_WEEKS` (12), `..._ALERT_RATE_WINDOW_WEEKS` (13), `..._ALERT_ENABLE_MAX_COUNT` (3), `..._ALERT_SUSPEND_MIN_COUNT` (7), `..._ALERT_RECENCY_DAYS` (14). Each with its reason, like the existing block |
| 2 | ml-engineer | `src/models/drift_watch.py`: immutable `calibration_window_end` on `CalibrationResult` (refit must not touch it; `from_dict` back-compat); robust offset on `DriftEpisode`; a pure function for the §3 count and transitions (episodes, labels, window → count, next state) |
| 3 | backend-engineer + ml-engineer | Explain 25 vs 26 (§9). Blocks step 10 |
| 4 | backend-engineer | `alembic/` + `src/db/models.py`: `drift_watch_inputs.alert_state` (+ `alert_state_reason`, `alert_state_changed_at`); `drift_watch_episodes.offset_robust`, `suppressed_by`; new `drift_watch_feedback` table on the natural key |
| 5 | backend-engineer | `drift_watch_service.py`: after scoring, alert writer (gates §3, §5, §6, recency) → upsert `anomaly_episodes`; gate evaluation and auto-suspend after each upload; backfill writes no alerts, keeps feedback, recomputes `alert_state` at the end |
| 6 | backend-engineer | `src/api/routes/drift_watch.py`: feedback POST on the natural key; OOS count, budget and `alert_state` on `GET /inputs`; admin enable/disable that rejects any input not `eligible` |
| 7 | frontend-engineer | `web/`: Drift Watch page shows OOS figure vs budget, `alert_state` and reason, in-sample marker, feedback buttons; drift alert card in the anomaly feed with the §4 wording (paired-probe text never names one probe) |
| 8 | qa-engineer | `tests/`: transition table with hysteresis edges (3/4, 6/7); flag off writes nothing; one alert across refits, re-uploads and a backfill; recency; offset limit uses the robust value; stuck suppression on target and on a predictor; feedback survives backfill; in-sample episodes not counted. Full suite under `./venv` |
| 9 | backend-engineer (script) → orchestrator review | tenant_1 measurement (§7 steps 1–2) as a dry-run report; no writes to production rows |
| 10 | orchestrator, with user approval | Per-scope enable, only after steps 3 and 9 and the grower review |

## 9. Open questions

1. **25 vs 26 episodes.** Must be explained before any enable, because the gate counts
   episodes. Candidates seen in the code, not yet tested:
   (a) `_discover_available_columns` samples the newest 200 rows *without* the synthetic filter,
   so live or synthetic rows in production can change which covariates `build_specs` picks;
   (b) `load_hourly_frame` excludes only `synthetic:` rows, so production `live` rows or other
   uploads in the scope enter the frames; (c) a different `data_start` moves the weekly chunk
   grid, and with it the calibration chunk and `h`; (d) Postgres and SQLite timestamp handling
   at resample and chunk edges. First diagnostic: diff both runs' `calibration_json` (`k`, `h`,
   `mu`, `sigma`, windows, feature names) and match episodes by `first_detected_ts`.
2. Exact OOS denominator for tenant_1, and how many of the 14 episodes fall in the first-score
   overlap (§1).
3. Should the first scoring after calibration skip in-sample hours instead of only labelling
   them? It changes stored history, so it is left to ml-engineer to propose.
4. A steady offset under 3 sigma is absorbed by the refit after about 8 weeks. Should an open,
   alerted episode freeze the refit for that input? Probably yes. It needs a measurement first.
5. Drift watch runs only on uploads. Alert latency is the upload cadence. Live-path drift watch
   is out of scope.
6. Notification delivery (email or push) for advisory alerts is out of scope. In-app only.
7. Per-tenant budget overrides (`customer_model_config`) are not proposed until one tenant has
   run enabled.
