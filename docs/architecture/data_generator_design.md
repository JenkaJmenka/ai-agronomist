# Synthetic Greenhouse Sensor Data Generator Design

## Overview
The Synthetic Greenhouse Sensor Data Generator is a modular pipeline designed to produce realistic, time-series sensor data for training and testing LSTM-VAE anomaly detection models. Unlike simple random-walk generators, this system models the underlying physical relationships between temperature, humidity, CO₂ levels, and light intensity within a greenhouse environment.

## Architecture

The generator follows a multi-stage pipeline architecture: `Environment State` $\rightarrow$ `Signal Engine` $\rightarrow$ `Perturbation Engine` $\rightarrow$ `Assembler`.

### 1. Environment State (The "Ground Truth")
This module maintains the global temporal state (e.g., time of day, seasonal progression). It defines the periodic driving force (solar cycle) that all other sensors depend on.

### 2. Signal Engine (The Physical Models)
This engine implements the core sensor relationships using deterministic physical models coupled with stochasticity.

#### A. Solar Cycle Model (Primary Driver)
Generates the light intensity ($L$) based on a diurnal cycle:
$$L(t) = L_{peak} \cdot \max(0, \sin(\frac{2\pi t}{P} + \phi))$$
- $P$: Period (24 hours).
- $\phi$: Random phase shift per sample to represent different days.

#### B. Atmospheric Model (Temperature & Humidity)
Models the thermodynamic relationship between light, temperature ($T$), and humidity ($H$):
- **Temperature**: $T(t) = T_{base} + \alpha \cdot L(t) + \epsilon_{temp}$
  - Heat transfer from solar radiation.
- **Humidity**: $H(t) = H_{base} \cdot (1 - \beta \cdot (T(t) - T_{ref})) + \epsilon_{hum}$
  - Models the decrease in relative humidity as temperature increases (evapotranspiration effect).

#### C. CO₂ Model (Photosynthesis)
Models the CO₂ concentration ($C$) based on metabolic activity:
- **CO₂**: $C(t) = C_{ambient} - \gamma \cdot (L(t) \cdot f(T(t))) + \epsilon_{CO2}$
  - $f(T(t))$: A temperature-dependent enzyme activity function (e.g., Bell curve).
  - Models the drawdown of CO₂ during daylight hours via photosynthesis.

### 3. Perturbation Engine (Anomalies & Noise)
Injects non-deterministic events into the clean signal.
- **Gaussian Noise**: High-frequency jitter to simulate sensor precision limits.
- **Anomaly Modules**:
    - **Spikes**: Sudden, high-magnitude transient pulses (e.s. power surge).
    - **Drift**: Gradual linear or polynomial bias (e.g., sensor degradation).
    - **Dropout**: Periodic or random replacement of values with `NaN` or zero (e.g., connectivity loss).

### 4. Assembler (Post-Processing)
The final stage that aggregates all signals into the target tensor format.
- **Normalization**: All features are scaled to the range $[0, 1]$ using Min-Max scaling. This ensures the LSTM-VAE training is stable and gradients are well-behaved.
- **Tensor Construction**: Aggregates $N$ sequences of length $T$ with $F$ features.

## Data Model & Tensor Format

### Input Parameters
| Parameter | Type | Description |
|-----------|------|-------------|
| `num_samples` | `int` | Number of independent sequences (e.g., 1000) |
| `seq_len` | `int` | Number of time steps per sequence (e.g., 20) |
| `num_features` | `int` | Fixed at 4 (Temp, Hum, CO2, Light) |
| `seed` | `int` | Random seed for reproducibility |

### Output Specification
The output is a single `torch.Tensor` with the following properties:
- **Shape**: `(1000, 20, 4)`
- **Data Type**: `torch.float32`
- **Feature Mapping**:
    - `index 0`: Temperature ($^\circ$C)
    - `index 1`: Humidity (%)
    - `index 2`: CO₂ (ppm)
    - `index 3`: Light (lux)

## Implementation Strategy (TODOs)

1. [ ] **Implement `SolarCycleModel`**: Define the sine-based light intensity generator.
2. [ ] **Implement `AtmosphericModel`**: Link Temperature and Humidity to the Solar Cycle.
3. [ ] **Implement `CO2Model`**: Link CO₂ levels to Light and Temperature.
4. [ ] **Implement `AnomalyModule`**: Re-use existing logic for spikes/drift/dropout but integrate into the new pipeline.
5. [ ] **Implement `NormalizationModule`**: Create a transformer for $[0, 1]$ scaling.
6. [ ] **Final Integration**: Wrap all modules into the `generate_greenhouse_data` entry point.

## File Ownership
- `src/models/data_generator.py`: `ml-engineer`
