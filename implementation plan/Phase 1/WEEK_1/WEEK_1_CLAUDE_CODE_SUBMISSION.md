# Week 1 Tasks - Claude Code Terminal Submission
## How to actually submit tasks to your orchestrator

---

## 📝 **The Simple Workflow**

```bash
# Terminal 1: Navigate to project
cd ~/Projects/ai-agronomist

# Start Claude Code session
claude

# You'll see:
❯ 
```

At the `❯` prompt, **paste your task as plain text** (no YAML, no @mentions, no config files).

---

## 🚀 **WEEK 1 PHASE 1 - Submit This:**

Copy and paste this entire block at the `❯` prompt:

```
Build Week 1 Phase 1 foundation - LSTM-VAE, data generation, Docker setup.

Phase 1 (parallel):

- architect: Design the LSTM-VAE anomaly detection system.
  Output: docs/architecture/lstm_vae_design.md
  
  Design document should specify:
  - LSTM-VAE architecture (2-layer encoder/decoder, 8D latent)
  - Loss function (reconstruction MSE + KL divergence with beta=0.1)
  - Hyperparameter justification (input_dim=4, hidden_dims=[64,32], seq_len=20)
  - Data assumptions and validation approach
  - Phase 1.5 extensibility considerations
  - Risks and mitigations

- ml-engineer: Implement LSTM-VAE model class.
  Output: src/models/lstm_vae.py
  
  Implement class LSTMVAE(nn.Module) with methods:
  - __init__(input_dim=4, hidden_dims=[64,32], latent_dim=8, seq_len=20, beta=0.1, dropout=0.2)
  - encode(x) -> (mu, log_var)
  - decode(z) -> x_recon
  - reparameterize(mu, log_var) -> z
  - forward(x) -> (x_recon, mu, log_var, z)
  - loss_function(recon_x, x, mu, log_var) -> loss
  - save_checkpoint(path, epoch, loss, accuracy)
  - load_checkpoint(path) -> metadata_dict
  
  Requirements: type hints, docstrings, no hardcoded magic numbers, device-agnostic

- ml-engineer: Implement synthetic data generator.
  Output: src/models/data_generator.py
  
  Implement class SyntheticDataGenerator with methods:
  - __init__(num_sequences=1000, seq_len=20, input_dim=4, random_seed=42)
  - generate_normal_data() -> (1000, 20, 4) numpy array
  - normalize_data(data) -> (normalized, params)
  - add_temporal_correlations(data) -> data
  
  Requirements: realistic greenhouse ranges, normalized to [0,1], reproducible with fixed seed, no NaN/Inf, temporal correlations

- devops-lead: Setup Docker development environment.
  Output: docker-compose.yml, Dockerfile, Dockerfile.jupyter, .env.development, .env.example, .dockerignore, requirements.txt
  
  Services (all on agronomist-network):
  - postgres:15-alpine (port 5432) with health check
  - jupyter (custom Dockerfile.jupyter, port 8888)
  - tensorboard (port 6006)
  - adminer (port 8080)
  
  Multi-stage Dockerfile: python:3.11-slim → non-root user → health check
  
  Environment: DATABASE_URL, DEBUG, DEVICE, RANDOM_SEED
  
  Requirements: health checks, named volumes, .dockerignore optimization

auto-proceed
```

Press **Enter**.

The orchestrator will:
- Display the task plan
- Dispatch all 4 agents in parallel
- Take ~10 minutes (architect, both ml-engineers, devops-lead work simultaneously)
- Show `❯` prompt when Phase 1 complete

---

## 🚀 **WEEK 1 PHASE 2 - After Phase 1 Complete**

At the new `❯` prompt, paste:

```
Continue Week 1 Phase 2 - Configuration and training pipeline.

Phase 2 (after Phase 1):

- ml-engineer: Create centralized configuration.
  Output: src/config.py
  
  Define these dictionaries:
  - MODEL_CONFIG: input_dim, hidden_dims, latent_dim, seq_len, beta, dropout
  - DATA_CONFIG: num_sequences, seq_len, input_dim, random_seed
  - TRAINING_CONFIG: epochs, batch_size, learning_rate, weight_decay, early_stopping_patience, validation_split
  - PATHS: checkpoint_dir, data_dir, log_dir
  
  All hyperparameters externalized, no magic numbers in code.

- ml-engineer: Implement complete training pipeline.
  Output: src/models/training.py, scripts/train_lstm_vae.py
  
  src/models/training.py implements:
  - class VAETrainer with __init__, train_epoch, validate, train, calculate_accuracy
  - Optimizer: Adam (lr from config, weight_decay)
  - TensorBoard logging
  - Checkpoint saving with metadata (epoch, loss, accuracy, timestamp, model_config)
  - Early stopping
  - Returns training history dict
  
  scripts/train_lstm_vae.py:
  - Command-line entry point with argparse
  - Generates synthetic data
  - Creates DataLoaders
  - Runs training
  - Logs results
  
  Requirements: type hints, docstrings, error handling, config-driven

auto-proceed
```

Phase 2 takes ~15 minutes.

---

## 🚀 **WEEK 1 PHASE 3 - After Phase 2 Complete**

At the new `❯` prompt, paste:

```
Continue Week 1 Phase 3 - Unit tests and coverage.

Phase 3 (after Phase 2):

- qa-engineer: Write comprehensive unit tests and coverage configuration.
  
  Read these files (implementation to test):
  - src/models/lstm_vae.py
  - src/models/data_generator.py
  - src/models/training.py
  - src/config.py
  
  Output: tests/unit/test_lstm_vae.py, tests/unit/test_data_generator.py, tests/integration/test_training.py, pytest.ini, .coveragerc
  
  Test coverage requirements:
  - Overall: >80%
  - Critical paths (forward, loss, training): >90%
  
  tests/unit/test_lstm_vae.py:
  - Model instantiation, forward pass shapes, NaN/Inf checks
  - Loss calculation, reconstruction error bounds
  - Checkpoint save/load with metadata
  - Reparameterization stochasticity
  - Device placement
  
  tests/unit/test_data_generator.py:
  - Shape (1000, 20, 4), range [0, 1]
  - No NaN/Inf, reproducibility
  - Temporal correlations
  - Different seeds produce different data
  
  tests/integration/test_training.py:
  - Training reduces loss
  - Validation computed correctly
  - Checkpoints saved and loaded
  - Early stopping works
  - History returned correctly
  
  pytest.ini: testpaths=tests, markers, cov-fail-under=80
  .coveragerc: source=src, precision=2
  
  Requirements: >80% coverage, clear test names, fixtures for common data

auto-proceed
```

Phase 3 takes ~10 minutes.

---

## 🚀 **WEEK 1 PHASE 4 - After Phase 3 Complete (Final Gate)**

At the new `❯` prompt, paste:

```
Complete Week 1 Phase 4 - Final code review and architecture decisions.

Phase 4 (after Phase 3):

- code-reviewer: Perform final quality gate and file architecture decisions.
  
  Read all implementation (code to review):
  - src/models/lstm_vae.py
  - src/models/data_generator.py
  - src/models/training.py
  - scripts/train_lstm_vae.py
  - src/config.py
  - docker-compose.yml
  - Dockerfile
  - Dockerfile.jupyter
  - tests/unit/test_lstm_vae.py
  - tests/unit/test_data_generator.py
  - tests/integration/test_training.py
  - pytest.ini
  - .coveragerc
  
  Output: docs/review/week1-review.md, docs/architecture/ADR-001-lstm_vae_architecture.md, docs/architecture/ADR-002-synthetic_data_strategy.md, docs/architecture/ADR-003-modular_design_phase_1_5.md
  
  Review checklist:
  - Code quality: type hints, docstrings, comments, organization
  - Security: no secrets, proper error handling, data validation
  - Performance: efficient tensor ops, no unnecessary copying
  - Correctness: loss function correct, checkpoint integrity, data generation accurate
  - Testing: >80% coverage, edge cases covered
  - Architecture: loose coupling, extensible for Phase 1.5
  
  docs/review/week1-review.md format:
  ```
  # Week 1 Code Review
  
  ## Overall Status
  [PASS / PASS WITH NOTES / NEEDS FIXES]
  
  ## By Component
  [Review each file/component with status, issues, observations]
  
  ## Guardrails Check
  - [ ] Type hints on all functions
  - [ ] No hardcoded magic numbers
  - [ ] No secrets in code
  - [ ] Error handling present
  - [ ] Docstrings complete
  - [ ] Tests >80% coverage
  - [ ] No circular dependencies
  - [ ] Configuration externalized
  
  ## Blocking Issues
  [Critical issues that must be fixed before merge]
  
  ## Non-Blocking Issues
  [Nice-to-have improvements]
  
  ## Recommendations for Phase 2
  [Suggestions for next phase]
  ```
  
  ADR-001: LSTM-VAE Architecture Choice
  - Why LSTM-VAE over alternatives?
  - Trade-offs and limitations
  - Scalability for Phase 1.5
  
  ADR-002: Synthetic Data Generation Strategy
  - Why synthetic data in Phase 1?
  - Limitations and Phase 1.5 transition plan
  - Transfer learning approach
  
  ADR-003: Modular Design for Phase 1.5 Scaling
  - How does this design enable multi-customer scaling?
  - What's extensible? What might need refactoring?
  - Phase 1.5 refactoring budget (<20% of Phase 1 effort)

```

Phase 4 takes ~10 minutes.

**This is the final gate. After this completes, Week 1 is DONE.** ✅

---

## 📊 **Complete Timeline**

```
Step  Time   Action                           Your Role
────  ────   ────────────────────────────     ─────────────────────
1     0:00   Open terminal, run 'claude'      Wait for ❯ prompt
2     0:01   Paste Phase 1 task               Press Enter
3     0:02   ⏳ Orchestrator dispatches        Wait 10 minutes
4     0:12   Phase 1 complete, Phase 2 ready  Paste Phase 2 task
5     0:13   ⏳ ml-engineer works             Wait 15 minutes
6     0:28   Phase 2 complete, Phase 3 ready  Paste Phase 3 task
7     0:29   ⏳ qa-engineer works             Wait 10 minutes
8     0:39   Phase 3 complete, Phase 4 ready  Paste Phase 4 task
9     0:40   ⏳ code-reviewer works           Wait 10 minutes
10    0:50   Phase 4 complete                 ✅ WEEK 1 DONE!
```

**Total: ~50 minutes elapsed, mostly waiting**

---

## ✅ **What You Actually Type/Do**

```
1. Open terminal
   $ cd ~/Projects/ai-agronomist
   $ claude
   
   ❯ 

2. At ❯ prompt, paste entire "PHASE 1" block above
   ❯ Build Week 1 Phase 1 foundation...
   [paste full task]
   ❯ 

3. Wait ~10 minutes

4. At new ❯ prompt, paste entire "PHASE 2" block
   ❯ Continue Week 1 Phase 2...
   [paste full task]
   ❯ 

5. Wait ~15 minutes

6. At new ❯ prompt, paste entire "PHASE 3" block
   ❯ Continue Week 1 Phase 3...
   [paste full task]
   ❯ 

7. Wait ~10 minutes

8. At new ❯ prompt, paste entire "PHASE 4" block
   ❯ Complete Week 1 Phase 4...
   [paste full task]
   ❯ 

9. Wait ~10 minutes

10. ✅ Week 1 complete! Check generated files.
```

---

## 🎯 **Key Differences from Old Instructions**

| Old Instructions | Actual System |
|---|---|
| `@architect @ml-engineer @devops-lead` | No @mentions, agents named in task description |
| Paste task files separately | Paste entire task as plain text |
| Complex YAML or config | Simple plain text with Phase sections |
| `python orchestrator.py` | Just `claude` command |
| `You:` prompt | `❯` prompt |
| Multiple files to read | Copy-paste full blocks |

---

## ✨ **You're Ready!**

1. ✅ Open terminal: `cd ~/Projects/ai-agronomist && claude`
2. ✅ Wait for `❯` prompt  
3. ✅ Copy **PHASE 1 block above** and paste it
4. ✅ Press Enter
5. ✅ Wait for Phase 1 to complete (~10 min)
6. ✅ Repeat for Phase 2, 3, 4

**Week 1 ready to go!** 🚀
