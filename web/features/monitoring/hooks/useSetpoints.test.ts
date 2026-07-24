import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSetpoints } from "@/features/monitoring/hooks/useSetpoints";
import { CHANNEL_META } from "@/features/monitoring/lib/channelMeta";

vi.mock("@/lib/useScope", () => ({
  useScope: vi.fn(() => ({ tenant: "tenant_1", gh: "gh_A" })),
}));

vi.mock("@/features/configuration/api/operationalConfig", () => ({
  fetchOperationalConfig: vi.fn(() => Promise.reject(new Error("offline"))),
}));

const STORAGE_KEY = "gh-setpoints:tenant_1:gh_A";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

function renderSetpoints() {
  return renderHook(() => useSetpoints(), { wrapper });
}

// ---------------------------------------------------------------------------
// Fake localStorage
// ---------------------------------------------------------------------------
let fakeStorage: Record<string, string> = {};

const localStorageMock = {
  getItem: vi.fn((key: string) => fakeStorage[key] ?? null),
  setItem: vi.fn((key: string, value: string) => {
    fakeStorage[key] = value;
  }),
  removeItem: vi.fn((key: string) => {
    delete fakeStorage[key];
  }),
  clear: vi.fn(() => {
    fakeStorage = {};
  }),
};

beforeEach(() => {
  fakeStorage = {};
  vi.clearAllMocks();
  Object.defineProperty(globalThis, "localStorage", {
    value: localStorageMock,
    writable: true,
    configurable: true,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("useSetpoints — defaults", () => {
  it("returns CHANNEL_META optimal ranges as defaults when no override exists", () => {
    const { result } = renderSetpoints();
    for (const [ch, meta] of Object.entries(CHANNEL_META)) {
      expect(result.current.setpoints[ch]).toEqual(meta.optimal);
    }
  });

  it("has setpoints for all 7 known channels by default", () => {
    const { result } = renderSetpoints();
    const keys = Object.keys(result.current.setpoints);
    expect(keys).toContain("temperature");
    expect(keys).toContain("humidity");
    expect(keys).toContain("co2");
    expect(keys).toContain("light");
    expect(keys).toContain("vpd");
    expect(keys).toContain("soil_moisture");
    expect(keys).toContain("root_zone_temp");
  });
});

describe("useSetpoints — updateSetpoint", () => {
  it("merges a user override into setpoints immediately", () => {
    const { result } = renderSetpoints();

    act(() => {
      result.current.updateSetpoint("temperature", [18, 30]);
    });

    expect(result.current.setpoints.temperature).toEqual([18, 30]);
    // Other channels unaffected.
    expect(result.current.setpoints.humidity).toEqual(CHANNEL_META.humidity?.optimal);
  });

  it("persists the override to localStorage under the scope key", () => {
    const { result } = renderSetpoints();

    act(() => {
      result.current.updateSetpoint("co2", [300, 600]);
    });

    expect(localStorageMock.setItem).toHaveBeenCalledWith(
      STORAGE_KEY,
      expect.stringContaining('"co2"'),
    );
    const stored = JSON.parse(fakeStorage[STORAGE_KEY] ?? "{}") as Record<string, unknown>;
    expect(stored.co2).toEqual([300, 600]);
  });

  it("is a no-op when min >= max", () => {
    const { result } = renderSetpoints();
    const before = { ...result.current.setpoints };

    act(() => {
      result.current.updateSetpoint("temperature", [30, 20]); // inverted
    });

    expect(result.current.setpoints.temperature).toEqual(before.temperature);
    expect(localStorageMock.setItem).not.toHaveBeenCalled();
  });

  it("is a no-op when min === max", () => {
    const { result } = renderSetpoints();
    const before = { ...result.current.setpoints };

    act(() => {
      result.current.updateSetpoint("temperature", [25, 25]);
    });

    expect(result.current.setpoints.temperature).toEqual(before.temperature);
  });
});

describe("useSetpoints — restore from localStorage", () => {
  it("loads persisted overrides on mount", () => {
    fakeStorage[STORAGE_KEY] = JSON.stringify({ humidity: [50, 80] });

    const { result } = renderSetpoints();

    expect(result.current.setpoints.humidity).toEqual([50, 80]);
    // Defaults still present for other channels.
    expect(result.current.setpoints.temperature).toEqual(CHANNEL_META.temperature?.optimal);
  });
});

describe("useSetpoints — resetToDefaults", () => {
  it("clears all overrides and reverts to CHANNEL_META defaults", () => {
    const { result } = renderSetpoints();

    act(() => {
      result.current.updateSetpoint("temperature", [15, 35]);
      result.current.updateSetpoint("co2", [200, 800]);
    });

    act(() => {
      result.current.resetToDefaults();
    });

    expect(result.current.setpoints.temperature).toEqual(CHANNEL_META.temperature?.optimal);
    expect(result.current.setpoints.co2).toEqual(CHANNEL_META.co2?.optimal);
  });

  it("removes the localStorage key on reset", () => {
    const { result } = renderSetpoints();

    act(() => {
      result.current.updateSetpoint("temperature", [15, 35]);
    });

    act(() => {
      result.current.resetToDefaults();
    });

    expect(localStorageMock.removeItem).toHaveBeenCalledWith(STORAGE_KEY);
  });
});

describe("useSetpoints — corrupt localStorage ignored", () => {
  it("falls back to defaults when stored JSON is invalid", () => {
    fakeStorage[STORAGE_KEY] = "not-valid-json{{{";

    const { result } = renderSetpoints();

    // Should not throw, and should return defaults.
    expect(result.current.setpoints.temperature).toEqual(CHANNEL_META.temperature?.optimal);
  });

  it("falls back to defaults when stored value fails Zod validation", () => {
    // humidity stored as a string instead of [number, number]
    fakeStorage[STORAGE_KEY] = JSON.stringify({ humidity: "too-warm" });

    const { result } = renderSetpoints();

    expect(result.current.setpoints.humidity).toEqual(CHANNEL_META.humidity?.optimal);
  });

  it("falls back to defaults when stored tuple has wrong length", () => {
    fakeStorage[STORAGE_KEY] = JSON.stringify({ temperature: [20, 25, 30] });

    const { result } = renderSetpoints();

    expect(result.current.setpoints.temperature).toEqual(CHANNEL_META.temperature?.optimal);
  });

  it("falls back to defaults when a setpoint range is inverted ([80, 20]) — F10", () => {
    // An inverted range passes tuple-shape validation but violates min < max;
    // the Zod refine rejects it so the whole stored object is discarded.
    fakeStorage[STORAGE_KEY] = JSON.stringify({ temperature: [80, 20] });

    const { result } = renderSetpoints();

    expect(result.current.setpoints.temperature).toEqual(CHANNEL_META.temperature?.optimal);
  });
});
