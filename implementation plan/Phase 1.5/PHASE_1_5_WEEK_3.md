# Phase 1.5 - WEEK 3: Seasonal Models
## Summer/Winter/Spring/Fall Variants (Sequential, 20 min)

---

## 📋 **Week 3 Overview**

**Sequential** — runs AFTER Week 2 completes  
**Duration:** ~20 minutes  
**Goal:** Implement seasonal model detection and training

**Dependencies:**
- ✅ Week 2 complete (Per-customer model registry working)

---

## 🚀 **Submit This to Orchestrator**

Wait for Week 2 to complete, then paste:

---

## **ORCHESTRATOR SUBMISSION TEXT**

```
Continue Phase 1.5 Week 3 - Seasonal model variants and auto-detection.

Phase 1.5.3 (after Phase 1.5.2):

- architect: Design seasonal model architecture and season detection strategy.
  Output: docs/architecture/seasonal_models_design.md
  
  Deliverable:
  Design document covering:
  
  1. Season Detection Strategy
     - Method 1: Calendar-based (Jan-Mar spring, Apr-Jun summer, etc.)
     - Method 2: Temperature-based (average > 25°C = summer?)
     - Method 3: Hybrid (calendar + temperature confirmation)
     - Recommendation: Use hybrid approach
     
     Example:
     - Spring: Mar 21 - Jun 20 (or avg_temp 15-20°C)
     - Summer: Jun 21 - Sep 22 (or avg_temp > 25°C)
     - Fall: Sep 23 - Dec 20 (or avg_temp 15-20°C)
     - Winter: Dec 21 - Mar 20 (or avg_temp < 15°C)
  
  2. Seasonal Model Training Strategy
     - Train separate decoder for each season
     - Share encoder (learned features)
     - Rationale: Same sensors, different operating patterns
     
     Example:
     - Customer Tomato Summer Model: trained on Jun-Aug data
     - Customer Tomato Winter Model: trained on Dec-Feb data
     - Both use same encoder, different decoders
  
  3. Model Selection at Inference
     - Input: current timestamp + customer_id
     - Determine season
     - Load customer + season model
     - Run inference with season-specific model
  
  4. Seasonal Baseline Detection
     - Baselines per season (summer ranges ≠ winter ranges)
     - Example: Summer temp 24-28°C, Winter temp 18-22°C

- ml-engineer: Implement seasonal detector and seasonal trainer.
  Output: src/models/seasonal_detector.py, src/models/seasonal_trainer.py
  
  Deliverables:
  
  1. src/models/seasonal_detector.py
     class SeasonalDetector:
       
       def __init__(self):
         """Initialize season boundaries"""
       
       def detect_season(self, timestamp: datetime) -> str:
         """
         Detect season from timestamp.
         
         Returns: "spring", "summer", "fall", "winter"
         
         Using calendar method (simple, reliable for greenhouses)
         """
       
       def confirm_season_by_temperature(self,
                                        avg_temp: float) -> str:
         """
         Confirm season by temperature.
         
         < 15°C → winter
         15-20°C → spring/fall
         > 25°C → summer
         """
       
       def get_season_range(self, season: str) -> Tuple[datetime, datetime]:
         """Get date range for season"""
  
  2. src/models/seasonal_trainer.py
     class SeasonalTrainer:
       
       async def train_seasonal_models(self,
                                      customer_id: str,
                                      all_customer_data: np.ndarray) -> Dict[str, str]:
         """
         Train separate models for each season.
         
         Input: all customer data (entire year if available)
         
         Steps for each season:
         1. Filter data for that season
         2. Fine-tune Week 1 model on season-specific data
         3. Register model with season label
         4. Return dict: {"spring": model_id, "summer": model_id, ...}
         
         Note: If data < 2 weeks for season, use fallback model
         """
       
       async def _partition_by_season(self,
                                     data: np.ndarray,
                                     timestamps: List[datetime]) -> Dict[str, np.ndarray]:
         """Separate data by season"""
       
       async def _train_season_model(self,
                                    customer_id: str,
                                    season: str,
                                    season_data: np.ndarray) -> str:
         """Train single season model, return model_id"""
       
       def _has_enough_data_for_season(self, season_data: np.ndarray) -> bool:
         """Check if season has minimum data (2 weeks)"""

- backend-engineer: Update inference to use seasonal models.
  Output: src/api/routes/inference.py (updated), src/models/inference.py (updated)
  
  Deliverables:
  
  1. src/api/routes/inference.py (UPDATED)
     Modify POST /api/inference:
     - New request field: "use_seasonal" (boolean, default True)
     - If use_seasonal=True:
       * Detect current season
       * Load season-specific model for customer
     - If use_seasonal=False:
       * Use primary customer model (non-seasonal)
     
     Example request:
     {
       "customer_id": "customer_tomato_1",
       "greenhouse_id": "gh1",
       "sequences": [...],
       "use_seasonal": true
     }
     
     Example response:
     {
       "model_id": "model_tomato_summer_v2",
       "season": "summer",
       "customer_id": "customer_tomato_1",
       "anomalies_detected": 5
     }
  
  2. src/models/inference.py (UPDATED)
     Enhance to support seasonal models:
     - load_seasonal_model(customer_id, season)
     - get_current_season()
     - Maintain cache of seasonal models

- backend-engineer: Update model registry for seasonal models.
  Output: src/models/model_registry.py (updated), src/db/migrations/002_add_model_tables.py (updated)
  
  Deliverables:
  
  1. model_version table (UPDATE)
     Add column: season VARCHAR (NULL, "spring", "summer", "fall", "winter")
     
     Now: model_id = customer_id + season (or NULL for primary)
  
  2. model_registry.py (UPDATE)
     - register_seasonal_model(customer_id, season, ...)
     - get_seasonal_model(customer_id, season)
     - get_all_seasonal_variants(customer_id)
  
  3. Dashboard integration:
     - Show "Seasonal Models" section
     - Display: Customer → Season → Model ID → Metrics
     - Example:
       Customer Tomato
       ├─ Spring: model_v1 (improvement: 15%)
       ├─ Summer: model_v3 (improvement: 28%)
       ├─ Fall: model_v2 (improvement: 18%)
       └─ Winter: model_v1 (improvement: 10%)

- qa-engineer: Test seasonal model detection and training.
  Output: tests/models/test_seasonal_detector.py, tests/models/test_seasonal_trainer.py, tests/integration/test_seasonal_inference.py
  
  Deliverables:
  
  1. tests/models/test_seasonal_detector.py
     Test cases:
     - test_detect_spring: Mar 21 → "spring"
     - test_detect_summer: Jul 1 → "summer"
     - test_detect_fall: Sep 23 → "fall"
     - test_detect_winter: Dec 25 → "winter"
     - test_confirm_season_by_temperature: warm → "summer", cold → "winter"
     - test_season_boundaries: boundary dates handled correctly
  
  2. tests/models/test_seasonal_trainer.py
     Test cases:
     - test_partition_by_season: Data correctly separated
     - test_train_seasonal_models: All 4 seasons trained
     - test_seasonal_models_differ: Summer model ≠ Winter model
     - test_insufficient_data_fallback: If season has < 2 weeks, use fallback
     - test_seasonal_model_registered: Models registered with season label
     - test_all_seasons_present: All 4 seasons have models
  
  3. tests/integration/test_seasonal_inference.py
     Integration test:
     - Train seasonal models for customer
     - Run inference in "summer" → use summer model
     - Run inference in "winter" → use winter model
     - Verify different models used
     - Verify results differ seasonally

---

## ✅ **Week 3 Success Criteria**

- [ ] `docs/architecture/seasonal_models_design.md` created
- [ ] `src/models/seasonal_detector.py` detects season correctly
- [ ] `src/models/seasonal_trainer.py` trains seasonal variants
- [ ] Can train separate models for each season
- [ ] Inference automatically selects season-specific model
- [ ] Seasonal baselines created (summer temp ≠ winter temp)
- [ ] Dashboard shows seasonal model variants
- [ ] Can rollback per-season
- [ ] `tests/models/test_seasonal_detector.py` passes (6 tests)
- [ ] `tests/models/test_seasonal_trainer.py` passes (6 tests)
- [ ] `tests/integration/test_seasonal_inference.py` passes (1 test)

---

## 🧪 **Test Commands** (After Week 3)

```bash
# Test seasonal detection
pytest tests/models/test_seasonal_detector.py -v

# Test seasonal training
pytest tests/models/test_seasonal_trainer.py -v

# Test seasonal inference end-to-end
pytest tests/integration/test_seasonal_inference.py -v -s

# Check that seasonal models are registered
sqlite3 app.db "SELECT model_id, season FROM model_version WHERE season IS NOT NULL LIMIT 5;"
# Should show: model_tomato_summer_v1, "summer"
#              model_tomato_winter_v1, "winter"
```

---

## 📊 **Seasonal Model Example**

```
Customer: Tomato Greenhouse
Crop: Tomato

Primary Model:
  - model_tomato_v1 (trained on synthetic, no season)
  
Seasonal Models:
  - model_tomato_spring_v1 (trained on Mar-Jun data)
  - model_tomato_summer_v2 (trained on Jun-Sep data, 28% improvement)
  - model_tomato_fall_v1 (trained on Sep-Dec data)
  - model_tomato_winter_v1 (trained on Dec-Mar data)

Inference Logic:
  - Today: July 15, 2026 (summer)
  - Detect season: "summer"
  - Load: model_tomato_summer_v2
  - Run inference with season-specific model
```

---

## 💡 **Key Design Points**

- **Separate Decoders:** Each season has different normal ranges
- **Shared Encoder:** Feature extraction is consistent
- **Hybrid Season Detection:** Calendar + temperature confirmation
- **Fallback:** If season has insufficient data, use primary model
- **Dashboard Visibility:** Show which seasonal model deployed

---

## 📍 **What Happens Next**

When Week 3 completes:
1. ✅ Seasonal models working
2. ✅ Auto-detect season, use right model
3. ✅ Better accuracy (season-specific baselines)
4. ✅ Dashboard shows seasonal variants

Next: **Week 4** (Operator feedback loop + auto-threshold tuning)

---

**Ready?** Wait for Week 2 to finish! 🚀
