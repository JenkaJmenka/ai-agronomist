# Root Cause + Recommendations — Notes

**Source:** root_cause_and_recommendations_review.md

## NOTE-RC-001: DIAGNOSIS_RULES confidences not in config

Confidence values (0.65–0.85) hardcoded in class-level dict. Rule-intrinsic metadata — co-location defensible. Consider config override for customer tuning.

---

## NOTE-RC-002: sensor_malfunction unreachable

`RECOMMENDED_ACTIONS` has `sensor_malfunction` key but no `DIAGNOSIS_RULES` entry produces it. Orphan actions dict entry.

**Fix:** Add diagnosis rule or remove orphan key.

---

## NOTE-RC-003: root_cause_analyzer.py exceeds 300-line cap

408 lines; ~100 are `DIAGNOSIS_RULES` data. Split to `diagnosis_rules.py` data module.

---

## NOTE-RC-004: Substring sensor matching fragile

`_sensors_match` uses substring matching. Safe with current sensor names; breaks if names change (e.g. `"temp"` matching `"temperature"` and `"temp_sensor"`).

---

## NOTE-RC-005: No test for non-string root_cause input

`RecommendationEngine.recommend()` would raise correctly via isinstance check but no explicit test.
