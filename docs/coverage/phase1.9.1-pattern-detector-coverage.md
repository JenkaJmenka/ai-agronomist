# Coverage Summary — Week 1 Phase 1.2 (Pattern Detector)

**Date:** 2026-06-13
**Command:** `./venv/bin/python -m pytest tests/ --cov=src --cov-fail-under=90`
**Result:** 627 passed, 1 skipped, 2 xfailed — **TOTAL coverage 94.31%** (gate: 90%) ✅

## Phase 1.2 Modules (new for Pattern Detection)

| Module | Stmts | Miss | Branch | BrPart | Coverage |
|---|---|---|---|---|---|
| `src/models/pattern_detector.py` | 69 | 0 | 22 | 0 | **100.00%** |
| `src/models/pattern_scores.py` | 125 | 0 | 30 | 0 | **100.00%** |

(Was 97.80% / 96.13% after the ml-engineer's 66 smoke tests; QA pass closed
the remaining lines: baseline-too-short raise, single-sensor dependency
guard, short-baseline spectra early return, constant-channel zero-power skip,
and the cyclic zero-power ternary at detect time.)

`src/config.py` additions (`PATTERN_*` block, 19 constants) remain at 100.00%.

## Test File (Phase 1.2 QA)

| File | Tests | Focus |
|---|---|---|
| `tests/models/test_pattern_detector.py` | 84 (83 pass + 1 strict xfail) | ml-engineer's 66 smoke tests (spec's 9 + per-pattern positive/negative/affected-sensor cases + validation + result contract + baseline stats) unchanged, plus 18 QA extensions: Inf-in-baseline rejection, baseline shorter than `PATTERN_MIN_SEQ_LEN`, list-input baseline coercion, baseline re-set replaces statistics, sequence at exactly `PATTERN_MIN_SEQ_LEN`, empty `detect_batch` → `[]`, batch error propagation on NaN, single-sensor dependency guard (manual well-formed baseline), single-sensor end-to-end (xfail — bug below), zero spectra for short baselines, constant baseline channel zero power fraction, constant cyclic channel at detect time breaks cycle, spike duration exactly at / one step past `PATTERN_SPIKE_MAX_DURATION_FRACTION`, negative-shift lag direction, confidence gap scaling (`gap × PATTERN_CONFIDENCE_GAP_SCALE`), three-way tie resolves to drift (declaration order) with confidence 0.0, cross-instance determinism |

## Bug Found (reported, NOT fixed — implementation files untouched)

- **Single-sensor detector crashes in `detect()`**: `PatternDetector(sensor_names=["solo"])`
  is accepted by the constructor, but `compute_baseline` stores a **0-d**
  `correlations` array for `input_dim=1` (`np.corrcoef` of one variable
  returns a scalar). `score_dependency` then fails inside
  `np.triu_indices_from` with `ValueError: input array must be 2-d` —
  *before* reaching its own empty-pairs guard, which is unreachable through
  the public API. Suggested fix (ml-engineer): `np.atleast_2d` the
  correlation matrix in `compute_baseline`/`_safe_corrcoef`, or reject
  `len(sensor_names) < 2` in the constructor. Tracked by strict xfail
  `TestQaSingleSensor::test_detect_full_pipeline_with_single_sensor`.

## Notes

- Spec called for 40+ tests; delivered 84, all green (xfail is strict and
  documents the bug above).
- Module-only run requires overriding the global gate from `pytest.ini`:
  `./venv/bin/python -m pytest tests/models/test_pattern_detector.py --cov=src.models.pattern_detector --cov-fail-under=0`
- All randomness seeded (`np.random.default_rng(42)`); pattern detection is
  pure numpy (no torch model), so results are exactly reproducible —
  verified by the cross-instance determinism test.
