# CSV Adapter Generic Normalization -- Design Document

**Status:** Approved
**Author:** Architect
**Date:** 2026-06-19
**Scope:** `src/vendors/csv_adapter.py` (single-file refactor, no other files changed)

## 1. Problem Statement

The `normalize()` method in `CSVAdapter` contains hardcoded column names, sensor lists, and default values scattered throughout the method body. This violates the project's "config values in one place" rule and makes the adapter fragile when sensor channels change.

### Specific Hardcoded Strings to Eliminate

| Location | Hardcoded value | Problem |
|---|---|---|
| `normalize()` line 221 | `for col in ["temperature", "humidity", "co2", "light"]` | Repeated literal sensor list |
| `normalize()` line 233 | `normalized.dropna(subset=["temperature", "humidity"])` | Hardcoded required sensor list |
| `normalize()` line 198 | `required = ["timestamp", "temperature", "humidity"]` | Hardcoded required column list |
| `normalize()` line 234 | `for col, default in _SENSOR_DEFAULTS.items()` | Only fills co2/light, not generically all optional sensors |
| `_smart_column_map` line 96 | `canonical_names = {"timestamp", "temperature", "humidity", "co2", "light"}` | Duplicates the canonical set |

### NaN Handling Bug

The current order is:
1. Drop rows where temperature OR humidity are NaN
2. Fill co2/light NaN with defaults

This is correct for the current sensor set but not generic. If a new optional sensor were added and it happened to share a default, the logic would silently skip the fill for that sensor because only `_SENSOR_DEFAULTS` keys are iterated, not all optional sensor columns.

More critically, the required/optional partition is implicit (hardcoded in two separate places) rather than declared once.

## 2. Design

### 2.1 Module-Level Constants

Add these constants at the top of `csv_adapter.py`, below the existing imports and above `_MATCH_RULES`:

```
TIMESTAMP_COL: str = "timestamp"

REQUIRED_SENSOR_COLS: Tuple[str, ...] = ("temperature", "humidity")

OPTIONAL_SENSOR_COLS: Tuple[str, ...] = ("co2", "light")

SENSOR_DEFAULTS: Dict[str, float] = {
    "co2": 400.0,
    "light": 0.0,
}

SENSOR_COLS: Tuple[str, ...] = REQUIRED_SENSOR_COLS + OPTIONAL_SENSOR_COLS

CANONICAL_COLS: Tuple[str, ...] = (TIMESTAMP_COL,) + SENSOR_COLS
```

**Design decisions:**

- Use `Tuple[str, ...]` (immutable) rather than `List[str]` for constants that must not be mutated at runtime.
- `SENSOR_COLS` is derived from `REQUIRED_SENSOR_COLS + OPTIONAL_SENSOR_COLS` so the two always stay in sync.
- `CANONICAL_COLS` is derived from `TIMESTAMP_COL` + `SENSOR_COLS` so the full column ordering is defined in exactly one place.
- `SENSOR_DEFAULTS` replaces the existing `_SENSOR_DEFAULTS`. Every key in `SENSOR_DEFAULTS` must be a member of `OPTIONAL_SENSOR_COLS`. An optional sensor without a default would cause rows with NaN in that column to be dropped (same behavior as required columns). With the current sensors, every optional has a default, so no rows are dropped for optional-sensor NaN.
- These constants are internal to `csv_adapter.py`. They are NOT imported from `src/config.py` because they describe the CSV normalization contract (how raw vendor data maps to the canonical schema), not the ML model's hyperparameters. The `SENSOR_NAMES` list in `config.py` serves a different purpose (model layer sensor identification). If a future change requires sharing these lists, that can be a separate unification step.

### 2.2 Rename `_SENSOR_DEFAULTS` to `SENSOR_DEFAULTS`

The existing private `_SENSOR_DEFAULTS` dict is promoted to a public module-level constant `SENSOR_DEFAULTS` since it is part of the adapter's public contract (callers may need to inspect what defaults are applied). The values remain identical.

### 2.3 Update `_smart_column_map`

Replace the hardcoded `canonical_names` set:

```python
# Before
canonical_names = {"timestamp", "temperature", "humidity", "co2", "light"}

# After
canonical_names = set(CANONICAL_COLS)
```

### 2.4 Rewrite `normalize()` Using Constants

The method body must reference only the module-level constants. No string literal should appear in the method body except for config dictionary keys (`"column_mapping"`, `"timestamp"`) and error message text.

Wait -- `"timestamp"` is used as a config key AND as a column name. The column-name usage must go through `TIMESTAMP_COL`. The string `"timestamp"` appearing as a dict key in `self.config.get("column_mapping")` is fine because that is a config key, not a sensor/column name.

#### Step-by-step normalize() logic (pseudocode using constants):

```
Step 1: Apply explicit column_mapping from config (unchanged)
Step 2: Auto-detect via _smart_column_map (unchanged)
Step 3: Fill missing optional sensor columns with defaults
  - for col in OPTIONAL_SENSOR_COLS:
      if col not in data.columns and col in SENSOR_DEFAULTS:
          data[col] = SENSOR_DEFAULTS[col]
Step 4: Check required columns
  - required = (TIMESTAMP_COL,) + REQUIRED_SENSOR_COLS
  - missing = [c for c in required if c not in data.columns]
  - raise NormalizationError if missing
Step 5: Type coercion
  - timestamp: pd.to_datetime, tz_localize/convert (reference TIMESTAMP_COL)
  - sensors: for col in SENSOR_COLS: data[col].astype("float64")
Step 6: NaN handling (NEW ORDER -- fill optionals first, then drop required-NaN)
  a. Fill NaN in optional sensor columns with defaults:
     for col in OPTIONAL_SENSOR_COLS:
         if col in SENSOR_DEFAULTS:
             normalized[col] = normalized[col].fillna(SENSOR_DEFAULTS[col])
  b. Drop rows where any required sensor column is still NaN:
     normalized = normalized.dropna(subset=list(REQUIRED_SENSOR_COLS))
Step 7: Select only canonical columns in canonical order:
  - normalized = normalized[list(CANONICAL_COLS)]
  - return normalized
```

**Key behavior changes:**

1. **NaN fill order is reversed** (fill optionals BEFORE dropping required-NaN rows). This is semantically cleaner: partial data is recovered before pruning. With the current data, the observable behavior is identical because `dropna(subset=["temperature", "humidity"])` and the fill of co2/light do not interact. But the new order is correct in the general case where an optional sensor with a default might also be NaN in the same row as a required sensor -- the fill should happen first so the row is complete if the required sensor is present.

2. **Sensor coercion loop** iterates `SENSOR_COLS` instead of a hardcoded list. This means if `SENSOR_COLS` is extended in the future, the coercion automatically covers the new column.

3. **Required column check** now includes only `TIMESTAMP_COL` + `REQUIRED_SENSOR_COLS`, not the full `CANONICAL_COLS`. Optional sensors that are genuinely absent from the source data and have no default will be caught by the missing-column check only if they appear in `REQUIRED_SENSOR_COLS`.

### 2.5 `_MATCH_RULES` -- No Structural Change

The `_MATCH_RULES` list remains as-is. Its canonical target strings (`"timestamp"`, `"temperature"`, etc.) are data declarations, not logic-body hardcoding. They are the scoring rules themselves and must enumerate their targets explicitly. Extracting them into constants would add indirection without reducing duplication (each rule references a specific canonical name exactly once in its declaration).

If `CANONICAL_COLS` is extended in the future, new match rules must be added to `_MATCH_RULES` as well -- this is an inherent part of the adapter's vendor-mapping responsibility.

### 2.6 Existing `_detect_csv_properties` -- No Change

This function detects delimiter, encoding, and decimal separator. It has no hardcoded sensor or column names. No change needed.

## 3. Constant Invariants

These invariants must hold at module load time. They are enforced by construction (tuple concatenation), not runtime assertions:

1. `set(SENSOR_DEFAULTS.keys()).issubset(set(OPTIONAL_SENSOR_COLS))` -- every default key must be an optional sensor.
2. `set(REQUIRED_SENSOR_COLS).isdisjoint(set(OPTIONAL_SENSOR_COLS))` -- a sensor cannot be both required and optional.
3. `CANONICAL_COLS == (TIMESTAMP_COL,) + REQUIRED_SENSOR_COLS + OPTIONAL_SENSOR_COLS` -- ordering is timestamp first, then required, then optional.
4. `len(CANONICAL_COLS) == len(set(CANONICAL_COLS))` -- no duplicate column names.

## 4. Test Compatibility

The existing test suite (`tests/unit/test_csv_adapter.py`) must pass without modification. Key assertions to verify:

| Test | Assertion | Why it still holds |
|---|---|---|
| `test_normalize_success` | `list(normalized_df.columns) == ["timestamp", "temperature", "humidity", "co2", "light"]` | This is exactly `list(CANONICAL_COLS)` |
| `test_normalize_drops_nan_sensor_rows` | `len(result) == 2` (NaN temperature row dropped) | `REQUIRED_SENSOR_COLS` includes `"temperature"`, so `dropna(subset=REQUIRED_SENSOR_COLS)` still drops that row |
| `test_normalize_missing_columns_after_mapping` | `NormalizationError("missing required columns")` | The required check uses `(TIMESTAMP_COL,) + REQUIRED_SENSOR_COLS`; with only timestamp mapped, temperature and humidity are still missing |
| `test_normalize_invalid_numeric_types_raises_normalization_error` | `NormalizationError("Failed to coerce column 'temperature' to float64")` | The coercion loop iterates `SENSOR_COLS` which starts with temperature |
| `test_get_data_lifecycle_integration` | `list(df.columns) == ["timestamp", "temperature", "humidity", "co2", "light"]` | Same as `list(CANONICAL_COLS)` |

## 5. What NOT to Change

- **`src/vendors/base.py`**: The `validate_canonical_schema` method has its own hardcoded column list (`required_cols = ["timestamp", "temperature", "humidity", "co2", "light"]`). This is the base class validation contract. Do NOT change it in this refactor -- it would require updating all concrete adapters. A separate unification task can extract a shared canonical schema definition later.
- **`src/config.py`**: Do not move the CSV adapter constants there. The adapter constants describe the normalization contract, not ML hyperparameters.
- **`_MATCH_RULES`**: The target strings in match rules are declarations, not logic-body hardcoding.
- **Test files**: No test logic changes.
- **Public method signatures**: `connect()`, `disconnect()`, `read()`, `normalize()`, `validate()`, `name` -- all unchanged.

## 6. Implementation Checklist

For the implementing agent (backend-engineer or tech-lead):

1. Add `Tuple` to the `typing` imports.
2. Define `TIMESTAMP_COL`, `REQUIRED_SENSOR_COLS`, `OPTIONAL_SENSOR_COLS`, `SENSOR_DEFAULTS`, `SENSOR_COLS`, `CANONICAL_COLS` as module-level constants after the imports and before `_MATCH_RULES`.
3. Remove the old `_SENSOR_DEFAULTS` dict (replaced by `SENSOR_DEFAULTS`).
4. In `_smart_column_map`: replace `canonical_names = {"timestamp", ...}` with `canonical_names = set(CANONICAL_COLS)`.
5. In `normalize()`:
   - Step 3: iterate `OPTIONAL_SENSOR_COLS` (with `SENSOR_DEFAULTS` lookup) instead of `_SENSOR_DEFAULTS.items()`.
   - Step 4: build required from `(TIMESTAMP_COL,) + REQUIRED_SENSOR_COLS`.
   - Step 5 timestamp: use `TIMESTAMP_COL` instead of the string `"timestamp"`.
   - Step 5 sensors: iterate `SENSOR_COLS` instead of the hardcoded list.
   - Step 6: fill optionals FIRST (`OPTIONAL_SENSOR_COLS` + `SENSOR_DEFAULTS`), THEN drop required-NaN rows (`REQUIRED_SENSOR_COLS`).
6. Verify: zero hardcoded sensor/column name strings remain in `normalize()` or `_smart_column_map` method bodies.
7. Run `pytest tests/unit/test_csv_adapter.py -v` -- all must pass unchanged.
8. Run full suite to check for regressions.

## 7. Future Considerations

- **Shared canonical schema**: If `base.py:validate_canonical_schema` and `csv_adapter.py` constants drift apart, a shared `CANONICAL_SCHEMA` definition (possibly in `src/vendors/schema.py` or `src/config.py`) would be warranted. Not in scope for this refactor.
- **Per-vendor sensor subsets**: Some vendors may only provide a subset of sensors. The current design handles this (optional sensors get defaults). If a vendor provides sensors NOT in `CANONICAL_COLS`, those extra columns are silently dropped during the final column selection step.
- **Config-driven match rules**: `_MATCH_RULES` could be made configurable per-instance (passed via `config` dict). Not needed today but the design does not prevent it.
