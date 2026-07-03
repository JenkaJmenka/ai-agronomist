# Project: Anomaly Detection Performance Fixes

## Architecture
This project focuses on resolving performance bottlenecks, memory leaks, and optimizing the data upload pipeline in the Anomaly Detection System.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| 1 | Fix failing tests | `tests/models/test_ensemble_fit_spy.py` | None | DONE |
| 2 | Revert unplanned changes | `src/api/routes/upload.py`, `src/dashboard/services/upload_validator.py`, `src/models/cold_run_threshold.py` | None | DONE |
| 3 | Verification & PR for perf/lru-memory-leaks | Full test suite, git commit, push, PR | Milestones 1, 2 | IN_PROGRESS |
| 4 | Phase 4 optimizations (B6, B4/B5) | `src/models/cold_run_detector.py`, `src/api/services/cold_run_upload.py` | Milestone 3 | DONE |
| 5 | Phase 4 verification & PR | New unit tests, full test suite pass, git commit, push, PR | Milestone 4 | IN_PROGRESS |

## Code Layout
- `src/models/cold_run_detector.py`
- `src/api/services/cold_run_upload.py`
- `tests/models/test_ensemble_fit_spy.py`
- `src/api/routes/upload.py`
- `src/dashboard/services/upload_validator.py`
- `src/models/cold_run_threshold.py`
