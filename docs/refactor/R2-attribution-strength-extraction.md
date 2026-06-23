# R2: Extract AttributionStrength to its own module

**Date**: 2026-06-15
**Status**: Complete
**Files changed**: 2 modified, 1 created

## Motivation

`anomaly_attributor.py` was 361 lines, exceeding the 300-line guideline. The
`AttributionStrength` class (lines 321-361) is a self-contained utility that
only depends on numpy and `NUMERICAL_EPSILON` from config. It has no coupling
to `AnomalyAttributor`, `LSTMVAE`, or torch.

Extracting it reduces `anomaly_attributor.py` to 313 lines and places the
distribution-analysis utilities in a focused module.

## Changes

1. **Created `src/models/attribution_strength.py`** (49 lines)
   - Moved `AttributionStrength` class verbatim (gini_impurity, entropy)
   - Imports: numpy, `NUMERICAL_EPSILON` from src.config

2. **Modified `src/models/anomaly_attributor.py`** (361 -> 313 lines)
   - Removed `AttributionStrength` class body
   - Added `from src.models.attribution_strength import AttributionStrength`
   - Added `__all__ = ["AnomalyAttributor", "AttributionStrength"]` for re-export
   - Trimmed the module docstring example block (already documented on
     `attribute_sequence`)

3. **No test files changed**
   - `from src.models.anomaly_attributor import AttributionStrength` continues
     to work via the re-export

## Backward compatibility

The public API is unchanged. Any code importing `AttributionStrength` from
`src.models.anomaly_attributor` will still work. New code can also import
directly from `src.models.attribution_strength`.

## Verification

All 46 tests in `tests/models/test_anomaly_attributor.py` pass, including
the 9 `AttributionStrength` tests (gini and entropy).
