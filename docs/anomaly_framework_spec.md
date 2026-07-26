# Multi-Sensor Anomaly Detection Framework — Reference Specification

> Source: "Automated Environmental Monitoring: A Mathematical and Algorithmic Framework for
> Multi-Sensor Anomaly Detection in Smart Greenhouse SCADA Systems" (provided 2026-07-10).
> This file is the authoritative reference for detector formulas, parameters, and
> verification datasets. Detector implementations in `src/models/` must match it.

## Taxonomy (15 anomalies)

| # | Category | English SCADA Term | Algorithmic Strategy |
|---|----------|--------------------|----------------------|
| 1 | Physical | Spike / Transient Outlier | Rate-of-Change Delta / Dynamic MAD Threshold |
| 2 | Physical | Out-of-Bounds | Static Range Validation against Physical Constraints |
| 3 | Physical | Stuck-at / Unresponsive Sensor | Rolling Variance Noise Floor (variance < epsilon) |
| 4 | Physical | Slow Baseline Drift | Cumulative Sum (CUSUM) Residual Analysis |
| 5 | Physical | Precision Degradation / Noise | Rolling Variance Amplification Ratio Check |
| 6 | Physical | Bias / Constant Offset | Redundant Sensor Cross-Difference Monitoring |
| 7 | Physical | Spatial Correlation Breakdown | Rolling Pearson Correlation Analysis |
| 8 | Physical | Thermodynamic Contradiction | Psychrometric Deficit Verification via Tetens Equation |
| 9 | Physical | Diurnal Profile Deviation | Historical Hourly Statistics and Z-score Evaluation |
| 10 | OT Network | Round-Trip Time and Jitter Anomaly | Gateway Latency and Jitter Threshold Monitoring |
| 11 | OT Network | Inter-Packet Interval variation (IAT CV) | Statistical CV Modeling of Packet Spacing |
| 12 | OT Network | TCP Timing Anomaly (SYN→SYN-ACK) | Handshake Latency vs. Baseline Timing Benchmarks |
| 13 | OT Network | DHCP Fingerprint Violation | Cryptographic SHA-256 Configuration Check |
| 14 | OT Network | Service Response Duration Anomaly | Dynamic Quantile Filtering of Microservice Latencies |
| 15 | OT Network | Multi-dimensional Behavior Anomaly | Multi-Feature Isolation Forest Scoring (19 features) |

Signal model: `x_t = x_t* + eta_t`, `eta_t ~ N(0, sigma_baseline^2)`.

## 1. Spike / Transient Outlier

Model: `x_t = x_t* + eta_t + s_t`, `s_t != 0` only at `t = t_s`, `|s_t| >> sigma_baseline`.

Detection: `delta_t = |x_t - median(W)|` over rolling window `W` of length `k`;
threshold `delta_t > c * MAD_t` with `c >= 2.5` (rolling Median Absolute Deviation).

### Verification dataset (Table 2, °C)
| Time | x_t | RollMedian | MAD_t | Delta | Expected |
|------|-----|-----------|-------|-------|----------|
| 12:00 | 21.8 | — | — | — | Normal (insufficient window) |
| 12:01 | 22.1 | — | — | — | Normal (insufficient window) |
| 12:02 | 22.0 | — | — | — | Normal (insufficient window) |
| 12:03 | 21.9 | — | — | — | Normal (insufficient window) |
| 12:04 | 22.2 | 22.0 | 0.1 | 0.2 | Normal (Delta <= 2.5*MAD) |
| 12:05 | 35.0 | 22.0 | 0.1 | 13.0 | **Anomalous** (Delta > 2.5*MAD) |
| 12:06 | 22.1 | 22.1 | 0.1 | 0.0 | Normal |
| 12:07 | 21.9 | 22.1 | 0.1 | 0.2 | Normal |
| 12:08 | 22.0 | 22.0 | 0.1 | 0.0 | Normal |
| 12:09 | 21.8 | 21.9 | 0.1 | 0.1 | Normal |

## 2. Out-of-Bounds

`anomaly if x_t < L_lower or x_t > L_upper`. For RH: `L_lower = 0.0`, `L_upper = 100.0`.

### Verification dataset (Table 7, RH %)
85.0, 88.0, 92.0, 95.0, 98.0 → Normal; **108.0 → Anomalous**; 95.0, 90.0, 85.0, 80.0 → Normal.

## 3. Stuck-at / Constant Value

Model: `x_t = C` (constant). Detection: rolling variance `sigma_w^2` over window `w`;
anomaly if `sigma_w^2 < epsilon`, `epsilon = 0.001`, `w = 5`.

### Verification dataset (Table 3, °C)
| Time | x_t | Window (w=5) | sigma_w^2 | Expected |
|------|-----|--------------|-----------|----------|
| 12:00–12:03 | 22.1, 21.9, 22.2, 22.0 | — | — | Normal (insufficient window) |
| 12:04 | 22.0 | {22.1,21.9,22.2,22.0,22.0} | 0.0130 | Normal |
| 12:05 | 22.0 | {21.9,22.2,22.0,22.0,22.0} | 0.0120 | Normal |
| 12:06 | 22.0 | {22.2,22.0,22.0,22.0,22.0} | 0.0080 | Normal |
| 12:07 | 22.0 | {22.0×5} | 0.0000 | **Anomalous** |
| 12:08 | 22.0 | {22.0×5} | 0.0000 | **Anomalous** |
| 12:09 | 22.0 | {22.0×5} | 0.0000 | **Anomalous** |

## 4. Slow Baseline Drift (CUSUM)

Model: `x_t = x_t* + d*(t - t0)` for `t >= t0` (drift rate d).
Residual `e_t = x_t - xhat_t`. CUSUM: `S_t^+ = max(0, S_{t-1}^+ + e_t - K)`,
`S_t^- = max(0, S_{t-1}^- - e_t - K)`; anomaly if `S_t^+ > H` or `S_t^- > H`.
Parameters: `K = 0.2` (slack), `H = 1.0` (decision threshold), `mu_target = 0`.

### Verification dataset (Table 4, °C)
| Time | x_t | x_t* | e_t | S_t^+ | Expected |
|------|-----|------|-----|-------|----------|
| 12:00 | 22.0 | 22.0 | 0.0 | 0.0 | Normal |
| 12:01 | 22.1 | 22.1 | 0.0 | 0.0 | Normal |
| 12:02 | 22.0 | 22.0 | 0.0 | 0.0 | Normal |
| 12:03 | 22.2 | 22.2 | 0.0 | 0.0 | Normal |
| 12:04 | 22.7 | 22.2 | 0.5 | 0.3 | Normal (below threshold) |
| 12:05 | 23.2 | 22.2 | 1.0 | 1.1 | **Anomalous** (S+ > 1.0) |
| 12:06 | 23.7 | 22.2 | 1.5 | 2.4 | **Anomalous** |
| 12:07 | 24.2 | 22.2 | 2.0 | 4.2 | **Anomalous** |
| 12:08 | 24.7 | 22.2 | 2.5 | 6.5 | **Anomalous** |
| 12:09 | 25.2 | 22.2 | 3.0 | 9.3 | **Anomalous** |

## 5. Precision Degradation / Noise

Model: `x_t = x_t* + psi_t`, `psi_t ~ N(0, sigma_fault^2)`, `sigma_fault >> sigma_baseline`.
Detection: ratio `sigma_w^2 / sigma_baseline^2 > theta_noise`;
`w = 5`, `sigma_baseline^2 = 0.02`, `theta_noise = 50.0`.

### Verification dataset (Table 5, °C)
| Time | x_t | Window | sigma_w^2 | Ratio | Expected |
|------|-----|--------|-----------|-------|----------|
| 12:00–12:03 | 22.0, 22.1, 21.9, 22.0 | — | — | — | Normal (insufficient window) |
| 12:04 | 23.8 | {22.0,22.1,21.9,22.0,23.8} | 0.6520 | 32.6 | Normal (< 50) |
| 12:05 | 20.2 | {22.1,21.9,22.0,23.8,20.2} | 1.7680 | 88.4 | **Anomalous** |
| 12:06 | 24.1 | {21.9,22.0,23.8,20.2,24.1} | 2.8480 | 142.4 | **Anomalous** |
| 12:07 | 19.8 | {22.0,23.8,20.2,24.1,19.8} | 3.9370 | 196.85 | **Anomalous** |
| 12:08 | 23.5 | {23.8,20.2,24.1,19.8,23.5} | 4.2730 | 213.65 | **Anomalous** |
| 12:09 | 22.0 | {20.2,24.1,19.8,23.5,22.0} | 3.1670 | 158.35 | **Anomalous** |

## 6. Bias / Constant Offset (dual-sensor)

Model: `x_t = x_t* + b`, `b != 0`. Detection: co-located sensors A (`y_t`), B (`z_t`);
rolling average difference `D_w = mean(|y - z| over w)`… anomaly if `|D_w| > theta_bias`;
`w = 3`, `theta_bias = 1.5` °C.

### Verification dataset (Table 6; B gets +3.0 °C offset at 12:05)
| Time | A | B | Diff | D_w | Expected |
|------|---|---|------|-----|----------|
| 12:00 | 22.0 | 22.1 | 0.1 | — | Normal (insufficient window) |
| 12:01 | 22.1 | 22.2 | 0.1 | — | Normal (insufficient window) |
| 12:02 | 22.0 | 22.1 | 0.1 | 0.1 | Normal |
| 12:03 | 21.9 | 22.0 | 0.1 | 0.1 | Normal |
| 12:04 | 22.2 | 22.3 | 0.1 | 0.1 | Normal |
| 12:05 | 22.0 | 25.1 | 3.1 | 1.1 | Normal (average within tolerance) |
| 12:06 | 22.1 | 25.2 | 3.1 | 2.1 | **Anomalous** (D_w > 1.5) |
| 12:07 | 21.9 | 25.0 | 3.1 | 3.1 | **Anomalous** |
| 12:08 | 22.0 | 25.1 | 3.1 | 3.1 | **Anomalous** |
| 12:09 | 21.8 | 24.9 | 3.1 | 3.1 | **Anomalous** |

## 7. Spatial Correlation Breakdown

Rolling Pearson correlation `rho_{X,Y}` over window `w = 5`;
anomaly if `rho < theta_corr = 0.70`.

### Verification dataset (Table 8)
| Time | A (x_t) | B anomalous (y_t) | rho | Expected |
|------|---------|-------------------|-----|----------|
| 12:00 | 20.1 | 21.24 | — | Normal (insufficient window) |
| 12:01 | 20.5 | 21.60 | — | Normal |
| 12:02 | 21.0 | 21.90 | — | Normal |
| 12:03 | 21.2 | 22.26 | — | Normal |
| 12:04 | 21.8 | 22.95 | 0.9972 | Normal (highly correlated) |
| 12:05 | 22.0 | 19.50 | 0.1251 | **Anomalous** (< 0.70) |
| 12:06 | 22.5 | 19.80 | -0.4320 | **Anomalous** |
| 12:07 | 22.4 | 19.20 | -0.6214 | **Anomalous** |
| 12:08 | 22.8 | 19.60 | -0.6510 | **Anomalous** |
| 12:09 | 23.0 | 19.40 | -0.6741 | **Anomalous** |

## 8. Thermodynamic Psychrometric Contradiction

Cross-validate T (°C), RH (%), measured humidity deficit HD_meas (g/m³).
Tetens: `e_s(T) = 0.61078 * exp(17.27*T / (T + 237.3))` kPa; `e_a = e_s * RH/100`.
Theoretical deficit from ideal gas law: `HD_calc = 2167.0 * (e_s - e_a) / (T + 273.15)` g/m³
(constant 2167.0 = 216.7 × 10: the standard psychrometric coefficient 216.7 applies when
vapour pressure is in **hPa**; since `e_s` above is in **kPa**, we multiply by 10).
Anomaly if `|HD_meas - HD_calc| > theta_thermo = 0.5` g/m³.

> **Corrected 2026-07: unit fix.** The original spec text used 2166.7 and the
> original implementation used 216.7 (hPa coefficient) with kPa inputs, which
> produced HD values 10× too small.  Both Table 9 values and the tolerance
> (was 0.15 g/m³) have been updated to the corrected g/m³ scale.  The
> production default tolerance is `PSYCHROMETRIC_HD_TOLERANCE = 0.5` g/m³.

### Verification dataset (Table 9)
| Time | T | RH | e_s (kPa) | HD_meas | HD_calc | Delta | Expected |
|------|---|----|-----------|---------|---------|-------|----------|
| 12:00 | 20.0 | 60.0 | 2.3382 | 6.9137 | 6.9137 | 0.0000 | Normal |
| 12:01 | 20.5 | 62.0 | 2.4116 | 6.7626 | 6.7626 | 0.0000 | Normal |
| 12:02 | 21.0 | 58.0 | 2.4869 | 7.6949 | 7.6949 | 0.0000 | Normal |
| 12:03 | 21.5 | 55.0 | 2.5643 | 8.4867 | 8.4867 | 0.0000 | Normal |
| 12:04 | 22.0 | 50.0 | 2.6438 | 9.7056 | 9.7056 | 0.0000 | Normal |
| 12:05 | 22.5 | 52.0 | 2.7255 | 25.000 | 9.5889 | 15.411 | **Anomalous** |

## 9. Diurnal Profile Deviation

`Z_diurnal = (x_t - mu_h) / sigma_h` for hour `h = hour(t)`;
anomaly if `|Z| > theta_diurnal = 3.0`.

### Verification dataset (Table 10)
| Hour | x_t | mu_h | sigma_h | Z | Expected |
|------|-----|------|---------|---|----------|
| 10 | 24.1 | 24.5 | 0.5 | -0.80 | Normal |
| 11 | 24.6 | 24.8 | 0.5 | -0.40 | Normal |
| 12 | 24.8 | 25.0 | 0.5 | -0.40 | Normal |
| 13 | 18.2 | 24.9 | 0.5 | -13.40 | **Anomalous** (|Z| > 3.0) |
| 14 | 24.7 | 24.5 | 0.5 | 0.40 | Normal |
| 15 | 24.0 | 23.8 | 0.5 | 0.40 | Normal |

## 10. Network RTT and Jitter

`J_t = |R_t - R_{t-1}|`. Anomaly if `R_t > theta_RTT = 3 * R_baseline` or `J_t > theta_jitter = 50` ms;
`R_baseline = 2.0` ms (so theta_RTT = 6.0 ms).

### Verification dataset (Table 11)
12:00–12:04: R_t = 2.0 (J = 0) → Normal.
12:05: R = 157.0, J = 155.0 → **Anomalous** (both limits).
12:06: R = 118.0, J = 39.0 → **Anomalous** (latency only).

## 11. Inter-Packet Interval Variation (IAT CV)

`IAT_i = t_i - t_{i-1}`; over `w = 5`: `CV_IAT = sigma_IAT / mu_IAT`;
anomaly if `CV_IAT > theta_CV = 1.5` (baseline mean interval 1.0 s).
Note Table 12 also flags 12:05 by an absolute spacing rule ("exceeds 3-second limit").

### Verification dataset (Table 12)
| Time | IAT_t (s) | mu | sigma | CV | Expected |
|------|-----------|----|-------|----|----------|
| 12:00–12:03 | 1.0 ×4 | — | — | — | Normal (insufficient window) |
| 12:04 | 1.0 | 1.00 | 0.00 | 0.00 | Normal |
| 12:05 | 4.95 | 1.79 | 1.76 | 0.98 | **Anomalous** (spacing > 3 s limit) |
| 12:06 | 0.05 | 1.60 | 2.14 | 1.34 | Normal (CV below limit) |
| 12:07 | 0.10 | 1.42 | 2.21 | 1.56 | **Anomalous** (CV > 1.5) |

## 12. TCP Handshake Timing

`T_handshake = T_SYNACK - T_SYN`; anomaly if `T_handshake > c * T_baseline`;
`T_baseline = 5.0` ms, `c = 2.5` (limit 12.5 ms).

### Verification dataset (Table 13, ms)
5.0, 5.0, 4.0, 6.0, 5.0 → Normal; **25.0 → Anomalous** (> 12.5).

## 13. DHCP Fingerprint Violation

`H_t = SHA256(P_t)` over concatenated config params (MAC, BSSID, gateway, DNS…);
anomaly if `H_t != H_baseline`.

### Verification dataset (Table 14)
12:00–12:04: MAC 00:1A:2B:3C:4D:5E, GW 192.168.1.1, DNS 8.8.8.8 → same hash → Normal.
12:05: DNS changes to 10.0.0.9 → hash differs → **Anomalous**.

## 14. Service Response Duration

`theta_service = P99(window of N=100)`, calibrated baseline `theta_service = 120.0` ms;
anomaly if `D_t > theta_service`.

### Verification dataset (Table 15, ms)
45, 48, 44, 52, 47 → Normal; **350.0 → Anomalous** (> 120).

## 15. Multi-Dimensional Behavior (Isolation Forest)

19 network features (RTT, jitter, IAT CV, RSSI, BSSID counts, DNS latency, …).
`s(x, psi) = 2^(-E[h(x)]/c(psi))`; threat score TS scaled 0–100;
anomaly if `TS > theta_threat = 70.0`.

### Verification dataset (Table 16, subset features)
12:00–12:04: RTT 2.0, jitter 0.0, CV 0.99, E[h] 12.4 → TS 35.0 → Normal.
12:05: RTT 157.0, jitter 118.0, CV 4.95, E[h] 3.1 → TS 85.0 → **Anomalous**.

---

## Errata (found during test-vector verification, 2026-07-11)

1. **Table 12 (IAT CV), rows 12:06–12:07**: the printed sigma/CV values do not
   reproduce from the given IAT series under either population or sample
   variance. Row 12:07's "Anomalous (CV > 1.5)" label is not reproducible —
   actual CV computes to ~1.43 < 1.5. The test suite skips that row's
   assertion; the 12:05 absolute-spacing flag is verified instead.
2. **Section 8 (Psychrometric), prose constant — CORRECTED 2026-07**: the
   original spec text stated 2166.7 and the original implementation used 216.7
   (the hPa coefficient) with kPa vapour pressures, producing HD values 10×
   too small.  The implementation now uses **2167.0** (= 216.7 × 10, the kPa
   coefficient), and Table 9 / tolerance have been updated to the corrected
   g/m³ scale.  The old errata note (which declared the prose erroneous and
   the code correct) was itself incorrect — the prose derivation was closer
   to the right constant; the code was the source of the 10× error.
