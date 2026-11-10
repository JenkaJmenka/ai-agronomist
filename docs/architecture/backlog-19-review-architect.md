# Architecture Review — BACKLOG-19 (pipeline must not mutate the shared detector)

**Reviewer:** architect · **Date:** 2026-11-10 · **Scope:** branch
`fix/backlog-19-pipeline-model-copy`, commits `1b9a87d` and `b44f271` on top of
`7b59a76` (= `origin/main`). Read-only review of the files on disk; no code
modified. Co-run with the tech-lead review, which covers line-level style and
the concrete cache-invalidation plumbing — neither is repeated here.

The reviewer has no Bash tool. Every figure below marked *measured* was
measured by the orchestrator and is reproduced as reported; every claim about
code was verified by reading the file at the cited line.

---

## Verdict: **PASS WITH NOTES** — no blocking findings

The fix is correct, minimal, and complete for the defect as filed. It removes
the only mutation of the shared cached `LSTMVAE` in the API process, and it
removes it at the only construction site in `src/`. The six new tests pin the
invariant rather than the race, which is the right thing to pin.

What it does not do — and was not asked to do — is make the invariant it
relies on *visible* anywhere other than a five-line comment and a test module
docstring. Every finding below is about that gap. None of them should delay
the merge; two of them should become tracked follow-ups.

| # | Finding | Severity | Owner of the follow-up |
|---|---|---|---|
| F1 | The mutation contract still belongs to the caller, and the class docstring still invites the bug | non-blocking | ml-engineer |
| F2 | Sharing of the cached module is incidental, not designed; needs an ADR | non-blocking | architect |
| F3 | Snapshot semantics are the right semantics; they impose a constraint on BACKLOG-12 | non-blocking | record in ADR |
| F4 | Live path is complete; `enrichment.py` needs no further isolation | — | — |
| F5 | Four of the six tests hold on pre-fix code too | non-blocking, informational | qa-engineer, optional |
| F6 | The `deepcopy` itself rests on the same invariant it enforces | non-blocking, informational | record in ADR |

---

## 0. Premises in the brief, checked

Every premise was verified against the files on disk. Two need a nuance.

- *"Exactly one construction site in `src/`"* — **correct.**
  `src/api/services/comprehensive_pipeline.py:551` is the only runtime
  `AnomalyAttributor(...)`; `src/models/anomaly_attributor.py:53` is a
  docstring example. Tests construct it 16 times across two files
  (`tests/models/test_anomaly_attributor.py`, `test_attributor_batched.py`).
- *Torch mechanics* — **correct as briefed.** In the installed wheel
  (`venv/lib/python3.11/site-packages/torch/nn/modules/rnn.py`): `_apply`
  empties `self._flat_weight_refs` at `:298` and restores it via
  `_init_flat_weights()` at `:304`; `_weights_have_changed` is at `:364` and
  zips `_flat_weight_refs` against `_flat_weights_names` with `strict=True` at
  `:368-369`. AGENT_MEMORY's `:368` and the brief's `:364` point at the zip and
  the `def` respectively; both are right.
- *"`online_learner.py:303` is a cache-invalidation path"* — **not for the
  cached detector.** `OnlineLearner.load_checkpoint` (`:300-304`) calls
  `load_state_dict` on `self.model`, which is always a `copy.deepcopy` of the
  manager's base model (`OnlineLearningManager.create_learner`, `:376`;
  `_get_or_restore_learner`, `:366`). It never touches a module obtained from
  `get_cached_detector`. Moreover both production constructors of the manager
  are dead today: the API route's `_load_base_model` raises 503 unconditionally
  (`src/api/routes/learning.py:69-76`, BACKLOG-12), and the worker's
  `_load_base_model` calls `get_cached_detector(None)`
  (`src/workers/training_consumer.py:43`), which raises
  `MissingCheckpointError` (`src/models/inference.py:39-44`). The consequence
  for F3 is stated there: **there is currently no code path in the API
  process that changes the cached detector's weights in place.**
- *"Process-wide"* means more than it sounds. `docker/serve.py:39` starts
  uvicorn with no `workers=` argument, so one process per replica, and
  ADR-009 §0.3 pins the API to one replica. The cached `LSTMVAE` is therefore
  the **only loaded copy of that checkpoint serving the API anywhere in the
  system** (the worker process has its own cache for upload scoring,
  `src/workers/upload_consumer.py:284`). A mutation on it is a mutation on the
  whole deployment's scoring. This is why the defect mattered and why F2
  belongs in an ADR rather than a comment.

Measured figures, reproduced from the brief without adjustment: full suite on
the branch tip 3997 / 35 / 3 / 0 against a 3991 / 35 / 3 / 0 baseline (+6 =
the new file); new tests 2 failed / 4 passed on pre-fix code, 6 passed on the
fix; `copy.deepcopy` 0.363 ms for 62,612 parameters (≈245 KiB);
`MAX_PIPELINE_CACHE_SIZE` defaults to 1 (`src/config.py:288`).

---

## F1 — Ownership of the mutation contract (non-blocking)

**Question asked:** is "callers defend themselves" a stable contract, or does
it leave the next caller to rediscover this? One construction site cuts both
ways — which way?

**Answer: it cuts toward "complete today, unstable tomorrow", and the
instability has a specific location.** The single site means the caller-side
fix is *exhaustive* — there is no second caller left exposed, so the fix is
correct and sufficient for this branch. But the contract the next caller will
read is the one in `src/models/anomaly_attributor.py:71-76`, and it says the
opposite of what the fix establishes:

> `model` is mutated in place: it is moved to `device` and switched to eval
> mode ... **Callers sharing the model with a training pipeline must expect
> this**

That sentence sanctions exactly the usage that produced BACKLOG-19. A caller
who reads it will conclude that sharing is anticipated and handled. The
pipeline comment at `comprehensive_pipeline.py:546-550` says otherwise, but it
is in the caller, not the class, and the next caller will not be in that file.

The same pattern exists in a sibling class with **zero** construction sites in
`src/`: `AnomalyEmbedding.__init__` does `self.model = model.to(self.device);
self.model.eval()` (`src/models/anomaly_embedding.py:52-53`). It is latent, not
exploited; it will be exploited the first time someone wires it to the cached
detector, and nothing will warn them.

Why the fix landed on the caller is not a design preference — it is file
ownership. `src/models/` is ml-engineer's; the backend agent fixing
`src/api/services/` could not touch the class. That is the right discipline
and it is also why the follow-up needs an explicit owner.

**Recommended end state — and it is not the one recorded.** AGENT_MEMORY's
BACKLOG-19 entry names option 3, "require callers to hand in a model already
on the right device in eval mode", as "the right end state". I disagree, for
two reasons:

1. It keeps the invariant in the caller's head. A precondition the class
   cannot enforce is a docstring with an assertion attached, and the
   assertion fires in the caller's stack trace at the wrong moment.
2. The attributor re-asserts `self.model.eval()` on **every** reconstruction
   pass (`anomaly_attributor.py:340`, `:365`, documented in ADR-004's
   implementation notes as a guard against a training pipeline flipping the
   flag). Under option 3 that guard is still a write to a shared object. It is
   a benign flag write, not an `_apply`, but it is a write, and the whole point
   is to have none.

The end state that actually removes the class of bug is: **the component owns
its model.** `AnomalyAttributor.__init__` (and `AnomalyEmbedding.__init__`)
takes a private `copy.deepcopy` and does its `.to()`/`.eval()` on that. The
cost is the one already measured and accepted on this branch — 0.36 ms and
245 KiB per instance — and it is paid in one place instead of at every call
site. The class docstring then reads "the model passed in is never mutated",
which is a contract a reader can rely on without reading the implementation.
Once that lands, the caller-side copy at `comprehensive_pipeline.py:551`
becomes a redundant double copy and should be removed in the same change,
with `test_attributor_does_not_share_the_detector_model_instance` moving to
the attributor's own unit tests where the invariant will live. Two places
claiming the same invariant is the smell to avoid; the regression test's
remaining five assertions keep guarding the pipeline either way.

Checked for breakage risk: no test in `tests/models/` asserts identity between
an attributor's `.model` and the model passed in (the two `is` assertions
found are `test_inference_enriched.py:134`, which is about `get_cached_detector`
returning one instance, and `test_online_learner.py:350`, which asserts the
*opposite* for learners). The 16 construction sites would therefore be
unaffected by a copying constructor.

**Severity: non-blocking.** The fix is correct today. File this as a
BACKLOG item owned by ml-engineer, scoped to `anomaly_attributor.py`,
`anomaly_embedding.py` and their tests, with the pipeline-side removal
delegated to backend-engineer in the same PR or the next.

---

## F2 — Is the shared mutable detector the real defect? (non-blocking, ADR recommended)

**Yes, and the precise defect is that the sharing is incidental.** It is
neither guaranteed nor prevented, so no consumer can safely rely on either
property.

The two consumers do not even hold the same instance by design:

- `DetectorResolver` holds its own strong reference for
  `LIVE_DETECTOR_REFRESH_SECONDS` (300 s, `src/config.py:1573`) once
  resolved (`src/live/detector_resolver.py:121-125`, `:144-145`).
- `get_cached_detector` is an `LRUDict` of `MAX_PIPELINE_CACHE_SIZE` = 1
  (`src/api/services/detector_cache.py:25`; eviction at
  `src/utils/lru.py:40-47`). A sync request for any **other** tenant's
  checkpoint evicts the first; the next sync request for the first tenant
  reloads a **new** instance. `clear_detector_cache()` does the same from six
  call sites (`src/api/routes/models.py:137,159,185,203`,
  `src/api/routes/inference.py:247`, `src/api/routes/learning.py:349`).

So after any eviction, the live scorer and the sync `/api/inference` route
hold **different** `LSTMVAE` objects loaded from the same checkpoint for up to
300 s. Before the eviction they hold the same one. Nothing in the code decides
which; it depends on request ordering. This is the architecture-level fact:
the system has no answer to "who owns this module, and may it change?"

- A design that relies on *sharing* (e.g. "update the cached weights in place
  and both paths pick it up") is wrong today, because identity is not
  guaranteed.
- A design that relies on *isolation* (e.g. "I can `.to()` this, it is mine")
  is wrong today, because identity is not prevented — which was BACKLOG-19.

**Should this be an ADR-level decision?** Yes. It spans three owners'
directories (`src/api/services/`, `src/live/`, `src/models/`), it constrains a
tracked backlog item (BACKLOG-12, see F3), and the only current record of the
invariant is a comment in a caller. The options, with a recommendation:

| Option | What it is | Cost | Assessment |
|---|---|---|---|
| **A. Read-only by decision** | Declare every module reachable from `get_cached_detector` — directly, via `ResolvedDetector`, or via `DetectorResolver` — **immutable after load**. Concurrent forward passes are permitted; `.to()`, `.train()`, `.eval()`, `load_state_dict`, `__setattr__` on parameters are not. Anything needing a mutable model takes a `deepcopy`. Weight changes ship as a new checkpoint, never in place. | A document, the F1 follow-up, and the regression test already on this branch. | **Recommended now.** It is what the fix already assumes; writing it down is the whole cost. |
| B. Per-consumer ownership | `DetectorResolver` loads its own instance per scope instead of going through the process LRU; sync routes keep the LRU. | One extra `torch.load` per live scope (already off the event loop, `detector_resolver.py:162`), ≈245 KiB per scope. | The structural fix if a second cross-path exploit appears. Does **not** remove intra-path sharing (scorer vs. its own enrichment in one scope), which is the sharing this branch actually fixed — so B alone would not have prevented BACKLOG-19. |
| C. Synchronise | A lock per detector around forward passes and any mutation. | Latency on the hot scoring path; serialises `set_baseline` (several forward passes) against scoring. | Rejected, as AGENT_MEMORY already records. |
| D. Hard freeze | A wrapper subclass that overrides `train()` and `_apply()` to raise on cached instances. | A new module type, and `_load_model` would need restructuring. | Over-engineered for one hazard; listed for completeness. |

**Recommend A, with B named as the escalation path.** Record both in the ADR
so the next person who finds a sharing bug reaches for B instead of inventing
E.

A note on what the single-replica constraint changes here: nothing about the
race, everything about the stakes. §0.3 is about duplicate *rows*; this is
about a single *object*. But both derive from the same fact — one process is
the whole system — and the ADR should cross-reference §0.3 so that a future
multi-replica design knows it is also changing "the cached module is the only
copy" into "there are N copies that may drift", which affects F3.

---

## F3 — Snapshot semantics (non-blocking; a constraint to record)

**Question asked:** is point-in-time attribution the semantics we want for a
model that can be retrained under a running process, or must attribution
follow weight updates?

**Answer: point-in-time is correct, because attribution has to be coherent
with the score it explains, not current with the newest weights.**

An attribution is a decomposition of one reconstruction error into per-sensor
contributions (ADR-004). It is meaningful only against the weights that
produced that error. If the detector scores a window with weights W₁ and the
attributor explains it with W₂, the per-sensor deltas describe a
reconstruction that never happened; the contributions can still sum to 1.0
and look plausible, and nothing downstream can detect the mismatch. So the
requirement is **same-run coherence**: within one `ComprehensiveInferencePipeline.run`,
detection (`LstmVaeDetector(self.detector)` → `AnomalyDetector.predict`,
`src/models/ensemble_detector.py:105`) and attribution must use identical
weights. "Following weight updates" is then a property of *how the pipeline
is replaced*, not of how the attributor holds its model.

Does the branch deliver coherence? Yes, by construction, for both paths:

- **Live path.** `EnrichmentRunner._run_enrichment` builds a **new** pipeline
  per spawn (`src/live/enrichment.py:95`), so the attributor copy is taken at
  spawn time from the very detector instance the resolver just returned
  (`:83`). Snapshot lifetime = one enrichment run.
- **Sync path.** `_get_pipeline` caches the pipeline by resolved checkpoint
  path (`src/api/routes/inference_integrated.py:41-59`), and the pipeline
  holds a strong reference to its detector for its whole life. The ensemble
  scores on that pinned detector; the attributor copy was taken from it. The
  LRU evicting the detector elsewhere does not change what this pipeline
  holds. A **new** checkpoint resolves to a **new** content-addressed path
  (`detector_cache.py:36-43`), which is a new cache key, which is a new
  pipeline, which is a new copy. Retraining therefore propagates at pipeline
  granularity — which is exactly the granularity at which coherence is
  defined.

The copy and the detector can diverge in only one way: **in-place mutation of
the pinned detector's weights after the copy is taken.** Per §0 above, no
such path exists in the API process today. That is not an accident to rely
on; it is the constraint to record:

> **Constraint on BACKLOG-12 (per-tenant online learning in the API):** an
> adapted model must be published as a new checkpoint — new artifact, new
> content-addressed path, new `ResolvedDetector`, new pipeline — never
> `load_state_dict` into a module obtained from `get_cached_detector`. In-place
> update would violate the read-only decision of F2 *and* silently decouple
> attribution from detection for the life of every cached pipeline.

One plumbing observation for the tech-lead's trace, stated once and not
pursued: `_get_cached_components` keys its attributor cache on the baseline
hash only (`comprehensive_pipeline.py:538-544`), never on detector identity.
That is sound *because* `self.detector` is fixed at construction. If anyone
later makes the pipeline's detector swappable, the attributor goes stale with
no signal. Worth a one-line comment at the cache check; not a defect today.

**Severity: non-blocking.** The semantics are right. Record the BACKLOG-12
constraint in the ADR (F2) so it is found before that work starts, not after.

---

## F4 — Is the live path complete? Does `enrichment.py` need to stop sharing?

**Complete; no.** After this branch, enrichment's only remaining use of the
shared detector is transitive and read-only:

`EnrichmentRunner._run_enrichment` → `pipeline.run` → `_get_ensemble` →
`LstmVaeDetector(self.detector)` → `AnomalyDetector.predict`
(`src/models/inference.py:74-82`): a forward pass under `torch.no_grad()` with
no `.to()`, no `.eval()`, no parameter replacement. `ensemble_detector.py`
contains no `.to(`/`.eval()`/`.train(` on the detector. The scorer's own use
is the same shape (`src/live/attribution.py:42-47`, `detector.model(tensor)`
under `no_grad`).

Two concurrent forward passes on one `nn.LSTM` from two threads are safe when
no parameter object is replaced between them: `_weights_have_changed`
(`rnn.py:364-375`) compares weakref targets against current parameters and
only calls `_init_flat_weights` if a *different object* is found. With the
`.to()` gone, nothing replaces a parameter object, so the forward pass is
purely read-only on module state and the comparison always comes back False.

Giving enrichment its own detector would cost a `torch.load` per scope for no
correctness gain and would not remove the one sharing that matters for F2
(scorer vs. sync route). Do not do it for this defect; do it as option B if a
second cross-path mutation appears.

Pre-existing, not introduced here, noted so it is not misattributed to this
branch: constructing a pipeline per enrichment spawn means each spawn pays
`set_baseline`'s forward passes on the event-loop thread (called synchronously
from `run()` at `:588`). The 0.36 ms deepcopy is added to that, and is
negligible next to it. A per-scope pipeline cache would amortise both, but
would have to key on detector identity because the resolver can return a new
instance every 300 s (F2) — which is F3's staleness hazard arriving through
the other door. Leave it.

---

## F5 — What the six tests actually discriminate (informational)

The brief measured 2 failed / 4 passed on pre-fix code. Reading the tests,
the two that discriminate are the identity assertion
(`test_attributor_does_not_share_the_detector_model_instance`) and the
training-flag assertion (`test_detector_model_is_left_in_the_training_mode_it_was_in`).
The other four — eval mode on the copy, weight equality, intact
`_flat_weight_refs`, and the shared model still scoring — hold on pre-fix code
too, because `_apply` restores `_flat_weight_refs` before returning
(`rnn.py:304`); the tear exists only *during* the call, which a single-threaded
test cannot observe. The module docstring (`:26-30`) says this honestly. The
one overstatement is the docstring of `test_detector_lstm_weight_refs_survive_intact`
(`:140`, "No torn RNNBase._apply state left behind") — pre-fix code left none
behind either. The test is still worth keeping: it pins the shape of the
invariant that F2's ADR will name, and it will catch a future `deepcopy`
implementation that returns a module with a broken ref list. Suggest the
docstring say what it pins ("the shared model's LSTM ref lists are
well-formed after the pipeline ran") rather than what it cannot observe.
Line-level; the tech-lead's call.

---

## F6 — The `deepcopy` itself rests on the invariant it enforces (informational)

`copy.deepcopy(self.detector.model)` runs on the event-loop thread while the
scorer's forward pass may be in flight on the same module in a worker thread
(`scoring.py:132-134`). The copy reads the original's `__dict__`, parameter
tensors (`Parameter.__deepcopy__` clones them), and the `_flat_weight_refs`
list; it is safe **because** the forward pass is read-only on the original —
which is true only because nothing calls `.to()` on it any more. So the fix
makes its own safety true. That is fine and it is the normal shape of this
kind of fix, but it is a second reason the read-only decision must be written
down: a future mutation of the cached module would break both the scorer
(the original race) *and* the copy that was meant to protect against it. The
repo already deep-copies `LSTMVAE` in production code on the same reasoning
(`online_learner.py:366`, `:376`), so this is established practice, not a new
dependency.

---

## F2/F4 — Does this belong in an ADR, and which one?

**A new ADR, not an amendment.** Neither existing ADR owns it: ADR-004 is
about the deterministic `decode(mu)` path and only mentions the `eval()`
guard in passing; ADR-009 is the live lifecycle and §0.3 is about replicas,
not objects. The decision is cross-cutting (three owners' directories, one
backlog constraint), which is the test for a standalone record. Next free
number is **ADR-013** (no `ADR-013*` exists anywhere under `docs/`; 002, 003
and 006 are absent from `docs/architecture/` and should not be reused).

Draft decision statement, for the ADR to carry verbatim or near it:

> **ADR-013: Cached detector modules are shared, read-only, and
> immutable after load.**
>
> Every `torch.nn.Module` reachable from `get_cached_detector` — directly,
> through `ResolvedDetector.detector`, or through `DetectorResolver.resolve()`
> — is shared across the live scoring path and the synchronous inference
> routes in the single API process, and its identity across those paths is
> **not guaranteed** (LRU eviction and `clear_detector_cache()` can leave
> them holding different instances of one checkpoint for up to
> `LIVE_DETECTOR_REFRESH_SECONDS`).
>
> Consumers may run concurrent forward passes on it. Consumers may **not**
> call `.to()`, `.train()`, `.eval()`, `load_state_dict()`, or assign to its
> parameters or buffers. A consumer that needs a model it can mutate takes
> `copy.deepcopy` and owns the copy; components whose construction mutates a
> model (`AnomalyAttributor`, `AnomalyEmbedding`) are to take that copy
> themselves rather than require it of callers.
>
> Weight changes are published as a new checkpoint. The content-addressed
> resolution path (`detector_cache.resolve_checkpoint`) then yields a new
> instance and a new pipeline, which is the only sanctioned way an attribution
> component comes to hold new weights. In-place update of a cached module is
> forbidden, including for per-tenant online learning (BACKLOG-12).
>
> Escalation: if a second cross-path mutation is found, move the live path to
> per-scope ownership (`DetectorResolver` loads its own instance) rather than
> adding synchronisation. Cross-reference ADR-009 §0.3 — "one process is the
> whole system" is the premise of both.

Writing the full ADR is **not blocking**; the invariant is recorded in the
fix's comment and the regression test's docstring, and this review now records
the decision and its options. Draft it as the architect's follow-up once F1's
owner is assigned, so the ADR can cite the component-side change rather than
promise it.

---

## What was not reviewed

Line-level style, naming, the `import copy` placement, and the exact
cache-invalidation plumbing (`clear_detector_cache()` vs. `_pipeline_cache`)
are the tech-lead's; nothing here depends on their outcome. No Postgres-only
behaviour is involved.

## Working-tree state

The reviewer has no Bash tool and cannot run `git status --short`. The brief
reports the branch checked out and clean before this review; this review adds
exactly one file, this one, at
`docs/architecture/backlog-19-review-architect.md`. `AGENT_MEMORY.md` was
**not** edited — it is orchestrator-owned per the project `CLAUDE.md` file
ownership table, which overrides the agent definition's "update
AGENT_MEMORY.md after completing"; the BACKLOG-19 entry and the proposed
follow-ups are handed to the orchestrator in the report message instead.
