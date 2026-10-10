TASK: Write one Incident per episode instead of one per sequence. Phase 2 of the
convergence design.

Read the "Incident write strategy" section of
`docs/architecture/upload_detector_convergence.md` and
`docs/architecture/episode_grouping.md` first.

## Prerequisite

Step 4 (detector fan-out) landed and is within budget. Confirm:

    venv/bin/python -m pytest tests/api/test_detector_fanout.py -q --timeout=600

## The problem

`ComprehensiveInferencePipeline._store_and_observe` writes an `Incident` per sequence via
`IncidentMemory` (`src/api/services/comprehensive_pipeline.py:1634`). At 105k sequences
that is not viable, and it is why the comprehensive pipeline was never brought to the
upload path.

The design's estimate: for the Dec 14–26 fixture, per-sequence writes would be **2,000+**
incidents (every stuck-fault and recovery sequence) versus **4–8 episodes**. Treat 2,000+
as an unverified estimate — measure the real number and report it.

Meanwhile the upload path already groups alerts into episodes
(`src/models/episode_grouper.py`, added 2026-09-09). Each episode is one logical event
with channel, start, end, duration, peak score, sample count and severity. That is
exactly the granularity an incident should have.

## What to implement

1. Write one `Incident` per episode on the upload path, not per sequence. Use the existing
   `IncidentMemory` (`src/db/incident_memory.py`) rather than writing SQL directly.

2. Populate the incident from the episode: channel, detector, severity, start/end,
   duration, peak score, sample count. Where the episode is a stuck fault, the frozen
   duration and the frozen value are the operator-relevant facts — do not lose them.
   `Incident.fault_type` follows the taxonomy in `src/db/models.py:204`; map
   `stuck_fault` correctly (`docs/ANOMALY_CATALOG.md:151` classifies it `urgent`).

3. Batch the writes. One transaction for a file's episodes, not one per episode. Guard
   non-finite floats before any JSON-column write — SQLite tolerates NaN, production
   PostgreSQL returns a 500. Follow the existing sanitisation pattern rather than
   inventing one.

4. **Do not break the group/enriched endpoints.** `/api/anomalies/groups` and
   `/api/anomalies/enriched` join alerts to incidents within a ±15-minute window
   (`_INCIDENT_WINDOW_MINUTES`). An episode-level incident has a span, not an instant —
   check how that join behaves and adjust it if a long episode would now fail to match
   its own alerts. `tests/api/test_anomaly_groups_parity.py` and
   `test_enriched_parity.py` pin this behaviour; both must keep passing.

5. **Embedding choice — record your decision.** The design flags that using the
   peak-score sequence's embedding as the episode representative may not capture the
   episode's character: a stuck fault's embedding comes from a zero-variance window,
   which is informative but very different from the recovery window. Pick an approach,
   justify it in `docs/architecture/episode_grouping.md`, and state plainly that
   similarity-search quality is unvalidated until evaluated empirically. Do not claim it
   is good.

## Explicitly out of scope

- Do NOT wire in `ComprehensiveInferencePipeline`.
- Do NOT change episode boundaries, the recovery score cap, the stuck-fault gate, the
  6.00 h measured latency, or any frozen-duration value.
- Do NOT add a migration unless the `Incident` model genuinely lacks a needed column —
  and if it does, say why in the done signal rather than adding one silently.

## Acceptance

Test file: `tests/api/test_episode_incidents.py`

  - running the December fixture through the upload path writes one incident per episode,
    not per sequence — assert the count equals the episode count
  - **report the measured before/after incident counts** for that fixture
  - the stuck-fault incident carries channel, frozen duration and `urgent` severity
  - incidents are written in one transaction, not N
  - non-finite values never reach a JSON column
  - `/api/anomalies/groups` and `/api/anomalies/enriched` still return the same shape and
    still match alerts to their incidents — both parity suites pass
  - a long episode (the 163.83 h stuck fault) matches its own alerts through the incident
    join

## Regression

Expected failure remains the SQLite NaN one in `test_023_migration.py`. Leave it.

    venv/bin/python -m pytest tests/ -q --timeout=900 --ignore=tests/load

Confirm unchanged: stuck latency 6.00 h, frozen durations 34.83 / 22.08 / 47.92 /
163.83 h, recovery capped below the fault.

When complete:
mkdir -p tasks/done
echo '{"agent":"backend-engineer","status":"done","output":"src/api/services/upload_portions.py","tests_passed":true,"incidents_before":"REPLACE","incidents_after":"REPLACE","embedding_choice":"REPLACE","migration_added":"REPLACE"}' > tasks/done/backend-engineer.json
On failure:
echo '{"agent":"backend-engineer","status":"failed","reason":"brief"}' > tasks/done/backend-engineer.json
