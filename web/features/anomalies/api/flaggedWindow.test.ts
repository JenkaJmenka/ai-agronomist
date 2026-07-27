/**
 * Regression tests for flaggedWindow.ts query-builder contract.
 *
 * HISTORY: max_points=1 was sent to minimise data transfer, but the backend
 * enforces ge=DOWNSAMPLE_MAX_POINTS_FLOOR (100) and returns HTTP 422 for any
 * lower value. The anomaly table view broke 100% of the time with "Could not
 * determine anomaly count". These tests assert the outgoing query params are
 * within the backend's declared contract so that class of regression is caught
 * before it ships.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DOWNSAMPLE_MAX_POINTS_FLOOR,
  fetchAnomalyCount,
  fetchFlaggedWindow,
  SERVER_GROUPING_THRESHOLD,
} from "@/features/anomalies/api/flaggedWindow";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Minimal valid server response for the anomalies-in-window endpoint. */
function anomaliesResponse(overrides: Record<string, unknown> = {}) {
  return {
    rows: [],
    anomaly_count: 0,
    total_count: 0,
    ...overrides,
  };
}

function mockFetchOk(body: unknown): URL {
  let captured: URL | null = null;
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: RequestInfo | URL) => {
      captured = new URL(String(input), "http://localhost");
      return Promise.resolve({
        ok: true,
        json: async () => body,
      });
    }),
  );
  // Return a proxy that reads captured lazily so callers can inspect after await.
  return new Proxy({} as URL, {
    get(_target, prop) {
      if (!captured) throw new Error("fetch was not called");
      return (captured as unknown as Record<string | symbol, unknown>)[prop];
    },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const BASE_PARAMS = {
  tenant: "tenant_1",
  gh: "gh_A",
  start: "2026-06-01T00:00:00+00:00",
  end: "2026-06-08T00:00:00+00:00",
  threshold: 0.5,
};

// ---------------------------------------------------------------------------
// Contract: DOWNSAMPLE_MAX_POINTS_FLOOR constant value
// ---------------------------------------------------------------------------

describe("DOWNSAMPLE_MAX_POINTS_FLOOR", () => {
  it("equals 100 — the backend ge constraint declared in src/config.py", () => {
    // If this fails, update BOTH the Python config and this constant together.
    expect(DOWNSAMPLE_MAX_POINTS_FLOOR).toBe(100);
  });

  it("is less than or equal to SERVER_GROUPING_THRESHOLD", () => {
    // The floor must never exceed the grouping threshold or the count-only
    // fetch would transfer more data than the full flagged-rows fetch.
    expect(DOWNSAMPLE_MAX_POINTS_FLOOR).toBeLessThanOrEqual(SERVER_GROUPING_THRESHOLD);
  });
});

// ---------------------------------------------------------------------------
// Contract: fetchAnomalyCount outgoing max_points
// ---------------------------------------------------------------------------

describe("fetchAnomalyCount — max_points query param", () => {
  it("sends max_points >= DOWNSAMPLE_MAX_POINTS_FLOOR (backend ge constraint)", async () => {
    const capturedUrl = mockFetchOk(anomaliesResponse({ anomaly_count: 7 }));

    await fetchAnomalyCount(BASE_PARAMS);

    const maxPoints = Number(capturedUrl.searchParams.get("max_points"));
    expect(maxPoints).toBeGreaterThanOrEqual(DOWNSAMPLE_MAX_POINTS_FLOOR);
  });

  it("does NOT send max_points=1 (the value that triggered HTTP 422)", async () => {
    const capturedUrl = mockFetchOk(anomaliesResponse());

    await fetchAnomalyCount(BASE_PARAMS);

    expect(capturedUrl.searchParams.get("max_points")).not.toBe("1");
  });

  it("returns the anomaly_count from the response body", async () => {
    mockFetchOk(anomaliesResponse({ anomaly_count: 42 }));

    const count = await fetchAnomalyCount(BASE_PARAMS);

    expect(count).toBe(42);
  });

  it("throws when the response is not ok", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 422 }));

    await expect(fetchAnomalyCount(BASE_PARAMS)).rejects.toThrow("422");
  });
});

// ---------------------------------------------------------------------------
// Contract: fetchFlaggedWindow outgoing max_points
// ---------------------------------------------------------------------------

describe("fetchFlaggedWindow — max_points query param", () => {
  it("sends max_points >= DOWNSAMPLE_MAX_POINTS_FLOOR", async () => {
    const capturedUrl = mockFetchOk(anomaliesResponse());

    await fetchFlaggedWindow(BASE_PARAMS);

    const maxPoints = Number(capturedUrl.searchParams.get("max_points"));
    expect(maxPoints).toBeGreaterThanOrEqual(DOWNSAMPLE_MAX_POINTS_FLOOR);
  });

  it("sends max_points equal to SERVER_GROUPING_THRESHOLD", async () => {
    const capturedUrl = mockFetchOk(anomaliesResponse());

    await fetchFlaggedWindow(BASE_PARAMS);

    const maxPoints = Number(capturedUrl.searchParams.get("max_points"));
    expect(maxPoints).toBe(SERVER_GROUPING_THRESHOLD);
  });
});
