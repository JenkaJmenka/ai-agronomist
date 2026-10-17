import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useLiveStatus } from "@/features/monitoring/hooks/useLiveStatus";

vi.mock("@/lib/useScope", () => ({
  useScope: vi.fn(() => ({ tenant: "tenant_1", gh: "gh_A" })),
}));

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: queryClient }, children);
  };
}

function mockFetchOk(body: unknown): void {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => body }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

beforeEach(() => {
  vi.clearAllMocks();
});

describe("useLiveStatus", () => {
  it("returns null freshness while the request is in flight", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => new Promise(() => {})),
    );

    const { result } = renderHook(() => useLiveStatus(), { wrapper: makeWrapper() });

    expect(result.current.freshness).toBeNull();
    expect(result.current.isLoading).toBe(true);
  });

  it("parses a live-and-healthy response", async () => {
    mockFetchOk({
      greenhouse_status: "live",
      sources: [],
      freshness: {
        state: "live",
        newest_reading_at: "2026-10-16T09:00:00+00:00",
        age_seconds: 12.5,
        is_stale: false,
        threshold_seconds: 180,
      },
    });

    const { result } = renderHook(() => useLiveStatus(), { wrapper: makeWrapper() });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.freshness).toEqual({
      state: "live",
      newest_reading_at: "2026-10-16T09:00:00+00:00",
      age_seconds: 12.5,
      is_stale: false,
      threshold_seconds: 180,
    });
    expect(result.current.greenhouseStatus).toBe("live");
  });

  it("parses a live-but-stale response with a null newest_reading_at (never written)", async () => {
    mockFetchOk({
      greenhouse_status: "live",
      sources: [],
      freshness: {
        state: "live",
        newest_reading_at: null,
        age_seconds: null,
        is_stale: true,
        threshold_seconds: 180,
      },
    });

    const { result } = renderHook(() => useLiveStatus(), { wrapper: makeWrapper() });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.freshness?.is_stale).toBe(true);
    expect(result.current.freshness?.newest_reading_at).toBeNull();
  });

  it("parses a negative age_seconds without rejecting (future-dated readings)", async () => {
    mockFetchOk({
      greenhouse_status: "live",
      sources: [],
      freshness: {
        state: "live",
        newest_reading_at: "2027-01-01T00:00:00+00:00",
        age_seconds: -42.0,
        is_stale: false,
        threshold_seconds: 180,
      },
    });

    const { result } = renderHook(() => useLiveStatus(), { wrapper: makeWrapper() });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.freshness?.age_seconds).toBe(-42.0);
  });

  it("returns a paused freshness state when Go Live is off", async () => {
    mockFetchOk({
      greenhouse_status: "paused",
      sources: [],
      freshness: {
        state: "paused",
        newest_reading_at: null,
        age_seconds: null,
        is_stale: false,
        threshold_seconds: 180,
      },
    });

    const { result } = renderHook(() => useLiveStatus(), { wrapper: makeWrapper() });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.freshness?.state).toBe("paused");
    expect(result.current.freshness?.is_stale).toBe(false);
  });

  it("tolerates a response missing freshness (older API, deploy skew)", async () => {
    mockFetchOk({ sources: [] });

    const { result } = renderHook(() => useLiveStatus(), { wrapper: makeWrapper() });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.freshness).toBeNull();
  });

  it("surfaces isError without throwing when the request fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 500 }));

    const { result } = renderHook(() => useLiveStatus(), { wrapper: makeWrapper() });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.freshness).toBeNull();
  });
});
