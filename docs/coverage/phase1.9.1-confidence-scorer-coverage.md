# Phase 1.9.1 Week 2 — Confidence Scorer Coverage Report

**Date:** 2026-06-14
**Agent:** qa-engineer
**Module:** `src/models/confidence_scorer.py` (334 lines, 76 executable statements)

## Summary

| Metric | Value |
|--------|-------|
| Test file | `tests/models/test_confidence_scorer.py` |
| Tests written | 118 |
| Tests passed | 118 |
| Tests failed | 0 |
| Module statements | 76 |
| Statements covered | 76 |
| **Module coverage** | **100.00%** |
| Missing lines | None |
| Run time | ~0.07s (no-cov), ~0.94s (with cov) |

## Coverage Command

```bash
venv/bin/python -m pytest tests/models/test_confidence_scorer.py \
  --cov=src.models.confidence_scorer \
  --cov-report=term-missing \
  -p no:cacheprovider
```

Output excerpt:
```
src/models/confidence_scorer.py   76   0   28   0  100.00%
118 passed in 0.94s
```

## Test Organisation

Tests are grouped into 8 test classes + 1 end-to-end class:

| Class | Tests | Focus |
|-------|-------|-------|
| `TestConstruction` | 4 | `__init__`, weight defaults, SIGNAL_NAMES, dict copy isolation |
| `TestWeightValidation` | 8 | All `_validate_weights` rejection paths (missing/extra keys, negative, non-finite, wrong sum) |
| `TestInputValidation` | 11 | `_validate_inputs` — NaN/Inf/negative for `reconstruction_error`, `baseline_error`, `uncertainty`; ensemble_agreement range + non-integer |
| `TestPatternScoresValidation` | 6 | `_validate_pattern_scores` — empty dict, NaN/Inf value, out-of-range value, NaN in multi-entry |
| `TestComputeSignals` | 20 | All four signal formulas; zero/full/capped/mid cases; single-entry pattern, tie gap, many-pattern gap |
| `TestScore` | 11 | Return type, range, weighted sum identity, monotonicity for all four inputs, default baseline, custom weights |
| `TestAssess` | 9 | All required keys, types, consistency with `score()` and `compute_signals()`, flag correctness |
| `TestHighConfidence` | 7 | Default + custom threshold, boundary (at threshold), validation |
| `TestLowConfidence` | 7 | Default + custom threshold, boundary (at threshold), validation |
| `TestConfidenceLevel` | 19 | All 15 confidence values mapped to all 5 labels; exact boundary values; label enumeration; validation |
| `TestEndToEnd` | 7 | Realistic agronomist scenario, noise scenario, zero-recon anchor, single-entry pattern, uncertainty=1.0, all ensemble boundaries, custom baseline monotonicity |

## Edge Cases Covered

- **Single-entry `pattern_scores`** — `second_score` defaults to `0.0` by design; tested via `{"cascade": 0.9}` and `{"spike": top}` shapes.
- **`uncertainty >= 1.0` capped** — `uncertainty=1.0` → signal 0.0; `uncertainty=5.0` → signal 0.0.
- **Ensemble agreement = 0 and = CONFIDENCE_ENSEMBLE_SIZE** — both boundary values accepted and produce 0.0 / 1.0 signals respectively.
- **Pattern clarity tie** — two equal scores → gap = 0.0 → clarity signal = 0.0.
- **`pattern_clarity` cap** — gap × SCALE > 1.0 is capped at 1.0.
- **`recon_error` at exact 3× baseline** — maps to signal ≈ 1.0.
- **All `confidence_level` boundaries** — values 0.0, 0.4, 0.6, 0.75, 0.85 tested explicitly.
- **Custom weights** — all-weight-on-one-signal case verifies output equals that signal × 1.0.
- **Weight isolation** — mutating original mapping after constructor does not affect stored weights.
- **Non-integer `ensemble_agreement`** — `2.5` rejected via `.is_integer()` check.

## Bugs Found

None. The `ConfidenceScorer` implementation is clean:
- All four signal formulas match the documented contracts.
- All validation paths raise `ValueError` with informative messages.
- `assess()` is fully consistent with `score()` and `compute_signals()`.
- `confidence_level()` correctly implements a top-down threshold scan.
- `high_confidence()` / `low_confidence()` use `>=` / `<=` as documented.

## Regression Status

Existing Week 1 test suites verified still passing:

```
tests/models/test_anomaly_attributor.py — 46 passed
tests/models/test_pattern_detector.py  — 83 passed + 1 xfailed (tracked bug)
Total: 129 passed, 1 xfailed — no regressions
```

## Notes

- `pytest.ini` has `--cov=src` and `--cov-fail-under=90` in `addopts`, which causes the full-suite coverage gate to fail when this file is run in isolation. Use `--no-cov` for quick test runs, or `--cov=src.models.confidence_scorer` to isolate module coverage (as documented in the task brief).
- No `src/` files were modified.
- The module is pure Python/numpy — no torch, no DB, no fixtures from `conftest.py` needed.
