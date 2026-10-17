import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LiveBadge } from "@/features/monitoring/components/LiveBadge";

// ---------------------------------------------------------------------------
// Mocks
//
// LiveBadge calls useQuery directly, so react-query is stubbed here rather
// than wrapping every render in a QueryClientProvider — the same approach
// LiveToggle.test.tsx takes for useLiveStatus.
// ---------------------------------------------------------------------------

const useQueryMock = vi.fn();
vi.mock("@tanstack/react-query", () => ({
  useQuery: () => useQueryMock(),
}));

vi.mock("@/lib/useScope", () => ({
  useScope: () => ({ tenant: "tenant_1", gh: "gh_A" }),
}));

vi.mock("@/features/monitoring/api/live", () => ({
  fetchLiveStatus: vi.fn(),
}));

function mockStatus(data: unknown): void {
  useQueryMock.mockReturnValue({ data });
}

beforeEach(() => {
  useQueryMock.mockReset();
  mockStatus(undefined);
});

afterEach(cleanup);

const ONLINE_SOURCE = {
  source_id: "climate",
  vendor: "mock",
  status: "online" as const,
  last_seen: null,
};

describe("LiveBadge detector state", () => {
  it("shows 'not analysed' instead of a healthy label when no model is deployed", () => {
    mockStatus({ sources: [ONLINE_SOURCE], detector_state: "missing" });
    render(<LiveBadge transport="sse" stale={false} sourceStatuses={[]} />);

    const badge = screen.getByTestId("live-badge");
    expect(badge).toHaveAttribute("data-detector-state", "missing");
    expect(badge).toHaveTextContent("Live · not analysed");
    expect(badge).not.toHaveTextContent("Live · SSE");
  });

  it("also flags a failing resolution rather than reading as healthy", () => {
    mockStatus({ sources: [ONLINE_SOURCE], detector_state: "error" });
    render(<LiveBadge transport="sse" stale={false} sourceStatuses={[]} />);
    expect(screen.getByTestId("live-badge")).toHaveTextContent("Live · not analysed");
  });

  it("shows the normal live label once a detector is ready", () => {
    mockStatus({ sources: [ONLINE_SOURCE], detector_state: "ready" });
    render(<LiveBadge transport="sse" stale={false} sourceStatuses={[]} />);
    expect(screen.getByTestId("live-badge")).toHaveTextContent("Live · SSE");
  });

  it("treats an absent detector_state as no information (deploy skew)", () => {
    mockStatus({ sources: [ONLINE_SOURCE] });
    render(<LiveBadge transport="sse" stale={false} sourceStatuses={[]} />);

    const badge = screen.getByTestId("live-badge");
    expect(badge).toHaveAttribute("data-detector-state", "unknown");
    expect(badge).toHaveTextContent("Live · SSE");
  });

  it("keeps 'Stalled' ahead of 'not analysed' when both apply", () => {
    // No data arriving is the worse problem, and the two are not exclusive.
    mockStatus({ sources: [ONLINE_SOURCE], detector_state: "missing" });
    render(<LiveBadge transport="sse" stale={true} sourceStatuses={[]} />);
    expect(screen.getByTestId("live-badge")).toHaveTextContent("Stalled");
  });
});

describe("LiveBadge paused sources", () => {
  it("renders a dot for a paused source without crashing on the new state", () => {
    mockStatus({
      sources: [{ ...ONLINE_SOURCE, status: "paused" }],
      detector_state: "ready",
    });
    render(<LiveBadge transport="sse" stale={false} sourceStatuses={[]} />);

    const dot = screen.getByTestId("source-dot-climate");
    expect(dot).toHaveAttribute("aria-label", "climate: paused");
    // Muted, not a severity colour: a paused scope is off, not broken.
    expect(dot.className).toContain("bg-muted-foreground/40");
  });
});
