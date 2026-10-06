# TASK 3 — PART C (qa-engineer): acceptance on native_dec_stuck_fault.csv

**STATUS: HELD. Do not dispatch until B2 is approved.** Depends on B2 merged.

Owner: qa-engineer (`tests/`, `docs/coverage/`). This brief was dictated by the
user and is recorded verbatim; it is not an orchestrator or agent design.

---

## Acceptance items

1. Four humidity stuck episodes, unchanged onsets, severity urgent, via sync
   AND async.
2. The 2026-12-18 13:00 episode read back FROM THE DATABASE:
   `end_ts` 2026-12-25 08:50, `duration_seconds` 589,800.
3. `detection_latency` for it queryable from the DB and equal to 6.00 h.
4. `frozen_sample_count` 1,897 and `raw_slot_count` 1,967 — confirm by
   measurement. If either differs, report the measured value and stop.
5. No phantom episode from the trailing 2-sample freeze.
6. Flip the strict xfail on episode `end_ts`; remove the marker.
7. A pinned test on the span values of all four episodes.
8. Idempotent re-upload: the same file twice gives the same row count.
9. Sync and async produce identical rows for this fixture.
10. Mixed-source rule: any synthetic reading in the window → episode
    `source = 'synthetic:<type>'`; with several types, the earliest by
    timestamp wins. Test it whether or not the fixtures reach it.
11. Detector level: only windows past the dual gate (hours AND samples) are
    exposed; counts are computed inside the detector.
12. Cadence branches: branch 1 pre-onset median when at least
    `CADENCE_PIN_MIN_INTERVALS` exist; otherwise branch 2, the in-freeze median
    pinned at gate fire. `CADENCE_EXPECTED_SECONDS` is never the cadence of a
    fired episode.
13. `raw_slot_count` on 900 s and 60 s files with a freeze at the first rows:
    the gap equals the rows deleted.
14. A freeze split across async chunks (split INSIDE the freeze, before the
    gate fires) gives the same pinned cadence and counts as single-pass.
15. `has_onset_fired` matches `'Z'` and `'+00:00'` onsets alike.
16. CHECK and unique constraint: a contradictory insert and a duplicate
    upsert key are rejected, on SQLite (and Postgres if supported).
17. `upload_portions.py` `:436` and `:515` go through `has_onset_fired`, not raw
    set membership; `from_dict` without `cadence_seconds` logs a WARNING.

**Regression:** `native_july_clearsky.csv` → zero stuck episodes. Fixture A
statistical counts unchanged. Full suite vs the 3432 baseline.

**Rules:** report measured values; never adjust code or expected values to make
a test pass; never edit `tests/fixtures/NATIVE_FIXTURES.md`; nothing committed
without approval.

---

## AMENDMENT NEEDED — items 1 and 8 count 4, the real figure is 5

Measured on fixture A after B1 and the cadence work, reproduced independently:
B2 persists **5 rows for `native_dec_stuck_fault.csv`, not 4**.

| channel | onset | branch | frozen | raw | gap | is_ongoing |
|---|---|---|---|---|---|---|
| humidity | 2026-12-14T00:00 | in_freeze | 419 | 419 | 0 | false |
| humidity | 2026-12-15T11:05 | pre_onset | 266 | 266 | 0 | false |
| humidity | 2026-12-16T11:20 | pre_onset | 576 | 576 | 0 | false |
| humidity | 2026-12-18T13:00 | pre_onset | 1897 | 1967 | 70 | false |
| **co2** | 2026-12-14T00:00 | in_freeze | 3387 | 3457 | 70 | **true** |

Three independent counts agree: `len(emitted_onsets)` = 5,
`len(completed_faults)` (4, all humidity) + open-and-fired (1, co2) = 5, and
the gate-fire INFO records = 5. By channel: temperature 0, humidity 4, co2 1.

co2 is frozen for the entire file (3387 samples, row 1 to EOF, never releases),
so it fires the dual gate and is persisted with `is_ongoing=true`, `release_ts`
NULL, `end_ts` = last frozen sample. Its gap of 70 is the same 70
adapter-dropped rows that give the 12-18 humidity episode its gap; both spans
cover them. Temperature opens ~2009 freezes but none reaches 6 h, so it fires
nothing.

Consequence: **item 1** ("four humidity stuck episodes") describes a
humidity-only subset, and **item 8** (re-upload row count) reads naturally as
4. Both must count against 5 total / 4 humidity / 1 co2, or they pass while
missing a row B2 writes. The exact wording is the user's call — this note
records the measurement, it does not change the items above.

---

## Notes for whoever dispatches this

Item 17 names a real latent defect, confirmed in the code: `upload_portions.py`
`:436` and `:515` test `key in stuck_carry.emitted_onsets` directly, bypassing
the normalisation in `has_onset_fired`. It is correct today only because both
call sites build the key from a canonical `state.onset_ts`, and returns a
silent wrong answer the moment a key arrives off the wire. That file is
backend-engineer's, so the fix belongs to B2 and this item is the test that
guards it.

Item 4's figures come from measurement, not from `NATIVE_FIXTURES.md`'s
documented 1,967 alone: `CSVAdapter._handle_nan`
(`src/vendors/csv_adapter.py:311-320`) drops rows whose required sensor columns
are null, so the fixture's 70 null-humidity rows never reach the detector
(3457 rows in, 3387 out). `frozen_sample_count` counts observed frozen samples
(1,897); `raw_slot_count` is EXPECTED slots at the pinned cadence
(589800/300 + 1 = 1,967). The gap of 70 is therefore "missing readings,
whether dropped upstream or never logged" — not a count of consumed rows.

Item 12's "never the cadence of a fired episode" is scoped to the PINNING
paths. Two non-pinning paths to `CADENCE_EXPECTED_SECONDS` remain by design:
the `ChannelFreezeState.cadence_seconds` field default, required so the
existing `tests/models/test_stuck_carry_serialization.py` constructions keep
working, and `from_dict`'s absent-key tolerance for deploy skew — which item 17
requires to log at WARNING.
