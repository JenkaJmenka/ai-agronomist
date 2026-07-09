# Performance Report — Upload/Train/Navigate run (2026-07-09)

Job `0d252089-8125-4f2e-91f9-40c9d4a3eb44` · tenant_1/gh_A · ~104k rows
Timezone: your local = **UTC+1**. All log times below are UTC.

## Verdict on the question you asked ("any DB/BE delays?")

- **Navigation & rendering: DB/BE is NOT the bottleneck.** During the monitoring/climate navigation window (11:47–11:50Z) the API was essentially idle — sparse calls, all 6–333 ms. The 2-minute monitoring render is **client-side (Streamlit dashboard service)**, not the API or Postgres.
- **Ingest & training: there ARE two real server-side costs** — the synchronous `/api/upload` (37.6 s) and the worker `produce_portions` phase (29.7 s). Both are fixable.
- **One real bug** on the Monitoring page: a `@st.cache_data` DataFrame with `list`-valued cells fails to hash and falls back to **pickling on every rerun**.

## End-to-end timeline (112 s wall clock)

| Phase | Duration | Where |
|---|---|---|
| `POST /api/upload` (sync ingest, Upload-Ops page) | **37.6 s** | api |
| `POST /api/inference/upload-async` → 202 | **7.6 s** | api |
| worker `upload.prepare` | **42.5 s** | worker |
| — bootstrap pass1 load+resample (105,120 rows) | 0.6 s | |
| — bootstrap pass1 physics_flags | 1.6 s | |
| — bootstrap pass1 isolation_forest | 0.24 s | |
| — bootstrap pass2 train (5000 seq, 10 ep, CPU) | 7.5 s | |
| — prepare.train total (incl. checkpoint store) | 11.3 s | |
| — **prepare.produce_portions** | **29.7 s** | ← 70% of prepare |
| worker `upload.portions` ×22 | score 0.2–1.2 s + **persist 1.6–2.6 s** each (pipelined, prefetch=2) | worker |
| worker `upload.finalize` | 0.5 s | |

Job done ~11:38:49Z. Your perceived "12:37→12:38" ≈ 112 s actual.

## Findings, ranked by impact

### 1. `produce_portions` = 29.7 s — biggest single server phase
70% of the 42.5 s prepare. Splitting scored data into 22 portions + publishing them dominates cold-start. Worth profiling internally (serialization? per-portion blob writes? re-scoring?).

### 2. Sync `/api/upload` = 37.6 s — double `iterrows()` over 104k rows
`src/api/routes/upload.py` iterates **every row twice** with `df.iterrows()` (once for `SensorReading`, once for `RawReading`), building dicts and committing per 5000-row chunk (~42 commits). `iterrows()` is the classic pandas anti-pattern.
- Fix: replace with `df.to_dict("records")` / vectorized construction; batch commits.

### 3. The file is uploaded **twice** from the browser, same CSV
Sync `/api/upload` (Upload-Ops page, `5_upload_operations.py`) **and** async `upload-async` (Anomalies page, `2_anomalies.py`, `persist_data=True`) both ingest the same file. `SensorReading` is written by both (async fills anomaly scores via `preserve_when_null` upsert). Redundant network transfer + redundant DB writes.
- Fix: consolidate to the async path only, or have Upload-Ops hand the already-stored blob to the async job instead of re-uploading.

### 4. Per-portion `persist` ~2 s × 22 dominates portion cost
`score` is fast (0.2–1.2 s); `persist` is 1.6–2.6 s each. DB write throughput is the portion bottleneck.
- Check: upsert batching, index/constraint on `(tenant_id, greenhouse_id, timestamp)`, connection pooling.

### 5. BUG — Monitoring page: unhashable `list` in `@st.cache_data`
`src/dashboard/views/1_Active_Monitoring/1_Monitoring.py` (near line 247). Log:
`Pandas DataFrame hash failed. Falling back to pickling the object.` (11:45:22, 11:48:30).
A cached function receives a DataFrame with `list`/`dict` cells (looks like the `RawReading.data` JSON column). pandas can't hash list cells → Streamlit **pickles the whole DataFrame to compute the cache key on every rerun** — a large repeated cost contributing to the 2-min render.
- Fix: don't pass list/dict-column DataFrames into `@st.cache_data`; drop/serialize those columns first, or use a `hash_funcs=` override, or cache the narrow numeric frame only.
- Also present: a `check_session_state_rules` widget warning (checkbox at line ~218) — a `session_state` default colliding with a widget `key`. Minor, but worth cleaning.

### 6. Job polling every ~2 s
Frontend polls `GET /api/jobs/{id}` every 2 s. Each is 3–6 ms so it's cheap, but consider backoff or SSE if you want to reduce chatter.

## Bottom line
- **DB/BE is fine for reads/navigation.** The monitoring slowness is Streamlit-side (finding #5 + Streamlit's full-script reruns).
- **Ingest is heavy but fixable**: kill the double upload (#3), vectorize `/api/upload` (#2), profile `produce_portions` (#1). Those three alone should roughly halve the ~112 s cold-start.
