import { afterEach, describe, expect, it, vi } from "vitest";

import { parseProfileLoggingEnabled } from "./profileFlag";
import { isProfileEnabled, profileEvent, resetProfileEnabledCache, timed } from "./profiling";

describe("parseProfileLoggingEnabled", () => {
  it("defaults when unset or blank", () => {
    expect(parseProfileLoggingEnabled(undefined, false)).toBe(false);
    expect(parseProfileLoggingEnabled(null, true)).toBe(true);
    expect(parseProfileLoggingEnabled("  ", false)).toBe(false);
  });

  it("accepts 1/true/yes/on case-insensitively", () => {
    for (const raw of ["1", "true", "TRUE", "yes", "YES", "on", "On"]) {
      expect(parseProfileLoggingEnabled(raw, false)).toBe(true);
    }
  });

  it("rejects other values", () => {
    for (const raw of ["0", "false", "no", "off", "maybe"]) {
      expect(parseProfileLoggingEnabled(raw, true)).toBe(false);
    }
  });
});

describe("client profiling", () => {
  afterEach(() => {
    resetProfileEnabledCache();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("does not POST profile events when disabled", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/profile" && (!init || init.method === undefined || init.method === "GET")) {
        return new Response(JSON.stringify({ enabled: false }), { status: 200 });
      }
      return new Response(null, { status: 204 });
    });
    vi.stubGlobal("fetch", fetchMock);

    expect(await isProfileEnabled()).toBe(false);
    profileEvent("ui.nav", { to: "/monitoring" });
    await timed("ui.time_window", async () => "ok", { period: "24h" });

    // Allow microtasks from void postProfile to settle.
    await Promise.resolve();
    await Promise.resolve();

    const posts = fetchMock.mock.calls.filter(
      ([, init]) => init && typeof init === "object" && init.method === "POST",
    );
    expect(posts).toHaveLength(0);
  });

  it("POSTs when enabled", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/profile" && (!init || init.method === undefined || init.method === "GET")) {
        return new Response(JSON.stringify({ enabled: true }), { status: 200 });
      }
      return new Response(null, { status: 204 });
    });
    vi.stubGlobal("fetch", fetchMock);

    await timed("ui.time_window", async () => 42, { period: "1h" });
    await Promise.resolve();
    await Promise.resolve();

    const posts = fetchMock.mock.calls.filter(
      ([, init]) => init && typeof init === "object" && init.method === "POST",
    );
    expect(posts).toHaveLength(1);
    const body = JSON.parse(String(posts[0]?.[1]?.body)) as {
      phase: string;
      ok: boolean;
      period: string;
      elapsed_ms: number;
    };
    expect(body.phase).toBe("ui.time_window");
    expect(body.ok).toBe(true);
    expect(body.period).toBe("1h");
    expect(typeof body.elapsed_ms).toBe("number");
  });
});
