/**
 * Contract tests for the episode zod schema — specifically duration_seconds
 * going from a required number to a nullable one.
 *
 * HISTORY: the API widened duration_seconds to Optional[float] so a
 * stuck-fault episode with no matching `anomaly_episodes` row can report
 * "unknown" instead of a fabricated 0.0. zod threw on a bare `null` under the
 * old `z.number()` schema, which took down the whole anomalies page for any
 * tenant with a pre-migration-026 stuck fault. These tests pin the fix.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchAnomalyEpisodes } from "@/features/anomalies/api/episodes";

const BASE_PARAMS = {
  tenant: "tenant_1",
  gh: "gh_A",
  start: "2026-06-01T00:00:00+00:00",
  end: "2026-06-08T00:00:00+00:00",
  threshold: 0.5,
};

/** Minimal valid episode, overridable per test. */
function buildEpisodePayload(overrides: Record<string, unknown> = {}) {
  return {
    episode_id: "temperature:stuck:2026-06-01T00:00:00Z",
    channel: "temperature",
    detector: "stuck",
    direction: null,
    start_ts: "2026-06-01T00:00:00+00:00",
    end_ts: "2026-06-01T00:00:00+00:00",
    duration_seconds: 0,
    sample_count: 1,
    peak_score: 0.9,
    peak_ts: "2026-06-01T00:00:00+00:00",
    severity: "critical",
    first_value: null,
    last_value: null,
    peak_value: null,
    trend: "flat",
    movement: "steady",
    slope_per_hour: null,
    affected_sensors: ["temperature"],
    headline: "Stuck fault",
    is_truncated_start: false,
    is_truncated_end: false,
    samples: [],
    frozen_sample_count: null,
    raw_slot_count: null,
    ...overrides,
  };
}

function mockFetchOk(body: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => body,
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchAnomalyEpisodes — duration_seconds nullability", () => {
  it("parses a null duration_seconds instead of throwing", async () => {
    mockFetchOk({
      episodes: [buildEpisodePayload({ duration_seconds: null })],
      episode_count: 1,
      anomaly_count: 1,
    });

    const result = await fetchAnomalyEpisodes(BASE_PARAMS);

    expect(result.episodes[0]?.duration_seconds).toBeNull();
  });

  it("still parses a real zero duration as 0, not null", async () => {
    mockFetchOk({
      episodes: [buildEpisodePayload({ duration_seconds: 0 })],
      episode_count: 1,
      anomaly_count: 1,
    });

    const result = await fetchAnomalyEpisodes(BASE_PARAMS);

    expect(result.episodes[0]?.duration_seconds).toBe(0);
  });
});

describe("fetchAnomalyEpisodes — frozen_sample_count / raw_slot_count", () => {
  it("surfaces both stuck-fault-only count fields when present", async () => {
    mockFetchOk({
      episodes: [buildEpisodePayload({ frozen_sample_count: 42, raw_slot_count: 48 })],
      episode_count: 1,
      anomaly_count: 1,
    });

    const result = await fetchAnomalyEpisodes(BASE_PARAMS);

    expect(result.episodes[0]?.frozen_sample_count).toBe(42);
    expect(result.episodes[0]?.raw_slot_count).toBe(48);
  });

  it("surfaces both as null for a non-stuck-fault episode", async () => {
    mockFetchOk({
      episodes: [buildEpisodePayload({ frozen_sample_count: null, raw_slot_count: null })],
      episode_count: 1,
      anomaly_count: 1,
    });

    const result = await fetchAnomalyEpisodes(BASE_PARAMS);

    expect(result.episodes[0]?.frozen_sample_count).toBeNull();
    expect(result.episodes[0]?.raw_slot_count).toBeNull();
  });

  it("folds a KEY THAT IS ABSENT ENTIRELY into null, not undefined", async () => {
    // Regression: GET /episodes is cached for REDIS_CACHE_EXPIRE_SECONDS
    // (300s, src/config.py). A response cached by the pre-this-change backend
    // has no frozen_sample_count/raw_slot_count keys at all — not even a
    // null one. A plain .nullable() schema rejects `undefined`, so for up to
    // 5 minutes after deploy a stale cache entry would throw here exactly
    // like the duration_seconds bug this task started from.
    const payload = buildEpisodePayload();
    delete (payload as Record<string, unknown>).frozen_sample_count;
    delete (payload as Record<string, unknown>).raw_slot_count;
    mockFetchOk({
      episodes: [payload],
      episode_count: 1,
      anomaly_count: 1,
    });

    const result = await fetchAnomalyEpisodes(BASE_PARAMS);

    expect(result.episodes[0]?.frozen_sample_count).toBeNull();
    expect(result.episodes[0]?.raw_slot_count).toBeNull();
  });
});
