# Live Monitoring (SSE) — Final Architecture Review

> **Historical — point-in-time record.** Written 2026-07-19. Status: record only — the design-conformance review that gated the first SSE live-monitoring merge; the live lifecycle it reviewed was later reworked under ADR-009. Not maintained against current code.

- **Reviewer:** architect (claude-fable-5), final design-conformance review per plan
  `prepare-plan-how-we-quirky-milner.md`
- **Date:** 2026-07-19
- **Scope reviewed:** `src/live/**` (events, hub, merge, scoring, attribution, persistence,
  runtime, sources/{base,polling,mock,priva,hoogendoorn}), `src/api/routes/live.py`,
  `src/api/services/live_hub_provider.py`, `src/api/main.py` lifespan,
  `src/config.py` live section, `web/app/api/bff/live/events/route.ts`,
  `web/features/monitoring/{api/live.ts, hooks/useLiveReadings.ts, MonitoringView.tsx,
  components/LiveToggle|LiveBadge|LiveAnomalyStrip}.tsx`, `docs/live_scada_connectors.md`.
- **Method:** every plan commitment verified against the actual code; BE event DTOs compared
  field-by-field against FE Zod schemas; failure modes traced end-to-end (publish path,
  resume path, enrichment path, multi-worker deployment).

---

## Verdict: **NEEDS FIXES**

The implementation is a faithful, in places improved, realization of the approved design —
hub/replay semantics, connector abstraction, merge, scoring, persistence, runtime resilience,
and the FE integration all conform. However, two defects are blocking:

1. **The hub leaks a Redis client on every `publish()`** — the production hot path creates
   a new connection-pool-backed client per event (~1 every 2 s per scope) and never closes it.
2. **Anomaly ↔ anomaly-detail correlation is broken end-to-end** — every event carries
   `seq=""`, so on the FE each arriving `anomaly-detail` overwrites the reasoning/fault of
   **all** buffered anomalies, showing operators wrong diagnoses on unrelated alerts.

Both are small, localized fixes. Everything else is PASS-quality.

---

## Conformance checklist

| # | Plan commitment | Status | Notes |
|---|---|---|---|
| 1 | SSE-only, one-directional; named events `reading`/`anomaly`/`anomaly-detail`/`source-status`/`heartbeat`/`reset` | **CONFORMS** | `routes/live.py` emits all six as named events; heartbeat is a named event (FE staleness badge depends on it) with `: keep-alive` comments between; no WebSockets anywhere. |
| 2 | SSE `id:` = Redis stream entry ID | **CONFORMS (transport) / DEVIATES (payload)** | The SSE frame `id:` is the XADD entry ID, so browser `Last-Event-ID` resume is correct. But the in-payload `seq` field the plan defined as "Redis stream ID string" is **always `""`** (merger sets it, nothing back-fills it). Harmless for `reading`; fatal for anomaly correlation — see finding B2. |
| 3 | Last-Event-ID resume with replay | **CONFORMS** | Header takes precedence over query param; `_id_in_stream` presence check; `XRANGE (id +` exclusive replay, then tail from the last replayed ID. Deadline check after emit guarantees replay delivery. |
| 4 | Reset-on-trimmed-gap | **CONFORMS** | Trimmed `last_event_id` → synthetic `("", "reset", {"reason":"replay-gap"})`, live tail restarts from `$`. FE clears buffers and invalidates `["readings"]`. |
| 5 | Redis Streams fan-out, multi-worker safe; XADD `MAXLEN ~` bounded; per-subscriber connections | **DEVIATES — B1** | Subscribe path conforms (factory → fresh client per subscriber, closed in `finally`; `XREAD BLOCK 5000`; `MAXLEN ~ LIVE_REPLAY_BUFFER_SIZE`). Publish path does **not**: `_get_client()` calls the factory on *every* `publish()` and never closes the returned client. Production hub (`live_hub_provider`) is factory-built → connection/fd leak on the hot path. |
| 6 | No DB sessions held for stream lifetime | **CONFORMS** | `/api/live/events` has no DB dependency; scorer opens a short-lived `AsyncSessionLocal` only for checkpoint resolution (5-min cadence) and enrichment; batcher opens its own session per flush. |
| 7 | Connector abstraction: `LiveSourceConnector` (push async-iterator), `PollingSourceConnector` base, full Priva (OAuth2 CC) + Hoogendoorn (LetsGrow REST), `CONNECTOR_TYPES` keyed by vendor, `LIVE_SOURCES_JSON`, secrets as env-var names | **CONFORMS** | ABC matches the planned signature incl. `DataSourceConnectionError` reuse. `PollingSourceConnector` adds since-cursor advance, inclusive-boundary dedup, 1→300 s backoff. Priva: cached token + proactive refresh margin + reactive 401 refresh + 429 Retry-After + pagination + `_FIELD_MAP` matching `src/vendors/priva_client.py`. Hoogendoorn: API-key header, `DEFAULT_CHANNEL_MAP` + `params.channel_map` override, pagination, 401 logged as config error. Registry holds mock/priva/hoogendoorn; `LIVE_SOURCES_JSON` parsed with per-entry validation and unknown-vendor skip. Secrets resolved from env-var *names* (`client_secret_env`, `api_key_env`) at `connect()`, never literals. OPC-UA push variant documented as future work (per plan). |
| 8 | Multi-source merge: bucket-align + grace watermark; per-channel priority (lowest wins, tie LWW); provenance map; health transitions on transition only | **CONFORMS** (one forward-looking gap, N4) | `_BucketEntry.accept` implements lowest-wins/tie-LWW exactly; provenance in `sources`; late-past-watermark drop with counter; degraded=3×/offline=10× interval, `SourceStatusEvent` only on state change; cancellation-safe final flush. Gap: `emit_interval` is hard-wired to `MOCK_SCADA_INTERVAL_SECONDS` for **all** scopes, including real-vendor ones — see N4. |
| 9 | Scoring: SEQ_LEN=20 window, batch normalization reuse, detector-cache reuse, torch off the event loop, debounced episodes, async enrichment never blocks/crashes hot path | **CONFORMS** (two deviations, N2/N3) | `deque(maxlen=SEQ_LEN)`; `normalize_data(…, SENSOR_RANGES)` = batch path; active-model resolution → `get_cached_detector` with 5-min refresh + baseline fallback; `asyncio.to_thread(forward_pass_sync, …)`; episode state machine with `LIVE_ANOMALY_REEMIT_SECONDS` re-emit; enrichment as supervised `create_task`, all exceptions logged & swallowed, tasks cancelled+awaited in `close()`. The bespoke `forward_pass_sync` (one pass returning score **and** per-channel MSE, same MSE math and `ANOMALY_SCALING_FACTOR` clip as `predict_batch_enriched`) is an **improvement** over calling the batch API twice. Deviations: plan's 5×-tick forward-fill staleness cutoff is missing (N3); `incident_id` is hard-wired `None` (N2). |
| 10 | Persistence: batched inserts, own session per flush, on-conflict-do-nothing, non-finite guard, flag-gated | **CONFORMS** (data-quality note N5) | Dialect-aware `INSERT … ON CONFLICT DO NOTHING` on `(tenant_id, greenhouse_id, timestamp)`; `sanitize_channels` second-line guard; own `AsyncSessionLocal` per flush; failed flush re-queues in original order under the 10 k cap; `LIVE_PERSIST_ENABLED` gate. Note: missing channels default to `0.0` — fabricated values reach `SensorReading` (N5). |
| 11 | Runtime: lifespan-managed supervised tasks, restart with backoff, API boots without Redis | **CONFORMS** (multi-worker implication N1 must be documented) | `_supervised` restart loop, backoff capped 60 s, `CancelledError` propagated; lifespan wraps the whole build/start in try/except → API boots with `live_runtime=None`; stop ordered before Redis/MQ/engine teardown so the final batcher flush lands. |
| 12 | FE: BFF-relay-only, Zod at boundary, `useLiveReadings` w/ polling fallback + transport state, opt-in `live` URL toggle, disabled when explicit window set, buffer caps, reset→invalidate | **CONFORMS** | EventSource targets `/api/bff/live/events` only (relay injects `X-API-Key`, forwards `last-event-id`, pipes body, honors `req.signal`); status via BFF catch-all. Every listener `safeParse`s with `.passthrough()` schemas. Transport `sse→polling` after 3 errors/30 s → 5 s `fetchRecentReadings({hours:1})`. `live` is a nuqs boolean URL param, default false; `LiveToggle` disabled with tooltip when from/to set **and** MonitoringView force-resets `live` when a window appears. Caps: 2 000 rows / 50 anomalies, 500 ms batched flush. Reset clears buffers + `invalidateQueries(["readings"])`. `dedupeByTimestamp` (live wins) feeds the untouched chart/table. Perf budgets unaffected (live off by default). |

---

## Findings

| ID | Severity | Location | Issue | Recommendation |
|----|----------|----------|-------|----------------|
| **B1** | **BLOCKING** | `src/live/hub.py:184-189` (`_get_client`) with `src/api/services/live_hub_provider.py:53` | When the hub is factory-built (which production is), **every `publish()` constructs a new `aioredis.from_url` client + connection pool and never closes it**. The runtime publishes a `reading` every ~2 s per scope, plus statuses/anomalies/details — a socket/fd leak that relies on GC finalizers to reclaim, degrading and eventually exhausting the process under sustained operation. The plan intended one publisher client ("uses its own `redis.asyncio` client"), with per-connection semantics only for *subscribers*. | Cache a single lazily-created publisher client on the hub (`self._pub_client = self._factory()` on first use; close in an `aclose()` hook wired to runtime/lifespan shutdown). Keep the existing per-subscriber factory behavior unchanged. |
| **B2** | **BLOCKING** | `src/live/merge.py:214` (`seq=""`), `src/live/scoring.py:227-233` (`_publish_anomaly` uses `event.seq`; `_spawn_enrichment(…, event.seq)`), `web/features/monitoring/hooks/useLiveReadings.ts:130-141` | The plan's correlation contract (`anomaly.seq` = stream ID; `anomaly-detail.anomaly_seq` links to it) is non-functional: `seq` is `""` on every event, so `anomaly_seq` is also `""`. The FE match `a.seq === detail.anomaly_seq` is `"" === ""` — **true for every buffered anomaly** — so each arriving `anomaly-detail` overwrites the reasoning/fault_type/root_cause of *all* (up to 50) strip entries. Operators see the latest diagnosis attached to unrelated historical alerts: wrong data on an operator-facing safety surface. | Smallest fix: in `LiveScorer._publish_anomaly`, capture the entry ID returned by `hub.publish(...)` and pass **that** to `_spawn_enrichment` as `anomaly_seq`; on the FE, key strip items by `event.lastEventId` (the SSE `id:` of the anomaly frame) instead of payload `seq`. Alternatively generate a UUID episode id pre-publish and stamp it into both events. |
| N1 | non-blocking (**must be documented**) | `src/api/main.py:113-130`, `src/live/runtime.py:145` | **Runtime runs per-process.** With `uvicorn --workers N`, every worker builds its own `LiveRuntime` → N× duplicate mock generators, N× scoring/enrichment cost, N× `reading`/`anomaly` events on the same stream. Fan-out (the hub) is multi-worker safe as designed; *generation* is not. Persistence dedupes via on-conflict; the FE chart dedupes by timestamp; but the anomaly strip and SSE volume multiply, and N mock sources publish divergent values for the same bucket. Currently benign (single-process deployments), but a real deployment scaling workers will silently corrupt the stream. | Short-term: state the single-worker constraint in `docs/live_scada_connectors.md` + deploy docs. Before scaling: elect one generator via a Redis lock (`SET live:leader NX EX`, renewed) or move `LiveRuntime` into a dedicated worker process (the `src/workers/` pattern), which is also where real SCADA connectors belong. |
| N2 | non-blocking | `src/live/scoring.py:335-336`, `src/live/events.py:165`, `web/features/monitoring/api/live.ts:68` | `AnomalyDetailEvent.incident_id` is typed `Optional[int]` but `Incident.id` is a UUID string, so the implementation permanently sends `None`. The plan's Incident linkage (deep-link from the live strip to the persisted incident) is unreachable; the enrichment pipeline *does* persist the Incident, but the event never references it. | Change the field to `Optional[str]` in the Pydantic model and `z.string().nullable().optional()` in Zod, and have `_run_enrichment` extract the incident id from the pipeline result. Additive, non-breaking (FE schemas are `.passthrough()`). |
| N3 | non-blocking | `src/live/scoring.py:148-161` (`_extract_vector`) | Plan committed a forward-fill **staleness cutoff of 5× tick**; the implementation forward-fills `_last_known` indefinitely. A channel in dropout (or a dead CO2 source) is fed frozen values into the LSTM-VAE forever — masking the very dropout/stuck anomalies the mock injects, and skewing scores when a real source dies. | Track per-channel last-update time; when a channel exceeds 5× its source interval, stop scoring (return `None`, marking the window not-warm) or surface the staleness rather than fabricating input. |
| N4 | non-blocking (will hurt with real SCADA) | `src/live/runtime.py:250-257` | `ChannelMerger.emit_interval` is hard-wired to `MOCK_SCADA_INTERVAL_SECONDS` (2.0 s) for **every** scope, including scopes built purely from `LIVE_SOURCES_JSON`. Real-vendor consequences: (a) 2 s buckets for 30–300 s pollers produce one event per polled row — acceptable; but (b) in a mixed-cadence scope (e.g. Hoogendoorn 30 s + Priva 120 s, the documented multi-source setup), the faster source advances `_emitted_watermark` to near-now, and the slower vendor's batch — timestamped minutes in the past — lands entirely `<= watermark` and is **dropped wholesale as late**, permanently. The 1 s grace is tuned for push-mode sources only. | Derive per-scope `emit_interval`/late-tolerance from the scope's max `poll_interval_seconds` (e.g. grace ≥ max poll interval + cloud lag), or hold the watermark back by the slowest source's interval while it is online. Flag in `docs/live_scada_connectors.md` until fixed. |
| N5 | non-blocking | `src/live/persistence.py:54-71` (`_event_to_row`) | Missing canonical channels are persisted as `0.0`. During warm-up, single-channel buckets, or CO2-source dropout, fabricated zeros enter `SensorReading` — the same table training/analytics/batch scoring read. A run of `co2=0.0` rows is indistinguishable from a real sensor fault and can poison future model training. | Skip rows lacking the full canonical set, or make the columns nullable for live-origin rows, or carry a `source`/completeness marker. At minimum document the sentinel. |
| N6 | non-blocking | `src/live/runtime.py:326-338` vs `web/features/monitoring/components/LiveBadge.tsx:17-19` | `/api/live/status` returns `{source_id, status}` only, but the FE parses it with `sourceStatusEventSchema`, which **requires `vendor`** → `statusResponseSchema.parse` throws on every response → the initial-status query is permanently in error state and the per-source dots render only after a live `source-status` transition (which, correctly, fires on transitions only — i.e. possibly never). The plan's "FE badge initial state" via `/api/live/status` is effectively dead code. | Include `vendor` (and `last_seen`) in `runtime.source_statuses()` — the merger already has `_source_vendors` — or relax the FE status schema to make `vendor` optional. Prefer the BE fix: one canonical source-status shape for both the endpoint and the event. |
| N7 | non-blocking | `src/api/routes/live.py:43` vs `web/features/monitoring/api/live.ts:93-97` | Heartbeat payload key mismatch: BE sends `{"ts": …}`, FE schema models `{timestamp?: …}`. Functionally harmless today (passthrough + the value is unused; arrival alone resets the stale timer), but the first consumer to read the heartbeat timestamp will silently get `undefined`. | Align on one key (`ts`) in the Zod schema. |
| N8 | non-blocking | `web/features/monitoring/hooks/useLiveReadings.ts:25-26` | `STALE_THRESHOLD_MS = 60_000` is documented as "2 × heartbeat interval" but `LIVE_HEARTBEAT_SECONDS` is 15 — 2× is 30 s, and the plan's badge spec said stalled at 2 missed heartbeats. A stalled stream shows green for up to a minute. Also the FE hardcodes a constant the BE derives from config (acceptable across the BFF boundary, but worth a comment). | Set 30 000 or fix the comment; keep plan semantics (2×) preferably. |
| N9 | non-blocking | `web/features/monitoring/hooks/useLiveReadings.ts:70,175-178` | Once `sseFailed` is set, SSE is never retried for the session (until the toggle is flipped or scope changes) — polling forever even if the API recovers seconds later. The plan required the fallback, not recovery, so this conforms; noting as a UX follow-up. | Optional: periodic SSE re-probe (e.g. every 60 s) while in polling mode. |
| N10 | non-blocking (observation) | `src/live/runtime.py:231-245`, `src/live/scoring.py:223-233` | The `anomaly` event is published *before* its triggering `reading` (scoring runs inside `on_reading` prior to the reading's own publish), so on the wire the anomaly precedes the reading it scored. No consumer currently depends on ordering, and both carry timestamps — recording so the ordering is a known property, not an accident. | None required; document if the anomaly strip ever needs the scored reading present first. |

---

## BE ↔ FE contract parity (field-by-field)

| Event | BE (`src/live/events.py`) | FE (`web/features/monitoring/api/live.ts`) | Result |
|---|---|---|---|
| `reading` | `seq:str, timestamp:dt, tenant_id, greenhouse_id, channels:dict[str,float], sources:dict[str,str], anomaly_score:float\|None` | `seq:string, timestamp:string, tenant_id, greenhouse_id, channels:record(number), sources:record(string), anomaly_score:number\|null\|undefined` | **MATCH** (datetime → ISO string; nullable/optional score aligned) |
| `anomaly` | `seq, timestamp, tenant_id, greenhouse_id, score, threshold, affected_channels:list[str], reasoning, window_start, window_end` | identical 10 fields, same types | **MATCH** (but `seq` value is always `""` — see B2) |
| `anomaly-detail` | `anomaly_seq, tenant_id, greenhouse_id, reasoning, fault_type?, root_cause?, incident_id: int?` | identical fields; `incident_id: z.number().int().nullable().optional()` | **SHAPE MATCH / SEMANTICS BROKEN** — `anomaly_seq` always `""` (B2); `incident_id` always `null` and int-typed against a UUID-string PK (N2) |
| `source-status` | `source_id, vendor, status: online\|degraded\|offline, last_seen?` | identical, `z.enum` matches literal set | **MATCH** |
| `heartbeat` | `{"ts": iso}` (built in route) | `{timestamp?: string}.passthrough()` | **TOLERATED MISMATCH** (N7) — parses, value inaccessible |
| `reset` | `{"reason": "replay-gap"}` | `{reason?: string}.passthrough()` | **MATCH** |
| `GET /api/live/status` | `{"sources": [{source_id, status}]}` | requires `{sources: [{source_id, vendor, status, last_seen?}]}` | **MISMATCH — FAIL** (N6): required `vendor` absent → parse throws, initial badge sources never load |

**Overall parity: FAIL** until B2 (correlation), N6 (status `vendor`), and ideally N2 (incident_id type) are fixed. The five streaming event shapes are otherwise exactly mirrored, and `.passthrough()` + `safeParse` gives the intended additive-change tolerance.

---

## Summary for the fix phase

- **Blocking (2):** B1 — cache one publisher Redis client in `LiveHub` (leak on hot path);
  B2 — thread a real correlation ID through `anomaly` → `anomaly-detail` and key the FE
  strip on it. Both are ≤20-line fixes with existing tests to extend
  (`tests/live/test_hub*`, `tests/live/test_scoring*`, `useLiveReadings.test.ts`).
- **Fix-worthy non-blocking:** N6 (status endpoint `vendor` — restores the initial badge),
  N2 (incident_id → str), N3 (staleness cutoff), N5 (0.0 sentinel persistence).
- **Document before real SCADA / scaling:** N1 (single-worker generation constraint),
  N4 (per-scope emit interval & watermark vs mixed vendor cadence).
- Everything else — hub replay/reset semantics, connector abstraction and both vendor
  implementations, merge priority/health logic, scoring reuse and enrichment isolation,
  persistence discipline, runtime resilience, and the FE opt-in/fallback/caps behavior —
  **conforms to the approved design**, with `forward_pass_sync` and the deterministic
  mock-clock (`start_time`) counted as improvements over the plan.

Other half of this co-review: [live-monitoring-review-techlead.md](live-monitoring-review-techlead.md).
