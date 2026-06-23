# Anomaly Attributor — Notes

**Source:** anomaly_attributor_review.md

## NOTE-AA-001: ADR for deterministic decode(mu) scoring

Attribution uses `encode → decode(mu)` not stochastic `forward`. Thresholds calibrated on detector forward path may not transfer. Deserves short ADR if attribution becomes production API contract.

**Path:** `docs/architecture/ADR-002-attribution-deterministic-scoring.md`

---

## NOTE-AA-002: affected_sensors inconsistent in fallback

Fallback sets uniform 0.25 contributions; all exceed threshold so all sensors listed while `is_fallback=True`. Consider empty `affected_sensors` when fallback fires.

---

## NOTE-AA-003: File length exceeds 300-line cap

360 lines. Move `AttributionStrength` enum to separate file.

---

## NOTE-AA-004: Entropy epsilon uses NUMERICAL_EPSILON (1e-6) vs spec 1e-10

Negligible mathematical difference. Reusing shared constant is correct.
