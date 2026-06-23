# Phase 1.5 - WEEK 1: Baseline Analysis & Transfer Learning
## Synthetic Customer Data + Model Adaptation (Parallel, 25 min)

---

## 📋 **Week 1 Overview**

**Parallel execution** — Multiple agents work simultaneously  
**Duration:** ~25 minutes  
**Goal:** Create synthetic customer data, implement baseline analysis, transfer learning pipeline

**Dependencies:**
- ✅ Week 2 complete (API + Dashboard working)
- ✅ Week 1 model (LSTM-VAE checkpoint ready)

---

## 🚀 **Submit This to Orchestrator**

```bash
cd ~/Projects/ai-agronomist
claude

# At ❯ prompt, paste everything below this line:
```

---

## **ORCHESTRATOR SUBMISSION TEXT**

```
Build Phase 1.5 Week 1 - Baseline analysis and transfer learning infrastructure.

Phase 1.5.1 (parallel):

- architect: Design baseline analysis and transfer learning architecture.
  Output: docs/architecture/baseline_transfer_learning_design.md
  
  Deliverable:
  Design document covering:
  1. Baseline Analysis Architecture
     - Input: 2 weeks of customer normal operation (CSV)
     - Analysis steps: detect ranges, correlations, drift patterns
     - Output: baseline profile (JSON)
     - Example baseline:
       {
         "customer_id": "customer_tomato_1",
         "greenhouse_type": "tomato",
         "ranges": {
           "temperature": {"min": 20.5, "mean": 24.0, "max": 27.5, "std": 1.2},
           "humidity": {"min": 60.0, "mean": 68.5, "max": 76.0, "std": 3.1},
           "co2": {"min": 350, "mean": 450, "max": 520, "std": 45},
           "light": {"min": 600, "mean": 850, "max": 1100, "std": 120}
         },
         "correlations": {
           "temp_humidity": 0.72,
           "humidity_light": 0.35
         },
         "drift": {"temperature": "stable", "humidity": "slight_morning_spike"}
       }
  
  2. Transfer Learning Architecture
     - Input: Week 1 LSTM-VAE model + customer baseline
     - Fine-tuning strategy: freeze encoder, fine-tune decoder
     - Training: 30-60 min on customer's 2 weeks data
     - Output: customer-specific model weights
     - Evaluation: accuracy comparison (Week 1 vs adapted)
  
  3. Model Evaluation Framework
     - Metrics: reconstruction error, anomaly score distribution
     - Comparison: synthetic vs customer model performance
     - Success criteria: 20%+ improvement on customer data

- ml-engineer: Implement baseline analyzer and transfer learning pipeline.
  Output: src/models/baseline_analyzer.py, src/models/transfer_learner.py, src/models/model_evaluator.py
  
  Deliverables:
  
  1. src/models/baseline_analyzer.py
     class BaselineAnalyzer:
       
       def __init__(self, config: Config):
         """Initialize with sensor ranges from config"""
       
       async def analyze(self, customer_data: np.ndarray) -> Dict:
         """
         Analyze 2 weeks of customer normal operation.
         
         Input: (N, 4) sensor readings [temp, humidity, co2, light]
         Output: baseline profile with ranges, correlations, drift
         
         Steps:
         1. Calculate per-sensor statistics (min, max, mean, std)
         2. Detect correlations between sensors
         3. Detect drift patterns (is something gradually changing?)
         4. Return baseline profile (dict)
         """
         results = {
           "ranges": {},
           "correlations": {},
           "drift": {}
         }
         
         for sensor in ["temperature", "humidity", "co2", "light"]:
           results["ranges"][sensor] = {
             "min": float(customer_data[:, idx].min()),
             "max": float(customer_data[:, idx].max()),
             "mean": float(customer_data[:, idx].mean()),
             "std": float(customer_data[:, idx].std())
           }
         
         # Correlations (temperature vs humidity, etc.)
         corr_matrix = np.corrcoef(customer_data.T)
         results["correlations"]["temp_humidity"] = float(corr_matrix[0, 1])
         results["correlations"]["humidity_light"] = float(corr_matrix[1, 3])
         
         # Drift detection (linear trend)
         for sensor_idx, sensor_name in enumerate(["temperature", "humidity", "co2", "light"]):
           trend = np.polyfit(np.arange(len(customer_data)), customer_data[:, sensor_idx], 1)[0]
           results["drift"][sensor_name] = "increasing" if trend > 0.001 else "stable"
         
         return results
       
       def save_baseline(self, customer_id: str, baseline: Dict) -> str:
         """Save baseline to database/file, return baseline_id"""
  
  2. src/models/transfer_learner.py
     class TransferLearner:
       
       def __init__(self, checkpoint_path: str):
         """Load Week 1 model as foundation"""
       
       async def fine_tune(self, 
                          customer_id: str,
                          customer_data: np.ndarray,
                          epochs: int = 10,
                          learning_rate: float = 0.0001) -> str:
         """
         Fine-tune Week 1 model on customer data.
         
         Strategy:
         - Freeze encoder (learned feature extraction)
         - Fine-tune decoder (customer-specific reconstruction)
         - Use lower learning rate (0.0001 vs 0.001)
         - Train for fewer epochs (10 vs 50)
         
         Input: customer_data (N, 20, 4) sequences
         Output: model checkpoint path
         
         Training loop:
         - Load Week 1 checkpoint
         - Freeze encoder weights
         - Train decoder on customer data
         - Evaluate on held-out test set (20% of customer data)
         - Save checkpoint with customer_id
         """
       
       def _freeze_encoder(self, model):
         """Freeze all encoder weights"""
         for param in model.encoder.parameters():
           param.requires_grad = False
       
       def _unfreeze_decoder(self, model):
         """Unfreeze decoder weights for training"""
         for param in model.decoder.parameters():
           param.requires_grad = True
  
  3. src/models/model_evaluator.py
     class ModelEvaluator:
       
       async def evaluate(self, 
                         model: LSTMVAE,
                         test_data: np.ndarray) -> Dict:
         """
         Evaluate model performance.
         
         Returns:
         {
           "mse": 0.045,
           "reconstruction_error_mean": 0.042,
           "reconstruction_error_std": 0.018,
           "anomaly_score_distribution": {
             "min": 0.01,
             "max": 0.95,
             "mean": 0.32,
             "percentiles": {"25": 0.15, "50": 0.28, "75": 0.45}
           }
         }
         """
       
       def compare(self, 
                  baseline_eval: Dict,
                  customer_eval: Dict) -> Dict:
         """
         Compare Week 1 vs adapted model.
         
         Returns improvement metrics:
         {
           "improvement_percent": 23.5,
           "baseline_error": 0.055,
           "customer_error": 0.042
         }
         """

- ml-engineer: Create synthetic customer datasets.
  Output: data/synthetic_customers/customer_tomato/normal.csv, anomalies.csv, etc.
  
  Deliverables:
  
  Create 3 synthetic customer profiles with realistic data:
  
  1. data/synthetic_customers/customer_tomato/normal.csv
     - 2 weeks (336 hours) of normal tomato greenhouse operation
     - Columns: timestamp, temperature, humidity, co2, light
     - Patterns:
       * Temperature: 22-26°C, daily cycle (warmer afternoon, cooler night)
       * Humidity: 65-75%, higher in morning
       * CO2: 400-500 ppm, varies with light
       * Light: 0-1000 lux, 0 at night, peak at noon
     - Row example: 2026-06-01T00:00:00Z,23.2,68.5,450,0
  
  2. data/synthetic_customers/customer_tomato/anomalies.csv
     - 1 week with known anomalies to detect:
       * Days 1-2: Normal operation
       * Day 3: Heating failure (temp drops to 15°C, stays low)
       * Days 4-5: Normal
       * Day 6: High humidity spike (95%+, indicates moisture problem)
       * Day 7: Normal
  
  3. data/synthetic_customers/customer_lettuce/normal.csv
     - 2 weeks lettuce greenhouse (cooler, more humid)
     - Temperature: 18-22°C
     - Humidity: 70-80%
     - CO2: 350-450 ppm
     - Light: 600-800 lux
  
  4. data/synthetic_customers/customer_lettuce/anomalies.csv
     - Known anomalies for lettuce
  
  5. data/synthetic_customers/customer_cucumber/normal.csv
     - 2 weeks cucumber greenhouse (warmest, drier)
     - Temperature: 24-28°C
     - Humidity: 60-70%
     - CO2: 450-550 ppm
     - Light: 900-1200 lux
  
  6. data/synthetic_customers/customer_cucumber/anomalies.csv
     - Known anomalies for cucumber
  
  Technical:
  - Use numpy + pandas to generate realistic time series
  - Include realistic correlations (temp ↔ humidity ~0.7)
  - Use sine curves for daily patterns
  - Add slight noise to make realistic
  - Save as CSV with ISO timestamps

- qa-engineer: Write tests for baseline analysis and transfer learning.
  Output: tests/models/test_baseline_analyzer.py, tests/models/test_transfer_learner.py, tests/models/test_model_evaluator.py
  
  Deliverables:
  
  1. tests/models/test_baseline_analyzer.py
     Test cases:
     - test_analyze_customer_data: Input 2 weeks data → baseline profile returned
     - test_baseline_ranges: Ranges match actual data (min/max correct)
     - test_baseline_correlations: Correlations computed correctly
     - test_drift_detection: Can detect increasing/decreasing trends
     - test_baseline_saved: Baseline saved to database with ID
     - test_baseline_multiple_customers: Different profiles for different customers
  
  2. tests/models/test_transfer_learner.py
     Test cases:
     - test_fine_tune_runs: Fine-tuning completes in <2 minutes
     - test_encoder_frozen: Encoder weights don't change during training
     - test_decoder_updated: Decoder weights change during training
     - test_model_checkpoint_saved: Checkpoint saved with customer_id
     - test_fine_tune_improves_accuracy: Adapted model better than baseline on customer data
     - test_multiple_customer_models: Can train separate models for different customers
  
  3. tests/models/test_model_evaluator.py
     Test cases:
     - test_evaluate_returns_metrics: Evaluation returns required fields
     - test_reconstruction_error_positive: Error values are positive
     - test_anomaly_scores_in_range: Scores in [0, 1]
     - test_compare_models: Comparison shows improvement percent
     - test_improvement_positive: Adapted model shows positive improvement
  
  Fixtures:
  - @pytest.fixture: sample_customer_data (2 weeks tomato greenhouse)
  - @pytest.fixture: sample_anomalies (anomaly sequences)
  - @pytest.fixture: transfer_learner instance

auto-proceed
```

---

## ✅ **Week 1 Success Criteria**

- [ ] `docs/architecture/baseline_transfer_learning_design.md` created
- [ ] `src/models/baseline_analyzer.py` analyzes customer data correctly
- [ ] `src/models/transfer_learner.py` fine-tunes Week 1 model
- [ ] `src/models/model_evaluator.py` compares models
- [ ] 3 synthetic customer datasets created (tomato, lettuce, cucumber)
- [ ] Each has normal.csv (2 weeks) + anomalies.csv (1 week)
- [ ] Baseline analysis extracts ranges, correlations, drift
- [ ] Transfer learning produces 20%+ improvement on customer data
- [ ] `tests/models/test_baseline_analyzer.py` passes (6 tests)
- [ ] `tests/models/test_transfer_learner.py` passes (6 tests)
- [ ] `tests/models/test_model_evaluator.py` passes (5 tests)
- [ ] Total: 17 tests passing

---

## 🧪 **Test Commands** (After Week 1)

```bash
# Run baseline analyzer tests
pytest tests/models/test_baseline_analyzer.py -v

# Expected output:
# test_analyze_customer_data PASSED
# test_baseline_ranges PASSED
# test_baseline_correlations PASSED
# ... (6 total)

# Run transfer learner tests (may take 1-2 min, fine-tuning runs)
pytest tests/models/test_transfer_learner.py -v

# Expected: model improves 20%+ on customer data
# test_fine_tune_improves_accuracy: Week1 error=0.055, Customer error=0.042 (+23%)

# Check file structure
ls data/synthetic_customers/
# customer_tomato/
# customer_lettuce/
# customer_cucumber/
```

---

## 📊 **Synthetic Data Structure**

```
data/synthetic_customers/
├── customer_tomato/
│   ├── normal.csv (336 hours × 4 sensors)
│   └── anomalies.csv (168 hours with 3 anomalies)
├── customer_lettuce/
│   ├── normal.csv
│   └── anomalies.csv
└── customer_cucumber/
    ├── normal.csv
    └── anomalies.csv
```

**Example normal.csv row:**
```
timestamp,temperature,humidity,co2,light
2026-06-01T00:00:00Z,23.2,68.5,450,0
2026-06-01T00:15:00Z,23.1,68.6,451,0
2026-06-01T00:30:00Z,23.0,68.7,452,0
...
2026-06-15T23:45:00Z,24.5,70.2,490,5
```

---

## 💡 **Key Implementation Points**

- **Baseline Analysis:** Statistical analysis of customer data (no ML)
- **Transfer Learning:** Use frozen encoder, fine-tune decoder
- **Synthetic Data:** Realistic time series with known anomalies for testing
- **Evaluation:** Compare Week 1 vs customer-adapted model

---

## 📍 **What Happens Next**

When Week 1 completes:
1. ✅ Baseline analysis working (can extract customer profiles)
2. ✅ Transfer learning working (can adapt models to customers)
3. ✅ Synthetic data ready for testing
4. ✅ Evaluation shows 20%+ improvement

Next: **Week 2** (Per-customer model registry + versioning)

---

**Ready?** Copy the "ORCHESTRATOR SUBMISSION TEXT" section above! 🚀
