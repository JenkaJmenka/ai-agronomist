# ADR-009: On-Demand Lifecycle for Live / Mock SCADA Data Sources

Status: **Accepted in principle, superseded in mechanism** — the problem in §1
was fixed on 2026-09-16 by `9c14dc3`, with a much smaller mechanism than the
§4–§6 design; the durable-state gap that fix left open was closed on 2026-10-17
by `f9b2378` (migration `028`), using the option §5.1 had rejected. The Redis
session/lease store (§5.2), the reconciler (§4.3), the `/sessions` API (§6), the
client-data guardrail (§7) and the §9 config changes were **never built**. §0 is
the authoritative record of what shipped; §2–§12 are preserved as the
2026-09-15 design and are read as history, not as current state.
Date: 2026-09-15 — revised 2026-10-17 (§0 added; §2 re-dated as history; §4–§12
annotated against what shipped. No design text was deleted.)
Scope: `src/live/**`, `src/api/routes/live.py`, `src/config.py`,
`src/api/services/live_boot_resume.py`, `src/api/services/live_freshness.py`,
`src/api/services/live_scope_state_service.py`,
`src/api/services/tenant_data_wipe.py`, `src/db/models.py`,
`alembic/versions/028_add_live_scope_state.py`, `web/features/monitoring/**`

> **Authoring note.** This document was produced on **Opus** (via the Agent tool),
> not on the `claude-fable-5` model specified for the architect role in
> `.claude/agents/architect.md`, because Fable 5 credits were exhausted at the
> time of writing. No other deviation from the architect role was made.
>
> The 2026-10-17 revision (§0 and the annotations) was likewise written on
> **Opus**, in the architect's `docs/architecture/` ownership area, because the
> `Edit`/`Grep` grant that would let the architect agent patch a 781-line file
> in place landed in `31fd26a` and does not apply to an already-running session.

---

## 0. Implementation record (added 2026-10-17)

This ADR stayed marked **Proposed** for a month while the code moved underneath
it — it was already stale the day after it was written. Everything below is
verified against the tree at `f9b2378`;
line references in §2–§12 are as of **2026-09-15** and several have since moved.

### 0.1 What shipped, and when

**2026-09-16 — `9c14dc3`, "stop idle scope connectors outright instead of muting
persistence."** The requirement-2 fix, built *without* the §4–§6 machinery:

| Piece | Where |
|---|---|
| `LIVE_AUTOSTART_ENABLED`, default `False` | `src/config.py:1345` |
| Gate: autostart on → start every scope; off → every scope idle | `src/live/runtime.py:119` (docstring `:103-111`) |
| Per-scope task map; `start_scope` / `stop_scope` spawn or cancel a scope's merger + connector tasks as a unit | `src/live/runtime.py:170`, `:187`, `_scope_tasks` at `:69` |
| `POST /api/live/resume` / `POST /api/live/pause` drive the lifecycle | `src/api/routes/live.py:198`, `:280` |
| `GET /api/live/status` reports `paused` for a configured-but-idle scope | `src/api/routes/live.py:46`, `:101` |

The commit message states the reason plainly: `MOCK_SCADA_ENABLED` defaults to
`True`, so connectors previously ran forever even when autostart gated
*persistence* off. Stopping the tasks outright means an idle scope never runs a
connector at all, rather than running one whose output is discarded.
`dbedda7`, the same day, added a data-layer proof that an idle scope writes no
rows.

**2026-09-17 — `719fd8f`, `242d000`, `ec17512`, `8a021cb`.** Honest
`not_configured` reporting. `POST /api/live/resume` on a scope with no source
wired up now returns `resumed: false, scope_status: "not_configured"`
(`src/api/routes/live.py:219-243`) instead of a green toggle that claims a
success it cannot deliver.

**2026-10-17 — `f9b2378`.** The remedy for the defect this ADR's own design
created (§0.2):

| Piece | Where |
|---|---|
| Desired state (`live` / `paused`) persisted per scope | `LiveScopeState`, `src/db/models.py:884`; table `live_scope_state` |
| Migration, `down_revision = "027"`, **inserts no rows** — an empty table means every scope idle, preserving `LIVE_AUTOSTART_ENABLED=false` semantics | `alembic/versions/028_add_live_scope_state.py` |
| Boot resume: intersect stored-`live` with `runtime.configured_scopes()`, start those | `src/api/services/live_boot_resume.py`, called at `src/api/main.py:155` |
| Boot log names resumed / idle / stored-live-but-unconfigured / failed scopes, replacing the bare `"Live runtime started."` | `live_boot_resume.log_boot_resume` |
| A wipe writes `paused` before its `DELETE`s, so a wiped scope never self-resumes | `src/api/services/tenant_data_wipe.py:59` |
| `GET /api/live/status` gains `freshness: {state, newest_reading_at, age_seconds, is_stale, threshold_seconds}` | `src/api/services/live_freshness.py`; `LIVE_STALE_AFTER_SECONDS` default `180.0` at `src/config.py:1378` |

### 0.2 The consequence this design had, recorded honestly

§4–§6 made Go Live **manual and in-process only**. Scope state lived solely in
`LiveRuntime`'s task map and `scope_control._suppressed_scopes`
(`src/live/scope_control.py:31`), both of which die with the uvicorn process.
The result, which this document did not anticipate:

> **Every Railway redeploy silently returned every scope to idle, with no resume
> and no signal.** `tenant_2/gh_A` was consequently silent for 27 days before
> anyone noticed. A production audit on **2026-10-16** found that **zero
> `source='live'` rows had ever been written, in any tenant.**

That is a direct cost of the decision recorded here, not a footnote. §10 row 5
("API restarts with a viewer connected → reconciler restarts generation") was
the design's answer to restart survival, and because the reconciler was never
built, nothing replaced it until `028`. §2.5 correctly identified that the
in-process set "does not survive an API restart — a suppressed scope silently
un-suppresses"; the symmetric failure — a *resumed* scope silently re-suppresses
— was not written down, and it is the one that bit.

**Interval note.** `9c14dc3` landed 2026-09-16 and the audit ran 2026-10-16, a
30-day span; the 27-day figure is the audit's own and presumably dates from a
later redeploy rather than from the commit.

### 0.3 Operational constraint: the API must run as a single replica

> **The API service must be deployed with exactly one replica.** This is a
> decision taken on 2026-10-17, not an open bug awaiting a fix: the defect below
> is **known, understood, and deliberately not being fixed**. Scaling the API to
> two or more replicas multiplies the persisted live row rate by the replica
> count, with **no error, no log line and no alert** when it happens.

From backend's analysis of `f9b2378`, recorded here because no other document
owns it.

**Why the database does not deduplicate the extra writes.** `PersistenceBatcher`
inserts both `SensorReading` and `RawReading` under
`on_conflict_do_nothing(index_elements=_CONFLICT_COLS)`
(`src/live/persistence.py:384`, `:391`), where `_CONFLICT_COLS` is
`["tenant_id", "greenhouse_id", "timestamp"]` (`:113`). That drops a row only
when all three key columns match an existing one — and two replicas never agree
on `timestamp`:

* `ChannelMerger` stamps a bucket with the **first raw reading to land in it**,
  not with a floored bucket boundary. `self._bucket_ts[bucket_idx] =
  reading.timestamp` runs only on the branch that creates the bucket
  (`src/live/merge.py:222`, under `if bucket is None:` at `:219`), and
  `_emit_bucket` reads it straight back out as the event timestamp (`:266`).
* That raw timestamp is microsecond-precision wall clock in production:
  `MockScadaSource.stream` takes `datetime.now(timezone.utc)` on every tick
  (`src/live/sources/mock.py:169`). The deterministic `start_time` branch just
  above it (`:164-167`) is a seeded test/replay path — `mock_factory.py` never
  sets that param — so a deployed replica always takes the wall-clock branch.

Each replica runs its own generator and so observes its own first reading for
the same logical bucket. The emitted timestamps differ, the primary keys differ,
nothing collides, and nothing is dropped. **N replicas → N× the persisted row
rate, silently.**

**Why `028` made this worse.** Before boot resume the invariant was enforced by
accident of the request path: a multi-replica deploy still required an operator
to click Go Live, and that single HTTP request was routed to exactly one
replica, so only that replica started a connector. Nothing in the code enforced
it — the load balancer did, as a side effect of there being exactly one
triggering request. Since `f9b2378` **every replica reads the same
`live_scope_state` rows at boot and self-resumes**, so every replica generates.
The accident is gone and nothing replaced it.

**The candidate fix, and why it is not being taken now.** Floor the bucket
timestamp in `_emit_bucket`: emit the bucket boundary
(`bucket_idx * self._emit_interval`, the inverse of the index computed at
`src/live/merge.py:209`) instead of the first reading's timestamp. Every replica
would then produce an identical primary key for the same logical bucket, and
`ON CONFLICT DO NOTHING` would deduplicate them for free — no new
infrastructure, no new process, a handful of lines. **The caveat, and the reason
it is deferred, is that it changes the timestamps that get persisted.** Live
rows would land on exact bucket boundaries rather than on the arrival instant of
whichever reading happened to open the bucket, which shifts future live rows
relative to the client CSV's 5-minute grid and makes already-stored rows
inconsistent with newly-written ones. That is a data-shape change requiring its
own analysis, not a local refactor.

The other two remedies are the ones §5.4 already names, and neither was built:

| Option | Cost |
|---|---|
| Floor the bucket timestamp in `_emit_bucket` | Smallest change; **changes persisted timestamps** — deferred for that reason |
| Per-scope Redis leader lock (`live:generator:{t}:{gh}`, §5.4) | Requires the Redis coordination layer of §5.2, which was never built |
| Relocate connectors to `src/workers/` | A new process, deploy target and restart/health surface |

§5.4's conclusion ("the invariant is kept for now, but designed away") is now
wrong in both halves: it was not designed away, and boot resume has made keeping
it harder. Until one of the three options above lands, **single replica is a
hard deployment requirement for the API service**, load-bearing in a way it was
not before `028`.

### 0.4 Dangling reference

§2.5, §5.4 and §11 cite `docs/live_scada_connectors.md` §8 as the record of the
single-worker invariant and of the scale-out options.
**That file does not exist** — not in the working tree and not anywhere in git
history (`git log --all -- docs/live_scada_connectors.md` is empty). The same
citation appears in `src/live/scope_control.py:20`. The invariant is in fact
documented only in that module docstring (`src/live/scope_control.py:1-23`) and
in `src/live/hub.py`. Left in place rather than silently rewritten, so the
missing file is findable; creating it is outside this ADR's scope.

---

## 1. Problem statement

Two requirements from the operator:

1. **Loading a plain CSV must never cause any live/synthetic data to be generated.**
2. **Live data must be generated only on demand** — when the operator clicks the
   *Live* button — and must **stop** when they are done.

As of 2026-09-15 neither held. Generation was unconditional from API startup,
defaulted ON into a **real tenant scope**, and persisted synthetic white noise
into the same tables the client CSV lands in.

> **Status of these requirements (2026-10-17).** Both are met, by `9c14dc3`
> rather than by the design below: with `LIVE_AUTOSTART_ENABLED=false` an
> unconfigured deploy spawns no connector task, so an upload cannot trigger
> generation, and generation starts only on an explicit `POST /api/live/resume`.
> What `9c14dc3` did *not* give them was durability across a restart — see §0.2.

---

## 2. State as of 2026-09-15, before `9c14dc3` (verified then, with file:line)

> **This section is history, not current state** (re-dated 2026-10-17). It
> records the world the design below was reacting to. Every subsection is
> annotated with what `9c14dc3` (2026-09-16) and `f9b2378` (2026-10-17) changed;
> the line numbers are as of 2026-09-15 and most have since moved — e.g. the
> `runtime.start()` call cited at `src/api/main.py:133` is now at `:142`.

### 2.1 Generation starts unconditionally at API boot *(fixed 2026-09-16)*

| Step | Location |
|---|---|
| FastAPI lifespan builds the runtime | `src/api/main.py:132` — `LiveRuntime.build_from_config(hub)` |
| …and immediately starts every task | `src/api/main.py:133` — `await runtime.start()` |
| `start()` spawns one supervised task per connector | `src/live/runtime.py:79-108` |
| Connectors were already instantiated at build time | `src/live/runtime_builder.py:78-80` |
| Mock stream loop has no gate at all | `src/live/sources/mock.py:163` — `while self._is_connected:` |

`MockScadaSource.connect()` (`src/live/sources/mock.py:147-149`) simply sets
`_is_connected = True`; `run_connector` (`src/live/supervision.py:91-93`) calls
it and then iterates `stream()` forever. There is **no subscriber check, no
scope gate, and no stop condition** other than task cancellation at shutdown.

> **Changed 2026-09-16 (`9c14dc3`).** `LiveRuntime.start()` no longer starts
> every scope. With `LIVE_AUTOSTART_ENABLED=false` (the default,
> `src/config.py:1345`) it starts only the persistence batcher and leaves every
> scope idle (`src/live/runtime.py:119`); the per-scope merger and connector
> tasks are spawned by `start_scope` and cancelled by `stop_scope`
> (`:170`, `:187`). The mock stream loop itself is unchanged — it still has no
> internal gate; it simply is not running. Since 2026-10-17 (`f9b2378`) the set
> of scopes started at boot is the stored-`live` set, not the empty set.

### 2.2 The defaults point at a real tenant *(still true — see §9.1)*

| Config | File:line | Current default | Effect |
|---|---|---|---|
| `MOCK_SCADA_ENABLED` | `src/config.py:1156` | `True` | Mock runs with zero configuration |
| `MOCK_SCADA_SCOPES` | `src/config.py:1157` | `"tenant_1:gh_A"` | A **real** tenant scope |
| `MOCK_SCADA_INTERVAL_SECONDS` | `src/config.py:1158` | `2.0` | 4 sources × 0.5 Hz per scope |
| `LIVE_PERSIST_ENABLED` | `src/config.py:1175` | `True` | Every merged bucket is written to the DB |
| `MOCK_SCADA_DEMO_MODE` | `src/config.py:1244` | `True` | Guaranteed anomaly episodes every 60–120 s |

`build_mock_sources_for_scope` (`src/live/sources/mock_factory.py:32-99`) creates
four connectors per scope — `mock_climate`, `mock_co2`, `mock_fertigation`,
`mock_gas`. Each channel value is `diurnal_sine(hour) + gauss(0, sigma)`
(`src/live/sources/mock.py:212-258`) with **no temporal state** between ticks, so
the signal is white noise around a slow sine — exactly the shape an LSTM-VAE
cannot reconstruct, which is why it dominated the anomaly counts.

> **Unchanged as of 2026-10-17.** `MOCK_SCADA_ENABLED` is still `True`
> (`src/config.py:1337`) and `MOCK_SCADA_SCOPES` is still `"tenant_1:gh_A"`
> (`:1338`). The §9.1 flips were never made; `LIVE_AUTOSTART_ENABLED=false` was
> judged to subsume them, since a configured-but-idle scope generates nothing.
> The residual risk is that the *name* of the shipped demo scope is still a real
> tenant, so a single `POST /api/live/resume` against `tenant_1/gh_A` is all it
> takes — and since `028` that choice now survives a redeploy. See §9.1.

### 2.3 Persisted rows are indistinguishable at the row level *(still true)*

`PersistenceBatcher` writes both `SensorReading` and `RawReading` with
`source="live"` (`src/live/persistence.py:168`, `:198`). The measured production
outcome: ordinary days had exactly 288 rows (client CSV, 5-min cadence);
2026-09-14/15 carried 11,119 and 19,394 extra live rows, and **96.7 % of all
flagged anomalies originated from them**.

> **Still true at the row level** (`src/live/persistence.py` writes `source="live"`
> for every vendor; the §9.3 `"live:mock"` tagging was never built).
>
> **Which database these row counts came from: production.** Resolved
> 2026-10-17; the earlier revision of this annotation had left it open. The
> figures were recorded by the same session that wrote this ADR, in
> `AGENT_MEMORY.md`, committed by `d34d2f5` (2026-09-15) — the very commit that
> added this document. That entry states the source of the data in its own
> words:
>
> > "Prod evidence: ordinary days had exactly 288 rows (client CSV, 5-min,
> > `source="Timestamp"`); 2026-09-14/15 had 11,119 and 19,394 EXTRA rows at ~2s
> > cadence, `source="live"`. **96.7% of flagged anomalies came from live rows.**
> > The client CSV was clean: 105,120 timestamps, every gap exactly 300.0s."
> > — `AGENT_MEMORY.md:601-604`
>
> and opens, four paragraphs earlier, "Verified against prod: not a detection
> regression" (`:586`). These are therefore **production** numbers — not a local
> SQLite dev database and not a seeded test fixture. Both cited lines entered the
> file in `d34d2f5`; line numbers are as of `ebd0852`.
>
> **This also resolves the apparent conflict with the 2026-10-16 audit** (§0.2),
> which found no `source='live'` rows in any tenant. The same memory entry
> records the containment that closes the gap — "user set
> `MOCK_SCADA_ENABLED=false` and purged `source='live'` rows via
> `DELETE /upload/live-data` (`wipe_live_demo_data`)" (`:608-609`) — and
> `d34d2f5`'s commit message gives the backfill's motivation as "with the live
> mock rows purged, the Anomaly page had almost nothing left to show even though
> 105,120 real readings were present". A later, independent entry corroborates
> it in passing: "Verified against prod (post mock-data purge)" (`:507`). The
> rows were in production on 2026-09-14/15 and were deleted on 2026-09-15,
> thirty-one days before the audit ran.
>
> **One correction to how that audit is stated, here and in §0.2.** A
> point-in-time count cannot establish that zero live rows *had ever been
> written* — only that none were **present** on 2026-10-16. `wipe_live_demo_data`
> issues ordinary `DELETE`s (`src/api/services/tenant_data_wipe.py:214`) and
> leaves no tombstone, so purged rows are invisible to precisely that query. Read
> as "none present", the audit and the counts above are consistent and always
> were; it was the "had ever been written" phrasing that made them look
> irreconcilable.

### 2.4 The *Live* button does not control generation *(fixed 2026-09-16)*

| Endpoint | File:line | What it actually does |
|---|---|---|
| `GET /api/live/events` | `src/api/routes/live.py:178-206` | **Subscribes** to an already-flowing Redis stream. Starts nothing. |
| `POST /api/live/resume` | `src/api/routes/live.py:155-175` | **Un-suppresses** a scope muted by *Clear all data*. Starts nothing. |
| `GET /api/live/status` | `src/api/routes/live.py:95-152` | Read-only health. Derives `greenhouse_status` from whether the runtime has a merger for the scope. |

The frontend toggle (`web/features/monitoring/components/LiveToggle.tsx:39-78`)
flips a `live` URL param, re-anchors the chart window, and calls
`resumeLiveScope`. The SSE connection is opened by
`web/features/monitoring/hooks/useLiveReadings.ts:85-210` purely as a reader.
**Nothing in the browser can start or stop a connector.**

> **Changed 2026-09-16 (`9c14dc3`).** `POST /api/live/resume`
> (`src/api/routes/live.py:198`) now calls `runtime.start_scope` and
> `POST /api/live/pause` (`:280`) calls `runtime.stop_scope`, so the browser
> does start and stop connectors — just via these two endpoints rather than via
> the §6 session API, which was never built. `/api/live/events` is still a
> read-only subscriber that starts nothing, as §6.5 intended.
>
> `/resume` also no longer claims success it cannot deliver: on a scope with no
> source wired up it returns `resumed: false, scope_status: "not_configured"`
> (documented `:219-222`, implemented `:235-241`; 2026-09-17). `/pause` records
> `paused` in every case including
> `not_configured`, because pause is the safe direction (`:302-306`).

### 2.5 `scope_control` is a suppression gate, not a lifecycle switch *(partly superseded)*

`src/live/scope_control.py:31` is a **module-level in-process `set`**. Its own
docstring (`:12-22`) records the consequences:

* invisible across uvicorn workers → **single-worker deployment is a documented
  invariant** (also `docs/live_scada_connectors.md` §8, `src/live/hub.py:6-9`);
* starts **empty**, so generation is ON by default;
* does not survive an API restart — a suppressed scope silently un-suppresses.

Suppression is also only a *publish/persist* mute: `on_reading`
(`src/live/runtime_builder.py:168-186`) returns early, but the connector keeps
spinning and the merger keeps bucketing. CPU and vendor API calls continue.
**It must not be naively repurposed as the start/stop mechanism.**

> **Where this stands at 2026-10-17.** The module survives; `scope_control` is
> still the publish/persist mute used after *Clear all data*, and `/resume` still
> falls back to it when no `LiveRuntime` is on `app.state`
> (`src/api/routes/live.py:257-277`, which reports `scope_status: "unmanaged"`
> rather than guessing). Start/stop proper moved to
> `LiveRuntime.start_scope` / `stop_scope`, so the warning above was heeded.
>
> Two of the three bullets are now obsolete for the *operator's* choice, which
> since `028` lives in `live_scope_state`: it starts empty rather than
> permissive (generation is OFF by default, not ON), and it survives an API
> restart. The first bullet — invisibility across uvicorn workers — still holds
> for `scope_control` itself, and §0.3 records that boot resume has made the
> single-worker invariant harder to keep rather than easier.

---

## 3. Design principles

1. **Generation is a privilege, not a default.** A zero-config deploy generates
   nothing, anywhere, ever.
2. **Provenance decides persistence.** Synthetic data may be *streamed* for a
   demo without being *written* into a tenant's dataset. These are two different
   permissions.
3. **Liveness is leased, never counted.** A refcount that requires a client to
   decrement is a leak waiting for a browser crash. Leases expire on their own.
4. **Ingestion of real measured data must not depend on a browser tab.** The
   on-demand rule applies to *generated* sources; see §5.3.
5. **Fail closed.** If the coordination layer is unreachable, generation stops.

---

## 4. Proposed state machine — **not built** (noted 2026-10-17)

> Nothing in §4 exists in the code. There is no six-state machine, no
> `ScopeSupervisor`, no reconciler loop and no `LIVE_RECONCILE_INTERVAL_SECONDS`.
> What shipped is a two-state model — a scope is either running tasks or it is
> not (`LiveRuntime.is_scope_active`, `src/live/runtime.py:278`) — driven
> directly by `/resume` and `/pause` rather than by a reconciler observing
> viewer leases.
>
> Two ideas from §4 did survive, in `9c14dc3`: §4.1's rule that **a scope's
> connectors start and stop as a unit** (that is exactly what `_scope_tasks`
> and `start_scope`/`stop_scope` implement, for the bucket-timing reason given
> below), and §4.4's point that `/api/live/status` must consult the scope
> registry rather than the merger map — `is_scope_configured`
> (`src/live/runtime.py:292`, over `configured_scopes` at `:282`) is that
> registry lookup, and it is what makes
> the `not_configured` and `paused` answers honest. §4.4's *lazy connector
> construction* was not adopted: connectors are still built eagerly in
> `build_scopes_from_config` and merely left unstarted.

### 4.1 Per-scope generation state

```
                    viewers > 0 && guardrail OK
      ┌────────────┐ ─────────────────────────► ┌────────────┐
      │  STOPPED   │                            │  STARTING  │
      └────────────┘ ◄───────────────────────── └────────────┘
            ▲          all connectors closed          │ connect() ok
            │                                         ▼
      ┌────────────┐          viewers == 0      ┌────────────┐
      │  STOPPING  │ ◄───────────────────────── │  RUNNING   │
      └────────────┘   || guardrail now fails   └────────────┘
            ▲          || redis grace exceeded        │
            │                                         │ N consecutive
            │                                         │ connector crashes
            │                                         ▼
            │                                   ┌────────────┐
            └────────────────────────────────── │   FAILED   │
                                                └────────────┘
      ┌────────────┐
      │  REFUSED   │  terminal for this request; scope holds client CSV data
      └────────────┘  (§7). Never auto-clears — operator must act.
```

States are per `(tenant_id, greenhouse_id)`, not per connector. A scope's
connectors start and stop as a unit, because the merger's bucket timing is
derived from the set of source configs (`src/live/runtime_builder.py:143-149`)
and a half-started scope would emit incomplete buckets that the batcher then
skips (`src/live/persistence.py:129-130`).

### 4.2 Transition triggers

| From | To | Trigger |
|---|---|---|
| `STOPPED` | `STARTING` | Reconciler observes `viewers > 0` and guardrail passes |
| `STARTING` | `RUNNING` | Every connector's `connect()` returned; merger task live |
| `STARTING` | `FAILED` | A connector raised `DataSourceConnectionError` `LIVE_MAX_START_ATTEMPTS` times |
| `RUNNING` | `STOPPING` | `viewers == 0`, **or** guardrail flipped to refuse, **or** Redis unreadable for `LIVE_REDIS_GRACE_SECONDS` |
| `STOPPING` | `STOPPED` | All tasks cancelled, `connector.close()` awaited, final batcher flush done |
| `FAILED` | `STARTING` | Operator retries (new session) — `FAILED` is sticky until then |
| any | `REFUSED` | Guardrail check fails at session start (§7) |

### 4.3 Who drives transitions — the reconciler

A single `ScopeSupervisor` coroutine per API process runs every
`LIVE_RECONCILE_INTERVAL_SECONDS` (default `5.0`). For each scope in the
registry:

```
desired = (viewers(scope) > 0) and guardrail_ok(scope) and generator_lease_held(scope)
if desired and state == STOPPED:  -> start
if not desired and state == RUNNING: -> stop
```

This is a **reconciliation loop, not an RPC**. Consequences that matter:

* The API can restart with viewers still connected; the loop rediscovers
  `viewers > 0` from Redis on its first tick and restarts generation. No client
  action needed. **This is the answer to "survives a worker restart".**
* A session-start request does not block on connectors coming up — it returns
  `state: "starting"` and the UI shows a warming-up state. Start latency is
  bounded by one reconcile tick; the endpoint may optionally kick the reconciler
  immediately (an `asyncio.Event` nudge) to make the common case sub-second.
* Stop is likewise eventually-consistent, bounded by one tick.

`LiveRuntime` keeps ownership of task lifecycle (it already has `start`/`stop`
and the `supervised()` wrapper); the new supervisor calls per-scope
`start_scope(tenant, gh)` / `stop_scope(tenant, gh)` methods that do for one
scope what `runtime.py:79-140` currently does for all of them at once.

### 4.4 Lazy connector construction

Today connectors are instantiated in `build_scopes_from_config`
(`src/live/runtime_builder.py:78-80`) whether or not they will ever run.
Change: startup builds a **registry of scope *definitions*** (the
`SourceConfig` list per scope); connector *objects* are constructed inside
`start_scope` and dropped in `stop_scope`.

Why it matters:

* `MockScadaSource` carries RNG and episode state (`mock.py:69-88`). A fresh
  instance per session gives a clean, reproducible demo instead of resuming an
  hours-old episode schedule.
* A real vendor connector holds OAuth tokens and HTTP clients. Not constructing
  it while nobody watches means no token is minted and no socket is held.
* `/api/live/status` must therefore read the **registry**, not the live merger
  map, to decide whether a scope exists — see §6.4 for the bug this avoids.

---

## 5. Where the state lives

### 5.1 Decision: Redis, with a reconciler; **not** the process, **not** Postgres

| Option | Survives restart | Multi-worker | Cost | Verdict |
|---|---|---|---|---|
| In-process dict (today's `scope_control` pattern) | ✗ | ✗ | free | Rejected — reproduces the exact defect being fixed |
| Postgres table | ✓ | ✓ | migration + poll query every tick | Rejected — ephemeral data does not belong in the system of record |
| **Redis keys with TTL** | ✓ | ✓ | one small write per viewer per 30 s | **Chosen** |

Redis is already a **hard dependency of the live path** — without it there is no
`LiveHub`, no Redis Stream, and therefore no SSE at all
(`src/live/hub.py`, `src/api/services/live_hub_provider.py`). Adding session
state there introduces no new failure domain. TTL and sorted-set range
expiry are native operations; in Postgres the same thing is a cron job.

Postgres was rejected for a second reason: the reconciler would poll it every
5 s per scope forever, and this project has already been bitten by migration
risk (`AGENT_MEMORY.md` — the 023 incident). A safety fix should not require a
schema change.

> ### 5.1a Correction — this rejection was wrong (2026-10-17)
>
> **Postgres is what shipped**, as the `live_scope_state` table in migration
> `028`. Redis was never used for scope state at all. The reasoning above fails
> on a distinction this document did not draw:
>
> * **"Ephemeral data does not belong in the system of record"** conflates two
>   different things. *Viewer leases* are ephemeral and Redis would indeed be
>   right for them. *The operator's desired state* — "I pressed Go Live on this
>   greenhouse" — is not ephemeral; it is a deliberate configuration choice that
>   must outlive both the browser and the process. Putting it behind a Redis TTL
>   would have reproduced the very defect §0.2 describes, just with a longer
>   fuse. `LiveScopeState`'s docstring (`src/db/models.py:896-899`) draws the
>   line this ADR missed: desired state, not observed state.
> * **The polling cost never materialised**, because the reconciler was never
>   built. The table is read once, at boot
>   (`src/api/services/live_boot_resume.py`), and written only when an operator
>   hits `/resume` or `/pause` or runs a wipe. That is a handful of queries per
>   deploy, not one per scope per 5 s.
> * **The migration risk was real and was handled, not avoided.** `028`
>   deliberately inserts no rows, so an empty table means every scope idle and
>   the `LIVE_AUTOSTART_ENABLED=false` semantics carry over unchanged. A deploy
>   where `028` has not yet run degrades to exactly the pre-`028` behaviour:
>   `/resume` returns `desired_state_persisted: false` and the scope streams
>   until the next restart only (`src/api/routes/live.py:228-230`).
>
> The table above is left as written. It is the record of a judgement that
> turned out to be wrong, and the reason it was wrong — treating operator intent
> as session state — is the useful part.

### 5.2 Key layout

| Key | Type | TTL | Contents |
|---|---|---|---|
| `live:viewers:{tenant}:{gh}` | ZSET | — (members expire by score) | member = `session_id`, score = lease expiry (Redis server time, unix seconds) |
| `live:session:{session_id}` | HASH | `LIVE_SESSION_TTL_SECONDS` | `tenant_id`, `greenhouse_id`, `mode`, `created_at`, `created_by` |
| `live:generator:{tenant}:{gh}` | STRING | `LIVE_GENERATOR_LEASE_SECONDS` | `worker_id` — exactly-one-generator lease (§5.5) |
| `live:guardrail:{tenant}:{gh}` | STRING | `LIVE_GUARDRAIL_CACHE_SECONDS` | cached `allow` / `refuse:<reason>` (§7) |

`viewers(scope)` = `ZCOUNT(live:viewers:{t}:{gh}, <now>, +inf)`, and each
reconcile tick first runs `ZREMRANGEBYSCORE(key, -inf, <now>)` to reap dead
leases. `<now>` comes from the Redis `TIME` command, **not** the app clock — the
app clock is untrustworthy across workers, and this codebase already has a
documented hazard with data running ahead of wall-clock.

The `live:session:{id}` hash exists so `heartbeat`/`release` can resolve a
session to its tenant **server-side** and enforce tenant access on it. Never
trust a bare `session_id` as authorisation (§6.5).

### 5.3 Lifecycle policy is per source kind, not global

This is the one place where a literal reading of requirement 2 would create a
data-loss bug. Gating a **real** Priva/Hoogendoorn feed on a browser tab means
the customer's measured data stops being ingested whenever nobody is looking.

| Vendor | `lifecycle` | Rationale |
|---|---|---|
| `mock` | `on_demand` — **forced, not overridable** | Synthetic. Exists only to be looked at. |
| `priva`, `hoogendoorn`, future real vendors | default from `LIVE_REAL_SOURCE_LIFECYCLE` (default `continuous`); per-source override via `SourceConfig.params["lifecycle"]` | Real measurements; ingestion must be independent of viewers. |

A scope containing only `on_demand` sources is fully governed by §4. A scope
containing any `continuous` source runs those connectors from boot and applies
the viewer gate only to its `on_demand` members.

> **Open question for the operator.** Today `LIVE_SOURCES_JSON` is empty
> (`src/config.py:1230`) so this table is currently theoretical — every live
> source in this deployment is mock. Confirm that real SCADA ingestion should be
> `continuous`; if the intent is that *nothing at all* flows without a viewer,
> flip `LIVE_REAL_SOURCE_LIFECYCLE` to `on_demand` and accept the gaps.

### 5.4 Multi-worker: the invariant is **kept for now, but designed away**

> **Correction (2026-10-17): the invariant is kept, *not* designed away, and
> `f9b2378` has weakened it.** Neither the Redis coordination state nor the
> `live:generator` lease was ever built, so none of the "designed away" half of
> this section exists. Worse, boot resume removed the accident that had been
> protecting a multi-replica deploy: previously Go Live required an operator
> click, and that one HTTP request landed on exactly one replica, so one replica
> generated. Now every replica self-resumes at boot from the same
> `live_scope_state` rows.
>
> The obvious defence does not work. `ON CONFLICT DO NOTHING`
> (`src/live/persistence.py:384`, `:391`) would dedupe the replicas only if they
> produced identical primary keys, and they do not: `ChannelMerger._emit_bucket`
> (`src/live/merge.py:257`) stamps the event with the **first raw reading's
> timestamp** for that bucket (`:222`, read back at `:266`) rather than with a
> floored bucket boundary, and two replicas observe different first readings.
> **N replicas means N× the row rate, silently.**
>
> Not fixed. The remedies are the two this section already names — a Redis
> leader lock, or relocating connectors to `src/workers/`. A third, cheaper
> option this section did not consider: floor the bucket timestamp to the bucket
> boundary in `_emit_bucket`, which would make `ON CONFLICT DO NOTHING` do the
> deduplication for free. That changes the timestamps of persisted live rows, so
> it is not free of consequences, but it is the smallest of the three.
>
> Until one of them lands, **single-worker remains a hard operational
> requirement**, and it is now load-bearing in a way it was not before.

**Recommendation: do not ship the multi-worker change with this fix.**

The single-worker invariant exists because generation is in-process
(`docs/live_scada_connectors.md` §8). This design removes the *correctness*
reason it was needed — coordination state moves from a process-local `set`
(`scope_control.py:31`) to Redis, so suppress/resume/session state is coherent
across workers from day one.

What remains process-bound is **generation itself**: N workers would still each
run N mock generators. The fix for that is the `live:generator:{t}:{gh}` lease:

```
SET live:generator:{t}:{gh} <worker_id> NX PX <LIVE_GENERATOR_LEASE_SECONDS>
```

acquired and renewed by the reconciler; `start_scope` is only reached when the
calling worker holds it. **Acquire and renew this lease from day one, even
under a single worker** — it costs one `SET` per scope per tick, and it means
turning on `--workers N` later is a deploy-config change rather than a redesign,
with the code path already exercised in production.

Why per-scope rather than one global leader lock: a global lock concentrates all
generation on one worker and creates a thundering herd on leader death. A
per-scope lease lets scopes spread naturally and limits the blast radius of a
lost lease to one greenhouse.

Why not move generation to `src/workers/` now (option 2 in
`docs/live_scada_connectors.md` §8): it is the right long-term home for real
SCADA connectors, but it adds a process, a deploy target, and a new
restart/health surface. The *reason to act today is safety, not scale*. Coupling
a process-topology change to a safety fix doubles the risk surface of the change
that must land. Revisit when real vendors are onboarded — at that point the
`continuous` sources from §5.3 are precisely the workload that belongs there.

Fan-out is already multi-worker safe (Redis Streams `XREAD`), so SSE serving
never needed the invariant.

### 5.5 Redis unavailable

| Situation | Behaviour |
|---|---|
| Redis down at `POST /api/live/sessions` | **503**, no session created, no generation. |
| Redis dies while a scope is `RUNNING` | Reconciler cannot read viewers. After `LIVE_REDIS_GRACE_SECONDS` (default `30.0`) of consecutive read failures → `STOPPING`. |
| Redis returns | Next tick re-reads leases; if viewers survived the outage (TTL not exceeded) the scope restarts. |

Fail-closed is correct here and costs nothing: the SSE stream is itself Redis-
backed, so during a Redis outage there is no path to the browser anyway.
Continuing to generate would write synthetic rows nobody can see — the exact
harm this design exists to prevent.

---

## 6. API contract — **session endpoints never built** (noted 2026-10-17)

> `POST /api/live/sessions`, its heartbeat and release (§6.1–§6.3), and the
> `session_id` parameter on `/events` (§6.5) do not exist. The live API at
> `f9b2378` is four endpoints: `GET /status` (`src/api/routes/live.py:101`),
> `POST /resume` (`:198`), `POST /pause` (`:280`) and `GET /events` (`:350`).
> §6.6 predicted `/resume` would be deprecated and deleted; instead it became
> the primary control, which is the opposite outcome.
>
> `GET /status` did change, but along a different axis than §6.4 proposed — no
> `generation_state` / `viewers` / `refusal_reason`. What it gained is:
>
> * **`greenhouse_status: "paused"`** (`:46`) for a configured-but-idle scope.
>   This is §6.4's `"idle"` value under a different name, and it fixes exactly
>   the bug §6.4 identified: a stopped scope has no merger, so deriving status
>   from `source_statuses()` would have reported `"no_source"` and sent the
>   operator off to reconfigure `LIVE_SOURCES_JSON` for a scope that is fine.
>   The route consults `is_scope_configured` (`src/live/runtime.py:292`), which
>   is the registry lookup §4.4 insisted on.
> * **`freshness: {state, newest_reading_at, age_seconds, is_stale,
>   threshold_seconds}`** (`:185-193`, built by
>   `src/api/services/live_freshness.py`), added 2026-10-17 in `f9b2378`. This
>   answers a question the 2026-09-15 design never asked — *is the feed I
>   believe is live actually delivering data?* — and it is the direct lesson of
>   §0.2: a scope can report itself live and have been silent for weeks.
>   `freshness.state` is deliberately narrower than `greenhouse_status`: it
>   describes the connectors only (`live` / `paused` / `not_configured`).
>   Threshold `LIVE_STALE_AFTER_SECONDS`, default `180.0`
>   (`src/config.py:1378`). A `live` scope with no rows at all counts as stale,
>   which is the case §0.2 would have caught
>   (`live_freshness.py:122-134`).
>
> §6.5's security rule — never treat a bare `session_id` as authorisation — was
> never needed, because sessions were never introduced. `/resume` and `/pause`
> both call `enforce_tenant_access` on the tenant in the query string.

All endpoints live on the existing router (`prefix="/api/live"`,
`src/api/routes/live.py:33`) and use the existing auth pattern
(`require_api_key` + `enforce_tenant_access`).

### 6.1 `POST /api/live/sessions` — start or join

Request body:

```json
{
  "tenant_id": "tenant_1",
  "greenhouse_id": "gh_A",
  "mode": "mock",
  "acknowledge_mixing": false
}
```

`mode` ∈ `"mock" | "source"`. `acknowledge_mixing` is the per-request half of
the two-lock guardrail override (§7).

`200 OK`:

```json
{
  "session_id": "0199b3c2-5c1e-7a41-9a2f-5a2b7c1d0e44",
  "tenant_id": "tenant_1",
  "greenhouse_id": "gh_A",
  "mode": "mock",
  "state": "starting",
  "viewers": 1,
  "lease_expires_at": "2026-09-15T12:01:30Z",
  "lease_ttl_seconds": 90,
  "heartbeat_interval_seconds": 30
}
```

Errors:

| Code | Body `detail` | Meaning |
|---|---|---|
| `403` | `"tenant_mismatch"` | `enforce_tenant_access` rejected the key |
| `404` | `"no_source_configured"` | Scope absent from the registry — nothing to start |
| `409` | `"scope_has_client_data"` | Guardrail refusal, see §7 for the full body |
| `503` | `"live_coordination_unavailable"` | Redis unreachable — fail closed |

Idempotent-ish: calling it twice creates **two** sessions (two viewers). That is
correct — two browser tabs are two viewers.

### 6.2 `POST /api/live/sessions/{session_id}/heartbeat` — renew

`200 OK`:

```json
{
  "session_id": "0199b3c2-…",
  "state": "running",
  "viewers": 2,
  "lease_expires_at": "2026-09-15T12:02:00Z"
}
```

`404 {"detail": "session_expired"}` when the lease has already been reaped. This
is a **normal** outcome (laptop slept) and the documented client recovery is to
`POST /api/live/sessions` again — see §8.

### 6.3 `DELETE /api/live/sessions/{session_id}` — release

Also exposed as `POST /api/live/sessions/{session_id}/release` with an empty
body, because `navigator.sendBeacon` can only issue POST.

`200 OK`:

```json
{ "session_id": "0199b3c2-…", "released": true, "viewers": 0, "state": "stopping" }
```

Idempotent: releasing an unknown or already-expired id returns `200` with
`"released": false`. Never 404 — a client tearing down must not have to handle
an error for a thing that is already in the desired state.

### 6.4 `GET /api/live/status` — extended (additive)

```json
{
  "greenhouse_status": "idle",
  "generation_state": "stopped",
  "mode": "mock",
  "viewers": 0,
  "refusal_reason": null,
  "sources": []
}
```

Changes to the existing contract:

* **New value** `"idle"` on `greenhouse_status`: scope is configured and
  startable, but no viewer and therefore no generation.
* **New fields** `generation_state`, `mode`, `viewers`, `refusal_reason`.
* `sources` stays present and keeps its shape — existing consumers
  (`web/features/monitoring/api/live.ts:129-133` parses `{sources: [...]}`)
  continue to work unchanged.

**Bug this avoids.** `live_source_status` currently derives
`greenhouse_status` from whether `runtime.source_statuses()` returned anything
(`src/api/routes/live.py:132-148`), and that method returns `[]` for an unknown
scope (`src/live/runtime.py:172-175`). Under on-demand, a *stopped* scope has no
merger, so the endpoint would report `"no_source"` — telling the operator to go
reconfigure `LIVE_SOURCES_JSON` when in fact the scope is fine and merely idle.
The route **must** consult the scope registry (§4.4), not the merger map.

### 6.5 `GET /api/live/events` — one additive parameter

New optional `session_id` query param. Behaviour:

* Present and valid → the stream is associated with that session for
  observability (`viewers` attribution, logging). It does **not** start
  generation and it does **not** renew the lease (§8.3).
* Absent → unchanged behaviour: a read-only tail of whatever is in the stream.
  Subscribing still starts nothing.

Backward compatible: today's callers keep working.

**Security note for §6.2/§6.3.** The handler must load
`live:session:{session_id}` to obtain its `tenant_id` and run
`enforce_tenant_access(auth_tenant, session.tenant_id)` before acting. A bare
`session_id` must never be sufficient — otherwise a leaked id lets one tenant
hold another tenant's generator open indefinitely.

### 6.6 `POST /api/live/resume` — deprecated

Kept for one release as a no-op returning
`{"resumed": true, "deprecated": true}`, then deleted (§9, Phase 4). Under the
new model "suppressed" collapses into "stopped with no session"; there is
nothing left for it to un-suppress.

---

## 7. Guardrail: refuse mock generation into a scope holding client data — **never built** (noted 2026-10-17)

> No part of §7 exists. There is no client-data check on `/resume`, no `409
> scope_has_client_data`, and neither `LIVE_ALLOW_MOCK_OVER_CLIENT_DATA` nor
> `acknowledge_mixing`. An operator can still point Go Live at a scope holding
> an uploaded CSV, and since `028` that choice now persists across restarts —
> which makes the gap wider than it was, not narrower.
>
> What substitutes for it is weaker and indirect: with
> `LIVE_AUTOSTART_ENABLED=false`, mixing requires a deliberate `/resume` rather
> than happening by default. That addresses the accident, not the mistake.
> §7 remains the open item with the clearest remaining value, and §7.1's
> "`source IS NULL` counts as client data" is still the right default if anyone
> picks it up.

**Yes — hard refuse.** This is the single check that most directly satisfies
requirement 1. Uploading a CSV cannot generate live data; and once a CSV is in a
scope, that scope becomes off-limits to the mock generator.

### 7.1 The check

Evaluated for `mode == "mock"` only. A scope is considered to hold client data
if **either** query returns a row:

```sql
-- raw_readings: any row whose provenance is not the live writer
SELECT 1 FROM raw_readings
 WHERE tenant_id = :t AND greenhouse_id = :gh
   AND (source IS NULL OR source NOT LIKE 'live%')
 LIMIT 1;

-- sensor_readings: same, excluding synthetic inject rows (non-NULL anomaly_type)
SELECT 1 FROM sensor_readings
 WHERE tenant_id = :t AND greenhouse_id = :gh
   AND (source IS NULL OR source NOT LIKE 'live%')
   AND anomaly_type IS NULL
 LIMIT 1;
```

The `live%` pattern matches both `"live"` and the new `"live:mock"` tag (§8.2)
and mirrors the filter already used by `wipe_live_demo_data`
(`src/api/services/tenant_data_wipe.py:192-196`).

**`source IS NULL` counts as client data — deliberately.** Pre-tag rows are
ambiguous (see the fallback branch at `tenant_data_wipe.py:200-213`), and the
conservative reading errs toward *refusing to generate*, which is the safe
direction. The failure mode of being too strict is "the demo will not start in
this scope"; the failure mode of being too loose is the incident that prompted
this document.

### 7.2 Refusal response (`409`)

```json
{
  "detail": "scope_has_client_data",
  "tenant_id": "tenant_1",
  "greenhouse_id": "gh_A",
  "client_rows_present": true,
  "hint": "Pick a demo scope, or set LIVE_ALLOW_MOCK_OVER_CLIENT_DATA=true on the deployment AND resend with acknowledge_mixing=true."
}
```

### 7.3 Two locks, both default off

Overriding requires **both**:

1. `LIVE_ALLOW_MOCK_OVER_CLIENT_DATA=true` — a deploy-level env flag, default
   `false`; and
2. `acknowledge_mixing: true` in the request body.

One click must never be enough. The deploy flag means a production API cannot be
talked into mixing by any request the UI can send.

### 7.4 Re-checked continuously, not just at start

The reconciler re-evaluates the guardrail each tick (served from the
`live:guardrail:{t}:{gh}` cache, TTL `LIVE_GUARDRAIL_CACHE_SECONDS`, default
`60`). On an `allow → refuse` transition — i.e. **a CSV upload landed while a
live session was open** — the scope moves to `STOPPING` and the API publishes:

```
event: live-stopped
data: {"reason":"scope_has_client_data","tenant_id":"tenant_1","greenhouse_id":"gh_A"}
```

so the UI can flip the toggle off and explain why. Cache invalidation on the
upload path is a nice-to-have (delete the guardrail key in `upload_csv` /
`upload-async` finalize); the 60 s TTL bounds the worst case regardless.

---

## 8. Frontend contract the *Live* button must honour — **superseded** (noted 2026-10-17)

> There are no sessions, so there is no heartbeat, no `sessionStorage` lease, no
> `sendBeacon` release and no `sessionId` gate on `useLiveReadings`. The shipped
> contract is the two-call one: `LiveToggle` calls `resumeLiveScope` on and
> `pauseLiveScope` off (`web/features/monitoring/api/live.ts:191`, `:204`;
> `LiveToggle.tsx:72`, `:99`).
>
> §8.1's "critical inversion" — call the backend first, flip the toggle only on
> success — was **not** adopted: `setLive(true)` still runs before
> `resumeLiveScope` (`LiveToggle.tsx:89` then `:99`), with a rollback to
> `setLive(false)` on failure (`:106`). Pause is likewise fire-and-tolerate
> (`:68-72`). Given that the backend now reports `not_configured` honestly
> (§2.4), the UI has the information §8.1 wanted; it just applies it as a
> rollback rather than as a precondition.
>
> §8.6's `stale` indicator is the one item §8 anticipated correctly — the
> `freshness` block added in `f9b2378` (§6) is what feeds it.

Target files: `web/features/monitoring/components/LiveToggle.tsx`,
`web/features/monitoring/hooks/useLiveReadings.ts`,
`web/features/monitoring/api/live.ts`. The generic BFF proxy
(`web/app/api/bff/[...path]/route.ts:65-75`) already forwards POST and DELETE
with bodies, so **no new BFF route is required** for the session endpoints; the
SSE relay (`web/app/api/bff/live/events/route.ts`) only needs to forward the new
`session_id` query param, which it already does via `target.searchParams`
additions.

### 8.1 Turning Live ON

```
1. POST /api/bff/live/sessions {tenant_id, greenhouse_id, mode:"mock"}
2. 200  -> store session_id in sessionStorage under `live:{tenant}:{gh}`
          -> setLive(true)
          -> open EventSource /api/bff/live/events?...&session_id=...
          -> start the heartbeat interval
   409  -> DO NOT flip the toggle; render the refusal message inline
   404  -> DO NOT flip; "No live source configured for this greenhouse"
   503  -> DO NOT flip; "Live coordination unavailable — try again"
```

The critical inversion versus today: **the session call happens first and the
toggle only flips on success.** Currently `setLive(true)` runs before
`resumeLiveScope` and ignores its failure
(`LiveToggle.tsx:54` then `:73-77`), so the UI can claim to be live when the
backend disagrees.

The existing chart-window re-anchoring (`LiveToggle.tsx:46-70`) is unchanged and
should run after the session is confirmed.

### 8.2 Turning Live OFF

```
1. DELETE /api/bff/live/sessions/{session_id}
2. close EventSource, clear the heartbeat interval, clear sessionStorage
3. setLive(false)
```

Release is best-effort: if the DELETE fails, the lease expires on its own within
`LIVE_SESSION_TTL_SECONDS`. The UI must not block the toggle on it.

### 8.3 Heartbeat — client-driven, and **only** client-driven

`setInterval(heartbeat_interval_seconds * 1000)` while `live === true`, calling
`POST /api/bff/live/sessions/{id}/heartbeat`. On `404` → re-run the §8.1 start
sequence (the lease expired while the laptop slept).

**Server-side SSE frames deliberately do not renew the lease.** A half-open TCP
connection can keep a server-side `async for` alive long after the browser is
gone; if emitting a frame renewed the lease, that dead client would pin the
generator open — reintroducing exactly the never-stops failure this design
exists to remove. Requiring a real browser to POST every 30 s makes liveness
provable rather than assumed. It also gives the polling-fallback transport
(`useLiveReadings.ts:251-257`, which has no SSE stream at all) the same renewal
path for free.

### 8.4 Navigation, scope changes, tab close

| Event | Action |
|---|---|
| Client-side route change (monitoring → climate) | Reuse the `session_id` from `sessionStorage`; reopen the EventSource. **No** release — the 90 s lease absorbs the remount gap, so the generator never thrashes. |
| Tenant/greenhouse switch | Release the old session, then start a new one for the new scope. |
| Real tab close / `pagehide` | `navigator.sendBeacon('/api/bff/live/sessions/{id}/release')`. |
| Browser crash / network death | Nothing. Lease expires; generation stops within `TTL + reconcile interval` ≤ 95 s. |

### 8.5 Hook signature change

`useLiveReadings(enabled: boolean)` becomes
`useLiveReadings({ enabled, sessionId })` and must not open the EventSource
while `sessionId` is null. This keeps "we have permission to be live" and "we
are rendering live" as one fact instead of two that can disagree.

### 8.6 New UI states to render

`idle` (nobody watching), `starting` (warming up — the first merged bucket needs
`grace_seconds`, and the scorer needs 20 readings before `anomaly_score` is
non-null), `refused` (with the §7.2 hint), `failed` (connector could not
connect). The existing `LiveBadge` / `transport` / `stale` indicators stay.

---

## 9. Config and default changes — **mostly not made** (noted 2026-10-17)

> Of the thirteen config changes below, **one landed and it is not on either
> list**: `LIVE_AUTOSTART_ENABLED` (default `False`, `src/config.py:1345`),
> added by `9c14dc3`. One more arrived later: `LIVE_STALE_AFTER_SECONDS`
> (default `180.0`, `:1378`), added by `f9b2378` for §6's freshness block.
>
> Everything in §9.1 and §9.2 is otherwise **unchanged or absent** — verified at
> `f9b2378`:
>
> | Proposed | Actual |
> |---|---|
> | `MOCK_SCADA_ENABLED` → `False` | still `True` (`src/config.py:1337`) |
> | `MOCK_SCADA_SCOPES` → `""` | still `"tenant_1:gh_A"` (`:1338`) |
> | `LIVE_PERSIST_ENABLED` unchanged `True` | still `True` (`:1361`) — correct by accident |
> | `MOCK_SCADA_PERSIST_ENABLED` | does not exist |
> | `LIVE_ALLOW_MOCK_OVER_CLIENT_DATA` | does not exist |
> | `LIVE_SESSION_TTL_SECONDS`, `LIVE_SESSION_HEARTBEAT_SECONDS`, `LIVE_RECONCILE_INTERVAL_SECONDS`, `LIVE_REDIS_GRACE_SECONDS`, `LIVE_GENERATOR_LEASE_SECONDS`, `LIVE_GUARDRAIL_CACHE_SECONDS`, `LIVE_MAX_START_ATTEMPTS` | none exist |
> | `LIVE_REAL_SOURCE_LIFECYCLE` | does not exist; §5.3's per-kind lifecycle was not built |
> | §9.3 `source="live:mock"` tagging | not done — `PersistenceBatcher` still writes `source="live"` for every vendor |
>
> **Why the §9.1 flips were skipped, and what it costs.** Stopping the tasks
> outright made the defaults look harmless: a configured scope that never starts
> generates nothing regardless of what `MOCK_SCADA_SCOPES` says. But the shipped
> default still *names a real tenant* as the mock scope, so one `/resume`
> against `tenant_1/gh_A` writes synthetic rows into it — and since `028` that
> choice survives a redeploy. §9.1 is therefore **still open**, and the
> `demo_tenant` rename in §9.4 ("part of the fix, not an optional extra") is
> still unmade.
>
> §9.2's claim that `MOCK_SCADA_PERSIST_ENABLED` was "the single
> highest-value new knob" was wrong in hindsight: the knob that actually solved
> requirement 1 was `LIVE_AUTOSTART_ENABLED`, which stops the generator rather
> than discarding its output — strictly better, because it also stops the CPU
> and the vendor API calls §2.5 complained about.

### 9.1 Changed defaults

| Config | File:line | Now | Proposed | Why |
|---|---|---|---|---|
| `MOCK_SCADA_ENABLED` | `src/config.py:1156` | `True` | **`False`** | A zero-config deploy must generate nothing |
| `MOCK_SCADA_SCOPES` | `src/config.py:1157` | `"tenant_1:gh_A"` | **`""`** | The current default is a **real tenant**. Empty means no mock scope exists; an operator must name one |
| `LIVE_PERSIST_ENABLED` | `src/config.py:1175` | `True` | **`True` (unchanged)** | Real measured data must still be persisted; provenance now decides — see next row |

### 9.2 New config

| Name | Default | Purpose |
|---|---|---|
| `MOCK_SCADA_PERSIST_ENABLED` | **`False`** | Mock data streams to the browser but is **never written to the DB** unless explicitly enabled. This is the single highest-value new knob: it decouples "show a demo" from "pollute a dataset". |
| `LIVE_ALLOW_MOCK_OVER_CLIENT_DATA` | `False` | Deploy-level half of the §7.3 override |
| `LIVE_SESSION_TTL_SECONDS` | `90.0` | Viewer lease lifetime |
| `LIVE_SESSION_HEARTBEAT_SECONDS` | `30.0` | Advertised client renewal cadence (TTL / 3 — tolerates two dropped heartbeats) |
| `LIVE_RECONCILE_INTERVAL_SECONDS` | `5.0` | Supervisor tick |
| `LIVE_REDIS_GRACE_SECONDS` | `30.0` | Consecutive Redis read failure before fail-closed stop |
| `LIVE_GENERATOR_LEASE_SECONDS` | `30.0` | Per-scope exactly-one-generator lease |
| `LIVE_GUARDRAIL_CACHE_SECONDS` | `60.0` | Guardrail result cache TTL |
| `LIVE_REAL_SOURCE_LIFECYCLE` | `"continuous"` | Default lifecycle for non-mock vendors (§5.3) |
| `LIVE_MAX_START_ATTEMPTS` | `3` | `STARTING → FAILED` threshold |

### 9.3 Provenance tagging

`PersistenceBatcher` currently writes `source="live"` for everything
(`src/live/persistence.py:168`, `:198`). Change to tag by vendor kind:

* mock connectors → `source="live:mock"`
* real vendors → `source="live"` (unchanged)

`wipe_live_demo_data` already matches `source LIKE 'live:%'`
(`tenant_data_wipe.py:192-196`), so the existing *Remove live demo data* button
keeps working with no change. The §7.1 guardrail uses the same `live%` prefix.
This also makes "how many synthetic rows are in this tenant?" a one-line query
for future audits — a question that cost real investigation time this week.

### 9.4 Demo deployment

The existing demo keeps working by setting, explicitly, on the demo service
only:

```
MOCK_SCADA_ENABLED=true
MOCK_SCADA_SCOPES=demo_tenant:gh_demo
MOCK_SCADA_PERSIST_ENABLED=true     # only if the demo needs history to survive
LIVE_ALLOW_MOCK_OVER_CLIENT_DATA=false
```

Note `demo_tenant`, **not** `tenant_1`. Moving the demo off the real tenant scope
is part of the fix, not an optional extra.

---

## 10. Failure modes — **analysis of a system that was not built** (noted 2026-10-17)

> Rows 1–4, 9, 10 and 12–14 concern sessions, leases and Redis coordination,
> none of which exist. The table is retained as the reasoning behind the design,
> not as a description of current behaviour.
>
> Two rows are worth re-reading against what happened:
>
> * **Row 5, "API restarts with a viewer connected → reconciler restarts
>   generation, ≤ 1 tick."** This was the design's whole answer to restart
>   survival, and it was never implemented. Nothing replaced it for a month;
>   §0.2 is the result. Since `f9b2378` the answer is boot resume from
>   `live_scope_state`, which is *better* than row 5 in one respect — it does
>   not require a viewer to be connected, so a redeploy at 3am restores the feed
>   — and worse in another, per §0.3.
> * **Row 15, "two API workers → only one generates."** Not true today; see
>   §0.3. The lease that row depends on was never built.
>
> The failure mode that actually occurred appears nowhere in this table: *the
> operator turned Live on, a redeploy turned it off, and nothing said so.* The
> table reasons carefully about clients vanishing and never about the **server**
> forgetting — understandable for a design whose state lived in Redis, and the
> precise gap that made the in-process implementation dangerous. The freshness
> block added in `f9b2378` (§6) exists to make this class of failure visible.

| # | Scenario | Behaviour | Bound |
|---|---|---|---|
| 1 | Operator closes the tab cleanly | `sendBeacon` release → viewers 0 → stop | ≤ 1 reconcile tick (5 s) |
| 2 | Browser crashes / laptop lid closes | No release, no heartbeats → lease reaped → stop | ≤ TTL + tick (95 s) |
| 3 | Two operators, one leaves | ZSET still has 1 member → keeps running | — |
| 4 | Half-open SSE (client gone, server unaware) | Server never renews (§8.3); lease expires | ≤ 95 s |
| 5 | API restarts with a viewer connected | Leases live in Redis; reconciler restarts generation | ≤ 1 tick |
| 6 | API restarts with no viewer | Nothing starts. **This is the requirement-1 case.** | — |
| 7 | CSV uploaded while live is on | Guardrail flips → `STOPPING` + `live-stopped` event | ≤ guardrail cache TTL (60 s) |
| 8 | CSV uploaded while live is off | Nothing happens at all — no generator exists to start | — |
| 9 | Redis down at session start | `503`, fail closed | immediate |
| 10 | Redis dies mid-session | Stop after `LIVE_REDIS_GRACE_SECONDS` | 30 s |
| 11 | Connector cannot connect | `supervised()` backoff; after `LIVE_MAX_START_ATTEMPTS` → `FAILED`, surfaced in `/status` | — |
| 12 | Clock skew between workers | Lease scores use Redis `TIME`, not app clock | — |
| 13 | Session id leaked across tenants | `enforce_tenant_access` against the session's stored `tenant_id` (§6.5) | — |
| 14 | Rapid toggle on/off/on | Session reused from `sessionStorage`; lease still valid → no stop/start churn | — |
| 15 | Two API workers (invariant violated) | Per-scope generator lease means only one generates (§5.4) | — |

---

## 11. Phased migration plan — **not followed** (noted 2026-10-17)

> None of the five phases ran as written. What happened instead:
>
> | Phase | Outcome |
> |---|---|
> | 1 — safe defaults + provenance | **Not done.** Defaults unchanged, `live:mock` tagging unbuilt (§9). Its goal was met a different way, by `9c14dc3`. |
> | 2 — Redis sessions, registry, reconciler | **Not done.** No session store, no `ScopeSupervisor`, no `LIVE_ON_DEMAND_ENABLED`. The `// TODO(architect): remove LIVE_ON_DEMAND_ENABLED — 2026-10-15` below is therefore a TODO for a flag that was never created; it is now a month past its date and should be read as dead text. |
> | 3 — frontend honours sessions | **Not done** (§8). |
> | 4 — remove the old path | **Not done**, and partly reversed: `/resume` became the primary control instead of a deprecated no-op, and `scope_control`'s module-level set still exists. |
> | 5 — multi-worker | **Not done**, and §0.3 records that `f9b2378` made it harder. |
>
> **Unplanned work that did ship:** `9c14dc3` (2026-09-16), the
> `not_configured` honesty fixes (2026-09-17), and durable scope state plus
> freshness reporting in `f9b2378` (2026-10-17, migration `028`).
>
> The plan's own premise held up, though — "each phase is independently
> shippable" is exactly why the cheap first increment could be lifted out and
> shipped the next day without the rest. The cost of stopping there was §0.2.

Each phase is independently shippable and independently revertable.

### Phase 1 — Safe defaults + provenance (config only)

* Flip `MOCK_SCADA_ENABLED` → `False`, `MOCK_SCADA_SCOPES` → `""`.
* Add `MOCK_SCADA_PERSIST_ENABLED` (default `False`); `PersistenceBatcher` skips
  mock-origin events when it is off.
* Tag mock rows `source="live:mock"`.
* Set the demo service's env explicitly (§9.4), on a **demo tenant**.

**This phase alone satisfies requirement 1** and removes the ability for a
zero-config deploy to write synthetic rows into a real tenant. Ship it first and
alone.

Tests: default-config assertions (`MOCK_SCADA_ENABLED is False`,
`MOCK_SCADA_SCOPES == ""`); batcher drops mock events when
`MOCK_SCADA_PERSIST_ENABLED` is false; `wipe_live_demo_data` still matches
`live:mock` rows.

### Phase 2 — Session store, registry, and reconciler (backend)

* Redis session/lease store + `ScopeSupervisor` reconciler.
* `LiveRuntime` gains `start_scope` / `stop_scope`; `build_from_config` returns a
  registry of definitions instead of constructed connectors.
* New `/sessions` endpoints; `/status` extended.
* Behind `LIVE_ON_DEMAND_ENABLED` (default `false` for exactly one release) so
  the demo can keep running always-on while the new path is validated.
  `// TODO(architect): remove LIVE_ON_DEMAND_ENABLED and the always-on branch — 2026-10-15`

Tests: state-machine transitions; lease expiry with a fake clock; two-viewer
refcount; restart-with-viewers restarts generation; Redis-down fail-closed;
guardrail refuse/allow including the `source IS NULL` case; tenant enforcement
on heartbeat/release with a foreign key.

### Phase 3 — Frontend honours sessions

* `LiveToggle` start/stop sequence per §8; heartbeat interval; sendBeacon
  release; `useLiveReadings` gated on `sessionId`.
* New UI states (`idle` / `starting` / `refused` / `failed`).
* Flip `LIVE_ON_DEMAND_ENABLED=true` on the demo deploy.
* `/resume` becomes a deprecated no-op.

Tests: Playwright — toggle on issues the session POST before opening the
EventSource; 409 keeps the toggle off and shows the message; toggle off issues
the release; route change does not release. All five `web/` gates green
(`web/AGENTS.md`).

### Phase 4 — Remove the old path

* Delete the `LIVE_ON_DEMAND_ENABLED` branch and the always-on code.
* Delete `src/live/scope_control.py`'s module-level set; suppression is now
  "stopped with no session".
* Delete `POST /api/live/resume` and `resumeLiveScope` in
  `web/features/monitoring/api/live.ts:145-155`.
* Update `docs/live_scada_connectors.md` §8.

### Phase 5 — Multi-worker (only when scale demands it)

* Enforce the `live:generator` lease as a hard precondition for `start_scope`
  (the lease is already being acquired since Phase 2).
* Raise `--workers`; update `docs/live_scada_connectors.md` §8 to record that
  the single-worker invariant is lifted.
* Consider relocating `continuous` real-vendor connectors to `src/workers/`.

---

## 12. Open questions for the operator

1. **Real-source lifecycle** (§5.3) — confirm `continuous` is right for
   Priva/Hoogendoorn, i.e. that real measured data should keep flowing with no
   browser open.
2. **Should mock data persist at all?** `MOCK_SCADA_PERSIST_ENABLED=False` means
   the demo chart is live but its history vanishes on reload. If the demo needs
   durable history, enable it **on the demo tenant only**.
3. **Session TTL of 90 s** — chosen as 3× a 30 s heartbeat. A longer TTL is
   gentler on flaky networks; a shorter one stops generation sooner after a
   crash. 90 s bounds worst-case orphan generation to ~95 s, i.e. ~48 mock
   readings per source rather than the ~19,000 rows observed.

> **Status of these questions (2026-10-17).** Questions 1 and 2 were never put
> to the operator and are moot as asked: §5.3's per-kind lifecycle and
> `MOCK_SCADA_PERSIST_ENABLED` do not exist, so there is no setting to confirm.
> Question 3 is moot outright — there are no sessions and no TTL.
>
> The questions that replace them, as of this revision:
>
> 1. **Move the demo off `tenant_1`** (§9.1, §9.4). Still the cheapest
>    outstanding safety improvement, and `028` has raised its value by making a
>    mistaken `/resume` durable.
> 2. **Build §7's client-data guardrail**, or decide explicitly not to.
> 3. **Pick a fix for §0.3 before anyone raises the API replica count above 1.**
>    The decision recorded in §0.3 is to live with the constraint rather than fix
>    it, so this is a question only for whoever next wants to scale the API out.
>    Flooring the bucket timestamp in `_emit_bucket` is the cheap one, at the
>    cost of changing persisted timestamps.
> 4. ~~**Reconcile §2.3's row counts with the 2026-10-16 audit.**~~ **Closed
>    2026-10-17.** The counts are production figures (`AGENT_MEMORY.md:601-604`,
>    committed by `d34d2f5` alongside this ADR), and the rows were purged on
>    2026-09-15 via `wipe_live_demo_data`, thirty-one days before the audit. The
>    two measurements never conflicted; only the audit's "had ever been written"
>    phrasing did. See §2.3.
