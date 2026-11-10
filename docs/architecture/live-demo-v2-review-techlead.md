# Tech-Lead Review — "Live Demo v2" Increment (PRs #36–#38)

> **Historical — point-in-time record.** Written 2026-07-20. Status: record only — the tech-lead half of a read-only co-review of the diff `2b5630d..333dbc1`; its findings were addressed in follow-up PRs and the live stack has changed substantially since (see ADR-009). Not maintained against current code.

**Reviewer:** tech-lead (claude-fable-5) · **Date:** 2026-07-20 · **Mode:** READ-ONLY
**Scope:** diff since v1 review merge `2b5630d` → `333dbc1` (commits `ed09208`, `6c2b5a9`, `d57902a` + merges)

## Verdict: **NEEDS FIXES**

One blocking finding (F1): a live-path mode gap of exactly the shape that produced this
round's two prod-only bugs — the scorer's forward-fill staleness clock is wired to a
mock-interval default instead of the per-scope cadence the runtime already computes.
Every test passes the correct value explicitly; the production wiring path never does.
Everything else is solid: the tick fix is properly regression-tested in wall-clock mode,
determinism is preserved, security is clean, and the FE work is well-tested with real
loading/error/empty states.

**Blocking findings: 1** · Non-blocking: 13

---

## Findings

| # | Severity | Location | Issue | Concrete fix |
|---|----------|----------|-------|--------------|
| F1 | **BLOCKING** | `src/live/runtime.py:237` | `LiveScorer(...)` is constructed **without `emit_interval`**, so it defaults to `MOCK_SCADA_INTERVAL_SECONDS` (2.0 s) and the forward-fill staleness cutoff is frozen at `5 × 2.0 = 10 s` regardless of actual scope cadence. The correct per-scope `emit_interval = min(poll_intervals)` is computed at line 260 — *23 lines after the scorer is built* — and handed only to the merger. In any multi-source scope where a channel's source polls slower than 10 s (real vendors via `LIVE_SOURCES_JSON`; Priva/Hoogendoorn typically poll 30–60 s), `_extract_vector` (scoring.py:175) returns `None` for most ticks → scoring is silently disabled or sporadic **in production only**. All tests mask this: `test_scoring.py:896` passes `emit_interval` explicitly; every runtime test uses mock intervals that happen to match the default. This is the same failure shape as the frozen tick counter and the unreachable threshold. | Move the emit-interval derivation (runtime.py:252–264) **above** scorer construction and pass `emit_interval=emit_interval` to `LiveScorer`. Add a runtime test asserting `scope.scorer._emit_interval == min(poll_intervals)` for a `LIVE_SOURCES_JSON` scope with a slow source. Separately reconsider whether `LIVE_FORWARD_FILL_MAX_TICKS` should scale off `max(intervals)` rather than min — even correctly wired, a 5 s + 60 s scope pauses scoring 25 s after each slow-channel update. |
| F2 | non-blocking (config-default) | `src/config.py:975` | `LIVE_ANOMALY_THRESHOLD` defaults to `ANOMALY_THRESHOLD = 0.8`, which is **unreachable on the live path** (live scaled scores sit ≈ 0.00–0.10; demo episodes are tuned to ≥ 0.08; prod pins env `0.05`). Any deployment that forgets the env var silently never emits an anomaly — test-invisible, prod-only. The task asked for an assessment: **yes, the default itself should change.** The live path and the batch path score in different regimes; inheriting the batch default is a category error, not a conservative fallback. | Decouple: `LIVE_ANOMALY_THRESHOLD = float(os.environ.get("LIVE_ANOMALY_THRESHOLD", "0.05"))` with a comment explaining the live-scaled-score regime and why it must not inherit `ANOMALY_THRESHOLD`. Env override behaviour is unchanged for prod. |
| F3 | non-blocking (coverage) | `tests/live/test_demo_compound_score.py:44–48` | The **only** test proving the central demo guarantee ("compound episodes reliably score ≥ 0.08") is `@pytest.mark.slow` **and** skipped when no checkpoint exists — so CI never verifies it. The empirical claim baked into `MOCK_SCADA_DEMO_SPIKE_SIGMA`'s comment is enforced by nothing on the merge path. Same masking shape as F1/F2. | Add a nightly/scheduled CI job that runs `pytest -m slow` with a cached baseline checkpoint, or add a fast CI-visible surrogate (assert the normalized offset magnitude of a generated compound episode exceeds a floor derived from the tuning constants). |
| F4 | non-blocking (structure) | `src/live/sources/mock.py` (474 lines) | 58 % over the 300-line limit; three responsibilities in one file (signal gen, episode engine, scope factory). Split is queued — recommended cut lines in the section below. | See "Recommended splits". |
| F5 | non-blocking (duplication) | `src/live/sources/mock.py:47` | `ML_CHANNELS` lives in `mock.py` (the task brief itself assumed it was in `events.py` — telling). Channel-order/set knowledge now exists in four places: `SENSOR_NAMES` (config), `CHANNEL_ORDER` (attribution.py:22), `ML_CHANNELS` (mock.py:47), plus a **local copy** in `test_demo_compound_score.py:52`. | During the split, move `ML_CHANNELS` (and consider `CHANNEL_ORDER`) into `src/live/events.py` next to `CHANNEL_BOUNDS`; import everywhere else, including the test. |
| F6 | non-blocking (guardrail WARN) | `src/live/sources/mock.py:299,305` | Inline magic numbers in episode generation: drift total `uniform(3, 6)`, dropout duration `randint(5, 15)`. Neighbouring values (`_SPIKE_TICKS`, `_DRIFT_TICKS`, `_STUCK_TICKS`, `_SPIKE_SIGMA_MIN/MAX`) are at least named module constants but per universal-guardrails belong in `src/config.py`. Mostly v1 debt — not re-litigated, but the queued split is the natural moment to hoist them. | Name `_DRIFT_SIGMA_RANGE` / `_DROPOUT_TICKS` and move episode-tuning constants to config alongside the `MOCK_SCADA_DEMO_*` family (which got this right). |
| F7 | non-blocking (magic number) | `src/live/sources/fertigation.py:78` | `self._rng.gauss(0, 0.02)` — the soil-moisture noise std is the one unnamed constant in an otherwise exemplary constants block. | `_SM_NOISE_STD: float = 0.02` at module top. |
| F8 | non-blocking (doc rot) | `src/live/sources/mock.py:366–367,401` and `src/config.py:1009` | Docstrings/comments hardcode "score ≥ 0.08", "8-12 ticks" — values that are env-configurable (`MOCK_SCADA_DEMO_SPIKE_SIGMA`, `*_COMPOUND_MIN/MAX_TICKS`). They will silently rot when the env tuning changes. | Reference the constant names instead of their current values. |
| F9 | non-blocking (perf) | `web/features/monitoring/hooks/useSetpoints.ts:102–105` | `getDefaultSetpoints()` and the merged `setpoints` object are rebuilt on **every render** → new object identity each render defeats downstream memoization: `SensorTiles.tileData` (memo dep `setpoints`) and the chart's `optimalRange` recompute on every parent render even when nothing changed. Harmless today, but this view re-renders on every SSE tick in live mode. | `const setpoints = useMemo(() => ({ ...getDefaultSetpoints(), ...overrides }) as Setpoints, [overrides]);` (defaults are static — can also be hoisted to module scope). |
| F10 | non-blocking (validation gap) | `web/features/monitoring/hooks/useSetpoints.ts:16–17` | The zod schema validates *shape* but not the *invariant*: an inverted range `[80, 20]` (hand-edited or legacy localStorage) passes `safeParse`, then `getChannelStatus` computes a negative span/pad producing nonsense statuses (a value below min classed critical inside what the user thinks is the range). `updateSetpoint` guards `min >= max` on write, but the read path doesn't — the exact "valid shape, invalid invariant" gap class this review was asked to hunt. Tests cover corrupt JSON and wrong tuple length but not inverted ranges. (Non-finite via JSON is impossible — `JSON.parse` can't produce `NaN`/`Infinity` — so the ordering check is the real hole.) | `setpointRangeSchema.refine(([min, max]) => min < max)` + one test: stored `[80, 20]` falls back to defaults. |
| F11 | non-blocking (extensibility) | `web/features/monitoring/lib/channelMeta.ts:98` | `getChannelStatus` special-cases `channel === "light"` by string literal for the nighttime-is-normal rule; the next below-min-is-normal channel (e.g. PAR) needs a code edit in alarm logic rather than a data edit. | Add `alarmBelowMin?: false` to `ChannelMeta`, set it on `light`, branch on the flag. |
| F12 | non-blocking (convention/a11y) | `web/features/monitoring/components/LiveBadge.tsx:16–20,116–134` | Uses raw Tailwind palette classes (`bg-green-400`, `bg-amber-500/15`, `bg-red-500`) where sibling components (SensorTile) use §7 design tokens (`sev-normal/warning/critical`). Also `animate-pulse` on the SSE dot does not respect `prefers-reduced-motion` (Tailwind default keyframe), while the tile flash correctly gates on it in globals.css — inconsistent motion policy in the same view. | Map statuses to `sev-*` tokens; wrap the pulse in `motion-safe:animate-pulse`. |
| F13 | non-blocking (a11y) | `web/features/monitoring/components/SetpointsPanel.tsx:156–166` | `role="dialog"` popover has no focus management: focus does not move into the panel on open nor return to the trigger on Escape/outside-close; keyboard users can tab behind an open dialog. (Listeners themselves are correctly added/removed per `isOpen` — **no leak**, verified.) Also `tryUpdate` fires per keystroke, so transient states (`""`, `"-"`, mid-decimal) flash "Must be numeric" — consider validate-on-keystroke but commit/error-on-blur. | Focus first input on open, restore focus to trigger on close; debounce or blur-commit the inputs. |
| F14 | non-blocking (resilience) | `src/live/runtime.py:108–122` | `_supervised` backoff never resets after a successful long run: a connector that crashes once per day ratchets toward the 60 s cap permanently. Pre-existing shape, but the fertigation/demo work added more supervised tasks that inherit it. | Reset `backoff = 1.0` when `coro_fn` ran longer than e.g. `2 × _MAX_BACKOFF_SECONDS` before failing. |

Minor notes, no action required: `SensorTiles.isLoading` is always `false` from `MonitoringView`
(loading handled upstream — dead prop path in prod usage, exercised only by tests);
`compute_vpd` returning `0.0` for non-finite inputs conflates "unknown" with "saturated air" —
unreachable in practice behind the finite-validated event boundary, acceptable as defense;
VPD can theoretically exceed its `CHANNEL_BOUNDS` cap of 4.0 kPa (only at temps > 40 °C with
RH < 15 % — unreachable with current signal ranges) since `_attach_derived_vpd` doesn't clamp
to the bound.

---

## Hunt results: mode/coverage gaps of the "tests mask prod" shape

Explicitly checked, per the brief:

| Axis | Result |
|------|--------|
| Wall clock vs pinned clock | ✅ Fixed and **now regression-tested**: `test_demo_mode.py::TestWallClockTickAdvance` runs `stream()` with no `start_time` and asserts tick advance + demo firing. The v2 fix (mock.py:194–197) is correctly placed and commented. |
| Seeded determinism after tick fix | ✅ Preserved. `now` is computed from `_tick_count` *before* the increment, so tick 0 still maps to `start_time`; RNG draw order is unchanged vs v1 for non-demo sources (`_maybe_start_demo_episode` returns before any draw when demo is off). `test_fertigation.py::test_fertigation_deterministic_with_pinned_start_time` covers the new signals. |
| Mock intervals vs real-vendor intervals | ❌ **F1** — scorer staleness clock wired to the mock default; prod-only, all tests mask it. |
| Threshold: env vs config default | ❌ **F2** — default remains live-path-unreachable; **F3** — the one test that proves the demo crosses the threshold never runs in CI. |
| demo_mode on/off | ✅ Both covered (`test_demo_mode.py`); demo-off draws no RNG; fertigation-only sources verified to never receive compound episodes. |
| fakeredis vs real redis | ✅ No new hub/redis surface in this diff (VPD attach and scoring changes are upstream of publish). Known single-worker constraint unchanged. |
| localStorage present/absent/corrupt | ⚠️ SSR-absent, quota, corrupt-JSON, and schema-fail paths all guarded and tested; inverted-range invariant is the residual gap (**F10**). |
| Reduced motion | ⚠️ Tile flash correctly gated behind `prefers-reduced-motion: no-preference` (globals.css:87); LiveBadge `animate-pulse` is not (**F12**). |
| Channels missing | ✅ `SensorTiles.getLatestValue` scans backward past rows lacking a channel; tiles render "—" for null; `SetpointsPanel` filters to known meta; scorer ignores `vpd`/fertigation channels (`test_vpd_wiring.py::test_extract_vector_ignores_vpd_and_soil`, `test_same_score_with_and_without_extra_channels`); persistence keeps canonical columns only (`test_persistence_extra_channels.py`). |

---

## Per-guardrail results

### `.claude/guardrails/universal-guardrails.md`
| Rule | Result |
|------|--------|
| Tight coupling LSTM-VAE ↔ API layer | **PASS** for this diff. (`scoring.py`'s lazy imports of `src.api.services.*` are pre-existing v1 debt covered by the accepted scoring-DI refactor — flagged only as interaction: the F1 fix touches the same constructor the DI refactor will, coordinate them.) |
| Secrets/API keys hardcoded | **PASS** — none in diff; FE talks only to BFF; no keys client-side. |
| Checkpoint without metadata | **N/A** — no checkpoints written in this diff. |
| DB schema without tenant_id | **PASS** — no schema changes; all new events carry `tenant_id`. |
| Coverage < 80 % on critical paths | **PASS with note** — new-code coverage is strong (6 new backend test files, 3 FE test files); the *empirical* demo-threshold guarantee is CI-invisible (**F3**). |
| Major design change without ADR | **PASS** — derived channels + demo episodes are incremental; no structural change requiring ADR. |
| WARN: hard-coded thresholds | **WARN** — F6, F7 (mock/fertigation tuning constants not in config.py); new `MOCK_SCADA_DEMO_*` family got it right. |
| WARN: missing error handling on external calls | **PASS** — scorer forward-pass, hub publish, detector resolution, localStorage, BFF fetch all guarded with context-bearing logs. |
| WARN: duplication (3+ blocks) | **WARN** — F5 (channel-set knowledge in 4 places). |
| WARN: silent failures (bare except/pass) | **PASS** — the two swallowed FE catches (`persistOverrides`, `removeOverrides`) are intentional, commented, and UX-correct. |

### `.claude/guardrails/red-flags.md`
| Rule | Result |
|------|--------|
| Writes outside file ownership | **PASS** (this review writes only `docs/refactor/`, `tasks/done/`). |
| `os.system` / `subprocess(shell=True)` / `eval()` | **PASS** — none in diff (FE: no `eval`/`Function`/`dangerouslySetInnerHTML`/`innerHTML`). |
| Hardcoded credentials | **PASS**. |
| DELETE/DROP without tenant_id | **PASS** — no destructive queries in diff. |
| `model.save()` without metadata | **N/A**. |
| Test files modifying implementation | **PASS** — tests monkeypatch config attrs and private state only; no implementation writes. |
| Circular imports models/ ↔ api/ | **PASS** — `derived.py` is pure; `mock.py → fertigation.py` is one-directional; no new cycles. |

### `.claude/guardrails/ml-guardrails.md`
| Rule | Result |
|------|--------|
| LSTM-VAE spec locked | **PASS** — untouched. |
| Hard-coded sensor ranges | **PASS** — mock signals derive from `TEMP_RANGE`/`HUMIDITY_RANGE`/`CO2_RANGE`/config noise stds; `CHANNEL_BOUNDS` extensions in events.py are the shared importable source as documented. |
| NaN/Inf detection | **PASS** — belt-and-braces: `_clamp_and_sanitize` at source, pydantic finite validators at event boundary, `compute_vpd` non-finite guards, scorer's `isfinite(score)` check. |
| Checkpoint/training rules | **N/A** — no training code in diff. |

### `web/AGENTS.md` conventions
| Rule | Result |
|------|--------|
| URL is the state | **PASS with justified exception** — setpoints are per-tenant/gh *preferences*, not view state; localStorage (keyed `gh-setpoints:{tenant}:{gh}`) is the right store, and reload reproduces the view. `live`/`channel`/`threshold` stay in the query string via nuqs. |
| Loading + error + explicit empty state | **PASS** — tiles: skeleton/empty/em-dash; panel: "No sensor data available yet"; view-level: waiting-for-live vs no-readings copy; badge errors degrade to empty source list (acceptable for a status pill). |
| One component per file, ≤300 lines, no `any` | **PASS** — largest new file SetpointsPanel at 196; no `any` (one narrowing cast via `unknown` in useSetpoints, commented). `ChannelSetpointRow` co-located in SetpointsPanel.tsx is a private subcomponent — acceptable, but extract it if the panel grows. |
| Zod at boundaries | **PASS** — BFF status fetch and localStorage both zod-validated (invariant gap F10). |
| A11y basics | **PASS with notes** — labeled inputs, `role="alert"` errors, `aria-expanded`/`aria-haspopup` trigger, `aria-label`ed source dots, `aria-hidden` decorative overlay; gaps: dialog focus management (F13), non-motion-safe pulse (F12). |
| Perf budgets | **PASS expected** — channel toggle path untouched (no refetch); F9 is the only render-cost note. |

---

## Recommended splits (queued follow-up — cut lines as of current main)

### `src/live/sources/mock.py` (474) → 3 files
1. **`mock.py`** (~215 lines) — `MockScadaSource` core: `__init__`, `_parse_start_time`, `connect/close/stream`, `_generate_channels`, `_base_signal`, module helpers `_hour_fraction`/`_diurnal_sine`. Keeps the class public API untouched.
2. **`mock_episodes.py`** (~170 lines) — cut lines **49–120** (`_SPIKE_TICKS`…`_SCORE_VISIBLE_EPISODES`, `_NOISE_STD`, `_AnomalyEpisode`, `_CompoundEpisode`) and **274–408** (`_maybe_start_episode`, `_apply_episode`, `_apply_compound_episode`, `_schedule_demo_episode`, `_maybe_start_demo_episode`) into an `EpisodeEngine(rng, config)` collaborator owning `_episode`/`_compound_episode`/`_demo_next_tick`; `MockScadaSource` delegates. This also unlocks direct unit-testing of episode logic without poking private source state (as `test_demo_mode.py` does today).
3. **`mock_factory.py`** (~65 lines) — cut lines **411–474** (`_clamp_and_sanitize`, `build_mock_sources_for_scope`). Re-export `build_mock_sources_for_scope` from `mock.py` to keep `runtime.py`'s import path stable.
   - While cutting: move `ML_CHANNELS` (line 47) to `src/live/events.py` (F5) and hoist episode tuning constants to config (F6).

### `src/live/runtime.py` (386) → 3 files
1. **`supervision.py`** (~45 lines) — cut lines **51–133** (`_MAX_BACKOFF_SECONDS`, `_supervised`, `_run_connector`). Generic, zero runtime-specific state; fix F14 here.
2. **`runtime_builder.py`** (~150 lines) — cut lines **165–302** (`build_from_config`, `_build_scope`, `_ScopeContext` from 79–88). Config parsing / scope assembly is the file's growth axis (each new vendor/env knob lands here). Apply the **F1 fix during this cut** — move timing derivation above scorer construction.
3. **`runtime.py`** (~150 lines) — `LiveRuntime` lifecycle (`start`/`stop`/`source_statuses`) + a thin `build_from_config` classmethod delegating to the builder, preserving the public API.
   - `_attach_derived_vpd` (lines 60–72) belongs in `src/live/derived.py` next to `compute_vpd` — it is derived-channel logic, not orchestration, and `test_vpd_wiring.py` already treats it as such.

### `src/live/scoring.py` (371) → 3 files
1. **`detector_resolver.py`** (~65 lines) — cut lines **185–226** (`_resolve_detector` + `_DETECTOR_REFRESH_SECONDS`) into a `DetectorResolver` class. This is also the seam the accepted **scoring-DI refactor** needs — doing this cut first makes that refactor mostly mechanical (inject the resolver instead of the lazy imports).
2. **`enrichment.py`** (~90 lines) — cut lines **294–371** (`_spawn_enrichment`, `_on_enrichment_done`, `_run_enrichment`) into an `EnrichmentRunner(hub, resolver)` owning the task set; `LiveScorer.close()` delegates.
3. **`scoring.py`** (~215 lines) — `LiveScorer` hot path: `score`, `_extract_vector`, `_handle_episode`, `_publish_anomaly`. Constructor keeps its public contract (`__init__(tenant_id, greenhouse_id, hub, threshold)` per the documented wiring contract) with resolver/runner defaults built internally until the DI refactor lands.

---

## Interactions with accepted debt (flagged, not re-litigated)
- **Scoring DI refactor (pending):** F1's one-line fix and the `detector_resolver.py` cut both touch `LiveScorer.__init__` — land F1 first (it's a prod gap), then split, then DI, to avoid three consecutive rewires of the same constructor.
- **Single-worker SSE constraint:** unaffected by this diff; VPD attach and demo episodes are upstream of the hub.
- **SQLite/Postgres NaN gap:** this increment strengthened the guard (pydantic finite validators + `_clamp_and_sanitize` + `compute_vpd` guards) — the new `vpd`/fertigation channels cannot reach a JSON column non-finite. Verified persistence keeps canonical columns only.

Other half of this co-review: [live-demo-v2-review.md](live-demo-v2-review.md).
