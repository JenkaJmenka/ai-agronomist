# Runbook — retraining a tenant's cold-start model

**Scope.** Retraining the cold-start LSTM-VAE for one tenant after a change to
Pass 1, and deciding whether to activate the result. Written for BACKLOG-14,
whose fix changed Pass-1 discard from 50.52% to 4.62% on the `tenant_1` corpus
and so changes which rows every future model learns "normal" from.

**Ownership note.** `docs/runbooks/` is devops-lead's by the ownership table.
This file was assigned to ml-engineer by the user, who waived that row for it,
because the procedure is detector/ML material rather than infra —
the same reasoning that leaves `STEP-2`..`STEP-5` in this directory under
ML authorship. `STEP-2-tenant-baseline.md` is **not** the home for this: it
covers seasonal baselines for the comparison detectors, not cold-start
training.

**The hard rule: a retrain never activates itself.** Steps 1 and 2 produce
evidence; step 3 is a human decision. Nothing in this procedure writes to
`model_version` or `model_deployment`.

---

## 0. Who runs what

**Agents have no production access, by the user's decision.** An agent prepares
the command and reads the output; the user runs anything that touches
production. So:

| step | prepared by | run by |
|---|---|---|
| 1-2 against a local corpus (rehearsal) | agent | agent |
| 1-2 against production data | agent | **user** |
| 3 activation | — | **user** |

`scripts/pass1_shadow_score.py` is built to make this safe rather than to rely
on discipline: it opens no database session, never imports `ModelRegistry`,
opens no network connection, and writes only to a temporary directory and
stdout. It therefore *cannot* activate a model or reach Railway even if run by
mistake.

---

## 1. Train the new model — do not activate

Train from the tenant's corpus. The shadow script does this as its first step,
so in practice step 1 and step 2 are one command; run it that way rather than
training separately, so the model you score is the model you trained.

What to confirm before going on, from the `metrics_json` block the script
prints:

- `pass1_discard.status` is `"ok"`. A `"warn"` is not a blocker but must be
  read; a `"fail"` means Pass 1 raised and there is no model to score.
- `pass1_discard.data_discard_fraction` — **expected ≈ 0.0462** on the
  `tenant_1` corpus. The denominator is rows *carrying data*, not rows in the
  frame; on a mostly-NaN upload those differ by an order of magnitude, which is
  why both are printed.
- `pass1_mad_skipped_channels` — expected `co2: mad_at_or_below_min_spread` and
  `light: excluded_channel`. A `light` reason of anything else means the
  exclusion list is not being applied and the BACKLOG-14 cull is back.
- `pass1_warnings` — a list of `{code, message}` objects, empty on a clean run.

## 2. Shadow-score the window with both models

```bash
./venv/bin/python scripts/pass1_shadow_score.py \
    --csv /path/to/tenant_corpus.csv \
    --window-start 2026-12-02 --window-end 2026-12-31 \
    --old-checkpoint /path/to/current_active.pt \
    --json-out /tmp/shadow_report.json
```

Report, for old and new: **alert counts, the calibration threshold, and the new
model's `metrics_json`.** The script prints all three.

### Pick the window explicitly — "the last 30 days" is a trap here

`--window-start` and `--window-end` are required and have no default on
purpose. The development corpus
`~/Projects/ai-agronomist-data/2026_1d_english_clean_fixed.csv` **ends
2026-12-31**, so a now-relative window would select nothing from it, and a
hardcoded one would compare two different periods between local and production.
An empty selection raises rather than reporting zero alerts, because "0 alerts"
reads as a quiet model when it actually means the window missed the data.

**A local run is a rehearsal of the procedure, never a production result.** Do
not quote a local alert count in an activation decision.

### How much movement is noise

**Cold-start training is non-deterministic**, so a threshold that moved is not
by itself evidence of anything. Two runs of the command above on the identical
corpus and identical window, measured 2026-11-08:

| run | threshold | alerts (8,283 rows) | alert rate |
|---|---|---|---|
| 1 | 0.133601 | 139 | 1.68% |
| 2 | 0.131892 | 149 | 1.80% |

That is a threshold spread of 1.7e-3 and a 10-alert spread from nothing but
retraining. `AGENT_MEMORY.md` records the same effect pre-fix, four production
runs on identical data spanning 0.06140-0.06501. So:

- a threshold difference of order 1e-3 between old and new is **noise**;
- an alert-count difference of order ten on ~8,000 rows is **noise**;
- if the decision hinges on a difference that small, run it again rather than
  deciding.

The measured post-fix threshold on the `tenant_1` corpus is **0.1319-0.1336**
across those two runs. Note this is **above** the ≈0.12-0.13 that the task
brief anticipated; the figures above are what the shipped code produced, and no
constant was adjusted toward the expectation.

### Fidelity caveat you must state in any write-up

**A checkpoint does not carry the sensor ranges its own training used.**
`Pass2BootstrapDetector.train` derives adaptive ranges via
`compute_adaptive_sensor_ranges` and holds them in memory only;
`save_checkpoint` persists no `model_config`. So a reloaded old model has no way
to reproduce its original normalization.

The script therefore scores **both** models with the new model's adaptive
ranges, and prints a note saying so. That is the right choice for comparing two
models — letting the two sides normalize differently would measure the
normalization rather than the models — but it does mean the old model's numbers
here are not identical to what it produced in production, where the live
inference path uses the static `SENSOR_RANGES`. Treat the old column as "the old
weights on a fair footing", not as "what production did".

This gap is worth closing separately: persisting the ranges in the checkpoint's
`model_config` would make old-model re-scoring exact. Not done here.

## 3. STOP — the user approves before anything is activated

Hand over: both alert counts, both thresholds, the new `metrics_json`, and the
fidelity caveat above. **Do not activate.** The script prints this reminder as
its last line.

Activation, once approved, is the ordinary model-activation path
(`POST /api/models/{model_id}/activate` → `ModelRegistry.activate_model`) and
is outside this runbook.

---

## 4. Where the env-gated corpus test runs — and where it does not

`tests/models/test_pass1_tenant_corpus.py` pins the three figures that can only
be stated about the real corpus: the 4.6187% discard, the 84.4713% hour-12
survival, and `light` skipped as `excluded_channel` with zero culls.

**It is gated on `PASS1_TENANT_CSV` and CI never sets that variable, so it
never runs in CI.** That is deliberate, not an oversight: the corpus is 12.7 MB
of customer data that lives outside the repository and must never be committed.
Without the variable every test in the module skips, which is the normal local
and CI outcome. The shape is borrowed from the `postgres`-marked suites, which
skip the same way when `POSTGRES_TEST_URL` is unset.

The consequence is the part that matters: **nothing automated will catch a
Pass-1 regression against the real corpus.** The synthetic half
(`tests/models/test_pass1_light_knife_edge.py`) always runs and covers the
knife-edge construction and both channel guards, but it cannot speak for the
corpus. So the corpus test has to be run by hand:

```bash
PASS1_TENANT_CSV=~/Projects/ai-agronomist-data/2026_1d_english_clean_fixed.csv \
    ./venv/bin/python -m pytest tests/models/test_pass1_tenant_corpus.py -q
```

### Pre-merge checklist — any change to Pass 1

Run all five before merging a change to `src/models/bootstrap.py`,
`mad_scrub*.py`, `pass1_discard_guard.py`, `pass1_warning.py` or the
`BOOTSTRAP_*` constants in `src/config.py`. Items 2 and 3 are the ones CI
cannot do for you.

1. **Full suite, foreground, never a subset.**
   `./venv/bin/python -m pytest tests/ -q`. Report the measured counts and
   explain any delta against the `CLAUDE.md` baseline by the number of tests
   added or removed.
2. **The env-gated corpus test**, with the command above. CI will not run it.
   If the corpus file is unavailable, say so in the PR rather than reporting the
   skip as a pass.
3. **Both native fixtures through the real `ColdStartBootstrap.run()`** —
   `tests/fixtures/native_july_clearsky.csv` and
   `native_dec_stuck_fault.csv`. The July fixture is the one that proves the
   named exclusion list is load-bearing: `light` is only 30.6% zeros there, below
   the 0.40 modal-share limit, so the generic degeneracy guard does **not** catch
   it and the exclusion list is the only thing protecting that corpus.
4. **Per-channel skip reasons**, not just the discard total. A change can leave
   the cull counts identical and still alter every reported reason — the
   zero-spread/modal-share precedence swap did exactly that.
5. **No internal names in tenant-facing output.** Grep the warning messages for
   `BOOTSTRAP_`; if one appears in a `Pass1Warning.message` it will reach the
   tenant and `web/openapi.json`. The remediation text belongs in the log, which
   may name environment variables freely.
