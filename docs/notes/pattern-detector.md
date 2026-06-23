# Pattern Detector — Notes

**Source:** pattern_detector_review.md

## NOTE-PD-001: Hardcoded lag-window divisor

`len(x) // 4` in `pattern_scores.py:276` not in config. Minor tuning knob.

**Effort:** Add `PATTERN_LAG_WINDOW_DIVISOR` to config.py

---

## NOTE-PD-002: Duplicate z-score expression

Same z-score formula in `score_spike` and `score_cascade`. Extract `_z_scores(sequence, baseline)` helper to prevent drift.

**Effort:** 30 min refactor

---

## NOTE-PD-003: 2-sensor minimal regression test gap

**Resolved** 2026-06-15 — `test_two_sensor_dependency_through_detect` added in `TestQaSingleSensor`.
