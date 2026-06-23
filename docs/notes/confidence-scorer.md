# Confidence Scorer — Notes

**Source:** confidence_scorer_review.md

## NOTE-CS-001: confidence_level depends on sorted CONFIDENCE_LEVEL_BOUNDS

No runtime validation that bounds are sorted descending. Add assert or sort on init.

---

## NOTE-CS-002: bare dict type annotations in config

Pre-existing pattern in config.py. Consider `Dict[str, float]` with TypedDict for signal weights.

---

## NOTE-CS-003: SIGNAL_NAMES could be Tuple[str, ...]

Minor typing improvement in confidence_scorer.py.
