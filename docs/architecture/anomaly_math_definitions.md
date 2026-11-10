# Anomaly Mathematical Definitions

> **Citation drift notice (2026-11-09).** The `file:line` references below were read at commit `8de9cb3` (2026-10-09) and have not been re-swept since; the code has moved under them. Two spot-checks at commit `54c6584`: the `STUCK_WALLCLOCK_CHANNELS` citation in §4 points at `src/config.py:908` but the constant now sits at `src/config.py:915` (the claim itself still holds — the tuple is still `("temperature", "humidity", "co2")`); the `spike_mad` vote-key citation `comprehensive_pipeline.py:161-170` checked clean. Treat every other line number as approximate, and treat a line number as verified at `54c6584` only where this document says so explicitly. The formulas and claims are not known to have drifted. A full citation sweep is ticketed as BACKLOG-30.

Formal, per-anomaly-type definitions of every anomaly the system detects:
notation, statistics, decision rules, parameters, guards and score mappings.
This is the mathematical companion to the operator-facing prose catalog
`docs/ANOMALY_CATALOG.md`. It carries no operator advice and no UI guidance.

**Provenance rule.** Every formula, constant, window length and comparison
operator below was read out of the implementation and `src/config.py` at
commit `8de9cb3` (2026-10-09). Where the code and the catalog disagree, this
document states what the code does and lists the disagreement in
[section 7](#7-discrepancies-found). Nothing has been reconciled toward the
catalog, and no quoted value has been rounded or adjusted.

**Reading the `file:line` references.** `path:N` is the line on which the
statement or decision is made; `path:N-M` is the span of the function or block.
Config constants are referenced as `src/config.py:N`.

---

## Table of contents

1. [Notation](#1-notation)
2. [Pipelines and where each detector runs](#2-pipelines-and-where-each-detector-runs)
3. [Rule detectors (sensor-fault layer)](#3-rule-detectors-sensor-fault-layer)
   - 3.1 `spike_fault` (rolling MAD)
   - 3.2 `out_of_bounds` / hard-limit gate
   - 3.3 `stuck_fault` — A: sample-indexed (batch), B: wall-clock (upload), C: SCADA flatline
   - 3.4 `calibration_drift` — A: CUSUM, B: BOCPD, C: physics `co2_out_of_range`
   - 3.5 `accuracy_decline` — A: noise / precision, B: multivariate Mahalanobis
   - 3.6 `bias_offset` — A: dual-sensor cross-difference, B: single-sensor EMA, C: midpoint pre-check
   - 3.7 `correlation_breakdown`
   - 3.8 `thermodynamic_contradiction` / psychrometric validation
   - 3.9 `diurnal_deviation`
   - 3.10 `polling_dropout`
4. [Crop band breaches](#4-crop-band-breaches)
5. [SCADA / climate-control rules](#5-scada--climate-control-rules)
6. [Multivariate and learned detection](#6-multivariate-and-learned-detection)
   - 6.1 LSTM-VAE reconstruction score
   - 6.2 Adaptive thresholds (EWMA, POT/GPD)
   - 6.3 Ensemble vote
   - 6.4 Physics validator
   - 6.5 Pattern classification and `FaultClassifier`
   - 6.6 Attribution and confidence
   - 6.7 Upload (cold-run) path: file-calibrated LSTM threshold and rolling z-score
   - 6.8 Live path
   - 6.9 Canonical score transform (ADR-005)
7. [Gas anomalies](#7-gas-anomalies)
8. [OT network anomalies](#8-ot-network-anomalies)
9. [Precedence and combination](#9-precedence-and-combination)
10. [Discrepancies found](#10-discrepancies-found)

---

## 1. Notation

| Symbol | Meaning |
|---|---|
| $x_t$ | Reading of one channel at timestep $t$ in **physical units** (°C, %RH, ppm, W/m²). |
| $\tilde{x}_t$ | The same reading **min-max normalised** to $[0,1]$: $\tilde{x} = (x - \ell)/(u - \ell)$ for a range $(\ell, u)$. The batch pipeline receives $\tilde{x}$ and recovers $x = \ell + \tilde{x}(u-\ell)$ with the static ranges `TEMP_RANGE` $(20,30)$, `HUMIDITY_RANGE` $(50,80)$, `CO2_RANGE` $(300,500)$, `LIGHT_RANGE` $(0,1200)$ (`src/config.py:139-149`, `comprehensive_pipeline.py:378-382`). The upload path normalises with **adaptive** per-file ranges (§6.7). |
| $\mathbf{x}_t \in \mathbb{R}^d$ | Joint reading over the $d = 4$ canonical channels in order `[temperature, humidity, co2, light]` (`SENSOR_NAMES`, `src/config.py:163`). |
| $n$ | Length of the sequence under evaluation. In the batch pipeline $n = $ `SEQ_LEN` $= 20$ (`src/config.py:75`). |
| $W^k_t$ | Trailing window of $k$ samples ending at $t$: $W^k_t = (x_{t-k+1}, \dots, x_t)$. Every rolling detector evaluates $t \ge k-1$ only; the first $k-1$ positions are **never** flagged (warm-up). |
| $\operatorname{med}(W)$ | Sample median. |
| $\operatorname{MAD}(W)$ | Median absolute deviation about the median: $\operatorname{med}(\lvert x_i - \operatorname{med}(W)\rvert)$. **Unscaled** unless a consistency constant is stated explicitly. |
| $\bar{x}(W)$ | Arithmetic mean. |
| $s^2_0(W)$, $s^2_1(W)$ | Sample variance with `ddof=0` (divide by $k$) and `ddof=1` (divide by $k-1$) respectively. The subscript is written wherever the code's choice matters. $s_0, s_1$ are the corresponding standard deviations. |
| $\rho(W_a, W_b)$ | Pearson correlation of two equal-length windows. |
| $\operatorname{EMA}_\alpha$ | Exponential moving average with smoothing factor $\alpha$: $m_t = \alpha x_t + (1-\alpha) m_{t-1}$. |
| $\operatorname{clip}_{[a,b]}(v)$ | $\min(b, \max(a, v))$. |
| $\mathbb{1}[\cdot]$ | Indicator function. |
| $\lambda_{\text{season}}(c)$ | Seasonal σ-scale factor for channel $c$ (§1.1). Equals $1.0$ when seasonal baselines are absent or sparse. |
| $P_q(S)$ | The $q$-th percentile of the multiset $S$ (NumPy linear interpolation). |

Throughout, comparison strictness is written exactly as in the code: `>` is
$>$, `>=` is $\ge$.

### 1.1 Seasonal σ-scale factor

`src/models/seasonal_scaler.py:38-58`

$$
\lambda_{\text{season}}(c) =
\begin{cases}
\operatorname{clip}_{[0.5,\,2.0]}(o_{\text{season}}) & \text{if an operator override } o > 0 \text{ exists} \\
1.0 & \text{if } n_{\text{season},c} < \texttt{SEASONAL\_MIN\_SAMPLES} \text{ or } \sigma_{\text{global},c} \le 0 \text{ or } \sigma_{\text{season},c} \le 0 \\
\operatorname{clip}_{[0.5,\,2.0]}\!\left(\dfrac{\sigma_{\text{season},c}}{\sigma_{\text{global},c}}\right) & \text{otherwise}
\end{cases}
$$

| Symbol | Constant | Default |
|---|---|---|
| clip lower | `SEASONAL_SCALE_MIN` (`src/config.py:1109`) | `0.5` |
| clip upper | `SEASONAL_SCALE_MAX` (`src/config.py:1110`) | `2.0` |
| min samples | `SEASONAL_MIN_SAMPLES` (`src/config.py:1108`) | `500` |

The factor multiplies the spike MAD floor (§3.1), the noise target (§3.5A,
fallback branch only) and the CUSUM decision interval $h$ (§3.4A).

---

## 2. Pipelines and where each detector runs

Three code paths produce anomaly decisions; a detector's math is identical
across paths but its inputs, gating and stored score differ.

| Path | Entry | Input space | Detectors |
|---|---|---|---|
| **Batch / fused pipeline** | `src/api/services/comprehensive_pipeline.py:634-1236` (`_run_with_components`) | one normalised sequence $\tilde{\mathbf{x}}_{1..20}$ | §3 (all), §4, §5, §6.1-6.6 |
| **Upload (cold run)** | `src/api/services/upload_portions.py:559-776`, driven by `cold_run_upload.py` | CSV rows, physical units, adaptive normalisation for the LSTM | §3.3B (wall-clock stuck), §6.7 (LSTM file-calibrated, rolling z), §3.8 (psychrometric pair suppression) |
| **Live (SSE)** | `src/live/scoring.py:103-144` | sliding window of 20 raw vectors, static normalisation | §6.8 (LSTM only) |

Gas (§7) and OT network (§8) detectors have their own API routes and do not
participate in any of the three.

---

## 3. Rule detectors (sensor-fault layer)

Sections are keyed by the `fault_type` value written to `incidents.fault_type`.
The permitted vocabulary is the CHECK constraint `ck_incidents_fault_type`
(`alembic/versions/019_add_band_breach_fault_types.py:21-46`): `calibration_drift`,
`bias_offset`, `stuck_fault`, `accuracy_decline`, `spike_fault`, `unknown`,
`polling_dropout`, `out_of_bounds`, `correlation_breakdown`, `diurnal_deviation`,
`thermodynamic_contradiction`, `under_heating`, `over_heating`,
`dehumidification_failure`, `humidity_saturation_risk`, `thermal_divergence`,
`temp_band_high`, `temp_band_low`, `vpd_band_high`, `vpd_band_low`,
`humidity_band_high`, `humidity_band_low`, `co2_band_high`, `co2_band_low`.

In the batch pipeline, every rule in this section (except §3.2 and §3.10, which
short-circuit) contributes a **vote** keyed by a profile name in
`_RULE_VOTE_PROFILES` (`comprehensive_pipeline.py:150-338`). The vote confirms
`is_anomaly = True` and, if it is the *first* vote inserted, sets `fault_type`
(§9). Votes do **not** change the stored `anomaly_score` (§6.1 score mapping).

### 3.1 `spike_fault` — rolling median absolute deviation

- **Vote key:** `spike_mad` → `fault_type = "spike_fault"`, root cause `transient_spike` (`comprehensive_pipeline.py:161-170`).
- **Implementation:** `src/models/spike_mad_detector.py:43-73`; pipeline call `comprehensive_pipeline.py:801-819`.

**Inputs.** Each canonical channel separately, in physical units
($x = $ denormalised sequence, `comprehensive_pipeline.py:695`), window
$k = $ `SPIKE_MAD_WINDOW_K`.

**Statistic.** For $t \ge k-1$:

$$
m_t = \operatorname{med}(W^k_t), \qquad
\operatorname{MAD}_t = \operatorname{med}\big(\lvert W^k_t - m_t\rvert\big), \qquad
\widehat{\operatorname{MAD}}_t = \max\big(\operatorname{MAD}_t,\ f_c\,\lambda_{\text{season}}(c)\big), \qquad
\Delta_t = \lvert x_t - m_t \rvert
$$

where $f_c$ is the per-channel floor `SPIKE_MAD_FLOOR[c]` and the seasonal
scaling is applied at the call site (`comprehensive_pipeline.py:806-809`).

**Decision rule** (`spike_mad_detector.py:70`):

$$
\text{flag}_t = \mathbb{1}\big[\widehat{\operatorname{MAD}}_t > 0\big] \land \mathbb{1}\big[\Delta_t > c \cdot \widehat{\operatorname{MAD}}_t\big]
$$

The channel votes if $\exists\, t:\ \text{flag}_t$ (`comprehensive_pipeline.py:811`).

**Parameters.**

| Symbol | Constant | Default |
|---|---|---|
| $k$ | `SPIKE_MAD_WINDOW_K` (`src/config.py:1147`) | `10` |
| $c$ | `SPIKE_MAD_TUNING_C` (`src/config.py:1148`) | `3.0` |
| $f_{\text{temperature}}$ | `SPIKE_MAD_FLOOR["temperature"]` (`src/config.py:1154`) | `0.1` °C |
| $f_{\text{humidity}}$ | `SPIKE_MAD_FLOOR["humidity"]` (`:1155`) | `0.2` % |
| $f_{\text{co2}}$ | `SPIKE_MAD_FLOOR["co2"]` (`:1156`) | `10.0` ppm |
| $f_{\text{light}}$ | `SPIKE_MAD_FLOOR["light"]` (`:1157`) | `4.0` W/m² |

**Edge cases / guards.**
- $n < k$ → all `False` (`:61-62`); positions $t < k-1$ never fire.
- $\widehat{\operatorname{MAD}}_t = 0$ (only possible when $f_c = 0$ and the window is constant) → cannot fire (`:70`).
- Constructor rejects $k < 2$ and $c \le 0$ (`:36-39`).
- NaN is not guarded: a NaN in the window propagates to `np.median`, giving NaN comparisons that evaluate `False`. The batch pipeline rejects NaN input upstream (`inference.py:54-55`).

**Score mapping.** None of its own: `detector_scores["spike_mad"] = 1.0` is
recorded on the incident (`comprehensive_pipeline.py:1154`); `anomaly_score`
is the LSTM score (§6.1).

**Related but separate.** `SPIKE_DELTA_MAX` (`src/config.py:1133-1138`) is
defined but not consumed by any module covered in this document.
`VELOCITY_LIMITS` (`src/config.py:856-861`) is consumed only by the pattern
classifier's `score_spike` (§6.5), not by this rule.

### 3.2 `out_of_bounds` — hard physical limits (short-circuit gate)

- **Stored `fault_type`:** `"spike_fault"` — see Discrepancy D1. The result builder `_build_oob_result` (`comprehensive_pipeline.py:1320-1360`) writes `fault_type="spike_fault"`, `pattern_type="spike"`, `root_cause="sensor_out_of_bounds"`, `severity="critical"`, `urgency="immediate"`.
- **Implementation:** `src/models/out_of_bounds_detector.py:26-58`; gate at `comprehensive_pipeline.py:781-799`.

**Inputs.** Each canonical channel in physical units, every timestep of the
20-step window.

**Decision rule** (`out_of_bounds_detector.py:40`, `:57`):

$$
\text{oob}_t = \mathbb{1}[x_t < L_c] \lor \mathbb{1}[x_t > U_c]
$$

The pipeline short-circuits and returns the OOB result if $\exists\,c,t:\ \text{oob}_{c,t}$.

**Parameters** (`OOB_LIMITS`, `src/config.py:1161-1170`).

| Channel | $(L_c, U_c)$ |
|---|---|
| temperature | `(-40.0, 80.0)` |
| humidity | `(0.0, 100.0)` |
| co2 | `(0.0, 5000.0)` |
| light | `(0.0, 2000.0)` |

**Edge cases.** Unknown sensor name → never flags (`:37-38`, `:53-54`). Both
bounds are strict, so $x_t = U_c$ is in bounds. No window, no history.

**Score mapping.** `anomaly_score = 1.0`, `confidence = 1.0` (sentinel; the
LSTM is not run before this gate) (`:1329-1331`). An embedding is computed
afterwards for persistence only (`:1599-1605`).

### 3.3 `stuck_fault` — frozen / unresponsive sensor

Three independent implementations exist. Each is given separately because
their statistics, input spaces and gates differ.

#### 3.3A Sample-indexed (`StuckFaultDetector.detect`) — ensemble pre-check, batch path

- **Implementation:** `src/models/stuck_fault_detector.py:62-101`; invoked by `EnsembleAnomalyDetector.detect` (`ensemble_detector.py:293-305`) on the **normalised** sequence $\tilde{\mathbf{x}}$ (the array passed to `ensemble.detect` is `seq_array`, `comprehensive_pipeline.py:1556`).
- **Resulting `fault_type`:** `"stuck_fault"` via `ensemble_result.fault_type` (`comprehensive_pipeline.py:1569-1571`), unless physics confirmed or a later rule vote overrides it (§9).

**Statistic.** For channel $c$ over the full sequence of length $n$ with window $k$:

$$
v_c = \begin{cases}
\min_{t \ge k-1} s^2_1\big(\tilde{W}^k_{t}\big) & n \ge k \\
s^2_0(\tilde{x}_{1..n}) & n < k
\end{cases}
\qquad
r_c = \max_t \tilde{x}_{c,t} - \min_t \tilde{x}_{c,t}
$$

(sliding-window variance with `ddof=1`, `:79-81`; whole-sequence `np.var`
default `ddof=0` in the short branch, `:83`). NaN variance is replaced by
$1.0$ (`:86`).

**Decision rule** (`:88-100`):

$$
\text{stuck}_c = \mathbb{1}[r_c < \theta_{\text{flat}}] \land \mathbb{1}[v_c < \theta_{\text{var}}]
$$

Any channel stuck → the ensemble returns `is_anomaly=True`,
`ensemble_score=1.0`, `agreement_count=4`, `fault_type="stuck_fault"` without
running the four-member vote (`ensemble_detector.py:296-305`).

**Parameters.**

| Symbol | Constant | Default |
|---|---|---|
| $k$ | `STUCK_WINDOW_K` (`src/config.py:878`) | `10` |
| $\theta_{\text{var}}$ | `STUCK_VARIANCE_THRESHOLD` (`src/config.py:879`) | `1e-4` |
| $\theta_{\text{flat}}$ | `STUCK_FLAT_RANGE_THRESHOLD` (`src/config.py:881`) | `1e-4` |

Both thresholds act in **normalised** units on this path: $r_c < 10^{-4}$
corresponds to a physical range below $10^{-4}(u-\ell)$, i.e. $0.001$ °C for
temperature, $0.003$ %RH, $0.02$ ppm, $0.12$ W/m².

**Edge cases.** `skip_channels` are reported `False` (`:93-95`); the ensemble
call passes none. Light is **not** excluded on this path.

`StuckFaultDetector.detect_series` (`:104-135`, $s^2_1(W^k_t) <$
`STUCK_EPSILON` $= 10^{-4}$, `src/config.py:1173`) is a per-timestep variant;
no pipeline read for this document calls it.

#### 3.3B Wall-clock (`WallclockStuckDetector`) — upload path

- **Implementation:** `src/models/stuck_fault_detector.py:905-1160`; driven by `upload_portions.score_portion` (`:584-586`) and `finalize_scores` (`:688-690`).
- **Alert fields:** `detector="stuck_fault"`, `severity="urgent"`, `method="wallclock_stuck"`, `score=1.0` (`upload_portions.py:155`).
- **Persisted episode:** `anomaly_episodes` row with `fault_type="stuck_fault"` (`anomaly_episode_writer.py:164-165`).

**Inputs.** Channels in `STUCK_WALLCLOCK_CHANNELS` = `("temperature", "humidity", "co2")` (`src/config.py:908`; light excluded), physical units from the normalised CSV frame, row timestamps $\tau_i$.

**Freeze state.** For each channel a freeze opens at the first finite sample
$x_{i_0}$ with `frozen_value` $= x_{i_0}$, `onset` $= \tau_{i_0}$,
`sample_count` $= 1$ (`_open_freeze`, `:479-506`). A subsequent finite row $i$
**extends** the freeze iff

$$
\lvert x_i - x_{i_0} \rvert \le \varepsilon_c \qquad (\text{`:1010`})
$$

in which case `last` $= \tau_i$ and `sample_count` $\mathrel{+}= 1$. Otherwise
the freeze **ends** at row $i$ (release) and a new freeze opens at $x_i$. NaN
and non-finite rows neither extend nor end a freeze (`:993-994`).

**Dual gate** (`_check_gate`, `:1139-1145`), evaluated on every extension row
and at end of input:

$$
D = \frac{\tau_{\text{last}} - \tau_{\text{onset}}}{3600\ \text{s}}, \qquad
\text{fire} = \mathbb{1}[D \ge H_{\min}] \land \mathbb{1}[\text{sample\_count} \ge N_{\min}]
$$

(the code tests `duration_hours < min_hours → None` and
`sample_count < min_samples → None`, so firing requires both $\ge$). A freeze
fires at most once per file (`emitted_onsets`, `:1147-1149`).

**Counts recorded on release** (`FaultWindow`, `:1048-1063`):

$$
\texttt{frozen\_sample\_count} = \text{sample\_count}, \qquad
\texttt{raw\_slot\_count} = \operatorname{round}\!\left(\frac{\tau_{\text{last}} - \tau_{\text{onset}}}{\kappa}\right) + 1
$$

(`expected_slot_count`, `:185-216`; inclusive of both endpoints, rounded to
nearest). $\kappa$ is the freeze's **pinned cadence** in seconds, fixed once:

- **Branch 1 (pre-onset)** `_pre_onset_cadence_seconds` (`:228-275`): $\kappa = \operatorname{med}(E)$ where $E$ is the list of completed-chunk interval medians plus, only if the current chunk has $\ge$ `CADENCE_PIN_MIN_INTERVALS` pre-onset intervals, the median of those. If $E$ is empty the freeze opens unpinned.
- **Branch 2 (in-freeze)** `_in_freeze_cadence_seconds` (`:278-301`): at the instant the gate first fires, $\kappa = \operatorname{med}(\text{row-to-row intervals observed since onset})$. No minimum-count floor applies here.
- Neither branch ever falls back to `CADENCE_EXPECTED_SECONDS`; that constant is only the placeholder for a hand-built or legacy-carried state (`:382`, `:437-476`).

Hence `raw_slot_count − frozen_sample_count > 0` means readings are missing
inside the frozen span.

**Cadence deviation flag** (`flush`, `:1092-1108`): with $\bar\kappa$ the median
of chunk medians,

$$
\text{cadence\_flagged} = \mathbb{1}\!\left[\frac{\bar\kappa}{\kappa_0} > \phi\right] \lor \mathbb{1}\!\left[\frac{\bar\kappa}{\kappa_0} < \frac{1}{\phi}\right]
$$

**Parameters.**

| Symbol | Constant | Default |
|---|---|---|
| $H_{\min}$ | `STUCK_WALLCLOCK_MIN_HOURS` (`src/config.py:889`) | `6.0` h |
| $N_{\min}$ | `STUCK_WALLCLOCK_MIN_SAMPLES` (`src/config.py:892`) | `12` |
| $\varepsilon_{\text{temperature}}$ | `STUCK_WALLCLOCK_EPSILON["temperature"]` (`:900`) | `0.01` °C |
| $\varepsilon_{\text{humidity}}$ | `STUCK_WALLCLOCK_EPSILON["humidity"]` (`:901`) | `0.01` % |
| $\varepsilon_{\text{co2}}$ | `STUCK_WALLCLOCK_EPSILON["co2"]` (`:902`) | `1.0` ppm |
| $\varepsilon_{\text{light}}$ | `STUCK_WALLCLOCK_EPSILON["light"]` (`:903`) | `1.0` W/m² (channel not monitored) |
| fallback $\varepsilon$ | literal in `:997` | `0.01` |
| $\kappa_0$ | `CADENCE_EXPECTED_SECONDS` (`src/config.py:913`) | `300` s |
| $\phi$ | `CADENCE_TOLERANCE_FACTOR` (`src/config.py:916`) | `1.5` |
| pin floor | `CADENCE_PIN_MIN_INTERVALS` (`src/config.py:934`) | `3` |

**Edge cases.**
- Imputed channels in `StuckCarry.skip_channels` are never examined (`:989-990`); a bare `StuckCarry()` has none.
- An open freeze at end of file is flushed through the same gate with `detected_ts = last_ts` (`:1084-1089`); it is persisted `is_ongoing=True`, `release_ts=NULL`.
- Alert `timestamp` is the **onset**; `detected_ts` is the row at which the gate was first satisfied; `duration_hours` is rounded to 2 dp (`:1151-1160`).

**Downstream effects on the statistical detector** (§6.7):
baseline exclusion NaN-s the faulted column over $[\tau_{\text{onset}}, \tau_{\text{last}}]$
(`upload_portions.py:476-556`); alert suppression drops statistical hits on the
faulted column over $[\tau_{\text{onset}}, \tau_{\text{release}} + N_{\text{sup}}]$
with $N_{\text{sup}} = $ `COLD_RUN_STAT_POST_RELEASE_SUPPRESS_HOURS` $= 6.0$ h
(`src/config.py:274`; `upload_portions.py:390-473`), or $[\tau_{\text{onset}}, \infty)$ for an open fired freeze.

#### 3.3C SCADA flatline (`SENSOR_FLATLINE`) — batch path

- **Vote key:** `sensor_flatline` → `fault_type = "stuck_fault"`, root cause `sensor_flatline_frozen_telemetry` (`comprehensive_pipeline.py:272-281`).
- **Implementation:** `src/models/setpoint_scada_detector.py:141-158` (`_is_flatline`), `:324-364` (`_flatline_rules`).

**Inputs.** Physical-unit series for `temperature` (column 0) and `humidity`
(column 1), plus `plant_temp` when a full `t_plant_series` is supplied.

**Decision rule.** With $N_w = $ `FLATLINE_VARIANCE_WINDOW`, $N_s = $ `FLATLINE_IDENTICAL_STEPS`, $m = \min(N_s, n)$ (`:153`):

$$
\text{flat} = \mathbb{1}[n \ge N_w] \land \mathbb{1}[\text{all finite}] \land \mathbb{1}\big[\max(W^{m}_n) - \min(W^{m}_n) = 0\big] \land \mathbb{1}\big[s^2_0(W^{N_w}_n) = 0\big]
$$

(`:149-158`). Both the range test and the variance test are exact equality
with $0$ — bit-identical values are required.

| Symbol | Constant | Default |
|---|---|---|
| $N_w$ | `FLATLINE_VARIANCE_WINDOW` (`src/config.py:977`) | `12` |
| $N_s$ | `FLATLINE_IDENTICAL_STEPS` (`src/config.py:978`) | `24` |

**Edge case.** In the batch pipeline $n = 20 < N_s = 24$, so $m = 20$ and the
rule requires the **entire** 20-step window to be constant (see D25).

**Score mapping.** `ScadaAnomalyRecord.score = 1.0` (`:353`) in `scada_anomalies`; vote only.

### 3.4 `calibration_drift`

Three code paths assign this label.

#### 3.4A CUSUM on EMA-standardised residuals — batch path

- **Implementation:** `src/models/cusum_drift_detector.py:75-92` (recurrence), `:183-207` (EMA baseline); pipeline `comprehensive_pipeline.py:915-947` and `:1006-1009`.
- **Not a vote.** CUSUM sets `is_anomaly = True` and `fault_type = "calibration_drift"` at step 5a unless physics confirmed; any rule vote at step 5c overrides the fault type (§9).

**Residual.** Per channel, per pipeline call, on the **last** normalised
timestep $\tilde{x}_n$ only (`comprehensive_pipeline.py:936-938`). The
estimator state persists across calls (per tenant, in `LearningState`). With
$\alpha = $ `CUSUM_EMA_ALPHA` and $\delta = \tilde{x} - m_{t-1}$:

$$
m_t = m_{t-1} + \alpha\,\delta, \qquad
v_t = (1-\alpha)\big(v_{t-1} + \alpha\,\delta^2\big), \qquad
r_t = \frac{\tilde{x} - m_{t-1}}{\max(\sqrt{v_t},\ 10^{-6})}
$$

(`cusum_drift_detector.py:199-207`). First observation of a channel:
$m = \tilde{x}$, $v = 0$, $r = 0$ (`:194-197`). Note the residual uses the
**previous** mean but the **updated** variance.

**Recurrence** (`:86-92`):

$$
S^+_t = \max\big(0,\ S^+_{t-1} + r_t - k\big), \qquad
S^-_t = \max\big(0,\ S^-_{t-1} - r_t - k\big), \qquad
\text{alarm}_t = \mathbb{1}[S^+_t > h] \lor \mathbb{1}[S^-_t > h]
$$

$h$ is rescaled on every call to $h = h_0 \cdot \overline{\lambda_{\text{season}}}$,
the mean seasonal factor over the channels (`comprehensive_pipeline.py:927-932`).
Accumulators are **not** reset on alarm by the pipeline; `alarmed_sensors()`
(`:103-113`) lists channels whose accumulator currently exceeds $h$.

| Symbol | Constant | Default |
|---|---|---|
| $k$ | `CUSUM_K_SIGMA` (`src/config.py:868`) | `0.25` |
| $h_0$ | `CUSUM_H_SIGMA` (`src/config.py:869`) | `4.0` |
| $\alpha$ | `CUSUM_EMA_ALPHA` (`src/config.py:1176`) | `0.01` |
| std floor | `min_std` literal (`cusum_drift_detector.py:174`) | `1e-6` |

**Edge cases.** Residuals are in normalised units. A second, **stub**
`CUSUMDriftDetector` exists in `src/models/drift_detector.py:106-215` whose
`update` always returns `False` (`:170-174`); the pipeline imports the real one
from `cusum_drift_detector.py`.

The unrelated `DriftDetector` in `drift_detector.py:29-103` monitors
reconstruction-**loss** drift for the online learner:
$\text{drift} = (\bar\ell_{\text{recent}} - \bar\ell_{\text{hist}})/(\bar\ell_{\text{hist}} + 10^{-6}) >$ `DRIFT_THRESHOLD` $= 0.3$
over `DRIFT_WINDOW_SIZE` $= 100$ (`src/config.py:363-367`). It produces a
retrain recommendation, not a `fault_type`.

#### 3.4B Bayesian online change-point detection (BOCPD) — batch path

- **Vote key:** `change_point` → `fault_type = "calibration_drift"`, root cause `level_shift_change_point` (`comprehensive_pipeline.py:212-221`).
- **Implementation:** `src/models/bocpd_detector.py:50-150`; pipeline `comprehensive_pipeline.py:897-910`.

**Standardisation** (`:100-103`), on the physical-unit 20-step series:

$$
z_t = \frac{x_t - \operatorname{med}(x)}{\max(\operatorname{MAD}(x),\ 10^{-6})}
$$

**Model.** Normal-Gamma conjugate prior with $\mu_0 = 0,\ \kappa_0 = 1,\ \alpha_0 = 1,\ \beta_0 = 1$ (`:106`), constant hazard $H = 1/\lambda$ (`:47`). For each run-length hypothesis $r$ the predictive is Student-t with
$\nu = 2\alpha_r$, location $\mu_r$, scale $\sqrt{\beta_r(\kappa_r+1)/(\alpha_r\kappa_r)}$ (`:119-121`, density `:153-169`). Run-length posterior update (`:123-136`):

$$
p(r_t = r+1) \propto p(r_{t-1} = r)\, \pi_r(z_t)\,(1-H), \qquad
p(r_t = 0) \propto \sum_r p(r_{t-1} = r)\, \pi_r(z_t)\, H
$$

Sufficient statistics (`:139-146`): $\kappa' = \kappa+1$, $\mu' = (\kappa\mu + z_t)/\kappa'$, $\alpha' = \alpha + \tfrac12$, $\beta' = \beta + \tfrac12\kappa(z_t-\mu)^2/\kappa'$. MAP run length $\hat r_t = \arg\max_r p(r_t = r)$ (`:148`).

**Decision rule** (`:72-79`, `:82-94`):

$$
\text{cp}_t = \mathbb{1}[\hat r_{t-1} \ge R_{\min}] \land \mathbb{1}[\hat r_t \le 2] \land \bigwedge_{j=1}^{2}\mathbb{1}\big[\hat r_{t+j} \le 2 + j \ \text{ or } t+j \ge n\big]
$$

The pipeline votes only if the **last** timestep is flagged on at least
$S_{\min}$ channels (`comprehensive_pipeline.py:903-905`):
$\text{vote} = \mathbb{1}\big[\lvert\{c : \text{cp}_{c,n}\}\rvert \ge S_{\min}\big]$.

| Symbol | Constant | Default |
|---|---|---|
| $\lambda$ | `BOCPD_HAZARD_LAMBDA` (`src/config.py:1238`) | `250.0` |
| $R_{\min}$ | `BOCPD_MIN_RUN` (`src/config.py:1239`) | `8` |
| $S_{\min}$ | `BOCPD_MIN_SENSORS` (`src/config.py:1242`) | `2` |
| reset run length | `_RESET_RUN_LENGTH` (`bocpd_detector.py:28`) | `2` |
| confirm steps | `_CONFIRM_STEPS` (`:32`) | `2` |
| MAD floor | `_MAD_FLOOR` (`:26`) | `1e-6` |

**Edge cases.** $n < R_{\min}+1$ or any non-finite value → all `False` (`:65-66`). Numerical underflow restarts the recursion and yields `None` at that step (`:127-134`). Because confirmation looks two steps ahead, a change-point at the final timestep is accepted on the end-of-window rule (`:90-91`).

#### 3.4C Physics `co2_out_of_range`

`_PHYSICS_REASON_TO_FAULT["co2_out_of_range"] = "calibration_drift"`
(`comprehensive_pipeline.py:138`); rule in §6.4.

### 3.5 `accuracy_decline`

#### 3.5A Noise / precision degradation — batch path

- **Vote key:** `noise` → `fault_type = "accuracy_decline"`, root cause `sensor_noise_floor_exceeded` (`comprehensive_pipeline.py:151-160`).
- **Implementation:** `src/models/noise_detector.py:92-130` (`detect`, the method the pipeline calls at `comprehensive_pipeline.py:767-771`), `:181-184` (filter).

**Inputs.** Each canonical channel in physical units, the whole 20-step window.

**Statistic.** Zero-phase 2nd-order Butterworth high-pass filter (`scipy.signal.butter(N=2, Wn=0.1, btype="high")` then `filtfilt`), cutoff `HP_CUTOFF = 0.1` of Nyquist (`:76`, `:183-184`):

$$
y = \operatorname{HP}_{0.1}(x_{1..n}), \qquad \hat\sigma^2 = s^2_0(y_{1..n})
$$

**Decision rule** (`:121-124`): if a baseline variance $\sigma^2_{b,c} > 0$ exists for the channel,

$$
\text{noise}_c = \mathbb{1}\!\left[\frac{\hat\sigma^2}{\sigma^2_{b,c}} > \theta_{\text{noise}}\right]
$$

Otherwise (`:126-130`) the fallback is $s_0(y) > \tau_c \cdot$ `NOISE_ALPHA` with $\tau_c = $ `target_override` (seasonally scaled `SENSOR_NOISE_TARGETS[c]`) — unreachable for the four canonical channels because every one has a `NOISE_BASELINE_VARIANCE` entry.

| Symbol | Constant | Default |
|---|---|---|
| $\theta_{\text{noise}}$ | `NOISE_RATIO_THRESHOLD` (`src/config.py:1189`) | `10.0` |
| $\sigma^2_{b,\text{temperature}}$ | `NOISE_BASELINE_VARIANCE["temperature"]` (`:1182`) | `0.01` (°C)² |
| $\sigma^2_{b,\text{humidity}}$ | `:1183` | `0.04` (%)² |
| $\sigma^2_{b,\text{co2}}$ | `:1184` | `25.0` (ppm)² |
| $\sigma^2_{b,\text{light}}$ | `:1185` | `100.0` (W/m²)² |
| min samples | `_MIN_HIGHPASS_SAMPLES` (`noise_detector.py:56`) | `10` |
| fallback multiplier | `NOISE_ALPHA` (`src/config.py:1020`) | `3.0` |
| fallback targets | `SENSOR_NOISE_TARGETS` (`src/config.py:1024-1029`) | temperature `0.05`, humidity `0.1`, co2 `5.0`, light `2.0` |
| window (unused on this path) | `NOISE_HYBRID_WINDOW` (`src/config.py:1179`) | `5` |

**Edge cases.** $n <$ `_MIN_HIGHPASS_SAMPLES` → `False` (`:114-115`). The
rolling per-timestep variant `detect_series` (`:132-179`, window $w$, `ddof=1`,
first $w-1$ positions `False`) is not called by the pipeline (see D5).

#### 3.5B Multivariate joint implausibility (rolling Mahalanobis) — batch path

- **Vote key:** `multivariate` → `fault_type = "accuracy_decline"`, root cause `multivariate_implausibility` (`comprehensive_pipeline.py:201-211`).
- **Implementation:** `src/models/multivariate_mahalanobis_detector.py:51-99`; pipeline `comprehensive_pipeline.py:886-895`.

**Inputs.** The full physical-unit window $\mathbf{X} \in \mathbb{R}^{n \times d}$.

**Statistic.** With noise floors $\mathbf{f}$ = `SENSOR_NOISE_TARGETS` per channel (`:42`, `:44-49`) and a fit set $F$:

$$
\boldsymbol{\mu}_F = \operatorname{med}(F) \ (\text{column-wise}), \qquad
\Sigma_F = \operatorname{cov}(F) + \operatorname{diag}(\mathbf{f}^2), \qquad
D^2_t(F) = (\mathbf{x}_t - \boldsymbol{\mu}_F)^\top \Sigma_F^{-1} (\mathbf{x}_t - \boldsymbol{\mu}_F)
$$

(`:92-99`; `np.cov` default `ddof=1`). Two passes (`:70-79`): pass 1 fits on all
$n$ rows; the $n_{\text{trim}} = \max(1, \operatorname{round}(0.1\,n))$ rows with
the largest $D^2$ are dropped and $D^2$ is recomputed on the refit.

**Decision rule** (`:83`): $\text{flag}_t = \mathbb{1}[D^2_t > \chi^2_{\text{thr}}]$. The pipeline votes on the **last** timestep only (`comprehensive_pipeline.py:890`), attributing all channels.

| Symbol | Constant | Default |
|---|---|---|
| $\chi^2_{\text{thr}}$ | `MAHALANOBIS_CHI2_THRESHOLD` (`src/config.py:1231`) | `18.47` (χ², df=4, p=0.999 per the config comment) |
| trim fraction | `_TRIM_FRACTION` (`:28`) | `0.1` |
| $\mathbf{f}$ | `SENSOR_NOISE_TARGETS` (`src/config.py:1024-1029`) | see §3.5A |

**Edge cases.** $n < 2d$ or any non-finite value → all `False` (`:65-66`). Singular covariance → all `False` (`:94-97`). With $n = 20$, $n_{\text{trim}} = 2$.

### 3.6 `bias_offset`

#### 3.6A Dual-sensor cross-difference — batch path, requires `sensor_pairs`

- **Vote key:** `bias_dual` → `fault_type = "bias_offset"`, root cause `redundant_sensor_bias_offset` (`comprehensive_pipeline.py:171-180`).
- **Implementation:** `src/models/bias_dual_sensor_detector.py:34-66`; pipeline `comprehensive_pipeline.py:840-855`.

**Inputs.** Two caller-supplied co-located series $a_t, b_t$ (`pair["sensor_a"]`, `pair["sensor_b"]`), any units, equal length $\ge 2$.

**Statistic / decision** (`:62-64`): with $\delta_t = a_t - b_t$ and $t \ge w-1$,

$$
\bar\delta_t = \bar{x}\big(\delta_{t-w+1..t}\big), \qquad \text{bias}_t = \mathbb{1}\big[\lvert\bar\delta_t\rvert > \theta_{\text{bias}}\big]
$$

| Symbol | Constant | Default |
|---|---|---|
| $w$ | `BIAS_DUAL_WINDOW` (`src/config.py:1194`) | `10` |
| $\theta_{\text{bias}}$ | `BIAS_DUAL_THRESHOLD` (`src/config.py:1195`) | `0.5` (sensor units) |

**Edge cases.** $n < w$ → all `False`; unequal lengths raise. The vote's
affected-sensor list is the pair's `variable` name (`comprehensive_pipeline.py:847`, `:855`).

#### 3.6B Single-sensor EMA deviation — batch path

- **Not a vote.** Sets `fault_type = "bias_offset"` at step 5b only when `is_anomaly` is already `True` and physics did not confirm (`comprehensive_pipeline.py:1011-1013`); overridden by any rule vote (§9).
- **Implementation:** `src/models/bias_detector.py:89-124`; pipeline `:821-838`.

**Statistic.** Over the physical-unit 20-step window, $\alpha = 2/(\text{span}+1)$, $m_1 = x_1$:

$$
m_t = \alpha x_t + (1-\alpha) m_{t-1}, \qquad \text{bias}_t = \mathbb{1}\big[\lvert x_t - m_t\rvert > \theta_c\big] \quad (t \ge 2)
$$

| Symbol | Constant | Default |
|---|---|---|
| span | `BIAS_EMA_SPAN` (`src/config.py:1197`) | `288` → $\alpha = 2/289 \approx 0.00692$ |
| $\theta_{\text{temperature}}$ | `BIAS_EMA_THRESHOLD_BY_SENSOR` (`src/config.py:1204`) | `2.0` °C |
| $\theta_{\text{humidity}}$ | `:1205` | `5.0` % |
| $\theta_{\text{co2}}$ | `:1206` | `50.0` ppm |
| $\theta_{\text{light}}$ | `:1207` | `100.0` W/m² |
| fallback $\theta$ | `BIAS_EMA_THRESHOLD` (`src/config.py:1198`) | `2.0` |

**Edge case.** The EMA is re-initialised at $x_1$ on every 20-step call and
state is not carried; with $\alpha = 2/289$ the weight retained by $x_1$ after
19 updates is $(1-\alpha)^{19} \approx 0.877$, so on this path the test is
dominated by $\lvert x_t - x_1\rvert$ rather than by a 24 h baseline (see D7).

#### 3.6C Midpoint pre-check (`BiasOffsetDetector.detect`) — inside the ensemble

- **Implementation:** `src/models/bias_detector.py:42-87`; invoked at `ensemble_detector.py:308-309`; consumed by `FaultClassifier.classify(bias_flag=…)` (`comprehensive_pipeline.py:1573-1577`), which returns `"bias_offset"` whenever `bias_flag` is `True` and the pattern is not `polling_dropout` (`fault_classifier.py:60-61`).

**Statistic / decision** (`:72-85`), per channel with range $(\ell_c, u_c)$ from `TEMP_RANGE`, `HUMIDITY_RANGE`, `CO2_RANGE`, `LIGHT_RANGE` (`ensemble_detector.py:264-271`):

$$
\text{mid}_c = \tfrac{\ell_c + u_c}{2}, \quad \text{span}_c = u_c - \ell_c, \quad
\text{dev}_c = \lvert \bar{x}_c - \text{mid}_c\rvert, \quad
\text{bias}_c = \mathbb{1}[\text{dev}_c > 0.3\,\text{span}_c]
$$

`bias_flag` $= \bigvee_c \text{bias}_c$. Fraction `_BIAS_FRACTION = 0.3` (`bias_detector.py:24`).

**Unit-space note.** The ensemble receives the **normalised** sequence
(`comprehensive_pipeline.py:1556`), so $\bar{x}_c \in [0,1]$ is compared with
physical midpoints $25,\ 65,\ 400,\ 600$ against thresholds $3,\ 9,\ 60,\ 360$
— see D17.

### 3.7 `correlation_breakdown` — rolling Pearson, requires `sensor_pairs`

- **Vote key:** `correlation_breakdown` → `fault_type = "correlation_breakdown"`, root cause `spatial_correlation_breakdown` (`comprehensive_pipeline.py:181-190`).
- **Implementation:** `src/models/correlation_breakdown_detector.py:31-74`; pipeline `comprehensive_pipeline.py:857-863`.

**Statistic / decision** (`:58-72`): for $t \ge w-1$,

$$
\text{break}_t = \mathbb{1}\big[s_0(W^w_{a,t}) < 10^{-12}\big] \lor \mathbb{1}\big[s_0(W^w_{b,t}) < 10^{-12}\big] \lor \mathbb{1}\big[\rho(W^w_{a,t}, W^w_{b,t}) \text{ is NaN}\big] \lor \mathbb{1}\big[\rho(W^w_{a,t}, W^w_{b,t}) < \theta_\rho\big]
$$

| Symbol | Constant | Default |
|---|---|---|
| $w$ | `CORR_BREAKDOWN_WINDOW` (`src/config.py:1213`) | `12` |
| $\theta_\rho$ | `CORR_BREAKDOWN_THRESHOLD` (`src/config.py:1214`) | `0.80` |

**Edge cases.** Constructor requires $w \ge 3$ (`:26-27`). A degenerate
(constant) window on **either** side is treated as a breakdown, not skipped.
`ENSEMBLE_CORRELATION_THRESHOLD` (`0.35`) belongs to the ensemble member
(§6.3), not to this rule.

### 3.8 `thermodynamic_contradiction` — psychrometric validation

- **Emitted `fault_type`:** `"humidity_fault"` via `_PHYSICS_REASON_TO_FAULT["psychrometric_inconsistency"]` (`comprehensive_pipeline.py:136`). `humidity_fault` is **not** in `ck_incidents_fault_type`; `thermodynamic_contradiction` is not emitted by any read code path — see D2.
- **Implementation:** `src/models/psychrometric_validator.py:37-119`; batch overlay `comprehensive_pipeline.py:1492-1522`; upload-path pair suppression `upload_portions.py:303-374`.

**Statistic** (Tetens; `psychrometric_validator.py:54`, `:82-87`), $T$ in °C, $\text{RH}$ in %:

$$
e_s(T) = 0.61078\,\exp\!\left(\frac{17.27\,T}{T + 237.3}\right)\ [\text{kPa}], \qquad
e_a = e_s \cdot \frac{\text{RH}}{100}, \qquad
\text{HD}_{\text{calc}} = \frac{2167.0\,(e_s - e_a)}{T + 273.15}\ [\text{g/m}^3]
$$

**Decision rule** (`:116`):

$$
\text{FAILED} = \mathbb{1}\big[\lvert \text{HD}_{\text{meas}} - \text{HD}_{\text{calc}}\rvert > \theta_{\text{HD}}\big]
$$

| Symbol | Constant | Default |
|---|---|---|
| $\theta_{\text{HD}}$ | `PSYCHROMETRIC_HD_TOLERANCE` (`src/config.py:1088`) | `0.5` g/m³ |

**Guards.** Raises `ValueError` for $T \le -273.15$ or $\text{RH} \notin [0,100]$ (`:19-34`); the batch overlay treats a raise as `"OK"` (`comprehensive_pipeline.py:1506-1508`), the upload path skips the timestamp (`upload_portions.py:342-345`).

**Batch behaviour** (`comprehensive_pipeline.py:1493-1522`). Requires
`climate_context["measured_hd"]`; evaluates the **last** denormalised
timestep. `FAILED` → `confirmed=True, reason="psychrometric_inconsistency"`
(overrides the trend-based physics result). Otherwise, if the physics
validator neither confirmed nor suppressed, `reason="psychrometric_consistent"`,
which later **suppresses** the result when `is_anomaly`, no rule voted, and
`"humidity"` is among the attribution's affected sensors (`:1085-1099`).

**Upload behaviour** (`upload_portions.py:303-374`). For a timestamp where
both a `temperature` and a `humidity` statistical hit exist and
$(T, \text{RH}, \text{HD}_{\text{meas}})$ are all present: `"OK"` → both hits
are suppressed; `FAILED` → both hits are kept and an extra detail with
`column="psychrometric"`, `method="psychrometric_inconsistency"`,
`severity="warning"`, `z_score=0.0` is appended (`:352-361`).

`BOOTSTRAP_PHYSICS_HD_MAX` (`src/config.py:1097`, `35.0`) is not referenced by
`psychrometric_validator.py` or by either pipeline file read here.

### 3.9 `diurnal_deviation` — hourly Z-score

- **Vote key:** `diurnal` → `fault_type = "diurnal_deviation"`, root cause `diurnal_profile_violation` (`comprehensive_pipeline.py:191-200`).
- **Implementation:** `src/models/diurnal_profile_detector.py:27-53`; profiles `src/api/services/diurnal_profile_service.py:37-89`; pipeline `comprehensive_pipeline.py:865-884`.

**Profile.** For each channel and hour-of-day $h$ (UTC hour of the stored
reading timestamp), over the trailing `DIURNAL_PROFILE_HISTORY_DAYS` of
`sensor_readings` (`:71-76`):

$$
\mu_h = \bar{x}(\{x : \text{hour}(x) = h\}), \qquad \sigma_h = \max\big(s_0(\{x : \text{hour}(x) = h\}),\ 10^{-6}\big)
$$

An hour bucket is kept only if it has $\ge$ `_MIN_SAMPLES_PER_HOUR` $= 5$
readings (`:34`, `:53`); the whole profile is empty if the tenant has fewer
than `DIURNAL_PROFILE_MIN_ROWS` readings in the window (`:78-79`).

**Decision rule** (`diurnal_profile_detector.py:48-53`), on the **last**
physical-unit timestep $x_n$ and the sequence timestamp's hour $h$:

$$
\text{diurnal} = \begin{cases}
\mathbb{1}[x_n \ne \mu_h] & \sigma_h \le 0 \\
\mathbb{1}\!\left[\left\lvert \dfrac{x_n - \mu_h}{\sigma_h}\right\rvert > \theta_Z\right] & \text{otherwise}
\end{cases}
$$

| Symbol | Constant | Default |
|---|---|---|
| $\theta_Z$ | `DIURNAL_Z_THRESHOLD` (`src/config.py:1219`) | `2.5` |
| history | `DIURNAL_PROFILE_HISTORY_DAYS` (`src/config.py:1222`) | `14` days |
| min rows | `DIURNAL_PROFILE_MIN_ROWS` (`src/config.py:1225`) | `288` |
| min per hour | `_MIN_SAMPLES_PER_HOUR` (`diurnal_profile_service.py:34`) | `5` |
| σ floor | literal (`:55`) | `1e-6` |

**Edge cases.** Missing hour → `False` (`:44-46`). Because $\sigma_h$ is floored at $10^{-6}$ when built, the $\sigma_h \le 0$ branch is reachable only for hand-supplied profiles.

### 3.10 `polling_dropout` — consecutive missing samples (short-circuit gate)

- **Stored `fault_type`:** `"polling_dropout"` (`_dropout_result`, `comprehensive_pipeline.py:1276-1318`).
- **Implementation:** `src/models/dropout_detector.py:32-62`; gate `comprehensive_pipeline.py:657-683`.

**Statistic / decision** (`:44-52`, `:55-62`). With $\psi_t = \mathbb{1}[\text{row } t \text{ is empty} \lor \exists\, v \in \text{row}_t : v = \text{None} \lor v = \text{NaN}]$ and run length $\kappa_t = \psi_t(\kappa_{t-1} + 1)$:

$$
\text{dropout} = \mathbb{1}\big[\exists\, t : \kappa_t \ge P\big]
$$

| Symbol | Constant | Default |
|---|---|---|
| $P$ | `DROPOUT_CONSECUTIVE_P` (`src/config.py:873`) | `5` |

**Score mapping.** `anomaly_score = 1.0`, `confidence = 1.0`, all four channels affected (`:1286-1292`).

**Edge case.** The reasoning string quotes `PERSISTENCE_THRESHOLD = 0.25` as
">25% of sequence steps" (`:666-670`); that constant is a legacy alias and is
not part of the decision (D14).

---

## 4. Crop band breaches

- **Vote keys / `fault_type`:** `temp_band_high`, `temp_band_low`, `humidity_band_high`, `humidity_band_low`, `vpd_band_high`, `vpd_band_low`, `co2_band_high`, `co2_band_low` (`BAND_TYPE_TO_VOTE`, `band_breach_detector.py:29-38`; profiles `comprehensive_pipeline.py:282-337`). All share root cause `crop_setpoint_band_breach`.
- **Implementation:** `src/models/band_breach_detector.py:112-238`; pipeline `comprehensive_pipeline.py:728-751`, gate `:1026-1055`, last resort `:1057-1079`.

**Inputs.** Physical-unit 20-step window; tenant operational config
(`setpoints`, `day_window`, `co2_enrichment`); sequence timestamp.

**Day/night selection** (`_is_day`, `:48-56`), $h$ = UTC hour:

$$
\text{day} = \begin{cases} \text{True} & s = e \\ \mathbb{1}[s \le h < e] & s < e \\ \mathbb{1}[h \ge s \lor h < e] & s > e \end{cases}
$$

with defaults $s = 6$, $e = 22$ (`DEFAULT_DAY_WINDOW`, `src/config.py:1555-1558`). The night band falls back to the day band when `night` is `null` (`:68-70`); a band is ignored if non-finite or $\ell \ge u$ (`:77-78`).

**Debounced value** (`_debounce_mean`, `:96-103`): mean of the finite values among the last $K$ samples (fewer if the window is shorter):

$$
\bar{x}^{(K)} = \bar{x}\big(\{x_t : t > n-K,\ x_t \text{ finite}\}\big)
$$

**VPD** (`_compute_vpd`, `:82-86`), from the debounced means of temperature and humidity:

$$
\text{VPD} = \max\big(0,\ e_s(\bar T^{(K)}) - e_s(\bar T^{(K)})\cdot \bar{\text{RH}}^{(K)}/100\big)\ [\text{kPa}]
$$

**Decision rule and score** (`_compare_band`, `:213-237`), for band $(\ell, u)$:

$$
\text{high} = \mathbb{1}[\bar{x}^{(K)} > u], \quad \text{score} = \min\!\left(1,\ \frac{\bar{x}^{(K)} - u}{\max(u-\ell,\ 10^{-6})}\right); \qquad
\text{low} = \mathbb{1}[\bar{x}^{(K)} < \ell], \quad \text{score} = \min\!\left(1,\ \frac{\ell - \bar{x}^{(K)}}{\max(u-\ell,\ 10^{-6})}\right)
$$

`co2_band_low` is discarded unless `config["co2_enrichment"]` is truthy (`:165-166`); `co2_band_high` is never suppressed.

| Symbol | Constant | Default |
|---|---|---|
| $K$ | `BAND_BREACH_DEBOUNCE_K` (`src/config.py:1116`) | `3` |
| enrichment default | `DEFAULT_CO2_ENRICHMENT` (`src/config.py:1130`) | `False` |
| crop profiles | `CROP_SETPOINT_PROFILES` (`src/config.py:1500-1528`) | tomato / cucumber / sweet_pepper / strawberry day/night bands |

**Fusion (batch).** Band findings are kept **outside** `rule_votes`
(`comprehensive_pipeline.py:741-745`). Two further rules apply:

1. **Sensor-trust gate** (`:1031-1055`): a band finding is dropped if any of its `affected_sensors` appears in the union of all rule-vote sensors.
2. **Last resort** (`:1065-1079`): the first surviving band finding sets `is_anomaly = True` and `fault_type` **only if** physics did not suppress, `rule_votes` is empty, and `is_anomaly` is still `False` after the LSTM/ensemble/CUSUM path.

**Score mapping.** `ScadaAnomalyRecord.score` above is carried in
`scada_anomalies`; the incident's `anomaly_score` remains the LSTM score (§6.1).

---

## 5. SCADA / climate-control rules

- **Implementation:** `src/models/setpoint_scada_detector.py:168-322`; pipeline `comprehensive_pipeline.py:697-726` (vote keys `_SCADA_TYPE_TO_VOTE`, `:705-712`).
- **Inputs.** Last physical-unit timestep: $T_{gh} = x_{n,\text{temperature}}$, $\text{RH} = x_{n,\text{humidity}}$; optional `ClimateContext` scalars resolved by alias lookup (`:82-107`): $T_{sp}$ (`SETPOINT_COLUMN_ALIASES`), $T_{plant}$ (`PLANT_TEMP_COLUMN_ALIASES`), $T_{out}$ (`OUTDOOR_TEMP_COLUMN_ALIASES`), $I_{solar}$ (`SOLAR_COLUMN_ALIASES`). Each rule is inert when its context scalar is absent or non-finite.

$\operatorname{clamp}(v) = \operatorname{clip}_{[0,1]}(v)$ (`:137-138`).

### 5.1 `under_heating`

(`:203-218`)

$$
\text{deficit} = T_{sp} - T_{gh}, \qquad \text{fire} = \mathbb{1}[\text{deficit} > \Delta_T], \qquad \text{score} = \operatorname{clamp}\!\left(\frac{\text{deficit}}{3\,\Delta_T}\right)
$$

### 5.2 `over_heating`

(`:220-247`)

$$
\text{excess} = T_{gh} - T_{sp}, \qquad
\text{solar} = \mathbb{1}[I_{solar} \text{ present}] \land \mathbb{1}[T_{out} \text{ present}] \land \mathbb{1}[I_{solar} > I_{\text{sol}}] \land \mathbb{1}[T_{out} > T_{\text{out,thr}}]
$$

$$
\text{fire} = \mathbb{1}[\text{excess} > \Delta_T] \land \lnot\text{solar}, \qquad \text{score} = \operatorname{clamp}\!\left(\frac{\text{excess}}{3\,\Delta_T}\right)
$$

If either $I_{solar}$ or $T_{out}$ is missing, `solar` is `False` and the rule fires on excess alone (D10).

### 5.3 `humidity_saturation_risk` and 5.4 `dehumidification_failure`

(`_humidity_rules`, `:253-295`). Both are gated by $\text{RH} \ge \text{RH}_{\text{crit}}$ (the code returns early on `rh < RH_CRIT_PERCENT`, `:253`). $\text{HD} = \text{HD}_{\text{calc}}(T_{gh}, \text{RH})$ (§3.8); a `ValueError` from unphysical inputs makes both rules inert (`:255-258`).

**`humidity_saturation_risk`** — always emitted once gated (`:265-278`):

$$
\text{score} = \operatorname{clamp}\!\left(\frac{\text{RH} - \text{RH}_{\text{crit}}}{100 - \text{RH}_{\text{crit}}} + 0.4\right)
$$

**`dehumidification_failure`** — additionally requires (`:280-281`):

$$
\text{fire} = \mathbb{1}[\text{HD} \le \text{HD}_{\text{crit}}], \qquad \text{score} = \operatorname{clamp}\big(0.7 + 0.01\,(\text{RH}_{\text{crit}} - \text{HD})\big)
$$

`RH_ABSOLUTE_MAX_PERCENT` influences only the reasoning text (`:119-123`), not the decision (D11).

### 5.5 `thermal_divergence`

(`:300-322`)

$$
\delta = \lvert T_{plant} - T_{gh}\rvert, \qquad \text{fire} = \mathbb{1}[\delta > \Delta_{\text{crit}}], \qquad
\text{severe} = \mathbb{1}[\delta \ge \Delta_{\text{severe}}], \qquad \text{score} = \operatorname{clamp}\!\left(\frac{\delta}{\Delta_{\text{severe}}}\right)
$$

### 5.6 `SENSOR_FLATLINE`

→ `stuck_fault`; see §3.3C.

**Parameters (all of §5).**

| Symbol | Constant | Default |
|---|---|---|
| $\Delta_T$ | `SETPOINT_TEMP_DELTA_C` (`src/config.py:942`) | `2.0` °C |
| $I_{\text{sol}}$ | `OVERHEAT_SOLAR_WM2` (`src/config.py:945`) | `400.0` W/m² |
| $T_{\text{out,thr}}$ | `OVERHEAT_OUTDOOR_TEMP_C` (`src/config.py:946`) | `25.0` °C |
| $\text{RH}_{\text{crit}}$ | `RH_CRIT_PERCENT` (`src/config.py:949`) | `95.0` % |
| (text only) | `RH_ABSOLUTE_MAX_PERCENT` (`src/config.py:950`) | `98.0` % |
| $\text{HD}_{\text{crit}}$ | `DEHUMID_HD_CRIT_G_M3` (`src/config.py:965`) | `5.0` g/m³ |
| $\Delta_{\text{crit}}$ | `THERMAL_DIVERGENCE_CRIT_C` (`src/config.py:968`) | `5.0` °C |
| $\Delta_{\text{severe}}$ | `THERMAL_DIVERGENCE_SEVERE_C` (`src/config.py:971`) | `10.0` °C |

**Score mapping.** `ScadaAnomalyRecord.score` is carried in
`scada_anomalies`; when any SCADA rule fired, its `reasoning` replaces the rule
reasoning (`comprehensive_pipeline.py:1139-1141`). The incident `anomaly_score`
is the LSTM score (§6.1).

---

## 6. Multivariate and learned detection

### 6.1 LSTM-VAE reconstruction score

- **Model:** `src/models/lstm_vae.py:24-214`. Encoder: stacked `nn.LSTM` layers with hidden sizes `HIDDEN_DIMS`, last hidden state → dropout → `fc_mu`, `fc_logvar` (`:115-139`). Decoder: $\mathbf{z}$ repeated `SEQ_LEN` times → stacked LSTM → dropout → `fc_out` (`:141-162`). Forward samples $\mathbf{z} = \boldsymbol{\mu} + \boldsymbol{\epsilon}\odot\exp(\tfrac12\boldsymbol{\log\sigma^2})$, $\boldsymbol{\epsilon}\sim\mathcal{N}(0,I)$, **also in eval mode** (`:100-113`, `:177-181`).

| Parameter | Constant | Default |
|---|---|---|
| input dim $d$ | `INPUT_DIM` (`src/config.py:72`) | `4` |
| sequence length $n$ | `SEQ_LEN` (`:75`) | `20` |
| latent dim | `LATENT_DIM` (`:78`) | `8` |
| hidden dims | `HIDDEN_DIMS` (`:81`) | `[64, 32]` |
| dropout | `DROPOUT_RATE` (`:87`) | `0.2` |
| $\beta$ | `BETA` (`:90`) | `0.1` |

**Training loss** (`:207-214`), batch size $B$:

$$
\mathcal{L} = \operatorname{MSE}(\hat{\mathbf{X}}, \mathbf{X}) + \beta \cdot \frac{-\tfrac12 \sum\big(1 + \log\sigma^2 - \mu^2 - \sigma^2\big)}{B}
$$

**Raw score** (`inference.py:64`, `:120`): per sequence, mean squared error over all timesteps and channels of the **normalised** input:

$$
e = \frac{1}{n\,d}\sum_{t=1}^{n}\sum_{c=1}^{d}\big(\tilde{x}_{t,c} - \hat{\tilde{x}}_{t,c}\big)^2
$$

Also returned: embedding $\boldsymbol{\mu}$ and uncertainty $u = \frac{1}{8}\sum_j \exp(\tfrac12 \log\sigma^2_j)$ (`:121`).

**Batch `anomaly_score`** (`comprehensive_pipeline.py:1467`):

$$
a = \operatorname{clip}_{[0,1]}\!\left(\frac{e}{\texttt{ANOMALY\_SCALING\_FACTOR}}\right), \qquad \texttt{ANOMALY\_SCALING\_FACTOR} = 2.0\ (\texttt{src/config.py:237})
$$

**Batch decision** (`:1557`): $\text{lstm} = \mathbb{1}[a > \theta_{\text{eff}}]$ where $\theta_{\text{eff}} = $ `ANOMALY_THRESHOLD` $= 0.8$ (`src/config.py:236`) unless an adaptive threshold is finite (§6.2). Since $a \le 1$, firing requires $e > 1.6$ at the default.

**Score mapping.** $a$ is the `anomaly_score` persisted on the incident for every batch result except the dropout and OOB gates (which store `1.0`). Rule votes, physics, CUSUM, band and ensemble outcomes change `is_anomaly` and `fault_type` but never $a$ (`:1182-1194`).

### 6.2 Adaptive thresholds (EWMA and POT/GPD)

(`comprehensive_pipeline.py:985-994`) $\theta_{\text{eff}} = \max(\theta_{\text{EWMA}}, \theta_{\text{POT}})$ if finite, else `ANOMALY_THRESHOLD`. Both trackers are updated with $a$ **after** the decision (`:1101-1108`).

**EWMA** (`src/models/ewma_tracker.py:40-82`). First score: $m = a,\ v = 0$. Then

$$
m_t = \alpha a_t + (1-\alpha) m_{t-1}, \qquad v_t = \alpha (a_t - m_t)^2 + (1-\alpha) v_{t-1}, \qquad
\theta_{\text{EWMA}} = \begin{cases} \infty & N < 10 \\ m_t + K\sqrt{\max(v_t, 0)} & N \ge 10 \end{cases}
$$

(note the variance update uses the already-updated mean $m_t$).

| Symbol | Constant | Default |
|---|---|---|
| $\alpha$ | `EWMA_ALPHA` (`src/config.py:819`) | `0.1` |
| $K$ | `EWMA_K` (`src/config.py:822`) | `3.0` |
| warm-up $N$ | `_MIN_OBS` (`ewma_tracker.py:26`) | `10` |

**POT / GPD** (`src/models/pot_threshold.py:53-111`). Circular buffer $S$ of the last `POT_BUFFER_SIZE` scores; `get_threshold()` returns $\infty$ until $\lvert S\rvert \ge 20$ and otherwise the last fitted value, which is refitted every 50 updates (`:62-64`). Fit (`:66-101`):

$$
q_0 = Q_{q}(S), \qquad Y = \{s - q_0 : s \in S,\ s > q_0\}
$$

$$
\theta_{\text{POT}} = \begin{cases}
Q_{1-\text{fpr}}(S) & \lvert Y\rvert < \texttt{min\_peaks}\ \text{or the GPD fit fails} \\
q_0 + \operatorname{GPD}^{-1}_{\hat\xi,\hat\sigma,\,\text{loc}=0}(1 - \text{fpr}) & \text{otherwise}
\end{cases}
$$

| Symbol | Constant | Default |
|---|---|---|
| $q$ | `POT_Q` (`src/config.py:825`) | `0.90` |
| fpr | `POT_FPR` (`src/config.py:828`) | `0.001` |
| buffer | `POT_BUFFER_SIZE` (`src/config.py:831`) | `500` |
| min peaks | constructor default (`pot_threshold.py:43`) | `10` |
| min fill | `_MIN_BUFFER_FILL` (`:21`) | `20` |
| refit interval | `_REFIT_INTERVAL` (`:24`) | `50` |

`_MIN_INCIDENTS_FOR_DYNAMIC_THRESHOLD = 50` (`comprehensive_pipeline.py:347`) is defined but not referenced in that module (D13).

### 6.3 Ensemble vote

- **Implementation:** `src/models/ensemble_detector.py:239-341`; four members `:89-236`. Called on the normalised sequence and the baseline (default: `build_default_baseline`, 10 identical sequences of physical-unit range midpoints `[25, 65, 400, 600]`, `comprehensive_pipeline.py:385-392`).

**Pre-checks** (in order): stuck (§3.3A) short-circuits the vote; midpoint bias (§3.6C) sets `bias_flag`.

**Member scores**, each in $[0,1]$ via $\operatorname{clip}_{[0,1]}$ with non-finite → 0 (`:74-86`):

| Member | Score $s_m$ | Vote $\mathbb{1}[\cdot]$ | Threshold constant |
|---|---|---|---|
| `lstm_vae` (`:102-110`) | $\operatorname{clip}(\max e / \theta_{\text{LV}})$ | $s \ge 0.5$ | `ENSEMBLE_LSTM_VAE_THRESHOLD` = `ANOMALY_THRESHOLD` = `0.8` (`src/config.py:566`) |
| `isolation_forest` (`:129-155`) | $\operatorname{clip}\big((r - r_{\min})/(r_{\max} - r_{\min})\big)$, $r = -\texttt{score\_samples}$, min/max from the baseline fit; `contamination=0.05`, `n_estimators=50` | $s \ge \theta_{\text{IF}}$ | `ENSEMBLE_ISOLATION_FOREST_THRESHOLD` = `0.5` (`:567`) |
| `statistical` (`:168-193`) | $\operatorname{clip}(D/\theta_{\text{S}})$, $D = \sqrt{(\bar{\mathbf{x}} - \bar{\mathbf{b}})^\top \Sigma_b^{-1} (\bar{\mathbf{x}} - \bar{\mathbf{b}})}$, $\Sigma_b = \operatorname{cov}(\text{per-sequence baseline means}) + 10^{-6} I$ | $s \ge 0.5$ (i.e. $D \ge 1.5$) | `ENSEMBLE_STATISTICAL_THRESHOLD` = `3.0` (`:568`) |
| `correlation` (`:209-236`) | $\operatorname{clip}\big(0.6\,\rho_{\text{range}} + 0.4\operatorname{clip}(\bar{\lvert\Delta\rho\rvert})\big)$ where $\rho_{\text{range}} = \frac{\#\{(t,c): x_{t,c} < \ell_c \lor x_{t,c} > u_c\}}{n\,d}$ against `TEMP_RANGE` … `LIGHT_RANGE`, and $\bar{\lvert\Delta\rho\rvert}$ = `nanmean` of $\lvert \rho_{\text{seq}} - \rho_{\text{baseline}}\rvert$ | $s \ge \theta_{\text{C}}$ | `ENSEMBLE_CORRELATION_THRESHOLD` = `0.35` (`:569`) |

**Combination** (`:320-333`), weights $w_m$ = `ENSEMBLE_DETECTOR_WEIGHTS` (all `0.25`, `src/config.py:558-563`):

$$
E = \frac{\sum_m w_m s_m}{\sum_m w_m}, \qquad V = \sum_m \mathbb{1}[\text{member } m \text{ votes}], \qquad
\text{ensemble} = \mathbb{1}[E \ge \theta_E] \land \mathbb{1}[V \ge V_{\min}]
$$

| Symbol | Constant | Default |
|---|---|---|
| $\theta_E$ | `ENSEMBLE_ANOMALY_THRESHOLD` (`src/config.py:564`) | `0.5` |
| $V_{\min}$ | `ENSEMBLE_MIN_AGREEMENT` (`src/config.py:565`) | `2` |

$V$ is stored as `ensemble_agreement` / `agreement_count`; the stuck pre-check
reports $V = 4$, $E = 1.0$. Member scores are rounded to 4 dp before storage.

**Batch use** (`comprehensive_pipeline.py:1557-1563`): $\text{is\_anomaly} = \text{lstm} \lor \text{ensemble}$ unless physics confirmed (then `True`).

**Unit-space note.** The sequence is normalised while the correlation member's
range bounds, the bias pre-check's ranges and the default baseline are in
physical units (D17).

### 6.4 Physics validator

`src/models/physics_validator.py:73-183`, on the denormalised window; first
matching rule wins. With $\bar{\Delta T} = \bar{x}(\operatorname{diff}(T))$,
$\bar{\Delta\text{RH}} = \bar{x}(\operatorname{diff}(\text{RH}))$, local hour
$h = (\text{hour}_{\text{UTC}} + \text{offset}) \bmod 24$ (offset default 0):

| # | Reason | Rule | Outcome | `fault_type` via `_PHYSICS_REASON_TO_FAULT` (`comprehensive_pipeline.py:134-140`) |
|---|---|---|---|---|
| 1 | `psychrometric_impossibility` | $\bar{\Delta T} > 0.5 \land \bar{\Delta\text{RH}} > 1.0$ | confirmed | `spike_fault` |
| 2 | `nocturnal_light` | $\max_t \text{light}_t > 50.0 \land h \in \{22,23,0,1,2,3,4\}$ | confirmed | `spike_fault` |
| 3 | `co2_out_of_range` | $\bar{\text{CO}_2} < 200 \lor \bar{\text{CO}_2} > 2000$ | confirmed | `calibration_drift` |
| 4 | `temperature_out_of_range` | $\exists t: T_t < 5 \lor T_t > 45$ | confirmed | `spike_fault` |
| 5 | `normal_diurnal_variation` | $\bar{\Delta T} \ne 0 \land \bar{\Delta\text{RH}} \ne 0 \land \operatorname{sgn}\bar{\Delta T} = \operatorname{sgn}\bar{\Delta\text{RH}} \land \lvert\bar{\Delta T}\rvert < 0.5 \land \lvert\bar{\Delta\text{RH}}\rvert < 1.0 \land T \in [10,40]\ \forall t \land \text{RH} \in [20,98]\ \forall t$ | **suppressed** | — |
| — | `psychrometric_inconsistency` (overlay, §3.8) | $\lvert\text{HD}_{\text{meas}} - \text{HD}_{\text{calc}}\rvert > 0.5$ | confirmed | `humidity_fault` (not in CK, D2) |

Constants are module literals (`physics_validator.py:21-24`, `:46-63`), not in `src/config.py`.

**Effect.** Suppression returns a non-anomaly result immediately **unless** any
rule vote exists (`comprehensive_pipeline.py:953-970`). Confirmation forces
`is_anomaly = True` and pins `fault_type`, which no later step overrides
(`:1007`, `:1012`, `:1021`).

### 6.5 Pattern classification and `FaultClassifier`

`src/models/pattern_detector.py:137-177` scores seven patterns on the
normalised sequence against a `PatternBaseline` (per-channel $\mu, \sigma$
(`ddof=0`), min, max, correlation matrix, dominant FFT bin — `pattern_scores.py:86-110`).
Primary pattern = argmax with ties resolved in insertion order
`drift, spike, dependency, lag, saturation, cyclic, cascade` (`pattern_detector.py:59-67`, `:165`).
Let $z_{t,c} = \lvert \tilde{x}_{t,c} - \mu_c\rvert/(\sigma_c + 10^{-6})$ (`pattern_scores.py:75-77`).

| Pattern | Score (`pattern_scores.py`) | Config |
|---|---|---|
| `drift` (`:113-121`) | fraction of channels with $s^2_0(\tilde{x}_c) < r\,(\sigma_c^2 + 10^{-6})$ | `PATTERN_DRIFT_VARIANCE_RATIO` $r = 0.01$ (`src/config.py:464`) |
| `spike` (`:124-167`) | **primary:** $1.0$ if any channel has $\lvert\Delta\tilde{x}_t\rvert > v_c \land \Delta\tilde{x}_t\cdot\Delta\tilde{x}_{t+1} < 0$ with $v_c$ = `VELOCITY_LIMITS[c]`; **fallback:** channels with $0 < \text{frac}(z > 3.0) \le 0.25$, score $\min(\max z / (3.0\cdot 2.0), 1)$ | `VELOCITY_LIMITS` (`:856-861`): temperature `2.0`, humidity `5.0`, co2 `50.0`, light `200.0`; `PATTERN_SPIKE_Z_THRESHOLD` `3.0`; `PATTERN_SPIKE_MAX_DURATION_FRACTION` `0.25`; `PATTERN_SPIKE_SCORE_SCALE` `2.0` (`:467-474`) |
| `dependency` (`:170-185`) | $\operatorname{clip}(\text{mean upper-triangle } \lvert\rho_{\text{seq}} - \rho_{\text{base}}\rvert)$; affected = pairs with diff $> 0.5$ | `PATTERN_DEPENDENCY_PAIR_THRESHOLD` `0.5` (`:478`) |
| `lag` (`:188-214`, `:323-349`) | lagged pairs / candidate pairs; candidate if $\lvert\rho_{\text{base}}\rvert \ge 0.5$; lagged if best $\lvert\rho\rvert$ over shifts $\pm\min(5, n/4)$ is at a non-zero shift, $\ge 0.5$, and beats shift 0 by $\ge 0.2$; needs $n \ge 10$ | `PATTERN_LAG_*` (`:481-499`) |
| `saturation` (`:217-231`) | fraction of channels with $\ge 0.9$ of readings within $0.02\,(\max_c - \min_c + 10^{-6})$ of a baseline bound | `PATTERN_SATURATION_BOUNDARY_TOLERANCE` `0.02`, `PATTERN_SATURATION_FRACTION_THRESHOLD` `0.9` (`:503-507`) |
| `cyclic` (`:234-264`) | broken / cyclic channels; cyclic if baseline dominant non-DC power fraction $\ge 0.3$; broken if current fraction at that bin $< 0.5 \times$ baseline fraction; needs $n \ge 20$ and $n$ = baseline length | `PATTERN_CYCLIC_*` (`:510-518`) |
| `cascade` (`:267-279`) | fraction of channels with $\bar{z}_c > 2.0$, zero if fewer than 2 such channels | `PATTERN_CASCADE_MIN_SENSOR_Z` `2.0`, `PATTERN_CASCADE_MIN_SENSORS` `2` (`:522-525`) |

Pattern confidence $= \min\big((s_{(1)} - s_{(2)})\cdot 2.0,\ 1\big)$ (`pattern_detector.py:190-201`, `PATTERN_CONFIDENCE_GAP_SCALE`, `src/config.py:529`).

**Note on `VELOCITY_LIMITS`.** The primary spike path compares the
**normalised** sequence's step difference against physical-unit limits
(`pattern_scores.py:147-149`); a normalised step can never exceed $1.0 < 2.0$,
so on the batch path the primary path cannot fire and the z-score fallback is
the operative rule (observation; see D4).

**`FaultClassifier.classify`** (`src/models/fault_classifier.py:38-94`), first match wins:
`polling_dropout` → `polling_dropout`; `bias_flag` → `bias_offset`; substring
`out_of_bounds` → `out_of_bounds`; `correlation_breakdown`; `diurnal` →
`diurnal_deviation`; `thermodynamic` → `thermodynamic_contradiction`; `stuck`
or `saturation` → `stuck_fault`; `spike` → `spike_fault`; `drift` →
`calibration_drift`; `sensor_stats["noise_level"] == "high"` →
`accuracy_decline` (never set by the pipeline, `comprehensive_pipeline.py:1575`); else `unknown`.
Hence the reachable classifier outputs from the seven patterns are
`calibration_drift` (drift), `spike_fault` (spike), `stuck_fault` (saturation)
and `unknown` (dependency, lag, cyclic, cascade), plus `bias_offset` when
`bias_flag` is set.

### 6.6 Attribution and confidence

**Attribution** (`src/models/anomaly_attributor.py:228-322`), deterministic path
`encode → decode(μ)`: for each channel $c$, replace it by its baseline mean
and recompute $e_{(c)}$;

$$
g_c = \max(0,\ e - e_{(c)}), \qquad
\text{attr}_c = \begin{cases} g_c / \sum_j g_j & \sum_j g_j > 10^{-6} \\ 1/d & \text{otherwise (fallback)} \end{cases}
$$

Affected sensors: $\text{attr}_c >$ `ATTRIBUTION_AFFECTED_SENSOR_THRESHOLD` $= 0.15$ (`src/config.py:442`). Confidence $= \min(\max_c \text{attr}_c \cdot 2.0, 1)$, or $0$ on fallback (`ATTRIBUTION_CONFIDENCE_SCALE`, `:446`).

**Confidence score** (`src/models/confidence_scorer.py:96-171`, `:242-261`), weights `CONFIDENCE_SIGNAL_WEIGHTS` (`src/config.py:536-541`):

$$
C = \operatorname{clip}_{[0,1]}\Big(0.4\,\min\!\big(\tfrac{a/(b + 10^{-6})}{3.0}, 1\big) + 0.3\,\min\!\big((s_{(1)} - s_{(2)})\cdot 2.0, 1\big) + 0.2\,\tfrac{V}{4} + 0.1\,\big(1 - \min(u, 1)\big)\Big)
$$

with $b$ = `CONFIDENCE_DEFAULT_BASELINE_ERROR` $= 0.05$ (`:549`), scale `CONFIDENCE_ERROR_RATIO_SCALE` $= 3.0$ (`:545`), ensemble size `CONFIDENCE_ENSEMBLE_SIZE` $= 4$ (`:552`). Levels: $\ge 0.85$ `very_high`, $\ge 0.75$ `high`, $\ge 0.6$ `medium`, $\ge 0.4$ `low`, else `very_low` (`CONFIDENCE_LEVEL_BOUNDS`, `:582-590`).

### 6.7 Upload (cold-run) path

**Normalisation.** Adaptive per-file ranges from a head sample of
`ADAPTIVE_RANGE_SAMPLE_ROWS` $= 20000$ valid rows (`src/config.py:284`;
`cold_run_upload.py:200-244`): $(\min_c - p_c,\ \max_c + p_c)$ with
$p_c = \max(0.05(\max_c - \min_c),\ 10^{-3})$, falling back to the static
range when the column is empty or $\max - \min < 10^{-6}$ (`data_utils.py:88-105`).

#### 6.7A LSTM, file-calibrated threshold

Per sequence $a_i = \operatorname{clip}_{[0,1]}(e_i / 2.0)$ (`upload_portions.py:622`). Over all $a$ in the file (`cold_run_threshold.py:40-70`):

$$
\theta_{\text{file}} = P_{99.9}(\{a_i\}) \quad (\texttt{COLD\_RUN\_PERCENTILE}, \texttt{src/config.py:241})
$$

Optional POT (method `"pot"`, `:103-117`): accepted only if finite, $\ge$ the percentile, $\le \max a$, and at least `COLD_RUN_MIN_ANOMALIES` $= 1$ score exceeds it. Optional robust gate (off by default, `COLD_RUN_ROBUST_GATE_ENABLED`, `src/config.py:259`):

$$
\theta_{\text{gate}} = \operatorname{med}(a) + z_g \cdot 1.4826 \cdot \operatorname{MAD}(a), \qquad \theta \leftarrow \max(\theta, \theta_{\text{gate}})
$$

with $z_g$ = `COLD_RUN_ROBUST_GATE_Z` $= 5.5$ (`:263`), `_MAD_TO_SIGMA = 1.4826` (`cold_run_threshold.py:33`); the gate is skipped when MAD $\le 0$.

**Decision** (`upload_portions.py:712`): $\text{hit}_i = \mathbb{1}[a_i > \theta_{\text{file}}]$. Empty file → $\theta = 1.0$.

**Score mapping:** canonical $\texttt{canonical}(a_i, p = \theta_{\text{file}}, hi = \text{None})$ (§6.9), fallback $0.5$ when $\theta_{\text{file}} \le 0$ (`:86-98`); `score_raw` $= a_i$; `detector="lstm"`; no severity.

#### 6.7B Rolling z-score statistical detector

`src/models/cold_run_detector.py:172-303`. Columns: any raw CSV column whose
lower-cased name contains one of `temp, humid, humidity, wind, deficit,
measured_hd` and none of `light, rain, screen, vent`, excluding gas and
metadata columns (`:45-55`). Per column, with the series shifted by one so the
current value is excluded from its own baseline (`:244-247`):

$$
\mu_t = \bar{x}(x_{t-W..t-1}), \quad \sigma_t = s_0(x_{t-W..t-1}) \ (\text{min\_periods} = \max(5, \lfloor W/4\rfloor)), \quad z_t = \frac{\lvert x_t - \mu_t\rvert}{\sigma_t}
$$

$$
\text{cand}_t = \mathbb{1}[x_t, \mu_t, \sigma_t \text{ finite}] \land \mathbb{1}[\sigma_t \ge 10^{-9}] \land \mathbb{1}[z_t \ge z_{\min}] \land \mathbb{1}[t \ge W]
$$

$$
\text{hit}_t = \mathbb{1}\big[\text{run length of consecutive cand} \text{ at } t \ \ge M\big]
$$

(`:252-276`). Severity: $z \ge 3.0$ `critical`, $z \ge 2.0$ `warning`, else `normal` (`:286-291`, `GAS_Z_CRITICAL`/`GAS_Z_WARNING`, `src/config.py:849-850`). Because $z_{\min} = 5.5 > 3.0$, every emitted hit is `critical`.

| Symbol | Constant | Default |
|---|---|---|
| $W$ | `COLD_RUN_STAT_ROLLING_WINDOW` (`src/config.py:277`) | `288` |
| $z_{\min}$ | `COLD_RUN_STAT_Z_MIN` (`src/config.py:278`) | `5.5` |
| $M$ | `COLD_RUN_STAT_MIN_CONSECUTIVE` (`src/config.py:279`) | `3` |
| IQR fences | `COLD_RUN_STAT_USE_IQR` (`src/config.py:280`) | `False` (fit/detect path only) |

**Short-file scaling** (single-call mode only, `_effective_rolling_window`, `:331-347`): $W_{\text{eff}} = 0$ if rows $< 24$; $W$ if rows $\ge 2W$; else $\min(W, \lfloor\text{rows}/2\rfloor)$. Incremental (portioned) mode uses $W$ unchanged and masks by **global** row index; the carry keeps $W + M - 1$ rows (`upload_portions.py:240-243`).

**Suppression** (in order, `upload_portions.py:676-685`): stuck-fault window suppression (§3.3B), then psychrometric pair suppression (§3.8).

**Score mapping:** canonical $\texttt{canonical}(z, p = 5.5, hi = \text{None})$ (`:71-83`), `score_raw` $= z$, `detector="statistical"`; only `critical`/`warning` details become hits (`:736-737`).

#### 6.7C Merge and ranking

LSTM and statistical hits at the same instant merge to `detector="lstm+statistical"` with `score = max`, `agreement=True` (`cold_run_upload.py:385-442`). Per-timestamp trigger ranking (`anomaly_ranking.py:70-178`): severity (`urgent` 3 > `critical` 2 > `warning` 1 > none 0) → detector tier (`stuck_fault` 3 > `statistical` 2 > `lstm` 1, by membership in the `+`-joined label) → $\lvert z\rvert$ → score → timestamp (lexicographically later wins) → column.

**Episodes** (`src/models/episode_grouper.py:82-162`): alerts grouped per
`(column, detector)` when consecutive timestamps differ by $\le$
`EPISODE_GAP_SECONDS` $= 600$ s (`src/config.py:1572`); `peak_score = max`;
no episode is rescored, and this grouper never sets `is_recovery`
(`episode_grouper.py:596`). Recovery tagging is applied query-time instead
(`anomaly_episode_service.py:359-455`): a non-stuck episode on a **different**
channel whose start falls in $[-600\text{ s}, +4.0\text{ h}]$ around a stuck
fault's `release_ts` is tagged `is_recovery=True` (`EPISODE_GAP_SECONDS` /
`RECOVERY_KNOCKON_WINDOW_HOURS`, `src/config.py:1572` / `:1582`). The tag is
informational: it never suppresses a hit, caps a score, or changes a rank.

### 6.8 Live path

`src/live/scoring.py:103-144`, `src/live/attribution.py:28-50`. Window of 20
raw vectors (forward-filled per channel; not warm until all four channels have
been seen and none is older than `LIVE_FORWARD_FILL_MAX_TICKS` $\times$
emit interval $= 5 \times 2.0$ s). Normalised with the **static** `SENSOR_RANGES`
(`scoring.py:120`). Score:

$$
a_{\text{live}} = \operatorname{clip}_{[0,1]}\!\left(\frac{e}{2.0}\right), \qquad \text{anomalous} = \mathbb{1}[a_{\text{live}} \ge \theta_{\text{live}}]
$$

(`attribution.py:49`, `scoring.py:194`; note $\ge$, unlike the batch $>$).
$\theta_{\text{live}}$ = `LIVE_ANOMALY_THRESHOLD` $= 0.05$ (`src/config.py:1368`).
Affected channels: those with per-channel MSE share $\ge 0.20$ of the total,
or at least the top one (`attribution.py:25`, `:68-71`). Episodes re-emit no
more often than `LIVE_ANOMALY_REEMIT_SECONDS` $= 60$ s.

### 6.9 Canonical score transform (ADR-005)

`src/api/services/score_transform.py:43-124`, for a producer with decision point $p > 0$ and optional saturation $hi > p$:

$$
\texttt{canonical}(r, p, hi) = \begin{cases}
\text{NULL} & r, p \text{ or } hi \text{ non-finite, } p \le 0, \text{ or } hi \le p \\
0 & r < 0 \\
0.5\, r/p & 0 \le r \le p \\
0.5 + 0.5\,\dfrac{r - p}{hi - p} & r > p,\ hi \ne \text{None} \\
0.5 + 0.5\,\dfrac{r - p}{r} & r > p,\ hi = \text{None}
\end{cases}
$$

Calibration pairs (`SCORE_SCALE_CALIBRATION`, `src/config.py:1385-1388`):
`live` $(0.05, \text{None})$, `batch` $(0.8, 1.0)$; the upload path uses
$p = \theta_{\text{file}}$ per file. `CANONICAL_ANOMALY_THRESHOLD` $= 0.5$ (`:1392`).

---

## 7. Gas anomalies

- **Implementation:** `src/models/gas_anomaly_detector.py:89-158`. No `incidents.fault_type`; results carry `is_anomaly`, `z_score`, `method`, `severity`.
- **Channels:** `GAS_SENSOR_COLUMNS` = `["c2h4", "gas_co2", "no", "no2", "nox", "co"]` (`src/config.py:837-844`), matched **exactly**.

**Baseline** (`:99-110`), per column over the finite historical values $B$ ($\lvert B\rvert \ge 5$, `_MIN_BASELINE_SAMPLES`, `:20`):

$$
\mu = \bar{x}(B), \quad \sigma = s_0(B), \quad q_{25} = P_{25}(B), \quad q_{75} = P_{75}(B), \quad \text{IQR} = q_{75} - q_{25}
$$

**Decision** (`:124-151`):

$$
z = \frac{\lvert x - \mu\rvert}{\sigma}, \qquad
\text{z\_anom} = \mathbb{1}[z > z_{\text{crit}}], \qquad
\text{iqr\_anom} = \mathbb{1}[\text{IQR} > 0] \land \big(\mathbb{1}[x < q_{25} - 1.5\,\text{IQR}] \lor \mathbb{1}[x > q_{75} + 1.5\,\text{IQR}]\big)
$$

$$
\text{is\_anomaly} = \text{z\_anom} \lor \text{iqr\_anom}, \qquad
\text{method} = \begin{cases}\texttt{"z\_score"} & \text{z\_anom} \\ \texttt{"iqr"} & \text{iqr\_anom} \\ \texttt{"none"} & \text{otherwise}\end{cases}, \qquad
\text{severity} = \begin{cases}\texttt{"critical"} & z \ge z_{\text{crit}} \\ \texttt{"warning"} & z \ge z_{\text{warn}} \\ \texttt{"normal"} & \text{otherwise}\end{cases}
$$

| Symbol | Constant | Default |
|---|---|---|
| $z_{\text{warn}}$ | `GAS_Z_WARNING` (`src/config.py:849`) | `2.0` |
| $z_{\text{crit}}$ | `GAS_Z_CRITICAL` (`src/config.py:850`) | `3.0` |
| fence | `_IQR_FENCE` (`:23`) | `1.5` |

**Edge cases.** $\sigma = 0$ → `is_anomaly=False`, `z=0`, `method="none"`,
`severity="normal"` (`:124-127`). Note `z_anom` uses strict $>$ while
`severity="critical"` uses $\ge$, so $z = 3.0$ exactly is `critical` but not a
z-score anomaly. Non-finite or missing values are skipped (`:200-208`).

---

## 8. OT network anomalies

All six are implemented under `src/models/network/` and exposed by
`src/api/routes/network_anomalies.py`. None writes `incidents.fault_type`.

| # | Endpoint | Statistic / decision | Constants (`src/config.py`) | Guards |
|---|---|---|---|---|
| 10 | `/api/network/rtt-jitter` (`rtt_jitter_detector.py:39-72`) | $J_t = \lvert R_t - R_{t-1}\rvert$; $\text{rtt} = \mathbb{1}[R_t > m\cdot R_0]$; $\text{jit} = \mathbb{1}[J_t > \theta_J]$; anomaly $= \text{rtt} \lor \text{jit}$ | `RTT_BASELINE_MS` $R_0 = 2.0$ (`:1256`), `RTT_MULTIPLIER` $m = 3.0$ (`:1257`) → limit 6 ms; `JITTER_THRESHOLD_MS` $\theta_J = 20.0$ (`:1258`) | jitter undefined on the first sample (per-tenant state) |
| 11 | `/api/network/iat` (`iat_cv_detector.py:38-69`) | per sample: $\text{sp}_t = \mathbb{1}[\text{iat}_t > k\,\bar\iota_0]$; for $t \ge w-1$: $\text{cv}_t = \mathbb{1}[s_1(W^w_t)/\bar{x}(W^w_t) > \theta_{\text{CV}}]$ if $\bar{x} > 0$; flag $= \text{sp}_t \lor \text{cv}_t$ | `IAT_CV_WINDOW` $w = 20$ (`:1263`), `IAT_CV_THRESHOLD` $\theta_{\text{CV}} = 1.0$ (`:1266`), `IAT_BASELINE_MEAN` $\bar\iota_0 = 1.0$ s (`:1267`), `IAT_SPACING_MULTIPLIER` $k = 2.5$ (`:1268`) → 2.5 s | CV needs a full window; spacing rule applies from $t = 0$ |
| 12 | `/api/network/tcp-handshake` (`tcp_handshake_detector.py:21`, `:37`) | $\mathbb{1}[T_{\text{hs}} > f \cdot T_0]$ | `TCP_BASELINE_MS` $T_0 = 5.0$ (`:1273`), `TCP_DELAY_FACTOR` $f = 2.0$ (`:1274`) → 10 ms | stateless |
| 13 | `/api/network/dhcp-fingerprint` (`dhcp_fingerprint_detector.py:18-42`) | $H = \operatorname{SHA256}(\text{"\textbar".join}(\text{params}))$; $\mathbb{1}[H \ne H_{\text{baseline}}]$ | — | string inequality; order of params matters |
| 14 | `/api/network/service-response` (`service_response_detector.py:59-91`) | $\theta = \begin{cases} B & \lvert S\rvert < N \\ \max(B, P_{q}(S)) & \text{otherwise}\end{cases}$; $\mathbb{1}[D_t > \theta]$; sample appended **after** the check | `SERVICE_RESPONSE_BASELINE_MS` $B = 120.0$ (`:1282`), `SERVICE_RESPONSE_WINDOW` $N = 1000$ (`:1279`), `SERVICE_RESPONSE_PERCENTILE` $q = 99.5$ (`:1281`) | explicit `baseline_threshold_ms` bypasses the window |
| 15 | `/api/network/threat/score` (`isolation_forest_threat_detector.py:86-114`) | $\text{TS} = \operatorname{clip}_{[0,100]}\big(-100 \cdot \texttt{score\_samples}(\mathbf{v})\big)$; $\mathbb{1}[\text{TS} > \theta_{\text{TS}}]$ over a 19-feature vector | `THREAT_SCORE_THRESHOLD` $\theta_{\text{TS}} = 65.0$ (`:1287`), `IF_THREAT_N_ESTIMATORS` `200` (`:1288`), `IF_THREAT_CONTAMINATION` `0.01` (`:1289`), `random_state=42` | 409 until `/threat/fit` has run for the tenant |

---

## 9. Precedence and combination

### 9.1 Batch pipeline order of evaluation

`comprehensive_pipeline.py:634-1236`, in execution order:

| Step | Detector | Effect |
|---|---|---|
| 0 | polling dropout (§3.10) | **return** dropout result (`fault_type=polling_dropout`, score 1.0) |
| 0a+ | setpoint SCADA rules (§5, §3.3C) | votes `under_heating`, `over_heating`, `humidity_saturation_risk`, `dehumidification_failure`, `thermal_divergence`, `sensor_flatline` in the detector's emission order (temperature → humidity → thermal → flatline) |
| 0a++ | band breaches (§4) | collected separately (not votes) |
| 0a | noise (§3.5A) | vote `noise` |
| 0b | out-of-bounds (§3.2) | **return** OOB result (`fault_type=spike_fault`, score 1.0) |
| 0c | spike MAD (§3.1) | vote `spike_mad` |
| 0d | bias EMA (§3.6B) | flag only |
| 0e | dual-sensor bias / correlation (§3.6A, §3.7) | votes `bias_dual`, `correlation_breakdown`, interleaved per pair |
| 0f | diurnal (§3.9) | vote `diurnal` |
| 0g | Mahalanobis (§3.5B) | vote `multivariate` |
| 0h | BOCPD (§3.4B) | vote `change_point` |
| 1 | LSTM-VAE (§6.1) | $a$ |
| 1a | CUSUM (§3.4A) | alarm flag |
| 2 | physics (§6.4, §3.8) | **return** suppressed result if suppressed and no votes; else confirmed/consistent flag |
| 3 | attribution + pattern (§6.5, §6.6) | |
| 4 | EWMA/POT (§6.2) | $\theta_{\text{eff}}$ |
| 5 | ensemble + classifier (§6.3, §6.5) | `is_anomaly`, provisional `fault_type` |
| 5a | CUSUM alarm | `is_anomaly=True`, `fault_type=calibration_drift` (unless physics confirmed) |
| 5b | bias EMA flag | `fault_type=bias_offset` if `is_anomaly` (unless physics confirmed) |
| 5c | rule votes | `is_anomaly=True`; `fault_type` = first vote's profile (unless physics confirmed) |
| 5c-gate | sensor-trust gate | drop band findings touching any voted sensor |
| 5c-last | band last resort | if no votes, not suppressed, and still `is_anomaly=False`: first band finding sets both |
| 5d | psychrometric-consistent suppression | **return** suppressed result if `is_anomaly`, no votes, and humidity attributed |

The dictionary `rule_votes` preserves insertion order, so "first fired rule"
means the first key inserted in the step order above, **not** the order of
`_RULE_VOTE_PROFILES`.

### 9.2 Final `fault_type` precedence

Because later assignments overwrite earlier ones and each is guarded as
shown, the effective priority (highest first) is:

$$
\text{physics confirmed} \;\succ\; \text{first rule vote} \;\succ\; \text{bias EMA (if anomalous)} \;\succ\; \text{CUSUM} \;\succ\; \text{ensemble stuck pre-check} \;\succ\; \texttt{FaultClassifier(pattern, bias\_flag)} \;\succ\; \text{band last resort}
$$

### 9.3 Final `is_anomaly`

$$
\text{is\_anomaly} = \text{physics\_confirmed} \lor \text{lstm} \lor \text{ensemble} \lor \text{cusum\_alarm} \lor [\text{rule\_votes} \ne \varnothing] \lor \text{band\_last\_resort}
$$

subject to the two suppression returns (physics `normal_diurnal_variation`
with no votes; `psychrometric_consistent` with no votes and humidity
attributed), each of which yields `is_anomaly=False`, `fault_type=None`.

### 9.4 What the stored fields mean

- `anomaly_score`: $a$ from §6.1 (or `1.0` for the two hard gates).
- `detector_scores`: the four ensemble member scores plus `1.0` for every rule vote key (`:1150-1154`).
- `ensemble_score`, `ensemble_agreement`: $E$ and $V$ from §6.3.
- `affected_sensors`: rule-vote sensors first, then attribution sensors not already listed (`:1151-1158`).
- `reasoning`: the first vote's template, with co-firing votes appended as `"; also flagged by: <root causes>"` (`build_rule_reasoning`, `:355-375`); replaced by the first SCADA finding's prose when any SCADA/band record exists (`:1139-1141`).

### 9.5 Upload path combination

Per timestamp: LSTM ∪ statistical merged by `max` (§6.7C); stuck-fault alerts
appended as a distinct detector; ranking as in §6.7C. No `fault_type` is
written to `incidents` by this path; stuck-fault freezes that crossed the dual
gate are written to `anomaly_episodes` with `fault_type="stuck_fault"`.

---

## 10. Discrepancies found

Each item states what the code does (with reference) against what
`docs/ANOMALY_CATALOG.md` says. Items marked **(code-internal)** are
observations about the implementation that the catalog does not address;
their runtime consequences were derived arithmetically from the code and were
not measured by executing it.

**D1 — Out-of-bounds stores `spike_fault`, not `out_of_bounds`.** The catalog
(§2) labels the detector `fault_type: out_of_bounds`. `_build_oob_result`
(`comprehensive_pipeline.py:1333`) writes `fault_type="spike_fault"` with
`root_cause="sensor_out_of_bounds"`. `out_of_bounds` is in the CHECK vocabulary
and in `FaultType`, but the only producer is `FaultClassifier` on a pattern
string containing `out_of_bounds`, which `PatternDetector` never emits.

**D2 — `thermodynamic_contradiction` is unreachable; `humidity_fault` is
emitted but not permitted.** The catalog (§8) heads the section
`fault_type: thermodynamic_contradiction`. The code maps
`psychrometric_inconsistency → "humidity_fault"`
(`comprehensive_pipeline.py:136`), a value absent from
`ck_incidents_fault_type` (`019_add_band_breach_fault_types.py:21-46`); an
incident insert with that value violates the constraint. No read code path
emits `thermodynamic_contradiction`.

**D3 — Stuck-fault definition.** The catalog (§3) gives the rule as
$\sigma^2_w < \varepsilon$ with `STUCK_EPSILON`. The ensemble pre-check
(`stuck_fault_detector.py:88-100`) requires **both** range
$<$ `STUCK_FLAT_RANGE_THRESHOLD` **and** min rolling variance (`ddof=1`)
$<$ `STUCK_VARIANCE_THRESHOLD`, evaluated in **normalised** units;
`STUCK_FLAT_RANGE_THRESHOLD` is not listed in the catalog and `STUCK_EPSILON`
is consumed only by `detect_series`, which no pipeline calls. The wall-clock
upload detector (§3.3B: 6 h, 12 samples, per-channel ε, pinned cadence,
`raw_slot_count`) is not described in the catalog at all.

**D4 — Spike velocity limit.** The catalog (§1) says `SPIKE_DELTA_MAX` /
`VELOCITY_LIMITS` "runs alongside" the MAD rule. `SPIKE_DELTA_MAX`
(`src/config.py:1133-1138`) is not consumed by any module read for this
document. `VELOCITY_LIMITS` is consumed only by `pattern_scores.score_spike`,
where it is compared against **normalised** step differences
(`pattern_scores.py:147-149`); since a normalised step is at most $1.0$ and the
smallest limit is $2.0$, that primary path cannot fire on the batch path
**(code-internal)**.

**D5 — Noise detector window and filter.** The catalog (§5) describes a rolling
$\sigma^2_w$ over `NOISE_HYBRID_WINDOW = 5`. The pipeline calls
`NoiseDetector.detect` (`comprehensive_pipeline.py:767`), which applies a
2nd-order Butterworth high-pass (cutoff 0.1) and takes the variance of the
**whole** filtered 20-step window; the rolling window is used only by
`detect_series`, which is not called. The catalog does not mention the filter.
Additionally, `NOISE_ALPHA`, `SENSOR_NOISE_TARGETS` and the seasonal
`target_override` only affect the fallback branch, unreachable for the four
canonical channels (`noise_detector.py:121-130`).

**D6 — Duplicate CUSUM class.** `drift_detector.py:106-215` contains a
`CUSUMDriftDetector` stub whose `update` always returns `False`
(`:170-174`). The pipeline imports the real implementation from
`cusum_drift_detector.py`. Not a catalog error, but a trap for anyone reading
the first file. The catalog also omits that $h$ is rescaled by the mean
seasonal factor (`comprehensive_pipeline.py:930-932`) and that CUSUM never
enters `rule_votes`, so any rule vote overrides its `calibration_drift`.

**D7 — EMA bias is window-local.** The catalog (§6) describes a "long-span EMA
… 288 × 5 min = 24 h". `detect_ema` re-initialises the EMA at $x_1$ on every
20-step call (`bias_detector.py:117`); with $\alpha = 2/289$ the retained
weight of $x_1$ after 19 steps is $\approx 0.877$, so the effective test on
the batch path is $\lvert x_t - x_1\rvert \gtrsim \theta_c$, not a deviation
from a 24 h baseline.

**D8 — Diurnal profile parameters.** The catalog (§9) leaves
`DIURNAL_PROFILE_HISTORY_DAYS` and `DIURNAL_PROFILE_MIN_ROWS` as "—". Code:
`14` and `288` (`src/config.py:1222-1227`), plus an undocumented
`_MIN_SAMPLES_PER_HOUR = 5` and a σ floor of `1e-6`
(`diurnal_profile_service.py:34`, `:55`).

**D9 — Band-breach "debounce" is a mean, not persistence.** The catalog
(Part 2) says a breach "must persist over `BAND_BREACH_DEBOUNCE_K` samples".
`_debounce_mean` (`band_breach_detector.py:96-103`) compares the **mean** of
the last $K$ finite samples against the band; one large excursion can trip it
and three small ones may not. The catalog also omits the sensor-trust gate and
the "only if nothing else fired" last-resort condition
(`comprehensive_pipeline.py:1026-1079`).

**D10 — Over-heating solar exemption.** The catalog says the detector "only
fires when outdoor temperature is below `OVERHEAT_OUTDOOR_TEMP_C`". Code
(`setpoint_scada_detector.py:220-227`): it fires whenever
$T_{gh} - T_{sp} > 2.0$ **unless** both $I_{solar} > 400$ and $T_{out} > 25$
are present and true; a missing solar or outdoor column disables the
exemption. `OVERHEAT_SOLAR_WM2 = 400.0` is not named in the catalog.

**D11 — Dehumidification failure parameters.** The catalog lists
`RH_CRIT_PERCENT` and `RH_ABSOLUTE_MAX_PERCENT`. The decision is
$\text{RH} \ge 95.0 \land \text{HD} \le$ `DEHUMID_HD_CRIT_G_M3` $= 5.0$
(`setpoint_scada_detector.py:253`, `:280`); `RH_ABSOLUTE_MAX_PERCENT` only
changes the reasoning phrase (`:121-123`).

**D12 — Humidity saturation risk is not exclusive.** The catalog says it fires
"without the full dehumidification-failure signature". Code (`:265-278`): it is
emitted whenever $\text{RH} \ge 95.0$, including every case in which
`dehumidification_failure` also fires, so both votes are present together.

**D13 — Dynamic-threshold activation.** The catalog says adaptive thresholds
"activate only after 50 stored incidents (`_MIN_INCIDENTS_FOR_DYNAMIC_THRESHOLD`)".
That constant is defined (`comprehensive_pipeline.py:347`) but not referenced
anywhere in the module. The actual warm-ups are EWMA $N \ge 10$
(`ewma_tracker.py:26`) and POT buffer $\ge 20$ with a fit after 50 updates
(`pot_threshold.py:21-24`, `:62`).

**D14 — Dropout reasoning text.** The stored reasoning says ">25% of sequence
steps have no sensor data" (`comprehensive_pipeline.py:666-670`, from the
legacy `PERSISTENCE_THRESHOLD = 0.25`); the rule is $\ge 5$ **consecutive**
null steps. The catalog states the rule correctly; the persisted text does not.

**D15 — Gas `method` semantics.** The catalog (Part 5) says the method is
chosen "by whichever method the baseline supports". Code
(`gas_anomaly_detector.py:139-144`): `method` records which rule **fired**
(z-score first, then IQR), and is `"none"` when neither fired or $\sigma = 0$.

**D16 — Precedence section omits two suppressions.** The catalog's Precedence
section names only dropout and OOB as short-circuits. Physics suppression
(`normal_diurnal_variation`, `comprehensive_pipeline.py:954-970`) and
psychrometric-consistent suppression (`:1085-1099`) also return early (when no
rule voted), and the ensemble's stuck pre-check short-circuits the member vote.

**D17 — Unit-space mismatch inside the ensemble (code-internal).**
`EnsembleAnomalyDetector.detect` receives the **normalised** sequence
(`comprehensive_pipeline.py:1556`), while (a) `BiasOffsetDetector` is built
with physical ranges (`ensemble_detector.py:264-271`), (b)
`CorrelationDetector` uses physical `_SENSOR_BOUNDS` (`:40-45`, `:217-219`),
and (c) the default baseline is physical-unit midpoints
(`comprehensive_pipeline.py:385-392`). Arithmetic consequences for any
normalised input in $[0,1]$: every element satisfies $x < \ell_c$ for the
correlation member's range check (so $\rho_{\text{range}} = 1$ and that member
always votes); the bias deviation $\lvert\bar{x}_c - \text{mid}_c\rvert$
always exceeds $0.3\,\text{span}_c$ (so `bias_flag` is always `True` and the
classifier returns `bias_offset` whenever it is consulted). Runtime behaviour
with tenant-supplied baselines was not measured.

**D18 — Correlation-breakdown degenerate windows.** The catalog gives only
$\rho < \theta_{\text{corr}}$. Code also flags when either window's standard
deviation is $< 10^{-12}$ or $\rho$ is NaN
(`correlation_breakdown_detector.py:64-72`).

**D19 — Mahalanobis details.** The catalog omits the median centre, the
noise-floor diagonal, the 10 % trimmed refit, and that only the last
timestep's flag votes (`multivariate_mahalanobis_detector.py:70-83`,
`comprehensive_pipeline.py:890`).

**D20 — Live score is scaled, not raw.** The config comment
(`src/config.py:1361-1367`) and the catalog's "Severity and score semantics"
describe live scores as raw reconstruction errors. `forward_pass_sync`
(`src/live/attribution.py:49`) returns $\operatorname{clip}(e/2.0)$, the same
scaling as the batch path, compared with $\ge$ `LIVE_ANOMALY_THRESHOLD`.
Likewise the catalog calls `ANOMALY_THRESHOLD` "a raw reconstruction-error
cutoff"; it is compared against $\operatorname{clip}(e/2.0)$.

**D21 — Cold-run statistical severity is always `critical` (code-internal).**
Because candidates require $z \ge 5.5$ and `critical` requires $z \ge 3.0$,
the `warning` branch at `cold_run_detector.py:288-289` is unreachable on the
rolling path. The catalog does not cover the upload path.

**D22 — SCADA flatline window versus `SEQ_LEN`.** The catalog (§3) says
`FLATLINE_IDENTICAL_STEPS = 24` requires 24 bit-identical repeats (~2 h). With
$n = 20 < 24$ the rule uses `required = series.size`
(`setpoint_scada_detector.py:153`), i.e. the whole 20-step window must be
constant; 24 samples are never examined on the batch path.

**D23 — `BOOTSTRAP_PHYSICS_HD_MAX` listed as a psychrometric parameter.** The
catalog (§8) lists it alongside `PSYCHROMETRIC_HD_TOLERANCE`. It is not
referenced by `psychrometric_validator.py`, `comprehensive_pipeline.py` or
`upload_portions.py`; it is a bootstrap pass-1 constant (`src/config.py:1092-1099`).

**D24 — Channels of the "nine physical anomalies".** The catalog states the
Part 1 detectors act on temperature, humidity, CO₂ and light. Dual-sensor bias
(§3.6A) and correlation breakdown (§3.7) act only on caller-supplied
`sensor_pairs` and never run on the four canonical channels
(`comprehensive_pipeline.py:840-863`).

**D25 — Catalog `calibration_drift` omits that CUSUM runs on normalised
residuals of only the last timestep per call** (`comprehensive_pipeline.py:936-939`),
with state carried across calls per tenant.

**D26 — Pattern types with no fault mapping.** The catalog's quick reference
implies `unknown` is "flagged but not classified". Per `FaultClassifier`,
`dependency`, `lag`, `cyclic` and `cascade` primary patterns always classify
as `unknown` (`fault_classifier.py:63-94`), and `saturation` classifies as
`stuck_fault`, which the catalog's pattern table does not state.

---

*Source files read for this document:* every module listed in §2's three
paths, all detector modules under `src/models/` and `src/models/network/`,
`src/config.py`, `src/live/detector_resolver.py`, `src/live/scoring.py`,
`src/live/attribution.py`, `src/api/services/{comprehensive_pipeline,
upload_portions, cold_run_upload, score_transform, fault_classification,
anomaly_episode_writer, anomaly_ranking, diurnal_profile_service,
network_detector_registry}.py`, `src/api/routes/network_anomalies.py`,
`src/models/{seasonal_scaler, data_utils, episode_grouper, confidence_scorer}.py`,
and `alembic/versions/019_add_band_breach_fault_types.py`.
