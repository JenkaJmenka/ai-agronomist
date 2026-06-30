# AI Agronomist — Refactoring Plan

> **Scope:** Full repository analysis as of 2026-06-30.
> **Method:** Direct code reading, grep, test-suite execution with coverage.
> **Status:** Analysis only — no code was modified.

---

## 1. Executive Summary — Top 5 Issues by Impact

| # | Issue | Impact |
|---|-------|--------|
| **1** | **Test suite is completely broken at the collection stage** — 27 of 27 test groups cannot even be imported due to missing pip packages (`python-json-logger`, `plotly`, `streamlit`, `aiosqlite`). Zero API or integration tests run. Actual achievable coverage is **33%** (unit tests only), far below the documented 90% Phase 3 target. | **Active defect blocker** |
| **2** | **Two parallel application stacks exist side by side** — the live production stack lives under `src/`, while a dead legacy stack (`api/`, `core/`, `database/`, `detectors/`, `engines/`, `parsers/`, `storage/`) remains on disk and is still referenced by tests. This pollutes the test runner, causes import confusion, and provides false reassurance to anyone reading the codebase. | **Architectural debt** |
| **3** | **`requirements.txt` is drastically incomplete and disagrees with `pyproject.toml`** — the project has two dependency specifications that list different packages at different versions, neither of which describes what is actually installed. Critical runtime dependencies (`torch`, `plotly`, `streamlit`, `aiosqlite`, `python-json-logger`, `scikit-learn`) are absent from `requirements.txt`, making the project non-reproducible from a clean environment. | **Reproducibility break** |
| **4** | **Configuration sprawl** — `ANOMALY_THRESHOLD`'s default value (`0.8`) is hardcoded as a string literal in at least 7 locations across 5 files. The API base URL (`http://localhost:8000`) is repeated 5 times across dashboard pages instead of being read from a single point. A second config module (`core/config.py`) duplicates a subset of `src/config.py` values. | **Likely source of future divergence bugs** |
| **5** | **20+ silent `except Exception: pass` blocks**, including one in the hot inference path (`src/api/routes/inference.py:90-91`) that silently drops anomaly score writes to the database when a timestamp cannot be parsed — the scores are lost with no log entry. | **Data integrity risk** |

---

## 2. Findings by Category

### 2.1 Code Organization and Module Boundaries

#### Two complete application stacks (legacy vs. live)

The project contains two parallel FastAPI applications that serve the same domain:

**Live stack (active):**
- `src/api/main.py` — FastAPI app with 12 routers, auth middleware, structured logging, async SQLAlchemy
- `src/db/` — SQLAlchemy 2.x async models, sessions, migrations
- `src/models/` — 47 Python modules covering LSTM-VAE, ensemble detection, online learning, calibration

**Legacy stack (dead):**
- `api/main.py:1-33` — Minimal FastAPI app with 3 routes, sync `init_db()`, `allow_origins=["*"]`
- `api/routes/anomaly.py`, `api/routes/feedback.py`, `api/routes/patterns.py`
- `core/config.py:1-35` — 35-line config with 6 constants, superseded by `src/config.py:1-670`
- `database/models.py:1-58` — Legacy SQLAlchemy models using old `declarative_base()` style
- `database/session.py`, `database/crud.py`
- `detectors/` — 11 detector files (`spike.py`, `stuck.py`, `drift.py`, `precision.py`, `polling.py`, `multivariate.py`, `lstm_ae_wrapper.py`, `physics_validator.py`, `registry.py`, `utils.py`, `base.py`)
- `engines/` — 12 files including `voting.py`, `pass1_learn.py`, `pass2_detect.py`, etc.
- `parsers/csv_parser.py`
- `storage/model_store.py`, `storage/result_store.py`

The legacy stack is internally self-consistent (they import each other: `api/main.py` → `database.session`, `engines/*.py` → `core.config`) but is not wired into any production entrypoint and receives no new development.

**Dead code also confirmed by:** the three test databases left at the project root: `test_global.db`, `test_phase4_analyze.db`, `test_phase4_detect.db`, `test_phase4_patterns.db`, and the standalone `test_debug.py:2` which imports `from detectors.physics_validator`.

#### Class-name collisions between stacks

| Name | Legacy location | Live location |
|------|-----------------|---------------|
| `DriftDetector` | `detectors/drift.py` | `src/models/drift_detector.py` |
| `PhysicsValidator` | `detectors/physics_validator.py` | `src/models/physics_validator.py` |
| `CSVParser` / `CSVAdapter` | `parsers/csv_parser.py` | `src/vendors/csv_adapter.py` |
| `OperatorFeedback` | `database/models.py:43` | `src/db/models.py:246` |

#### File-size violations (300-line rule)

| File | Lines |
|------|-------|
| `src/dashboard/pages/2_anomalies.py` | 535 |
| `src/config.py` | 670 |
| `src/models/ensemble_detector.py` | 336 |
| `src/models/online_learner.py` | 323 |
| `src/vendors/csv_adapter.py` | 388 (with imports) |
| `src/api/services/comprehensive_pipeline.py` | ~800 (estimated from grep depth) |

#### Module-level global mutable state

`src/api/routes/learning.py:48-49`:
```python
_manager: Optional[OnlineLearningManager] = None
_drift_detectors: Dict[str, DriftDetector] = {}
```
These are process-level singletons. Under any multi-worker deployment (e.g., `uvicorn --workers 4`) each worker maintains independent state, so drift history and online learner updates are invisible across workers. The state is also lost on every process restart.

---

### 2.2 Duplicated Logic

#### CSV parsing (high impact)

Two independent CSV parsers implement overlapping logic:

- `parsers/csv_parser.py:17-29` — tries each of four delimiters by "most columns" heuristic; uses `decimal=','` unconditionally
- `src/vendors/csv_adapter.py:74-105` — reads first 4 KB, counts `;` vs `,` in the header, detects BOM, handles decimal format

The heuristics differ in important ways: the legacy parser uses `decimal=','` for all files, which silently misparses English-format CSVs. The live parser handles this correctly. Tests in `tests/unit/test_csv_parser.py` test the legacy parser (`from parsers.csv_parser import CSVParser`), so they do not provide coverage of the live adapter.

Additionally, `parsers/csv_parser.py:63-77` (`validate()`) duplicates the minimum-row and minimum-column checks present in `src/vendors/csv_adapter.py` under `normalize()`.

#### Timestamp parsing (medium impact)

`src/api/routes/inference.py:88-91`:
```python
try:
    dt_timestamps.append(datetime.fromisoformat(ts_str.replace('Z', '+00:00')))
except Exception:
    pass  # ← silent drop
```

`src/vendors/csv_adapter.py:369`:
```python
ts = pd.to_datetime(data[TIMESTAMP_COL], errors="raise", format="mixed", dayfirst=True)
```

Three other files also perform independent timestamp coercion (`src/vendors/csv_adapter.py:369`, `src/vendors/csv_adapter.py:258-263` in `parse_raw_rows`, `src/api/routes/inference.py:88-91`). There is no shared `parse_timestamp_utc()` utility.

---

### 2.3 Configuration Sprawl

#### Two config modules

- `src/config.py` — canonical, 670 lines, 90+ named constants, imported by all `src/` code
- `core/config.py` — 35 lines, 6 constants (`DATABASE_URL`, `MODEL_DIR`, `RESULTS_DIR`, `SEASONS`, `DETECTOR_SPIKE_MULTIPLIER` etc.) used only by the legacy stack

The constants do not always agree: `core/config.py:31` sets `DETECTOR_SPIKE_MULTIPLIER = 2.5` while `src/config.py:588-593` defines `VELOCITY_LIMITS` as a per-sensor dict.

#### Hardcoded `ANOMALY_THRESHOLD` default (0.8) in 7 locations

The value `0.8` appears as a magic number default for `anomaly_threshold` in:

| File | Line | Usage |
|------|------|-------|
| `src/dashboard/pages/2_anomalies.py` | 320 | `st.session_state.get("anomaly_threshold", 0.8)` |
| `src/dashboard/pages/3_thresholds.py` | 30 | `st.session_state.get('anomaly_threshold', 0.8)` |
| `src/dashboard/pages/99_Operator_Feedback.py` | 21 | `st.session_state.get("anomaly_threshold", 0.8)` |
| `src/dashboard/pages/1_Active_Monitoring/2_Climate_Data.py` | 48 | `st.session_state.get("anomaly_threshold", 0.8)` |
| `src/dashboard/components/charts.py` | 983 | `threshold: float = 0.8` (function default) |
| `src/dashboard/components/inference_results.py` | 251, 433 | `payload.get("threshold_used", 0.8)` |

All should reference `src.config.ANOMALY_THRESHOLD` (which is `0.8` in config, but the point is the default is defined once and changes propagate). If the threshold is tuned in config, the dashboard defaults silently disagree.

#### Hardcoded API base URL in 5 dashboard locations

| File | Line |
|------|------|
| `src/dashboard/pages/2_anomalies.py` | 48 |
| `src/dashboard/pages/5_upload_operations.py` | 10 |
| `src/dashboard/pages/99_Operator_Feedback.py` | 42, 65 (twice in same file) |
| `src/dashboard/pages/1_Active_Monitoring/3_Gas_Monitoring.py` | 22 |
| `src/dashboard/services/api_client.py` | 14 (`DEFAULT_API_URL`) |

The correct pattern already exists (`DashboardApiClient` in `api_client.py` reads `API_URL` from env with a fallback); dashboard pages should use that client instead of fetching `API_URL` locally.

#### `lru_cache` maxsize ignores `MAX_PIPELINE_CACHE_SIZE`

`src/config.py:205` defines `MAX_PIPELINE_CACHE_SIZE: int = 10`.
`src/api/routes/inference.py:33` uses `@lru_cache(maxsize=16)` — ignoring the config value.
`src/api/routes/inference_integrated.py:61` correctly reads `MAX_PIPELINE_CACHE_SIZE`. The two routes are therefore inconsistent: the `/api/inference` route caches up to 16 models; the integrated route caps at 10.

#### Streamlit cache TTLs are magic numbers

`@st.cache_data(ttl=60)`, `(ttl=120)`, `(ttl=300)` appear 13 times across `src/dashboard/components/charts.py` and `src/dashboard/services/anomalies_page_data.py` with no constants, no documentation of the reasoning, and no single place to tune them.

#### `.claude/settings.local.json` sprawl

`settings.local.json` contains 14 `allow` entries for specific one-off pytest invocations from past agent runs (e.g., `"Bash(./venv/bin/python -m pytest tests/models/test_anomaly_attributor.py -q --no-cov)"`). These are vestigial and pollute the permission config. The documented routing bug caused by conflicting settings files is directly reproducible in spirit here: if a future agent adds a global permission that conflicts with a local one, the behavior is undefined.

---

### 2.4 Test Coverage

#### Actual coverage: 33% (unit tests only)

When only unit tests are run (the only group that can actually execute):

```
TOTAL   4820 stmts   3139 missed   33.04% covered
FAIL Required test coverage of 90% not reached.
```

#### Why the full suite cannot run (immediate blockers)

| Error | Root cause | Affected test groups |
|-------|-----------|---------------------|
| `ValueError: Unable to configure formatter 'json'` | `python-json-logger` not installed; `src/logging/config.py:35` references `pythonjsonlogger.jsonlogger.JsonFormatter` | ALL 12 API test files |
| `ModuleNotFoundError: No module named 'plotly'` | `plotly` not in requirements | `tests/unit/test_chart_datetime_filter.py` |
| `ModuleNotFoundError: No module named 'streamlit'` | `streamlit` not in requirements | `tests/unit/test_color_manager.py` |
| `ModuleNotFoundError: No module named 'aiosqlite'` | `aiosqlite` not in requirements | `tests/models/test_training_pipeline.py` |

The `python-json-logger` failure is the highest-priority because it blocks all 12 API test files from loading. The actual `logging.config.dictConfig()` call happens at module import time inside `src/api/main.py:27` without defensive fallback, causing every test that imports the app to immediately fail.

#### Modules with 0% coverage in the live `src/` stack

The following modules in `src/models/` have **zero tests** (0% coverage):

`catastrophic_forgetting.py`, `checkpoint_manager.py`, `confidence_scorer.py`, `customer_config_store.py`, `cusum_drift_detector.py`, `diagnosis_rules.py`, `drift_detector.py`, `dropout_detector.py`, `embedding_utils.py`, `ensemble_detector.py`, `feedback_collector.py`, `feedback_validator.py`, `model_evaluator.py`, `model_registry.py`, `noise_detector.py`, `online_learner.py`, `pattern_detector.py`, `pattern_scores.py`, `psychrometric_validator.py`, `recommendation_engine.py`, `replay_buffer.py`, `retrain_evaluator.py`, `retrain_executor.py`, `retraining_orchestrator.py`, `root_cause_analyzer.py`, `transfer_learner.py`

The entire `src/monitoring/` package has 0% coverage.

Tests that exist but test legacy code instead of live code:
- `tests/unit/test_csv_parser.py` — tests `parsers.csv_parser.CSVParser` (legacy)
- `tests/unit/test_detectors.py` — tests `detectors.spike`, `detectors.stuck`, `detectors.drift` (legacy)
- `tests/unit/test_advanced_detectors.py` — tests `detectors.multivariate` (legacy)
- `tests/unit/test_voting.py` — tests `engines.voting` (legacy)
- `tests/integration/test_detect_api.py` — tests `api.main` (legacy)

#### Two failing unit tests

`tests/unit/test_csv_parser.py::test_parse_csv_comma_delimiter` and `::test_validate_insufficient_rows` fail at runtime with `ValueError`. These test the legacy parser but are included in the active `tests/` tree, so they reduce the pass rate of every `pytest` run.

---

### 2.5 Error Handling Consistency

#### Silent exception swallowing (ordered by severity)

**Critical — data loss risk:**
- `src/api/routes/inference.py:90-91`: `except Exception: pass` inside the timestamp-matching loop. When a timestamp string cannot be parsed, the exception is silently ignored, no log is written, and the anomaly score for that timestamp is never written to the database. The anomaly *is* returned to the caller but is not persisted.

**High — metrics reliability:**
- `src/monitoring/metrics_persistence.py:40`: `except Exception:` with no body shown; if the metrics snapshot write fails, the monitoring page will show stale data with no indication.
- `src/monitoring/metrics_persistence.py:67`: same pattern on the second persistence path.

**Medium — operational visibility:**
- `src/dashboard/services/http_utils.py:17-18`: `except Exception: return None` — any network or parse error from the API silently returns `None` to the caller; the dashboard page then typically renders nothing with no error message.
- `src/api/services/cold_run_upload.py:108`: bare `except Exception:` around sequence building.
- `src/api/services/comprehensive_pipeline.py:398`, `423`, `764`: three separate silent catches.

**Low — reasonable defensive patterns:**
- `src/models/pot_threshold.py:95-97`: logs a warning before using fallback — acceptable.
- `src/db/session.py:49-51`: rolls back and re-raises — correct.
- `src/api/routes/feedback_integrated.py:151-152`: logs at `exception` level before continuing — acceptable.

#### Inconsistent use of `logger` in route files

`src/api/routes/inference.py` has no module-level logger and uses no logging calls in the hot inference path (lines 40-141). Errors are converted directly to `HTTPException`. This means failed timestamp matching (line 90) is invisible in the log stream. Compare to `src/api/routes/learning.py:39` which defines `logger = logging.getLogger(__name__)` and uses it throughout.

---

### 2.6 Naming and Readability

#### Inconsistent parameter naming: `customer_id` vs `tenant_id`

The live API and database use `tenant_id` consistently. However:
- `src/models/training_pipeline.py:49`: `train_customer_model(self, customer_id: str)`
- `src/db/models.py:247`: `customer_id: Mapped[str]` on `OperatorFeedback` (while `Incident` at line 154 uses `tenant_id`)
- `src/api/routes/learning.py:228`: `registry.clear_models(tenant_id)` but the API parameter is also called `tenant_id`

The `OperatorFeedback` table column (`customer_id`) is inconsistent with every other multi-tenant table in the same file. This is a latent FK/query bug surface: a query joining `Incident.tenant_id` with `OperatorFeedback.customer_id` requires an explicit alias.

#### `src/dashboard/pages/2_anomalies.py:54-68` — fault type list duplicated

```python
_FAULT_TYPES = ["spike_fault", "calibration_drift", "stuck_fault", "bias_offset", "accuracy_decline"]
_FAULT_LABELS = {"spike_fault": "Spike fault", ...}
```

This list is also present in other dashboard pages (verified in `99_Operator_Feedback.py`). There is no shared constants module for dashboard-level domain strings.

#### `src/models/training_pipeline.py:29` — hardcoded glob path

```python
checkpoints = sorted(glob.glob("models/checkpoints/epoch_*.pt"))
```

This assumes the process working directory is the project root. `src/config.py:94-98` defines `PATHS["checkpoint_dir"]` precisely to avoid this, but `training_pipeline.py` does not import it.

#### `src/models/training_pipeline.py:93` — ignores `ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD`

```python
if not active_model or comp['improvement_percent'] > 0:
```

`src/config.py:523` defines `ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD: float = 0.05` (5%), but the pipeline registers any model with positive improvement, however tiny. This means a 0.001% improvement triggers a model deployment.

---

### 2.7 Dead Code

#### Legacy root-level packages (entire directories)

Safe to remove after confirming no deployment references them:

| Path | Lines of code | Still imported by |
|------|--------------|------------------|
| `api/` | ~120 | `tests/integration/test_detect_api.py`, `test_patterns_api.py`, `test_analyze_api.py`, `test_train_api.py` |
| `core/` | ~35 | `database/session.py`, `storage/`, `engines/` |
| `database/` | ~100 | `api/routes/*.py`, integration tests |
| `detectors/` | ~600 | 5 test files in `tests/unit/` |
| `engines/` | ~800 | `tests/unit/test_voting.py`, `test_challenger_m1_2.py`, `test_pass1_learn.py` |
| `parsers/` | ~80 | `tests/unit/test_csv_parser.py` |
| `storage/` | ~60 | only `core/config.py` |

#### Stale test files in project root

`test_debug.py` (imports `from detectors.physics_validator`), `test_global.db`, `test_phase4_analyze.db`, `test_phase4_detect.db`, `test_phase4_patterns.db`, `greenhouse.db.bak`.

#### `notebooks/` directory

Not reviewed in detail, but notebooks are not part of the test suite. Confirm whether any are load-bearing (e.g., referenced in CI or by team members) before removing.

---

### 2.8 Dependency Hygiene

#### Three conflicting dependency files

| File | Key packages | Status |
|------|-------------|--------|
| `requirements.txt` | fastapi, uvicorn, sqlalchemy, pydantic, numpy, pandas, python-dotenv, pytest, pytest-asyncio (9 packages) | Missing ~15 runtime deps; no torch, no streamlit, no plotly, no aiosqlite |
| `pyproject.toml` | fastapi, uvicorn, sqlalchemy, pydantic, structlog, python-json-logger, numpy (no pandas, no python-dotenv) | Disagrees with requirements.txt; lists `psycopg2-binary` which is PostgreSQL-only; pytest version pinned to 7.4.3 |
| `requirements-dev.txt` | locust>=2.20, pytest-asyncio>=0.23 | Contains only 2 packages — not a complete dev requirements spec |

The result: `python3 -m pip install -r requirements.txt` installs an environment that cannot run the test suite, cannot import the logging config, and cannot start the dashboard.

The version pins in `pyproject.toml` are outdated: `fastapi==0.104.1` (the installed version is `0.136.3`), `numpy==1.26.2` (installed: `3.0.3`/pandas), `pydantic==2.5.2` vs Pydantic v2.x in production.

#### `pytest.mark.async_io` typo

`tests/unit/test_priva_client.py:83`:
```python
@pytest.mark.async_io  # ← should be asyncio
```
This is an `Unknown pytest.mark` warning on every test run. The test runs anyway (because `asyncio_mode = auto`), but the mark does nothing and is misleading.

---

## 3. Proposed Sequencing

### Phase A — Stop the bleeding (1–2 days)

**Why first:** Nothing else matters if the test suite cannot run. These are mechanical fixes with no architectural decisions.

1. **Fix missing packages**: Install `python-json-logger`, `plotly`, `streamlit`, `aiosqlite`, `torch`, `scikit-learn` and write a single authoritative `requirements.txt` that reflects what's actually installed. Delete `pyproject.toml` or reconcile it. Delete `requirements-dev.txt` and fold its contents into a `requirements-dev.txt` that extends `requirements.txt`.

2. **Fix the logging config fault**: `src/logging/config.py:30-65` is called at module import time in `src/api/main.py:27`. The `try/except ImportError` on line 25-29 was intended as a fallback but doesn't catch the `ValueError` from `dictConfig()`. Add `except (ImportError, ValueError)` or move the `setup_logging()` call to inside the lifespan function.

3. **Fix the two failing unit tests** in `tests/unit/test_csv_parser.py` — either fix the legacy parser they test, or gate those tests with `pytest.mark.skip` and a comment.

4. **Fix the `pytest.mark.async_io` typo** in `tests/unit/test_priva_client.py:83`.

### Phase B — Legacy stack tombstoning (2–3 days)

**Why second:** Once tests run cleanly, confirm which test files reference the legacy stack, then move them or remove them. This prevents the legacy code from continuing to receive coverage accounting credit, and removes the class-name collision risk.

1. Audit all imports of `api.*`, `core.*`, `database.*`, `detectors.*`, `engines.*`, `parsers.*`, `storage.*` across `tests/`.
2. For each test file that tests a legacy module and has a live equivalent already tested: delete the legacy test.
3. For each test file that tests a legacy module with no live equivalent: port the test to the live module or mark it `skip`.
4. Delete `api/`, `core/`, `database/`, `detectors/`, `engines/`, `parsers/`, `storage/`.
5. Delete root-level stale databases and `test_debug.py`.
6. Remove stale entries from `.claude/settings.local.json`.

### Phase C — Configuration centralization (1 day)

**Why third:** After the legacy stack is gone, the remaining config sprawl is entirely within `src/`. Fixing it now means Phase D (coverage) tests against a single source of truth.

1. Create `src/dashboard/constants.py` with:
   - `FAULT_TYPES: List[str]` and `FAULT_LABELS: Dict[str, str]` (extracted from `2_anomalies.py:54-68`)
   - `DEFAULT_ANOMALY_THRESHOLD` imported from `src.config` — dashboard pages reference this instead of `0.8`
   - Cache TTL constants: `CHART_CACHE_TTL_SHORT = 60`, `CHART_CACHE_TTL_MEDIUM = 120`, `CHART_CACHE_TTL_LONG = 300`

2. Consolidate API URL: remove the 5 duplicated `API_URL = os.environ.get(...)` lines; have all pages use `DashboardApiClient` directly or import a single `_API_URL` from `src.dashboard.constants`.

3. Fix `src/api/routes/inference.py:33`: change `@lru_cache(maxsize=16)` to `@lru_cache(maxsize=MAX_PIPELINE_CACHE_SIZE)`.

4. Fix `src/models/training_pipeline.py:29`: replace the `glob.glob("models/checkpoints/epoch_*.pt")` with `glob.glob(f"{PATHS['checkpoint_dir']}epoch_*.pt")`.

5. Fix `src/models/training_pipeline.py:93`: replace `comp['improvement_percent'] > 0` with `comp['improvement_percent'] > ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD * 100`.

### Phase D — Error handling hardening (1 day)

**Why fourth:** With clean tests and consolidated config, these fixes are small and targeted. Their correctness is testable.

1. `src/api/routes/inference.py:88-91`: log a warning for unparseable timestamps and continue, not `pass`. Add `import logging; logger = logging.getLogger(__name__)` at the top of the file.

2. `src/monitoring/metrics_persistence.py:40` and `67`: log at `ERROR` level before continuing, not silent swallow.

3. `src/dashboard/services/http_utils.py:17-18`: log at `WARNING` before returning `None`; callers can still handle `None` gracefully but the failure becomes visible in the log.

### Phase E — Coverage push to 90% (3–5 days)

**Why last:** Coverage work requires stable imports (Phase A), correct module boundaries (Phase B), and correct config (Phase C) as prerequisites. Writing tests against a broken setup produces fragile fixtures.

Priority order within Phase E based on risk:
1. `src/models/ensemble_detector.py` — used in production via `comprehensive_pipeline.py`
2. `src/models/online_learner.py` — used in production via `learning.py`
3. `src/models/model_registry.py` — used by inference route on every request
4. `src/models/drift_detector.py` — used in production via learning route
5. `src/monitoring/` — currently 0% but critical for ops visibility
6. Remaining 0% modules in `src/models/`

---

## 4. Overlap Flags for Planned Workstreams

### Workstream 1: Caching layer around anomaly detection results and filtered views

**Relevant findings:**
- `src/dashboard/components/charts.py` already has 8 `@st.cache_data` decorators with TTLs hardcoded to 60, 120 seconds. `src/dashboard/services/anomalies_page_data.py` adds 4 more. If the new caching layer targets the same data (sensor readings windows, filtered anomaly views), it may conflict with or duplicate these decorators.
- `src/api/routes/inference.py:33` uses `@lru_cache(maxsize=16)` for the model, but the inference results themselves are not cached server-side. The upload-result cache (`src/dashboard/services/upload_results_store.py`) is disk-based (JSON files in `data/upload_inference_cache/`).

**Recommendation:** Before adding a new caching layer, consolidate the existing TTL magic numbers into constants (Phase C above). Then decide whether the new layer should be server-side (e.g., Redis, or an in-memory dict keyed by `(tenant_id, greenhouse_id, start, end)`), or an extension of the existing `@st.cache_data` TTL approach. If server-side: the `_manager` global mutable state issue in `learning.py` (Section 2.1) will also need addressing before you can safely cache inference results across workers.

### Workstream 2: UI robustness improvements to the dashboard

**Relevant findings:**
- `src/dashboard/services/http_utils.py:17-18` silently returns `None` on any HTTP error. Callers then typically render nothing. Fixing this (Phase D) is a prerequisite for meaningful UI robustness: you cannot distinguish "API returned empty data" from "API is unreachable" with the current pattern.
- The duplicate `API_URL = os.environ.get(...)` definitions (Section 2.3) mean that if the URL changes in one page it doesn't change in others. UI robustness work on pages 5 or 99 won't benefit the anomaly page unless both are fixed.
- `src/dashboard/pages/2_anomalies.py` is 535 lines — above the 300-line limit and has 3 distinct responsibilities: upload section, date range filtering, and anomaly table rendering. Splitting it before adding robustness improvements will reduce merge conflicts and make robustness changes easier to test.

### Workstream 3: Changes to anomaly detection training/calibration

**Relevant findings:**
- `src/models/training_pipeline.py:93` registers any model with positive improvement (`> 0`), ignoring `ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD` (Section 2.6). Any calibration work that relies on a minimum improvement gate will not actually be enforced until this is fixed (Phase C).
- `src/models/training_pipeline.py:58`: minimum training data is hardcoded as `100` rows. `src/config.py:519` defines `ACTIVE_LEARNING_MIN_TRAINING_SAMPLES: int = 10`. These are different thresholds with different semantics (rows vs. sequences), but the discrepancy should be resolved before calibration work adds more thresholds.
- `src/models/training_pipeline.py:63`: `contamination=0.05` is hardcoded. If calibration work tunes the IsolationForest contamination parameter, this line will be missed. `src/config.py:639` already defines `BOOTSTRAP_IF_CONTAMINATION: float = 0.04` for a different context, but there is no constant for the training-pipeline contamination.
- The `ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD` config key is at 0.00% test coverage. Any calibration change to this constant cannot be tested until Phase E.

---

## 5. Non-findings — Areas Reviewed and Found Acceptable

The following areas were specifically checked and did not produce actionable findings:

- **`src/vendors/csv_adapter.py` column-mapping logic** (`_MATCH_RULES`, `_smart_column_map`) — the keyword-score approach is well-structured and handles the documented edge cases (European decimals, BOM, semicolon delimiters). The fallback ordering is deliberate (outdoor/plant sensors are lower-priority than indoor sensors). No duplication within the file.

- **`src/vendors/base.py` exception hierarchy** — `DataSourceConnectionError`, `DataReadError`, `NormalizationError`, `DataValidationError` are distinct, well-named, and consistently used in `csv_adapter.py`. No silent swallowing within the adapter itself (the bare `except Exception: pass` at line 266 is actually inside `parse_raw_rows`, not in the core adapter path).

- **`src/db/models.py` table design** — multi-tenant indexes (`ix_incidents_tenant_detected`, `ix_learning_logs_tenant_created`) are present and correctly scoped. The `UniqueConstraint` on `(tenant_id, greenhouse_id, timestamp)` in `SensorReading` and `RawReading` prevents duplicate ingestion. `EmbeddingType` abstraction (in `src/db/embedding_type.py`) cleanly handles the SQLite vs. PostgreSQL storage difference.

- **`src/models/lstm_vae.py`** — 97.83% coverage, correctly reads `INPUT_DIM`, `HIDDEN_DIMS`, `LATENT_DIM`, `DROPOUT_RATE`, `BETA` from `src/config.py`. No magic numbers in the model definition.

- **`src/api/main.py` middleware and exception handlers** — request ID generation, timing, CORS origins, and the three exception handlers are correctly structured. The `asynccontextmanager` lifespan properly disposes of the SQLAlchemy pool on shutdown.

- **Alembic migration chain** — 13 migrations (`001` through `013`) are present and appear sequential. The `alembic.ini` and `alembic/env.py` reference `src.db.models.Base` (the live models), not the legacy `database.models.Base`. Migration history is consistent with the current `src/db/models.py` schema.

- **`src/vendors/priva_client.py`** — 100% test coverage. No hardcoded credentials. Timeout values are function parameters with documented defaults.

- **`src/models/physics_validator.py`** and `src/models/gas_anomaly_detector.py`** — both at 100% coverage.

- **CORS configuration** in `src/api/main.py:86-100` — correctly restricts origins to `localhost` variants; does not use `allow_origins=["*"]` unlike the legacy `api/main.py`.
