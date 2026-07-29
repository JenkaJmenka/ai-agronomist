# Anomaly Catalog

Every anomaly AI Agronomist can raise: what it means, how it is detected, which
parameters control it, what you see in the UI, and what an operator should do.

**Related docs:** [User Guide](USER_GUIDE.md) · [Framework spec](anomaly_framework_spec.md) · [README](../README.md)

---

## How to read this catalog

Anomalies come from four different layers of the system. Knowing which layer
raised a finding tells you how much to trust it and how to act.

| Layer | What it is | Trust model |
|-------|-----------|-------------|
| **Rule detectors** | Deterministic maths on the raw signal (MAD, CUSUM, variance, correlation, psychrometrics). Each has a closed-form threshold. | Deterministic. If it fires, the maths held. Tune the threshold, not the model. |
| **Crop band breaches** | Comparison against the day/night setpoint bands you configured on the **Configuration** page. | As good as your setpoints. Wrong crop profile → noisy alarms. |
| **LSTM-VAE (learned)** | Reconstruction error from a per-tenant sequence autoencoder trained on *your* clean history. | Statistical. Flags "unlike what I was trained on". Needs a trained model. |
| **Ensemble / pattern** | Weighted vote across LSTM-VAE, Isolation Forest, Mahalanobis distance and correlation checks, plus a pattern classifier. | Highest confidence when detectors agree (`agreement_count ≥ 2`). |

Every stored incident carries a **`fault_type`** (the machine label), a
**`root_cause`** (why it probably happened), a **`reasoning`** string naming the
algorithm that fired, and the **`affected_sensors`** list. The Anomaly Detection
page shows all four.

### Precedence

Rule detectors **vote**; they do not short-circuit. The full LSTM/ensemble path
still runs on every sequence. When one or more rules fire, the **first rule in
gate order** takes precedence for the `fault_type`, and co-firing rules are
appended to the reasoning as `"…; also flagged by: <root causes>"`. This is why
you can see `spike_fault` as the label while the reasoning also mentions a
correlation breakdown.

Two detectors *do* short-circuit and return immediately, because nothing
downstream is meaningful once they fire:

- **Polling dropout** — the data itself is missing.
- **Out-of-bounds** — the value is physically impossible, so every derived
  statistic is garbage.

---

## Severity and score semantics

The UI colours everything from one function (`web/lib/severity.ts`):

| Chip | When |
|------|------|
| **Normal** (grey/green) | `score < threshold` |
| **Warning** (amber) | Above threshold, single statistical detector |
| **LSTM / drift** (orange) | Above threshold, flagged by an LSTM or drift detector |
| **Critical** (red) | Above threshold **and** multiple detectors agree |

The default UI threshold is **0.15** (`DEFAULT_SCORE_THRESHOLD`). This is *not*
the same number as the backend's `ANOMALY_THRESHOLD = 0.8`: the UI slider works
on the calibrated per-tenant score scale, where scores top out around 0.2, while
the backend constant is a raw reconstruction-error cutoff. Adjust the UI
threshold on the **Thresholds** page, where you can see exactly how many points
each cutoff would flag before committing.

Findings also carry an operational urgency used by the recommendation engine:

- **`urgent`** — act now; the crop or the equipment is at risk.
- **`scheduled`** — put it on the maintenance list.

---

# Part 1 — Physical sensor anomalies

These nine correspond to categories 1–9 of the reference framework
([`anomaly_framework_spec.md`](anomaly_framework_spec.md)). They act on the
climate channels: temperature, humidity, CO₂, light.

---

## 1. Spike / transient outlier

**`fault_type: spike_fault`** · root cause `transient_spike` · **urgent**

**What it means.** A single sample (or a very short burst) jumps far away from
its local neighbourhood and then comes back. Classic causes: electrical
interference on the sensor loop, a loose connector, a door opened next to the
probe, or a genuine transient event like a heating valve slamming open.

**How it is detected.** Rolling median absolute deviation. Over a window of
`k` samples the detector computes `Δ_t = |x_t − median(W)|` and flags when
`Δ_t > c · MAD_t`. MAD is used instead of standard deviation precisely because
the spike itself would inflate a standard deviation and hide the event.

A per-sensor absolute velocity limit (`SPIKE_DELTA_MAX` / `VELOCITY_LIMITS`)
runs alongside it, so a spike is caught even when the MAD floor is degenerate
(a perfectly flat window has MAD = 0).

**Parameters** (`src/config.py`):

| Name | Default | Meaning |
|------|---------|---------|
| `SPIKE_MAD_WINDOW_K` | 10 | Rolling window length |
| `SPIKE_MAD_TUNING_C` | 3.0 | Multiplier on MAD (spec minimum 2.5) |
| `SPIKE_MAD_FLOOR` | per sensor | Minimum MAD so flat windows do not divide by ~0 |
| `SPIKE_DELTA_MAX` | per sensor | Hard per-step delta ceiling |
| `VELOCITY_LIMITS` | temp 2.0, humidity 5.0, CO₂ 50.0, light 200.0 | Units per timestep |

**In the UI.** Anomaly Detection → the row's *Fault* column reads "Spike fault";
reasoning reads *"Transient spike: temperature deviated beyond the rolling
median-absolute-deviation (MAD) threshold"*. On Monitoring the affected channel
gets an anomaly band at that timestamp.

**What to do.** One isolated spike on one channel is usually electrical — check
cabling and shielding before you touch the climate computer. Repeated spikes at
regular intervals point to an interfering device on the same circuit. Spikes on
*several* channels at once are almost never sensor faults; look for a real event
(vent opening, screen movement).

---

## 2. Out-of-bounds

**`fault_type: out_of_bounds`** · root cause `sensor_out_of_bounds` · **short-circuits**

**What it means.** The reading is outside what the sensor can physically report
— relative humidity above 100 %, negative light, and so on. This is a hard
validity failure, not a statistical judgement.

**How it is detected.** Static range validation against physical constraints:
`x_t < L_lower` or `x_t > L_upper`. No window, no history.

**Parameters.** `OOB_LIMITS` in `src/config.py` (per sensor lower/upper). The
nominal operating ranges used elsewhere are `TEMP_RANGE (20–30 °C)`,
`HUMIDITY_RANGE (50–80 %)`, `CO2_RANGE`, `LIGHT_RANGE (0–1000 lux)` — note that
those are *normalisation* ranges, not the out-of-bounds limits, which are wider.

**Why it short-circuits.** Once a value is physically impossible, every derived
statistic downstream (variance, correlation, VPD, reconstruction error) is
meaningless. The pipeline returns immediately with this fault rather than
producing a cascade of nonsense findings.

**In the UI.** The finding appears with the sensor name and the violating value.
Because it short-circuits, you will see *only* this fault for that window.

**What to do.** This is a sensor or transport fault, essentially always. Check
whether the raw value is a sentinel (−999, 65535) that failed to be filtered, a
unit mismatch (°F fed into a °C channel), or a genuinely dead probe.

---

## 3. Stuck-at / unresponsive sensor (flatline)

**`fault_type: stuck_fault`** · root cause `sensor_flatline_frozen_telemetry` · **urgent**

**What it means.** The sensor stopped changing. A real greenhouse channel always
has noise; a perfectly constant value over hours means the probe is frozen, the
polling loop is serving a cached value, or the transport is repeating the last
good frame.

**How it is detected.** Rolling variance against a noise floor: flag when
`σ²_w < ε` over window `w`. A second check counts *identical consecutive
values*, which catches a frozen digital value that a variance test on a coarse
sensor might miss.

**Parameters:**

| Name | Default | Meaning |
|------|---------|---------|
| `STUCK_WINDOW_K` | 10 | Rolling window |
| `STUCK_VARIANCE_THRESHOLD` | 1e-4 | Variance floor |
| `STUCK_EPSILON` | 1e-4 | Spec-aligned ε |
| `FLATLINE_VARIANCE_WINDOW` | 12 | Window for the multi-hour flatline gate |
| `FLATLINE_IDENTICAL_STEPS` | 24 | Consecutive identical samples that trigger the gate |

**In the UI.** Reasoning: *"Sensor flatline: humidity frozen (zero variance) for
the configured multi-hour window"*. The Monitoring chart makes this obvious — a
dead-flat line while neighbouring channels still move.

**What to do.** Compare against a redundant sensor in the same compartment. If
the redundant one is still moving, the probe is dead. If *both* froze, suspect
the gateway or the SCADA polling loop, not the sensors.

> **Caveat:** a genuinely well-controlled compartment on a stable night can look
> flat. `FLATLINE_IDENTICAL_STEPS = 24` exists to require *bit-identical* repeats
> rather than merely small variance, which is what separates control quality from
> a frozen channel.

---

## 4. Slow baseline drift (calibration drift)

**`fault_type: calibration_drift`** · root cause `level_shift_change_point` · **scheduled**

**What it means.** The sensor is slowly walking away from truth. Nothing looks
wrong sample-to-sample, but over days the reading is progressively biased. This
is the single most economically damaging sensor fault, because the climate
computer faithfully controls to a wrong number.

**How it is detected.** Two complementary methods:

1. **CUSUM** on the residual `e_t = x_t − x̂_t` (where `x̂` comes from an EMA
   baseline estimator). Accumulate `S⁺_t = max(0, S⁺_{t−1} + e_t − K)` and the
   symmetric `S⁻`; flag when either exceeds `H`. CUSUM is the right tool here
   because it integrates a small persistent bias that no per-sample test would
   catch.
2. **BOCPD** (Bayesian online change-point detection) for abrupt *level shifts*
   rather than gradual ramps — a recalibration event, a probe swap, a sensor
   moved to a different height.

**Parameters:**

| Name | Default | Meaning |
|------|---------|---------|
| `CUSUM_K_SIGMA` | 0.25 | Slack, in standard deviations. Detects ~0.5σ shifts |
| `CUSUM_H_SIGMA` | 4.0 | Decision threshold. ARL₀ ≈ 340 samples |
| `CUSUM_EMA_ALPHA` | 0.01 | EMA baseline adaptation rate |
| `BOCPD_HAZARD_LAMBDA` | 250.0 | Expected run length between change points |
| `BOCPD_MIN_RUN` | 8 | Minimum samples before a change point can be declared |
| `BOCPD_MIN_SENSORS` | 2 | Sensors that must agree for a multivariate change point |

> The spec's textbook values are `K = 0.2`, `H = 1.0`. Production deliberately
> runs `K = 0.25`, `H = 4.0` — on live telemetry the textbook values produce a
> drift alarm roughly every few hours. ARL₀ ≈ 340 means about one false drift
> alarm per 340 samples of in-control data.

**In the UI.** Reasoning: *"Change-point: Bayesian online change-point detection
(BOCPD) flagged a level shift on temperature"*. Drift is easiest to confirm on
the Monitoring chart over a **7-day or 1-month** period — it is invisible at 1 h.

**What to do.** Schedule a calibration against a reference instrument. Drift does
not resolve itself. If the drift started abruptly (BOCPD rather than CUSUM),
check the maintenance log for that date first — a probe swap explains it.

---

## 5. Precision degradation / noise

**`fault_type: accuracy_decline`** · root cause `sensor_noise_floor_exceeded` · **urgent**

**What it means.** The sensor still tracks the right *average* value, but the
sample-to-sample scatter has blown up. The mean is fine; the precision is gone.
Typically a failing analogue front end, moisture ingress, or a degrading
connector.

**How it is detected.** Rolling variance amplification ratio. Compute `σ²_w` over
a short window and compare against the calibrated per-sensor baseline variance:
flag when `σ²_w / σ²_baseline > θ_noise`.

**Parameters:**

| Name | Default | Meaning |
|------|---------|---------|
| `NOISE_HYBRID_WINDOW` | 5 | Rolling window |
| `NOISE_BASELINE_VARIANCE` | per sensor | Calibrated clean-sensor variance |
| `NOISE_RATIO_THRESHOLD` | 10.0 | Amplification ratio that trips the alarm |
| `NOISE_ALPHA` | 3.0 | Noise model shaping constant |
| `SENSOR_NOISE_TARGETS` | per sensor | Expected noise targets |

> The spec quotes `θ_noise = 50.0` against `σ²_baseline = 0.02`. Production runs
> a more sensitive `10.0` against per-sensor calibrated baselines.

**In the UI.** Reasoning: *"Precision degradation: rolling variance on
temperature exceeded the calibrated noise-floor ratio"*. On the chart the line
becomes visibly furry while its centre stays put.

**What to do.** Check the sensor housing for condensation and the wiring for
water ingress or a corroded terminal. A noisy sensor degrades every downstream
detector, so fix it before trusting other findings on that channel.

---

## 6. Bias / constant offset

**`fault_type: bias_offset`** · root cause `redundant_sensor_bias_offset` · **urgent**

**What it means.** The sensor reads consistently high or low by a fixed amount.
Unlike drift, the offset appears at once and then stays. Only detectable when
you have a redundant co-located sensor to compare against.

**How it is detected.** Two variants:

1. **Dual-sensor cross-difference** (`bias_dual`): for co-located sensors A and
   B, compute the rolling mean absolute difference `D_w = mean(|y − z|)` and flag
   when `|D_w| > θ_bias`. Averaging over a window is deliberate — it lets a
   single large disagreement pass while catching a *persistent* offset.
2. **EMA bias** (`bias_detector`): a long-span EMA of the deviation from the
   expected level, for installations without redundancy.

**Parameters:**

| Name | Default | Meaning |
|------|---------|---------|
| `BIAS_DUAL_WINDOW` | 10 | Rolling window for the cross-difference |
| `BIAS_DUAL_THRESHOLD` | 0.5 | Tolerance in sensor units |
| `BIAS_EMA_SPAN` | 288 | EMA span (288 × 5 min = 24 h) |
| `BIAS_EMA_THRESHOLD` | 2.0 | Default EMA bias limit |
| `BIAS_EMA_THRESHOLD_BY_SENSOR` | per sensor | Per-channel overrides |

**In the UI.** Reasoning: *"Dual-sensor bias: cross-difference between redundant
temperature sensors exceeded the calibration tolerance"*.

**What to do.** The finding tells you the *pair* disagrees, not which one is
wrong. Bring a reference instrument, or check which sensor's history shows the
step change. Recalibrate the offender.

---

## 7. Spatial correlation breakdown

**`fault_type: correlation_breakdown`** · root cause `spatial_correlation_breakdown` · **urgent**

**What it means.** Two channels that normally move together stopped doing so.
In a greenhouse, temperature sensors in the same compartment are strongly
correlated; so are temperature and humidity through the psychrometric
relationship. When that coupling breaks, either a sensor failed *or* the physical
space has genuinely split into two microclimates (a screen closed, a vent stuck).

**How it is detected.** Rolling Pearson correlation `ρ_{X,Y}` over a window;
flag when `ρ < θ_corr`.

**Parameters:**

| Name | Default | Meaning |
|------|---------|---------|
| `CORR_BREAKDOWN_WINDOW` | 12 | Rolling window |
| `CORR_BREAKDOWN_THRESHOLD` | 0.80 | Correlation floor (spec: 0.70) |
| `ENSEMBLE_CORRELATION_THRESHOLD` | 0.35 | Correlation member's vote threshold in the ensemble |

**In the UI.** Reasoning: *"Spatial correlation breakdown: rolling Pearson
correlation on temperature, humidity fell below the expected floor"*.

**What to do.** This one is genuinely ambiguous and worth investigating rather
than dismissing. Walk the compartment. If the microclimate really has split, the
sensors are fine and you have a climate control problem. If it has not, one of
the two sensors is lying.

---

## 8. Thermodynamic contradiction (psychrometric)

**`fault_type: thermodynamic_contradiction`** · root cause `psychrometric_inconsistency`

**What it means.** The reported temperature, relative humidity and measured
humidity deficit cannot all be true at once. Physics says they are linked; the
sensors say otherwise. Therefore at least one of the three is wrong — and this
is the only detector that can prove a sensor is faulty without a redundant unit.

**How it is detected.** Tetens equation for saturation vapour pressure:

```
e_s(T) = 0.61078 · exp( 17.27·T / (T + 237.3) )      [kPa]
e_a     = e_s · RH/100
HD_calc = 2167.0 · (e_s − e_a) / (T + 273.15)         [g/m³]
```

Flag when `|HD_meas − HD_calc| > θ_thermo`.

> **Unit note.** The constant is `2167.0` = 216.7 × 10. The standard
> psychrometric coefficient 216.7 applies when vapour pressure is in **hPa**;
> since `e_s` above is in **kPa**, it is multiplied by 10. An earlier version of
> this code used 216.7 with kPa inputs and produced humidity deficits ten times
> too small. See the errata in [`anomaly_framework_spec.md`](anomaly_framework_spec.md).

**Parameters:** `PSYCHROMETRIC_HD_TOLERANCE` (default **0.5 g/m³**),
`BOOTSTRAP_PHYSICS_HD_MAX`.

**Requires:** a measured humidity-deficit column in your data. The CSV adapter
picks it up from columns matching *humidity deficit*, *HD (g…)*, or *HD* — see
`MEASURED_HD_COLUMN_ALIASES`. Without it this detector is inert.

**In the UI.** Two outcomes are distinguished internally:
`psychrometric_impossibility` (mapped to `spike_fault` — the combination is
physically impossible) and `psychrometric_inconsistency` (mapped to
`humidity_fault` — inconsistent but not impossible).

**What to do.** Trust this one. Physics does not have false positives; only your
column mapping does. Verify first that the HD column really is in g/m³ and the
temperature is in °C, then investigate the humidity sensor — it is the usual
culprit.

---

## 9. Diurnal profile deviation

**`fault_type: diurnal_deviation`** · root cause `diurnal_profile_violation` · **scheduled**

**What it means.** The reading is abnormal *for this hour of the day*. 18 °C at
04:00 is unremarkable; 18 °C at 13:00 in a heated glasshouse is not. Absolute
thresholds cannot express this; a per-hour baseline can.

**How it is detected.** Build per-hour statistics (`μ_h`, `σ_h`) from historical
telemetry, then score `Z = (x_t − μ_h) / σ_h` and flag when `|Z| > θ_diurnal`.

**Parameters:**

| Name | Default | Meaning |
|------|---------|---------|
| `DIURNAL_Z_THRESHOLD` | 2.5 | Z-score cutoff (spec: 3.0) |
| `DIURNAL_PROFILE_HISTORY_DAYS` | — | Days of history used to build the profile |
| `DIURNAL_PROFILE_MIN_ROWS` | — | Minimum rows before the profile is considered valid |

**In the UI.** Reasoning: *"Diurnal profile deviation: temperature exceeded the
historical hourly Z-score for this hour of day"*.

**What to do.** Check whether something changed in the schedule — screens, lamp
timing, a new setpoint programme. A diurnal deviation with no equipment fault
usually means the *profile* is stale, not the reading. The profile rebuilds from
recent history, so it will follow a deliberate seasonal change on its own.

> The day/night boundary used by the crop bands comes from the **Configuration →
> Day / night window** setting (hours UTC). The diurnal profile is independent of
> it: it learns all 24 hours separately.

---

# Part 2 — Crop setpoint band breaches

These eight are **not** sensor faults. They say the greenhouse is outside the
agronomic band you configured for your crop. They are the findings your grower
cares about, and they are entirely driven by the **Configuration** page.

Each has a day band and a night band, selected by the configured day/night
window. Detection is debounced: a breach must persist over `BAND_BREACH_DEBOUNCE_K`
samples (default **3**) before it is raised, so a single sample crossing the line
does not page anyone.

| Fault type | Meaning | Severity | Urgency |
|-----------|---------|----------|---------|
| `temp_band_high` | Temperature above the crop day/night max | high | urgent |
| `temp_band_low` | Temperature below the crop day/night min | high | urgent |
| `humidity_band_high` | Humidity above the crop max | high | urgent |
| `humidity_band_low` | Humidity below the crop min | medium | scheduled |
| `vpd_band_high` | VPD above the crop max — air too dry, stomata close, growth stops | high | urgent |
| `vpd_band_low` | VPD below the crop min — air too wet, transpiration stalls, disease risk | medium | scheduled |
| `co2_band_high` | CO₂ above the crop max — ventilation failure or respiration build-up | medium | scheduled |
| `co2_band_low` | CO₂ below the crop min — enrichment underperforming | medium | scheduled |

All eight share the root cause `crop_setpoint_band_breach`.

**VPD** (vapour pressure deficit) is computed from temperature and RH rather than
measured, so a VPD band breach is only as good as those two channels. If a
psychrometric contradiction is also firing, fix that first.

### The CO₂ enrichment flag

`co2_band_low` is special. Ambient outdoor CO₂ is ~420 ppm while crop lower
bounds start around 600–800 ppm. A greenhouse **without** enrichment equipment is
therefore permanently below band, and reporting it would fire on every single
reading — pure alarm fatigue with zero diagnostic value.

So: when **Configuration → CO₂ enrichment equipment installed** is **off**,
`co2_band_low` is suppressed entirely. Turn it **on** and the finding becomes
meaningful — it now means "your enrichment is underperforming".

`co2_band_high` is **always** reported regardless of the flag, because high CO₂
indicates a ventilation failure or respiration build-up either way.

**What to do about band breaches.** These are climate-control findings, not
maintenance findings. Either the equipment failed to hold the setpoint, or your
configured band is wrong for the crop stage. Check the Configuration page before
you check the boiler.

---

# Part 3 — SCADA / climate-control anomalies

These read the climate computer's own setpoints alongside the measurements, so
they can tell "the greenhouse is cold" from "the greenhouse is cold *and the
heating was asked to prevent that*".

They require setpoint / plant-temperature / outdoor-temperature columns in your
data. The CSV adapter recognises them via `SETPOINT_COLUMN_ALIASES`,
`PLANT_TEMP_COLUMN_ALIASES`, `OUTDOOR_TEMP_COLUMN_ALIASES` and
`SOLAR_COLUMN_ALIASES`. Without those columns these detectors stay inert.

## Under-heating

**`fault_type: under_heating`** · root cause `heating_setpoint_deficit` · **urgent**

Air temperature is below the climate computer's heating setpoint by more than
`SETPOINT_TEMP_DELTA_C` (default **2.0 °C**). The system asked for heat and did
not get it.

*Reasoning:* "Under-heating: temperature below climate-computer heating setpoint
by more than the configured margin."

*What to do:* boiler, valve, pump, or pipe circuit. This is a real equipment
failure, and at night in winter it is time-critical.

## Over-heating

**`fault_type: over_heating`** · root cause `cooling_or_ventilation_shortfall` · **urgent**

Temperature is above the heating setpoint **without** solar conditions to explain
it. The solar check matters: on a bright day an over-temperature is expected
physics, not a fault. The detector only fires when outdoor temperature is below
`OVERHEAT_OUTDOOR_TEMP_C` (default **25.0 °C**) and solar radiation does not
account for the excess.

*What to do:* vents stuck closed, screens stuck shut, or a heating valve that
failed open.

## Dehumidification failure

**`fault_type: dehumidification_failure`** · root cause `dehumidification_capacity_failure` · **urgent**

RH is at critical level **and** the humidity deficit is near zero — the air is
saturated and the dehumidification strategy is not moving it. Controlled by
`RH_CRIT_PERCENT` (default **95.0 %**) and `RH_ABSOLUTE_MAX_PERCENT` (default
**98.0 %**).

*What to do:* this is a disease-risk emergency (Botrytis, mildew). Check
minimum-pipe temperature, vent position, and whether the dehumidification
programme is actually enabled.

## Humidity saturation risk

**`fault_type: humidity_saturation_risk`** · root cause `moisture_saturation_near_dewpoint` · **scheduled**

RH at or above the critical threshold near dew point, without the full
dehumidification-failure signature. The early warning for the above.

## Thermal divergence

**`fault_type: thermal_divergence`** · root cause `crop_air_thermal_disconnect` · **urgent**

Canopy (plant) temperature and greenhouse air temperature have separated by more
than the microclimate critical delta. Thresholds: `THERMAL_DIVERGENCE_CRIT_C`,
`THERMAL_DIVERGENCE_SEVERE_C`.

*What it means agronomically:* the crop is not experiencing the climate you think
you are providing. Cold canopy with warm air means condensation on the leaf —
direct disease risk. Requires a plant-temperature column.

## Polling dropout

**`fault_type: polling_dropout`** · root cause `polling_dropout` · **short-circuits**

Consecutive missing samples in the telemetry stream. Controlled by
`DROPOUT_CONSECUTIVE_P` (default **5** consecutive missing samples).

**Short-circuits** — with no data there is nothing to analyse, and every other
detector would produce noise from the gap. This is an infrastructure fault: the
gateway, the network, or the SCADA polling loop.

---

# Part 4 — Multivariate and learned detection

## Multivariate implausibility (Mahalanobis)

**`fault_type: accuracy_decline`** · root cause `multivariate_implausibility` · **urgent**

**What it means.** Every individual channel is inside its range, but the
*combination* is inconsistent with anything ever observed. High temperature with
high humidity with low light at midday, for example — each plausible alone,
jointly impossible.

**How it is detected.** Mahalanobis distance of the joint reading vector from the
learned mean, using the covariance of the tenant's clean history. Distance is
compared against a χ²-derived cutoff.

*Reasoning:* "Multivariate implausibility: the joint temperature, humidity
reading exceeded the Mahalanobis distance cutoff — each channel is in range but
the combination is inconsistent."

*What to do:* this is a strong signal that is easy to dismiss because no single
number looks wrong. Trust it. Start by checking whether two channels have been
swapped in the mapping.

---

## LSTM-VAE reconstruction (the learned detector)

**`fault_type`: from the pattern classifier** (see below)

**What it means.** The sequence does not look like the data the model was trained
on. This is the general-purpose detector: it catches shapes no hand-written rule
anticipated.

**How it works.** A sequence autoencoder — LSTM encoder → 8-dimensional latent
distribution → LSTM decoder — is trained on **your** clean history. At inference
it reconstructs a 20-step window; the reconstruction error is the anomaly score.
Windows resembling training data reconstruct well (low score); unfamiliar shapes
do not (high score).

**Architecture** (`src/config.py`):

| Parameter | Value |
|-----------|-------|
| `INPUT_DIM` | 4 (temperature, humidity, CO₂, light) |
| `SEQ_LEN` | 20 timesteps |
| `HIDDEN_DIMS` | [64, 32] |
| `LATENT_DIM` | 8 |
| `DROPOUT_RATE` | 0.2 |
| `BETA` | 0.1 (KL weight; loss = MSE + β·KL) |

The encoder also emits a **latent embedding** (μ) and an **uncertainty**
(mean of `exp(0.5·logvar)`). The embedding is what powers incident memory:
similar incidents are near each other in the 8-dimensional latent space, which is
how "find past cases like this one" works.

**Important:** the LSTM-VAE only detects meaningfully once a tenant model has been
trained. Without one, the pipeline falls back to the global baseline and the
score is far less informative. See [Training](USER_GUIDE.md#training-models) in
the User Guide.

---

## Pattern classification (what *kind* of anomaly)

Once something is flagged, the pattern detector classifies the *shape*. Seven
types, scored independently; the highest score wins (ties break in this order):

| Pattern | Explanation shown | Reading |
|---------|-------------------|---------|
| `drift` | "Sensor(s) {sensors} stuck at constant value" | Signal lost its dynamics |
| `spike` | "Sudden deviation in {sensors}" | Sharp transient |
| `dependency` | "Expected correlation broken ({sensors})" | Coupled channels decoupled |
| `lag` | "Delayed response in {sensors}" | A channel responds late to its driver |
| `saturation` | "Sensor(s) {sensors} at boundary" | Pinned at range limit |
| `cyclic` | "Daily pattern disrupted in {sensors}" | Diurnal rhythm lost |
| `cascade` | "Multi-sensor cascade detected ({sensors})" | Failure propagating across channels |

Governed by the `PATTERN_*` constants in `src/config.py`. **Cascade** is the one
to prioritise: it means the failure is spreading, and the earliest channel in the
cascade is usually the root.

The pattern label is what feeds the **Pattern distribution** chart on the System
Metrics page.

---

## Ensemble vote

Four detectors vote, weighted (`ENSEMBLE_DETECTOR_WEIGHTS`):

| Member | Threshold constant | Default |
|--------|-------------------|---------|
| `lstm_vae` | `ENSEMBLE_LSTM_VAE_THRESHOLD` | = `ANOMALY_THRESHOLD` (0.8) |
| `isolation_forest` | `ENSEMBLE_ISOLATION_FOREST_THRESHOLD` | 0.5 |
| `statistical` (Mahalanobis) | `ENSEMBLE_STATISTICAL_THRESHOLD` | 3.0 |
| `correlation` | `ENSEMBLE_CORRELATION_THRESHOLD` | 0.35 |

An anomaly is declared when the weighted score exceeds
`ENSEMBLE_ANOMALY_THRESHOLD` (0.5) **and** at least `ENSEMBLE_MIN_AGREEMENT`
(2) detectors agree. That agreement count is exactly what promotes a finding to a
**critical** (red) chip in the UI. Two independent detectors agreeing is a much
stronger signal than one detector scoring high.

---

## Adaptive thresholds

Two mechanisms adapt the cutoff to your data instead of relying on a fixed
number:

- **EWMA** (`EWMA_ALPHA` 0.1, `EWMA_K` 3.0) — an exponentially weighted moving
  threshold at `μ + K·σ`, which follows slow legitimate changes in the process.
- **POT / GPD** (`POT_Q` 0.90, `POT_FPR` 0.001, `POT_BUFFER_SIZE` 500) — peaks
  over threshold with a generalised Pareto tail fit, targeting an explicit
  false-positive rate of 0.1 %. This is the principled way to pick a tail cutoff
  when you care about a specific FP budget rather than a specific score.

Dynamic thresholds activate only after **50 stored incidents** for the tenant
(`_MIN_INCIDENTS_FOR_DYNAMIC_THRESHOLD`). Below that there is not enough
distribution to fit.

---

# Part 5 — Gas anomalies

Separate detector and separate page (**Gas Monitoring**), because gas channels
live in the raw-reading JSON rather than the four canonical climate columns.

**Channels** (`GAS_SENSOR_COLUMNS`): `c2h4` (ethylene), `gas_co2`, `no`, `no2`,
`nox`, `co`.

> Note `gas_co2` is deliberately distinct from the climate `co2` channel. The Gas
> page matches channel names **exactly** so it never picks up climate CO₂.

**How it is detected.** Per-column baselines are fitted from history, then each
value is scored by whichever method the baseline supports:

- **`z_score`** — when the baseline has usable mean and standard deviation.
- **`iqr`** — interquartile-range fallback for skewed or heavy-tailed
  distributions where a Gaussian z-score would mislead.
- **`none`** — insufficient baseline data; no scoring.

Severity comes back as `critical` / `warning` / `normal` directly from the
detector.

**API:** `GET /api/gas/summary` and `GET /api/gas/readings`.

**What to do.** Ethylene is the one to watch — it causes flower abortion and
premature ripening at low ppb, and it usually indicates incomplete combustion in
a CO₂-enrichment burner. NOₓ from the same source is the corroborating signal.

---

# Part 6 — OT network anomalies

Categories 10–15 of the framework. These monitor the operational-technology
network the SCADA system runs on — the transport, not the crop. They are
**API-only**: there is no dedicated UI page.

All six are exposed under `/api/network/*`.

## 10. RTT and jitter

`POST /api/network/rtt-jitter`

Gateway latency and jitter: `J_t = |R_t − R_{t−1}|`. Flags when
`R_t > RTT_MULTIPLIER · RTT_BASELINE_MS` or `J_t > JITTER_THRESHOLD_MS`.

Defaults: `RTT_BASELINE_MS` 2.0, `RTT_MULTIPLIER` 3.0 (→ 6 ms limit),
`JITTER_THRESHOLD_MS` 20.0.

*Meaning:* a control loop that cannot get its readings on time is not a control
loop. Rising jitter usually precedes a dropout.

## 11. Inter-packet interval variation (IAT CV)

`POST /api/network/iat`

Coefficient of variation of packet spacing over a window:
`CV = σ_IAT / μ_IAT`. Flags when `CV > IAT_CV_THRESHOLD`, plus an absolute
spacing rule at `IAT_BASELINE_MEAN × IAT_SPACING_MULTIPLIER`.

Defaults: `IAT_CV_WINDOW` 20, `IAT_CV_THRESHOLD` 1.0, `IAT_BASELINE_MEAN` 1.0 s,
`IAT_SPACING_MULTIPLIER` 2.5.

*Meaning:* SCADA polling is supposed to be metronomic. Irregular spacing means
congestion, a struggling device, or something injecting traffic.

## 12. TCP handshake timing

`POST /api/network/tcp-handshake`

`T_handshake = T_SYNACK − T_SYN`; flags when it exceeds
`TCP_BASELINE_MS × TCP_DELAY_FACTOR`.

Defaults: `TCP_BASELINE_MS` 5.0, `TCP_DELAY_FACTOR` 2.0 (→ 10 ms limit).

*Meaning:* isolates *connection setup* slowness from data-transfer slowness —
they have different causes.

## 13. DHCP fingerprint violation

`POST /api/network/dhcp-fingerprint`

SHA-256 hash over concatenated configuration parameters (MAC, BSSID, gateway,
DNS…). Flags when `H_t ≠ H_baseline`.

*Meaning:* this is a **security** control, not a performance one. A changed
fingerprint means a device's network identity changed — a rogue DHCP server, a
spoofed gateway, or an unauthorised device swap. Treat a hash change as an
incident until proven to be planned maintenance.

## 14. Service response duration

`POST /api/network/service-response`

Dynamic quantile filtering of microservice latencies: flags when `D_t` exceeds
the rolling percentile threshold.

Defaults: `SERVICE_RESPONSE_WINDOW` 1000, `SERVICE_RESPONSE_PERCENTILE` 99.5,
`SERVICE_RESPONSE_BASELINE_MS` 120.0.

## 15. Multi-dimensional behaviour (Isolation Forest)

`POST /api/network/threat/fit` then `POST /api/network/threat/score`

Isolation Forest over **19 network features** (RTT, jitter, IAT CV, RSSI, BSSID
counts, DNS latency, …). Produces a threat score scaled 0–100; flags when
`TS > THREAT_SCORE_THRESHOLD`.

Defaults: `THREAT_SCORE_THRESHOLD` 65.0, `IF_THREAT_N_ESTIMATORS` 200,
`IF_THREAT_CONTAMINATION` 0.01.

You must **fit** before you can **score** — the model needs a baseline of normal
network behaviour. `NETWORK_REGISTRY_MAX_TENANTS` bounds how many per-tenant
models are held in memory.

---

# Quick reference: fault type → meaning

Complete list of values permitted in `incidents.fault_type`
(constraint `ck_incidents_fault_type`, migration 019):

| Fault type | Layer | Meaning |
|-----------|-------|---------|
| `spike_fault` | Rule | Transient outlier |
| `out_of_bounds` | Rule | Physically impossible value |
| `stuck_fault` | Rule | Frozen / unresponsive sensor |
| `calibration_drift` | Rule | Slow baseline drift or level shift |
| `accuracy_decline` | Rule | Noise amplification or multivariate implausibility |
| `bias_offset` | Rule | Constant offset vs redundant sensor |
| `correlation_breakdown` | Rule | Coupled channels decoupled |
| `thermodynamic_contradiction` | Rule | Psychrometric inconsistency |
| `diurnal_deviation` | Rule | Abnormal for this hour of day |
| `polling_dropout` | Rule | Missing telemetry |
| `under_heating` | SCADA | Below heating setpoint |
| `over_heating` | SCADA | Above setpoint without solar explanation |
| `dehumidification_failure` | SCADA | Saturated air, dehumidification not working |
| `humidity_saturation_risk` | SCADA | Near dew point |
| `thermal_divergence` | SCADA | Canopy vs air disconnect |
| `temp_band_high` / `temp_band_low` | Band | Outside crop temperature band |
| `humidity_band_high` / `humidity_band_low` | Band | Outside crop humidity band |
| `vpd_band_high` / `vpd_band_low` | Band | Outside crop VPD band |
| `co2_band_high` / `co2_band_low` | Band | Outside crop CO₂ band |
| `unknown` | — | Flagged but not classified |

---

# Synthetic anomaly types (testing)

The **Upload & Operations → Inject test anomalies** panel generates tagged
synthetic readings. These are *not* detector outputs — they are inputs used to
exercise the pipeline and demo the UI.

| Type | What it generates |
|------|-------------------|
| `random` | Mixed set across all types |
| `temperature_spike` | Sharp upward temperature excursion |
| `temperature_drop` | Sharp downward temperature excursion |
| `humidity_spike` | Sharp humidity rise |
| `humidity_drop` | Sharp humidity fall |
| `co2_surge` | CO₂ concentration surge |
| `light_failure` | Light channel drops to zero |
| `multi_sensor` | Simultaneous failure across channels |
| `gradual_drift` | Slow baseline walk |

Injected rows are **tagged**, so removing them by type (or all at once) never
touches real sensor data. Always clean up before handing an environment to a
customer — injected rows are indistinguishable from real ones in the charts.
