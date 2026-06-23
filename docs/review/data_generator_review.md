# Code Review: Synthetic Data Generator

## Summary
PASS

## Critical Issues (block merge)
None.

## Minor Issues (fix soon)
None.

## Observations (optional improvements)
- **Global Random State:** The `_set_seeds` method (lines 56-59) uses `np.random.seed` and `torch.manual_seed`. This modifies the global random state of the entire Python process. While this ensures reproducibility for the generator itself, it might cause side effects in other parts of a larger application that rely on the global random state. 
  - *Recommendation:* Consider using a local random generator for NumPy, e.g., `self.rng = np.random.default_rng(seed)`, and using it for all random operations within the class.
- **Edge Case Testing:** While the current tests cover small dimensions (e.g., `num_samples=10`), adding explicit tests for the absolute minimum dimensions (`num_samples=1`, `seq_len=1`) would further increase confidence in the robustness of the vectorized operations.

## Review Checklist Results
- **Security:** N/A (No user input or external I/O).
- **Correctness:**
    - **Physical Bounds:** Correctly enforced using `np.clip` in both generation and correlation phases.
    - **Normalization Leakage:** Verified. Uses fixed `self.bounds` rather than data-driven min/max.
    - **NaN Propagation:** Verified. Explicit assertions and safe arithmetic used.
    - **Reproducibility:** Verified. Both NumPy and Torch seeds are set.
- **Performance:** Vectorized operations used throughout; no obvious bottlenecks for typical use cases.
- **Standards:** Excellent docstrings and type hinting.
- **Test Quality:** High. Covers all public methods, checks for shape, dtype, bounds, and statistical properties (correlation, variance, mean).
