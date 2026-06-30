My apologies! I completely misunderstood your instruction regarding markdown compatibility and mistakenly wrapped the equations in LaTeX blocks anyway.

Let's strip out the LaTeX syntax (`$$` or `$` symbols) entirely. Here is the fully corrected, pure Markdown-compatible text for your agent. It utilizes standard text notation, basic superscripts, subscripts, and plain-text operations so that **any standard markdown viewer or AI engine** will parse it perfectly without needing a LaTeX rendering engine.

# System Prompt for AI Engineering Agent: Two-Pass Cold-Start Anomaly Detection

## Context & Architecture Objective

We are deploying a telemetry anomaly detection service for a Priva greenhouse SCADA system using a single, unlabeled dataset spanning the entirety of 2026 (2026_union_combined_merged.csv). We face a **cold-start problem**: we have no pre-labeled anomalies and no previously trained models. We cannot use standard chronological or random data splitting because hidden anomalies within the 2026 data will contaminate our deep learning model's training baseline.

To resolve this, you will implement an **Unsupervised Two-Pass Bootstrapping Pipeline** that runs sequentially on the backend to dynamically isolate faults, establish a pristine training baseline, and run precise multivariate inference.

## Technical Specifications & Pass Implementation

### 1. Data Ingestion & Preprocessing Layer

- 
  **Parsing:** Efficiently stream and parse the 2026 CSV file. Convert European numerical formatting where commas are used as decimals (24,5 becomes 24.5) into float numbers.
- 
  **Time-Series Indexing:** Establish a strict 5-minute sampling interval index using the source timestamps.
- 
  **Null-Handling:** Missing values in core climate metrics (Outdoor Temp, Measured Relative Humidity, Plant Temp, Pipe Temps, Vent Settings) must be handled gracefully via linear interpolation. Gas telemetry columns exceeding a 95% null threshold must be flagged via a binary mask and dropped from neural network input vectors.

### 2. PASS 1 — The Unsupervised Bootstrapping Engine (Scrubbing Phase)

In Pass 1, do **not** use deep neural networks. Instead, run the entire 2026 raw dataset through a deterministic pipeline consisting of **Physics-Informed Pre-filtering** and an **Isolation Forest** to isolate suspected anomalies.

#### Component A: Hardcoded Thermodynamic Validation (Psychrometrics)

Apply the laws of psychrometrics to verify the relationship between Ambient Greenhouse Temperature (T_air) and Measured Relative Humidity (RH).

1. Calculate Saturation Vapor Pressure (e_s) in kPa using the Tetens equation:
  e_s = 0.61078  *exp((17.27*  T_air) / (T_air + 237.3))
2. Compute Actual Vapor Pressure (e_a):
  e_a = e_s * (RH / 100)
3. Calculate the theoretical Humidity Deficit (HD_calc in g/m^3) based on the ideal gas equation for water vapor:
  HD_calc = (216.7 * (e_s - e_a)) / (T_air + 273.15)
4. Compare HD_calc against the sensor's reported field value (Measured_HD_t). If the absolute difference is greater than 0.05, flag the row instantly as a physical contradiction:
  Absolute(Measured_HD_t - HD_calc) > 0.05

#### Component B: Contamination-Aware Machine Learning

1. Extract a multivariate matrix containing: Light Intensity, Temperature, Humidity, and Vent Settings.
2. Fit an unsupervised **Isolation Forest** across the entire, unified 2026 timeline. Configure the algorithm with a strict contamination parameter set to 0.04 (4% expected fault threshold).
3. 
  **Output Bundling:** Separate the data rows into two distinct pools based on Pass 1 flags:
  - 
    `bootstrapped_clean.csv` (Rows that passed all thermodynamic rules and scored as normal in the Isolation Forest).
  - 
    `pass1_suspected_anomalies.csv` (Rows containing statistical outliers or physical contradictions).

### 3. PASS 2 — Deep Learning & Multi-Perspective Inference Engine

With a purified baseline now established, execute Pass 2 to catch subtle, time-lagged, or drifting multi-sensor faults.

#### Component A: Model Training

- 
  **Architecture Strategy:** Initialize an **LSTM-Autoencoder** to process spatial (cross-sensor correlations) and temporal (diurnal tracking) dependencies.
- 
  **Training Set:** Train the LSTM-Autoencoder **strictly** on the `bootstrapped_clean.csv` dataframe generated during Pass 1.
- 
  **Loss Optimization:** Train by minimizing Mean Squared Error (MSE) reconstruction loss over the clean windows:
  MSE = (1 / N) * sum((X_input - X_reconstructed)^2)

#### Component B: Full-System Inference & Detection

Once training converges, run the **entire, original 2026 dataset** (all rows) back through the trained network to perform a multi-perspective scan.

1. For every time step, calculate the reconstruction error (e_t) as the Euclidean distance:
  e_t = Sqrt( sum((X_input - X_reconstructed)^2) )
2. Establish a dynamic threshold (tau) at the 99th percentile of the training data's loss distribution. If e_t > tau, flag the row as a multivariate anomaly.

#### Component C: Downstream Univariate Classification

For every row flagged as an anomaly by the multivariate network, run 5 sequential mathematical routines to append structural root-cause tags:

- 
  **Spike Fault Detector:** Flag if the absolute change from the previous step is greater than a maximum physical velocity (Delta_max), AND the direction of the change flips in the immediate next step.
  Absolute(x_t - x_t-1) > Delta_max AND Sign(x_t - x_t-1) != Sign(x_t+1 - x_t)
- 
  **Stuck Fault Detector:** Calculate rolling variance over a sliding 1-hour window (k = 12 samples). Flag if the variance drops below a near-zero operational baseline floor (epsilon):
  RollingVariance(x) < epsilon
- 
  **Drift Fault Detector:** Track residual deviations from historical means (mu_0) using two-sided Cumulative Sums (S_pos and S_neg) with an allowed slack parameter (K). Trigger an alarm if either sum exceeds the threshold (H):
  S_pos_t = Maximum(0, S_pos_t-1 + (x_t - mu_0) - K)
  S_neg_t = Maximum(0, S_neg_t-1 - (x_t - mu_0) - K)
  Flag Anomaly if: S_pos_t > H OR S_neg_t > H
- 
  **Precision Degradation Detector:** Pass the data through a digital high-pass filter (y = HighPassFilter(x)). Flag if the rolling standard deviation of the filtered stream exceeds factory noise tolerances multiplied by a scaling factor (alpha):
  StandardDeviation(y) > (noise_target * alpha)
- 
  **Dropout Detector:** Convert the data stream into a binary mask (psi) where 1 = Null/NaN and 0 = Valid Value. Sum this mask over a rolling persistence window (P). Flag if the sum equals P:
  sum(psi_from_t-P_to_t) >= P

## Code Quality & Engineering Deliverables

- 
  **Language:** Implement the pipeline in strictly typed TypeScript (Node.js backend framework) or pythonic FastAPI microservices where vectorized mathematical operations are executed.
- 
  **Output Payload:** Return a standardized JSON response matching this schema:

JSON

```
{
  "timestamp": "2026-12-25T12:00:00Z",
  "is_anomaly": true,
  "reconstruction_loss": 0.0842,
  "pass1_filtered": false,
  "root_cause_tags": ["Statistical Outlier", "Spike Anomaly"],
  "contributing_sensors": ["Measured Relative Humidity"],
  "physics_validation_status": "FAILED_PSYCHROMETRIC_LIMIT"
}

```

Implement backend routes, database schema models, and calculation modules ensuring optimization using vectorized arithmetic libraries. Proceed with full test-driven deployment.