import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useLiveReadings } from "@/features/monitoring/hooks/useLiveReadings";

// ---------------------------------------------------------------------------
// Mock useScope so the hook doesn't need the nuqs provider
// ---------------------------------------------------------------------------
vi.mock("@/lib/useScope", () => ({
  useScope: vi.fn(() => ({ tenant: "tenant_1", gh: "gh_A" })),
}));

// ---------------------------------------------------------------------------
// MockEventSource — installed on globalThis before each test
// ---------------------------------------------------------------------------

type NamedHandler = (event: MessageEvent) => void;

class MockEventSource {
  static lastInstance: MockEventSource | null = null;

  readonly url: string;
  readyState = 1; // OPEN

  private namedHandlers: Map<string, NamedHandler[]> = new Map();

  onerror: ((e: Event) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockEventSource.lastInstance = this;
  }

  addEventListener(type: string, handler: NamedHandler): void {
    const list = this.namedHandlers.get(type) ?? [];
    list.push(handler);
    this.namedHandlers.set(type, list);
  }

  removeEventListener(type: string, handler: NamedHandler): void {
    const list = this.namedHandlers.get(type) ?? [];
    this.namedHandlers.set(
      type,
      list.filter((h) => h !== handler),
    );
  }

  /** Dispatch a named SSE event with JSON-serialised data. */
  emit(type: string, data: unknown): void {
    const event = new MessageEvent(type, { data: JSON.stringify(data) });
    for (const handler of this.namedHandlers.get(type) ?? []) {
      handler(event);
    }
  }

  /** Trigger the onerror callback. */
  triggerError(): void {
    if (this.onerror) this.onerror(new Event("error"));
  }

  close(): void {
    this.readyState = 2; // CLOSED
  }
}

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
}

function makeWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: queryClient }, children);
  };
}

/** Count only the /live/resume fetch calls — the polling fallback also uses
 *  the global fetch mock (fetchRecentReadings), so a raw call count would
 *  conflate an unrelated poll with a resume. */
function resumeFetchCallCount(): number {
  return vi
    .mocked(globalThis.fetch)
    .mock.calls.filter(([url]) => String(url).includes("/api/bff/live/resume")).length;
}

const READING_PAYLOAD = {
  seq: "",
  timestamp: "2026-07-18T10:00:00Z",
  tenant_id: "tenant_1",
  greenhouse_id: "gh_A",
  channels: { temperature: 22.5, co2: 420.0 },
  sources: { temperature: "src_1", co2: "src_2" },
  anomaly_score: null,
};

const ANOMALY_PAYLOAD = {
  seq: "anom-1",
  timestamp: "2026-07-18T10:01:00Z",
  tenant_id: "tenant_1",
  greenhouse_id: "gh_A",
  score: 0.91,
  threshold: 0.15,
  affected_channels: ["co2"],
  reasoning: "Co2 spike",
  window_start: "2026-07-18T09:41:00Z",
  window_end: "2026-07-18T10:01:00Z",
};

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------

beforeEach(() => {
  vi.useFakeTimers();
  MockEventSource.lastInstance = null;
  globalThis.EventSource = MockEventSource as unknown as typeof EventSource;
  // resumeLiveScope's fetch — not under test in this file; live.test.ts owns
  // request-shape coverage. Stubbed so it resolves instead of hitting a real
  // network call from the one-shot-resume effect.
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("useLiveReadings — idle when disabled", () => {
  it("returns idle transport and empty collections when enabled=false", () => {
    const qc = makeQueryClient();
    const { result } = renderHook(() => useLiveReadings(false), {
      wrapper: makeWrapper(qc),
    });

    expect(result.current.transport).toBe("idle");
    expect(result.current.rows).toHaveLength(0);
    expect(result.current.anomalies).toHaveLength(0);
    expect(result.current.stale).toBe(false);
  });

  it("does not open an EventSource when disabled", () => {
    const qc = makeQueryClient();
    renderHook(() => useLiveReadings(false), { wrapper: makeWrapper(qc) });
    expect(MockEventSource.lastInstance).toBeNull();
  });
});

describe("useLiveReadings — SSE named events", () => {
  it("opens an EventSource with the correct BFF URL", () => {
    const qc = makeQueryClient();
    renderHook(() => useLiveReadings(true), { wrapper: makeWrapper(qc) });

    expect(MockEventSource.lastInstance).not.toBeNull();
    expect(MockEventSource.lastInstance?.url).toContain("/api/bff/live/events");
    expect(MockEventSource.lastInstance?.url).toContain("tenant=tenant_1");
    expect(MockEventSource.lastInstance?.url).toContain("gh=gh_A");
  });

  it("appends rows to state after the flush interval fires", () => {
    const qc = makeQueryClient();
    const { result } = renderHook(() => useLiveReadings(true), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      MockEventSource.lastInstance?.emit("reading", READING_PAYLOAD);
    });

    // Rows are buffered — not yet in state.
    expect(result.current.rows).toHaveLength(0);

    // Advance past the 500 ms flush interval.
    act(() => {
      vi.advanceTimersByTime(600);
    });

    expect(result.current.rows).toHaveLength(1);
    expect(result.current.rows[0]?.timestamp).toBe(READING_PAYLOAD.timestamp);
    expect(result.current.rows[0]?.temperature).toBe(22.5);
  });

  it("adds anomalies immediately (no flush buffer)", () => {
    const qc = makeQueryClient();
    const { result } = renderHook(() => useLiveReadings(true), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      MockEventSource.lastInstance?.emit("anomaly", ANOMALY_PAYLOAD);
    });

    expect(result.current.anomalies).toHaveLength(1);
    expect(result.current.anomalies[0]?.seq).toBe("anom-1");
  });

  it("upgrades an anomaly in place when anomaly-detail arrives", () => {
    const qc = makeQueryClient();
    const { result } = renderHook(() => useLiveReadings(true), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      MockEventSource.lastInstance?.emit("anomaly", ANOMALY_PAYLOAD);
    });

    act(() => {
      MockEventSource.lastInstance?.emit("anomaly-detail", {
        anomaly_seq: "anom-1",
        tenant_id: "tenant_1",
        greenhouse_id: "gh_A",
        reasoning: "Fan failure confirmed",
        fault_type: "spike_fault",
        root_cause: "Fan failure",
        // incident_id is a UUID string per CONTRACT 3.
        incident_id: "550e8400-e29b-41d4-a716-446655440001",
      });
    });

    const upgraded = result.current.anomalies[0];
    expect(upgraded?.seq).toBe("anom-1");
    expect(upgraded?.fault_type).toBe("spike_fault");
    expect(upgraded?.root_cause).toBe("Fan failure");
    expect(upgraded?.incident_id).toBe("550e8400-e29b-41d4-a716-446655440001");
    expect(upgraded?.reasoning).toBe("Fan failure confirmed");
  });

  it("does not upgrade a non-matching anomaly", () => {
    const qc = makeQueryClient();
    const { result } = renderHook(() => useLiveReadings(true), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      MockEventSource.lastInstance?.emit("anomaly", ANOMALY_PAYLOAD);
    });

    act(() => {
      MockEventSource.lastInstance?.emit("anomaly-detail", {
        anomaly_seq: "other-seq",
        tenant_id: "tenant_1",
        greenhouse_id: "gh_A",
        reasoning: "Different anomaly",
      });
    });

    expect(result.current.anomalies[0]?.fault_type).toBeUndefined();
  });

  it("updates sourceStatuses on source-status event", () => {
    const qc = makeQueryClient();
    const { result } = renderHook(() => useLiveReadings(true), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      MockEventSource.lastInstance?.emit("source-status", {
        source_id: "src_1",
        vendor: "acme",
        status: "degraded",
        last_seen: "2026-07-18T10:00:00Z",
      });
    });

    expect(result.current.sourceStatuses).toHaveLength(1);
    expect(result.current.sourceStatuses[0]?.status).toBe("degraded");
  });

  it("replaces an existing source on a subsequent source-status event", () => {
    const qc = makeQueryClient();
    const { result } = renderHook(() => useLiveReadings(true), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      MockEventSource.lastInstance?.emit("source-status", {
        source_id: "src_1",
        vendor: "acme",
        status: "online",
      });
      MockEventSource.lastInstance?.emit("source-status", {
        source_id: "src_1",
        vendor: "acme",
        status: "offline",
      });
    });

    expect(result.current.sourceStatuses).toHaveLength(1);
    expect(result.current.sourceStatuses[0]?.status).toBe("offline");
  });

  it("silently ignores malformed payloads", () => {
    const qc = makeQueryClient();
    const { result } = renderHook(() => useLiveReadings(true), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      MockEventSource.lastInstance?.emit("reading", { broken: true }); // missing required fields
    });

    act(() => {
      vi.advanceTimersByTime(600);
    });

    expect(result.current.rows).toHaveLength(0);
  });
});

describe("useLiveReadings — row buffer cap", () => {
  it("caps the row buffer at 2000 and drops oldest", () => {
    const qc = makeQueryClient();
    const { result } = renderHook(() => useLiveReadings(true), {
      wrapper: makeWrapper(qc),
    });

    // Emit 2100 readings.
    act(() => {
      for (let i = 0; i < 2100; i++) {
        MockEventSource.lastInstance?.emit("reading", {
          ...READING_PAYLOAD,
          timestamp: `2026-07-18T${String(Math.floor(i / 60)).padStart(2, "0")}:${String(i % 60).padStart(2, "0")}:00Z`,
        });
      }
    });

    act(() => {
      vi.advanceTimersByTime(600);
    });

    expect(result.current.rows.length).toBe(2000);
  });
});

describe("useLiveReadings — reset event", () => {
  it("clears rows and anomalies on reset", () => {
    const qc = makeQueryClient();
    const { result } = renderHook(() => useLiveReadings(true), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      MockEventSource.lastInstance?.emit("reading", READING_PAYLOAD);
      MockEventSource.lastInstance?.emit("anomaly", ANOMALY_PAYLOAD);
    });
    act(() => {
      vi.advanceTimersByTime(600);
    });

    expect(result.current.rows).toHaveLength(1);
    expect(result.current.anomalies).toHaveLength(1);

    act(() => {
      MockEventSource.lastInstance?.emit("reset", {});
    });

    expect(result.current.rows).toHaveLength(0);
    expect(result.current.anomalies).toHaveLength(0);
  });

  it("invalidates the readings query cache on reset", () => {
    const qc = makeQueryClient();
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    renderHook(() => useLiveReadings(true), { wrapper: makeWrapper(qc) });

    act(() => {
      MockEventSource.lastInstance?.emit("reset", { reason: "config_reload" });
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["readings"] });
  });
});

describe("useLiveReadings — error fallback to polling", () => {
  it("switches to polling transport after 3 errors within 30 s", () => {
    const qc = makeQueryClient();
    const { result } = renderHook(() => useLiveReadings(true), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      MockEventSource.lastInstance?.triggerError();
      MockEventSource.lastInstance?.triggerError();
      MockEventSource.lastInstance?.triggerError();
    });

    expect(result.current.transport).toBe("polling");
  });

  it("does not switch to polling for 2 errors within 30 s", () => {
    const qc = makeQueryClient();
    const { result } = renderHook(() => useLiveReadings(true), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      MockEventSource.lastInstance?.triggerError();
      MockEventSource.lastInstance?.triggerError();
    });

    expect(result.current.transport).toBe("sse");
  });

  it("closes the EventSource when switching to polling", () => {
    const qc = makeQueryClient();
    renderHook(() => useLiveReadings(true), { wrapper: makeWrapper(qc) });

    const src = MockEventSource.lastInstance;
    if (src == null) throw new Error("EventSource was never constructed");
    act(() => {
      src.triggerError();
      src.triggerError();
      src.triggerError();
    });

    expect(src.readyState).toBe(2); // CLOSED
  });
});

describe("useLiveReadings — stale detection", () => {
  it("marks the stream stale after 60 s with no reading or heartbeat", () => {
    const qc = makeQueryClient();
    const { result } = renderHook(() => useLiveReadings(true), {
      wrapper: makeWrapper(qc),
    });

    // First reading resets the timer.
    act(() => {
      MockEventSource.lastInstance?.emit("reading", READING_PAYLOAD);
    });
    act(() => {
      vi.advanceTimersByTime(600);
    });

    expect(result.current.stale).toBe(false);

    // Advance past the 60 s stale threshold.
    act(() => {
      vi.advanceTimersByTime(61_000);
    });

    expect(result.current.stale).toBe(true);
  });

  it("resets stale to false when a heartbeat arrives", () => {
    const qc = makeQueryClient();
    const { result } = renderHook(() => useLiveReadings(true), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      vi.advanceTimersByTime(61_000);
    });
    expect(result.current.stale).toBe(true);

    act(() => {
      MockEventSource.lastInstance?.emit("heartbeat", {});
    });

    expect(result.current.stale).toBe(false);
  });
});

describe("useLiveReadings — one-shot resume on mount/scope change", () => {
  it("calls resumeLiveScope once when mounted already enabled", () => {
    const qc = makeQueryClient();
    renderHook(() => useLiveReadings(true), { wrapper: makeWrapper(qc) });

    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url, init] = vi.mocked(globalThis.fetch).mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("/api/bff/live/resume");
    expect(init.method).toBe("POST");
  });

  it("does not call resumeLiveScope when mounted disabled", () => {
    const qc = makeQueryClient();
    renderHook(() => useLiveReadings(false), { wrapper: makeWrapper(qc) });

    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("does not call resumeLiveScope again on SSE reconnect (error fallback)", () => {
    const qc = makeQueryClient();
    renderHook(() => useLiveReadings(true), { wrapper: makeWrapper(qc) });
    expect(resumeFetchCallCount()).toBe(1);

    // Trip the SSE error-fallback path — this re-runs the SSE effect (deps
    // include sseFailed) and also activates the polling fallback (its own
    // fetchRecentReadings call), but must NOT re-trigger resume: that would
    // re-seed a database the operator just wiped via Clear all data.
    act(() => {
      MockEventSource.lastInstance?.triggerError();
      MockEventSource.lastInstance?.triggerError();
      MockEventSource.lastInstance?.triggerError();
    });

    expect(resumeFetchCallCount()).toBe(1);
  });

  it("calls resumeLiveScope again when the scope (tenant/gh) changes while live", async () => {
    const useScopeModule = await import("@/lib/useScope");
    const qc = makeQueryClient();
    let enabled = true;
    const { rerender } = renderHook(() => useLiveReadings(enabled), {
      wrapper: makeWrapper(qc),
    });
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);

    vi.mocked(useScopeModule.useScope).mockReturnValue({ tenant: "tenant_2", gh: "gh_B" });
    enabled = true;
    rerender();

    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
  });
});

describe("useLiveReadings — disabled teardown", () => {
  it("clears all state when enabled transitions to false", () => {
    const qc = makeQueryClient();
    let enabled = true;
    const { result, rerender } = renderHook(() => useLiveReadings(enabled), {
      wrapper: makeWrapper(qc),
    });

    act(() => {
      MockEventSource.lastInstance?.emit("anomaly", ANOMALY_PAYLOAD);
    });
    expect(result.current.anomalies).toHaveLength(1);

    enabled = false;
    rerender();

    expect(result.current.transport).toBe("idle");
    expect(result.current.rows).toHaveLength(0);
    expect(result.current.anomalies).toHaveLength(0);
  });
});
