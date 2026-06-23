# Automated Testing Skill
skill_id: "automated-testing"
name: "Automated Testing"
description: "Capability to implement robust unit and integration tests and verify coverage gates"

applies_to:
  - "qa-engineer"
  - "ml-engineer"

checklist:
  unit_tests:
    - "Test files are located under tests/unit/ and start with test_"
    - "Verify input and output tensor shapes on all key model layers"
    - "Test mathematical stability (check that loss is positive, non-NaN, non-Inf)"
    - "Test model reparameterization trick for correct stochastic behavior"
    - "Test model checkpoint saving and loading metadata (epoch, loss, accuracy, config)"
    - "Test reproducibility with fixed random seed constraints"

  integration_tests:
    - "Integration test files are located under tests/integration/"
    - "Verify end-to-end model training runs and successfully reduces loss over epochs"
    - "Verify early stopping triggers correctly when loss plateaus"
    - "Verify data generation produces correct shape, bounds [0, 1], and correlations"

  coverage:
    - "Coverage configuration is maintained in .coveragerc"
    - "pytest.ini defines term-missing and cov-fail-under=80"
    - "Critical paths (model forward, loss calculation, training loop) target >90% coverage"
    - "Ensure coverage stays above the 80.0% global gate"

output_format:
  - "✅ Test Passed: [test name]"
  - "❌ Test Failed: [issue details]"
  - "📊 Coverage Status: [X]% (Target: >80%)"
