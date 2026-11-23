import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DriftWatchView } from "@/features/drift-watch/DriftWatchView";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock("@/lib/useScope", () => ({
  useScope: vi.fn(() => ({ tenant: "tenant_1", gh: "gh_A" })),
}));

vi.mock("next/navigation", () => ({
  usePathname: vi.fn(() => "/drift-watch"),
  useSearchParams: vi.fn(() => new URLSearchParams()),
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const ARMED_INPUT = {
  input_id: "input-001",
  kind: "paired_probes",
  target_channel: "temperature",
  status: "armed",
  status_reason: "Calibrated on 32 weeks of data",
  updated_at: "2026-10-01T00:00:00Z",
  false_alarm_rate: 0.03,
  detection_rate: 0.91,
};

const SHADOW_INPUT = {
  input_id: "input-002",
  kind: "virtual_sensor",
  target_channel: "humidity",
  status: "shadow",
  status_reason: "Only 8 weeks of history",
  updated_at: "2026-10-01T00:00:00Z",
};

const ARMED_EPISODE = {
  input_id: "input-001",
  episode_id: "ep-001",
  target_channel: "temperature",
  direction: "+",
  first_detected_ts: "2026-10-10T08:00:00Z",
  last_confirmed_ts: "2026-10-11T08:00:00Z",
  offset_estimate: 0.42,
  input_status_at_detection: "armed",
  ongoing: true,
  armed: true,
};

const SHADOW_EPISODE = {
  input_id: "input-002",
  episode_id: "ep-002",
  target_channel: "humidity",
  direction: "-",
  first_detected_ts: "2026-10-05T12:00:00Z",
  last_confirmed_ts: "2026-10-06T12:00:00Z",
  offset_estimate: -0.15,
  input_status_at_detection: "shadow",
  ongoing: false,
  armed: false,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let queryClient: QueryClient;

function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

function mockFetch(inputs: unknown[], episodes: unknown[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      const payload = String(url).includes("/inputs") ? inputs : episodes;
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(payload),
      });
    }),
  );
}

beforeEach(() => {
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("DriftWatchView — inputs", () => {
  it("renders an input with its status chip and reason", async () => {
    mockFetch([ARMED_INPUT], []);

    render(createElement(DriftWatchView), { wrapper });

    await waitFor(() => {
      expect(screen.getByTestId("inputs-list")).toBeInTheDocument();
    });

    const rows = screen.getAllByTestId("drift-input-row");
    expect(rows).toHaveLength(1);

    expect(screen.getByTestId("input-channel")).toHaveTextContent("temperature");
    expect(screen.getByTestId("input-status-reason")).toHaveTextContent(
      "Calibrated on 32 weeks of data",
    );

    // Status chip is present for the armed status
    const chipWrapper = screen.getByTestId("input-status-chip");
    expect(chipWrapper).toHaveTextContent("armed");
  });

  it("renders a shadow input with shadow status chip", async () => {
    mockFetch([SHADOW_INPUT], []);

    render(createElement(DriftWatchView), { wrapper });

    await waitFor(() => {
      expect(screen.getByTestId("inputs-list")).toBeInTheDocument();
    });

    expect(screen.getByTestId("input-channel")).toHaveTextContent("humidity");
    const chipWrapper = screen.getByTestId("input-status-chip");
    expect(chipWrapper).toHaveTextContent("shadow");
    // shadow status uses unclassified severity (gray chip)
    const chip = chipWrapper.querySelector("[data-severity]");
    expect(chip).toHaveAttribute("data-severity", "unclassified");
  });
});

describe("DriftWatchView — episodes", () => {
  it("renders an armed episode with Armed chip", async () => {
    mockFetch([ARMED_INPUT], [ARMED_EPISODE]);

    render(createElement(DriftWatchView), { wrapper });

    await waitFor(() => {
      expect(screen.getByTestId("episodes-table")).toBeInTheDocument();
    });

    const rows = screen.getAllByTestId("drift-episode-row");
    const armedRow = rows.find((r) => r.getAttribute("data-armed") === "true");
    expect(armedRow).toBeDefined();
    expect(armedRow?.querySelector("[data-testid='episode-armed-chip']")).toBeInTheDocument();
  });

  it("renders a shadow episode with Shadow chip and reduced opacity", async () => {
    mockFetch([SHADOW_INPUT], [SHADOW_EPISODE]);

    render(createElement(DriftWatchView), { wrapper });

    await waitFor(() => {
      expect(screen.getByTestId("episodes-table")).toBeInTheDocument();
    });

    const rows = screen.getAllByTestId("drift-episode-row");
    const shadowRow = rows.find((r) => r.getAttribute("data-armed") === "false");
    expect(shadowRow).toBeDefined();
    expect(shadowRow?.querySelector("[data-testid='episode-shadow-chip']")).toBeInTheDocument();
    // Shadow rows are visually quieter — opacity-60 class applied
    expect(shadowRow?.className).toContain("opacity-60");
  });

  it("renders armed and shadow episodes with distinct visual treatment", async () => {
    mockFetch([ARMED_INPUT, SHADOW_INPUT], [ARMED_EPISODE, SHADOW_EPISODE]);

    render(createElement(DriftWatchView), { wrapper });

    await waitFor(() => {
      expect(screen.getByTestId("episodes-table")).toBeInTheDocument();
    });

    const rows = screen.getAllByTestId("drift-episode-row");
    expect(rows).toHaveLength(2);

    const armedRow = rows.find((r) => r.getAttribute("data-armed") === "true");
    const shadowRow = rows.find((r) => r.getAttribute("data-armed") === "false");

    // Armed row does NOT have opacity-60
    expect(armedRow?.className).not.toContain("opacity-60");
    // Shadow row DOES have opacity-60
    expect(shadowRow?.className).toContain("opacity-60");
  });
});

describe("DriftWatchView — empty state", () => {
  it("shows the empty state message when no episodes are returned", async () => {
    mockFetch([ARMED_INPUT], []);

    render(createElement(DriftWatchView), { wrapper });

    await waitFor(() => {
      expect(screen.getByTestId("episodes-empty-state")).toBeInTheDocument();
    });

    expect(screen.getByTestId("episodes-empty-state")).toHaveTextContent(
      "No drift episodes recorded yet.",
    );
  });
});

describe("DriftWatchView — display formatting", () => {
  it("renders false_alarm_rate as per-month without scaling", async () => {
    mockFetch([{ ...ARMED_INPUT, false_alarm_rate: 0.55 }], []);
    render(createElement(DriftWatchView), { wrapper });
    await waitFor(() => {
      expect(screen.getByTestId("inputs-list")).toBeInTheDocument();
    });
    expect(screen.getByTestId("drift-input-row")).toHaveTextContent("0.55/mo false alarms");
  });

  it("renders a '-' episode magnitude with a minus sign", async () => {
    mockFetch([], [{ ...SHADOW_EPISODE, direction: "-", offset_estimate: 1.637 }]);
    render(createElement(DriftWatchView), { wrapper });
    await waitFor(() => {
      expect(screen.getByTestId("episodes-table")).toBeInTheDocument();
    });
    const row = screen.getByTestId("drift-episode-row");
    expect(row).toHaveTextContent("-1.637");
    expect(row).not.toHaveTextContent("+1.637");
  });

  it("renders a '+' episode magnitude with a plus sign", async () => {
    mockFetch([], [{ ...ARMED_EPISODE, direction: "+", offset_estimate: 3.417 }]);
    render(createElement(DriftWatchView), { wrapper });
    await waitFor(() => {
      expect(screen.getByTestId("episodes-table")).toBeInTheDocument();
    });
    expect(screen.getByTestId("drift-episode-row")).toHaveTextContent("+3.417");
  });
});

describe("DriftWatchView — alert policy display", () => {
  const ALERT_INPUT = {
    ...ARMED_INPUT,
    greenhouse_id: "gh_A",
    oos_count: 2,
    raw_count: 3,
    budget: 3,
    alert_state: "eligible",
    alert_state_reason: "2 episodes in 13 weeks, at or under budget",
  };

  it("shows the out-of-sample figure against budget and labels calibration", async () => {
    mockFetch([ALERT_INPUT], []);
    render(createElement(DriftWatchView), { wrapper });
    await waitFor(() => expect(screen.getByTestId("input-oos")).toBeInTheDocument());
    expect(screen.getByTestId("input-oos")).toHaveTextContent(
      "2 of 3 episodes in 13 weeks (budget 3)",
    );
    expect(screen.getByTestId("input-calibration-far")).toHaveTextContent("(calibration)");
  });

  it("omits the out-of-sample text when oos_count is null", async () => {
    mockFetch([{ ...ALERT_INPUT, oos_count: null }], []);
    render(createElement(DriftWatchView), { wrapper });
    await waitFor(() => expect(screen.getByTestId("inputs-list")).toBeInTheDocument());
    expect(screen.queryByTestId("input-oos")).toBeNull();
  });

  it("shows the alert_state badge with its reason", async () => {
    mockFetch([ALERT_INPUT], []);
    render(createElement(DriftWatchView), { wrapper });
    await waitFor(() => expect(screen.getByTestId("input-alert-state")).toBeInTheDocument());
    expect(screen.getByTestId("input-alert-state")).toHaveTextContent("eligible");
    expect(screen.getByTestId("input-alert-reason")).toHaveTextContent(
      "2 episodes in 13 weeks, at or under budget",
    );
  });

  it("prefers the signed robust offset over the legacy magnitude", async () => {
    mockFetch(
      [],
      [{ ...ARMED_EPISODE, direction: "+", offset_estimate: 3.417, offset_robust: -2.9 }],
    );
    render(createElement(DriftWatchView), { wrapper });
    await waitFor(() => expect(screen.getByTestId("episodes-table")).toBeInTheDocument());
    const row = screen.getByTestId("drift-episode-row");
    expect(row).toHaveTextContent("-2.900");
    expect(row).not.toHaveTextContent("3.417");
  });

  it("shows suppressed text, label and alert message", async () => {
    mockFetch(
      [],
      [
        {
          ...ARMED_EPISODE,
          suppressed_by: "stuck_fault",
          label: "false_alarm",
          alert_message: "Two probes disagree. One of the two probes may have drifted.",
        },
      ],
    );
    render(createElement(DriftWatchView), { wrapper });
    await waitFor(() => expect(screen.getByTestId("episodes-table")).toBeInTheDocument());
    expect(screen.getByTestId("episode-suppressed")).toHaveTextContent("suppressed (stuck sensor)");
    expect(screen.getByTestId("episode-label")).toHaveTextContent("False alarm");
    expect(screen.getByTestId("episode-message")).toHaveTextContent(
      "Two probes disagree. One of the two probes may have drifted.",
    );
  });
});

describe("DriftWatchView — feedback", () => {
  function mockFetchWithPost(post: () => Promise<{ ok: boolean; status?: number }>) {
    const fn = vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === "POST") return post();
      const payload = String(url).includes("/inputs") ? [] : [ARMED_EPISODE];
      return Promise.resolve({ ok: true, json: () => Promise.resolve(payload) });
    });
    vi.stubGlobal("fetch", fn);
    return fn;
  }

  it("POSTs the natural key then refetches", async () => {
    const fn = mockFetchWithPost(() => Promise.resolve({ ok: true }));
    render(createElement(DriftWatchView), { wrapper });
    await waitFor(() => expect(screen.getByTestId("feedback-confirmed")).toBeInTheDocument());
    const getsBefore = fn.mock.calls.filter(([, i]) => i?.method !== "POST").length;

    fireEvent.click(screen.getByTestId("feedback-confirmed"));

    await waitFor(() => {
      const gets = fn.mock.calls.filter(([, i]) => i?.method !== "POST").length;
      expect(gets).toBeGreaterThan(getsBefore);
    });
    const post = fn.mock.calls.find(([, i]) => i?.method === "POST");
    expect(post?.[0]).toBe("/api/bff/drift-watch/feedback");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({
      tenant_id: "tenant_1",
      greenhouse_id: "gh_A",
      input_id: "input-001",
      direction: "+",
      first_detected_ts: "2026-10-10T08:00:00Z",
      label: "confirmed_drift",
    });
  });

  it("disables both buttons while the request is pending", async () => {
    let resolve: (v: { ok: boolean }) => void = () => {};
    mockFetchWithPost(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    render(createElement(DriftWatchView), { wrapper });
    await waitFor(() => expect(screen.getByTestId("feedback-false-alarm")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("feedback-false-alarm"));

    await waitFor(() => expect(screen.getByTestId("feedback-false-alarm")).toBeDisabled());
    expect(screen.getByTestId("feedback-confirmed")).toBeDisabled();
    resolve({ ok: true });
    await waitFor(() => expect(screen.getByTestId("feedback-false-alarm")).not.toBeDisabled());
  });

  it("shows an error when the POST fails", async () => {
    mockFetchWithPost(() => Promise.resolve({ ok: false, status: 500 }));
    render(createElement(DriftWatchView), { wrapper });
    await waitFor(() => expect(screen.getByTestId("feedback-confirmed")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("feedback-confirmed"));
    await waitFor(() => expect(screen.getByTestId("feedback-error")).toBeInTheDocument());
    expect(screen.getByTestId("feedback-error")).toHaveTextContent("500");
    expect(screen.getByTestId("feedback-confirmed")).not.toBeDisabled();
  });
});
