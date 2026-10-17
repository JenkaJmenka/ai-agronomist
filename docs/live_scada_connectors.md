# Live SCADA Connectors — Integration Guide

This document covers the two production SCADA connectors shipped in Phase 5 of Live Monitoring:
**PrivaConnector** (OAuth2 client-credentials) and **HoogendoornConnector** (LetsGrow REST + API key).

---

## 1. Architecture Overview

Each physical data source is a **connector** (`LiveSourceConnector` subclass) that polls its
vendor REST API on a configurable interval and emits `SourceReading` objects. Within each
greenhouse scope (`tenant_id` / `greenhouse_id` pair) all connectors funnel readings through a
single **`ChannelMerger`**, which aligns readings into time buckets, resolves per-channel
conflicts by priority, and emits merged `LiveReadingEvent` objects. The **`LiveRuntime`**
supervises every long-running coroutine (connectors, mergers, and the persistence batcher) with
an exponential-backoff restart loop so transient vendor failures are handled automatically. The
**`LiveHub`** publishes each `LiveReadingEvent` to a per-scope Redis Stream, and the
**SSE endpoint** (`GET /api/live/events`) replays and streams those events to browser clients.

### Per-scope multi-source model

A single greenhouse scope can have **any number of connectors** — for example, one Priva
connector supplying temperature, humidity, and light, alongside a separate Hoogendoorn connector
supplying CO2. Both connectors run independently; the merger resolves which value wins when two
sources report the same channel. Scopes are created automatically: every unique
`(tenant_id, greenhouse_id)` combination across all registered sources becomes one scope.

---

## 2. Registering Connectors via `LIVE_SOURCES_JSON`

Set the `LIVE_SOURCES_JSON` environment variable to a JSON array of **`SourceConfig`** objects.
The runtime parses this at startup (`LiveRuntime.build_from_config`). Unknown vendors are logged
and skipped; malformed objects are logged and skipped.

### `SourceConfig` fields

| Field | Type | Default | Description |
|---|---|---|---|
| `source_id` | `str` | required | Unique identifier for this connector instance |
| `vendor` | `str` | required | `"priva"` or `"hoogendoorn"` (or `"mock"`) |
| `tenant_id` | `str` | required | Tenant scope |
| `greenhouse_id` | `str` | required | Greenhouse scope |
| `channels` | `list[str]` | required | Canonical channels this source provides |
| `priority` | `int` | `100` | Lower number wins per-channel conflicts |
| `poll_interval_seconds` | `float` | `5.0` | Polling cadence and health-check baseline |
| `params` | `dict` | `{}` | Vendor-specific configuration (see sections 3 and 4) |

### Single Priva source

```json
LIVE_SOURCES_JSON='[
  {
    "source_id": "priva-gh-a-climate",
    "vendor": "priva",
    "tenant_id": "acme",
    "greenhouse_id": "gh_A",
    "channels": ["temperature", "humidity", "light"],
    "priority": 10,
    "poll_interval_seconds": 5.0,
    "params": {
      "base_url": "https://api.priva.com",
      "client_id": "acme-client",
      "client_secret_env": "PRIVA_CLIENT_SECRET",
      "installation_id": "inst-42",
      "zone_id": "zone-north"
    }
  }
]'
```

### Single Hoogendoorn source

```json
LIVE_SOURCES_JSON='[
  {
    "source_id": "hoogendoorn-gh-a-co2",
    "vendor": "hoogendoorn",
    "tenant_id": "acme",
    "greenhouse_id": "gh_A",
    "channels": ["co2"],
    "priority": 20,
    "poll_interval_seconds": 10.0,
    "params": {
      "base_url": "https://api.letsgrow.com",
      "api_key_env": "LETSGROW_API_KEY",
      "installation_id": "lgw-7890"
    }
  }
]'
```

### Multi-source greenhouse (Priva climate + Hoogendoorn CO2)

This is the recommended production pattern. Both connectors share the same scope
(`acme` / `gh_A`). The merger will use Priva (priority 10) for temperature, humidity, and light,
and Hoogendoorn (priority 20) for CO2. If Priva also reports a CO2 value it would lose to
Hoogendoorn because 20 > 10 (higher number = lower priority = loses).

```json
LIVE_SOURCES_JSON='[
  {
    "source_id": "priva-gh-a-climate",
    "vendor": "priva",
    "tenant_id": "acme",
    "greenhouse_id": "gh_A",
    "channels": ["temperature", "humidity", "light"],
    "priority": 10,
    "poll_interval_seconds": 5.0,
    "params": {
      "base_url": "https://api.priva.com",
      "client_id": "acme-client",
      "client_secret_env": "PRIVA_CLIENT_SECRET",
      "installation_id": "inst-42",
      "zone_id": "zone-north"
    }
  },
  {
    "source_id": "hoogendoorn-gh-a-co2",
    "vendor": "hoogendoorn",
    "tenant_id": "acme",
    "greenhouse_id": "gh_A",
    "channels": ["co2"],
    "priority": 20,
    "poll_interval_seconds": 10.0,
    "params": {
      "base_url": "https://api.letsgrow.com",
      "api_key_env": "LETSGROW_API_KEY",
      "installation_id": "lgw-7890"
    }
  }
]'
```

**Priority rule:** lower `priority` number wins. In this example Priva (10) outranks
Hoogendoorn (20). To make Hoogendoorn the authoritative source for a channel, assign it a
lower number.

---

## 3. Credential Handling

Secrets are **never stored as literal values** in `LIVE_SOURCES_JSON`. Instead, `params` holds
the **name** of an environment variable, and the connector reads it at `connect()` time. If the
env var is unset or empty the connector raises `DataSourceConnectionError` and the runtime logs
the error, applies backoff, and retries.

### Priva — required `params` keys

| Key | Type | Description |
|---|---|---|
| `base_url` | `str` | Priva API root, e.g. `https://api.priva.com` |
| `client_id` | `str` | OAuth2 client identifier (not a secret) |
| `client_secret_env` | `str` | **Env-var name** whose value is the OAuth2 client secret |
| `installation_id` | `str` | Priva installation identifier |
| `zone_id` | `str` | Zone within the installation to poll |

**Required environment variables:**

```
PRIVA_CLIENT_SECRET=<your OAuth2 client secret>
```

*(The env var name is whatever you put in `client_secret_env`; `PRIVA_CLIENT_SECRET` is the
recommended name.)*

### Hoogendoorn — required `params` keys

| Key | Type | Description |
|---|---|---|
| `base_url` | `str` | LetsGrow API root, e.g. `https://api.letsgrow.com` |
| `api_key_env` | `str` | **Env-var name** whose value is the LetsGrow API key |
| `installation_id` | `str` | LetsGrow installation identifier |
| `channel_map` | `dict` *(optional)* | Per-installation tag overrides — see section 4 |

**Required environment variables:**

```
LETSGROW_API_KEY=<your LetsGrow API key>
```

*(The env var name is whatever you put in `api_key_env`; `LETSGROW_API_KEY` is the
recommended name.)*

---

## 4. Channel Mapping Tables

All connectors normalise vendor-specific field/tag names to the four **canonical channels** used
throughout the pipeline. Values are validated to be finite and clamped to the bounds defined in
`CHANNEL_BOUNDS` (`src/live/events.py`):

| Canonical channel | Bounds |
|---|---|
| `temperature` | 0.0 – 50.0 °C |
| `humidity` | 0.0 – 100.0 % |
| `co2` | 0.0 – 1000.0 ppm |
| `light` | 0.0 – 2000.0 lux |

Entries outside these bounds are clamped (not rejected) so a single out-of-range spike does not
drop an entire reading.

### Priva — raw field → canonical (fixed mapping)

The mapping is hardcoded in `_FIELD_MAP` (`src/live/sources/priva.py`) and mirrors the column
renaming in `src/vendors/priva_client.py`. It cannot be overridden per installation.

| Priva raw field | Canonical channel |
|---|---|
| `ambient_temp` | `temperature` |
| `rel_humidity` | `humidity` |
| `co2_level` | `co2` |
| `radiation_lux` | `light` |

Fields absent from the metric dict or with non-numeric / non-finite values are silently skipped.
The Priva connector looks for the timestamp in `metric["time"]` and falls back to
`metric["timestamp"]`.

### Hoogendoorn — tag → canonical (configurable)

The default mapping (`DEFAULT_CHANNEL_MAP` in `src/live/sources/hoogendoorn.py`) covers the
most common LetsGrow tag names:

| LetsGrow tag | Canonical channel |
|---|---|
| `Air Temp` | `temperature` |
| `RH` | `humidity` |
| `CO2` | `co2` |
| `Solar Radiation` | `light` |

**Per-installation override:** LetsGrow tag names vary by grower configuration. To add or
replace entries, supply `channel_map` in `params`. Your dict is **merged on top** of the
defaults at `connect()` time (`{**DEFAULT_CHANNEL_MAP, **custom_map}`), so you only need to
specify the tags that differ:

```json
"params": {
  "base_url": "https://api.letsgrow.com",
  "api_key_env": "LETSGROW_API_KEY",
  "installation_id": "lgw-7890",
  "channel_map": {
    "Temp In": "temperature",
    "Hum In": "humidity",
    "CO2 Meas": "co2",
    "Rad In": "light"
  }
}
```

Unknown tags (not in the resolved map) are logged at `DEBUG` and skipped. Each LetsGrow
measurement item produces **at most one `SourceReading` with one channel**, which lets the
merger apply per-channel priority resolution across sources independently.

---

## 5. Source Health and Priority Semantics

### Health thresholds

The `ChannelMerger` (`src/live/merge.py`) tracks the monotonic time since the last reading
was received from each source. Thresholds are multiples of the source's
`poll_interval_seconds`:

| Threshold | Multiplier | Status transition |
|---|---|---|
| `3 × poll_interval_seconds` | `_DEGRADED_MULTIPLIER = 3.0` | `online` → `degraded` |
| `10 × poll_interval_seconds` | `_OFFLINE_MULTIPLIER = 10.0` | `degraded` → `offline` |
| `< 3 × poll_interval_seconds` | — | any → `online` (recovery) |

A `SourceStatusEvent` is emitted **only on status transitions** — repeated polls in the same
state produce no events. Status events flow through the `LiveHub` and appear in the SSE stream
as `event: source-status`.

**Example:** a Priva source with `poll_interval_seconds: 5.0` transitions to `degraded` after
15 s of silence and to `offline` after 50 s.

### Per-channel priority conflict rules

When multiple sources in the same scope report the same channel within the same time bucket,
the `_BucketEntry.accept()` method in `ChannelMerger` applies:

1. **Lower `priority` number wins.** If the incoming reading has a higher number than what is
   already in the bucket, it is discarded.
2. **Equal priority → last-write-wins.** The reading most recently submitted to the merger's
   inbox overwrites the existing value.

The winning `source_id` is recorded in `LiveReadingEvent.sources[channel]` so downstream
consumers can see which source contributed each channel.

### Connector-level backoff

`PollingSourceConnector` (`src/live/sources/polling.py`) has its own independent backoff for
`fetch_since` failures: starts at 1 s, doubles on each consecutive error, caps at
**300 s** (`_MAX_BACKOFF_SECONDS = 300.0`). Resets to 1 s after the next successful fetch.

The `LiveRuntime` supervisor (`_supervised()` in `src/live/runtime.py`) wraps the entire
connector coroutine with a separate restart backoff: starts at 1 s, doubles, caps at
**60 s** (`_MAX_BACKOFF_SECONDS = 60.0`).

### Grace-period buffering

The merger waits `LIVE_MERGE_GRACE_SECONDS` (default **1.0 s**, env var
`LIVE_MERGE_GRACE_SECONDS`) after a bucket's end time before emitting the merged reading.
This absorbs small clock skew between vendors whose polling intervals are not synchronised.
Readings that arrive after a bucket has already been emitted are dropped and counted in
`ChannelMerger.dropped_late_count`.

---

## 6. Go-Live Checklist

### Priva

1. **Obtain credentials** — get `client_id` and `client_secret` from the Priva developer
   portal for your installation.
2. **Set the secret env var** — e.g. `PRIVA_CLIENT_SECRET=<secret>` on your deployment
   (Railway, Docker, etc.).
3. **Configure `LIVE_SOURCES_JSON`** — add a Priva entry (see section 2) with
   `client_secret_env: "PRIVA_CLIENT_SECRET"`, your `installation_id`, and `zone_id`.
4. **Sandbox poll verification** — run the API with `MOCK_SCADA_ENABLED=false` and tail the
   logs for `live.sources.priva`. Confirm `Priva token refreshed` appears on startup and
   readings begin flowing.
5. **Verify channel mapping** — confirm that `ambient_temp`, `rel_humidity`, `co2_level`, and
   `radiation_lux` fields exist in the Priva response. If your installation uses different field
   names, raise an issue — `_FIELD_MAP` is hardcoded and requires a code change.
6. **Tune priority** — if this is the authoritative climate source, set `priority` lower than
   any backup source for the same channels.
7. **Monitor** — call `GET /api/live/status?tenant_id=<t>&greenhouse_id=<gh>` to check
   `sources[].status` ("online" / "degraded" / "offline").

### Hoogendoorn (LetsGrow REST)

1. **Obtain credentials** — get an API key from the LetsGrow portal for your installation.
2. **Set the secret env var** — e.g. `LETSGROW_API_KEY=<key>`.
3. **Configure `LIVE_SOURCES_JSON`** — add a Hoogendoorn entry with
   `api_key_env: "LETSGROW_API_KEY"` and your `installation_id`.
4. **Sandbox poll verification** — tail `live.sources.hoogendoorn` logs. You should see
   readings begin flowing. Watch for DEBUG-level `unknown tag` messages — they indicate tags
   present in the API response that are not in the channel map.
5. **Verify and tune channel mapping** — if tag names differ from the defaults (`Air Temp`,
   `RH`, `CO2`, `Solar Radiation`), supply a `channel_map` override in `params`. Iterate
   until all four canonical channels appear in readings.
6. **Tune priority** — assign relative to any co-located Priva or other sources.
7. **Monitor** — call `GET /api/live/status?tenant_id=<t>&greenhouse_id=<gh>`.

---

## 7. Future: Hoogendoorn On-Prem OPC-UA

Hoogendoorn hardware exposes an on-prem OPC-UA server in addition to the cloud LetsGrow REST
path. An on-prem variant would subscribe to OPC-UA node-change notifications and **push**
readings as they arrive, mapping natively to the push-mode `stream()` interface of
`LiveSourceConnector` (subscribe once at `connect()`; yield on each node-change notification in
`stream()`). This is cleaner than polling and eliminates cursor management.

The current connector (`HoogendoornConnector`, `vendor="hoogendoorn"`) targets the cloud
**LetsGrow REST path only** — `PollingSourceConnector` handles the poll loop, since-cursor, and
deduplication. The OPC-UA variant is a planned future addition and would register under a
distinct vendor string (e.g. `"hoogendoorn-opcua"`) so both can coexist in `CONNECTOR_TYPES`.

---

## 8. Deployment Constraints

### Single-worker requirement for live data generation

`LiveRuntime` is created inside the FastAPI lifespan (`src/api/main.py`).  This means **each
uvicorn worker process builds its own runtime** — if you scale to `--workers N`, you get N
independent runtimes all generating and publishing to the same Redis stream.  The result:

- **Mock sources:** N× duplicate readings, divergent values for the same bucket (each mock
  has independent randomness), N× mock anomaly events.
- **Persistence:** The `ON CONFLICT DO NOTHING` dedup guard prevents duplicate DB rows, but
  the SSE anomaly strip and event volume multiply by N.
- **Real SCADA connectors:** N separate pollers will each hold their own OAuth tokens and
  call the vendor API N× more often than intended.  For Priva/Hoogendoorn this can exhaust
  rate limits.

**Current guidance: deploy the API as a single worker** while live generation runs in-process.

```sh
# Single-worker deploy (Railway / Docker)
uvicorn src.api.main:app --host :: --port 8000 --workers 1
```

#### Scaling options (future)

Two paths exist when horizontal scale is needed:

1. **Redis leader lock** — `SET live:leader NX EX <ttl>` at startup; only the lock-holder
   runs `LiveRuntime`.  All workers serve SSE from the shared Redis stream.  Requires a
   renewal task to re-acquire on the leader's death.

2. **Move generation to `src/workers/`** — extract `LiveRuntime` into a standalone worker
   process (same pattern as `src/workers/main.py`).  The API process only subscribes to the
   Redis stream; generation is not duplicated regardless of API worker count.  This is also
   the correct architecture for real SCADA connectors in production, which belong in a
   long-lived background process rather than inside the request-serving API.

Fan-out via the `LiveHub` (Redis Streams `XREAD`) is multi-worker safe by design — any
number of API workers can subscribe and serve SSE to different browser clients from the same
stream.  It is only *generation* that must run exactly once.

---

### Mixed-cadence scope guidance

When a single greenhouse scope contains sources with different polling intervals (e.g. a Priva
climate source at 30 s and a Hoogendoorn CO2 source at 120 s), the `ChannelMerger` derives
its timing parameters **per scope**:

| Parameter | Derived value | Rationale |
|---|---|---|
| `emit_interval` | `min(poll_interval_seconds)` across scope sources | Bucket width matches the fastest source so no readings are unnecessarily delayed |
| `grace_seconds` | `max(LIVE_MERGE_GRACE_SECONDS, max(poll_interval_seconds))` | The grace window must be at least as wide as the slowest source's poll interval; otherwise its readings (timestamped up to one full interval in the past) arrive after the bucket has already been flushed and are **silently dropped as late** |

**Example:** Priva (30 s) + Hoogendoorn (120 s) in the same scope:
- `emit_interval = 30 s` — buckets are 30 s wide
- `grace_seconds = max(1.0 s, 120 s) = 120 s` — the merger waits 120 s after a bucket ends
  before flushing, long enough for the Hoogendoorn 120 s reading to arrive

> **Note:** If `LIVE_MERGE_GRACE_SECONDS` (default 1.0 s) is larger than the slowest source
> interval (e.g. a fast mock scope with 0.05 s intervals), the config default wins.  The
> formula is `max(config, max_poll_interval)` so the grace is never *smaller* than the config
> floor.

**Warm-up behaviour:** During the first few seconds after startup, a scope may only have
readings from its fast source.  These single-channel merged buckets are complete from the
merger's perspective but will be **skipped by the persistence batcher** (see section 5 below)
because the slow source's channels are absent.  This is by design — fabricating `0.0` for a
missing CO2 channel would corrupt the training/analytics dataset.

**`LIVE_MERGE_GRACE_SECONDS` env var** still applies as a floor.  For real-vendor deployments
with slow pollers (≥60 s), the per-scope derivation handles the grace automatically and you do
not need to raise `LIVE_MERGE_GRACE_SECONDS` manually.
