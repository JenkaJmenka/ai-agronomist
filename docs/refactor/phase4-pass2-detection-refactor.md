# Phase 4 Pass 2 Detection Pipeline Refactor

**Date:** 2026-06-24
**Author:** tech-lead agent

## Summary

Refactored the Phase 4 Pass 2 detection pipeline to eliminate code duplication,
improve adherence to SOLID principles (especially Single Responsibility,
Open/Closed, and DRY), and derive seasonal mappings from the single source of
truth in `core/config.py`.

## Changes

### 1. New file: `detectors/registry.py`

Created a central detector registry exporting `DEFAULT_DETECTOR_CLASSES`.
Previously, the same 5-element list `[SpikeDetector, StuckDetector,
DriftDetector, PrecisionDetector, PollingDetector]` was hardcoded in both
`engines/pass2_detect.py` and `engines/pass2_detect_seasonal.py`.

**SOLID benefit (Open/Closed):** Adding a new detector type now requires a
single edit to `detectors/registry.py` instead of modifying every detection
engine that uses the detector set.

### 2. New file: `engines/detection_runner.py`

Extracted the shared row-level detection loop into two reusable functions:

- `collect_triggered(value, pattern, sensor, detectors, mv_info)` -- runs all
  univariate detectors plus the multivariate check for a single sensor value.
- `run_detection_loop(df, pattern, detectors, explainer, timestamp_column, season)`
  -- iterates all rows and sensors, calling collect_triggered, aggregating
  votes, validating physics, generating explanations, and building Anomaly
  objects.

Previously, this ~40-line inner loop was duplicated verbatim in
`Pass2Detector.detect()` and `Pass2DetectorSeasonal._detect_season()`.

**SOLID benefit (Single Responsibility):** The detection loop logic is now in
one place.  The detector classes (`Pass2Detector`, `Pass2DetectorSeasonal`) are
responsible only for orchestration (choosing patterns, splitting seasons,
clustering) -- not for the row-by-row detection mechanics.

### 3. Simplified `engines/pass2_detect.py`

Reduced from 121 lines to 55 lines.  `Pass2Detector.detect()` now delegates to
`run_detection_loop()` and only handles stats computation.

### 4. Simplified `engines/pass2_detect_seasonal.py`

Reduced from 180 lines to 123 lines.  `_detect_season()` now delegates to
`run_detection_loop()` (was a 38-line method, now 9 lines).

### 5. Derived `_MONTH_TO_SEASON` from `core/config.py SEASONS`

Replaced the hardcoded `_MONTH_TO_SEASON` dict (12 entries manually mapping
month numbers to season names) with `_build_month_to_season()` which derives
the mapping from `core.config.SEASONS`.  This eliminates a second source of
truth for season definitions -- if SEASONS is updated in config, the detection
pipeline picks up the change automatically.

## Files not changed

- `models/anomaly.py` -- no issues found; field types match actual usage.
- `models/explainability.py` -- self-contained, no duplication.
- `engines/pass2_result_aggregator.py` -- no duplication; already under 82
  lines; composable via static methods.
- `api/routes/patterns.py` -- clean, no issues.
- `api/routes/anomaly.py` -- no changes needed; imports the same public classes.
- `storage/result_store.py` -- clean, no issues.
- `api/main.py` -- no import changes needed.

## Public API preserved

No class names, method signatures, return types, or module-level exports were
changed.  All existing imports (`from engines.pass2_detect import Pass2Detector`,
`from engines.pass2_detect_seasonal import Pass2DetectorSeasonal`) continue to
work unchanged.

## Verification

Import smoke tests:
- `from engines.pass2_detect_seasonal import Pass2DetectorSeasonal`
- `from api.routes.anomaly import router`
- `from engines.pass2_detect import Pass2Detector`
- `from detectors.registry import DEFAULT_DETECTOR_CLASSES`
- `from engines.detection_runner import run_detection_loop, collect_triggered`
