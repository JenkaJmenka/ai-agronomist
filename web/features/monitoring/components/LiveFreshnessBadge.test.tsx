import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Freshness } from "@/features/monitoring/api/live";
import { LiveFreshnessBadge } from "@/features/monitoring/components/LiveFreshnessBadge";

const useLiveStatusMock = vi.fn();
vi.mock("@/features/monitoring/hooks/useLiveStatus", () => ({
  useLiveStatus: () => useLiveStatusMock(),
}));

function mockStatus(freshness: Freshness | null, isLoading = false): void {
  useLiveStatusMock.mockReturnValue({
    greenhouseStatus: null,
    sources: [],
    freshness,
    isLoading,
    isError: false,
  });
}

afterEach(cleanup);

describe("LiveFreshnessBadge", () => {
  it("renders nothing while loading", () => {
    mockStatus(null, true);
    render(<LiveFreshnessBadge />);
    expect(screen.queryByTestId("live-freshness-badge")).not.toBeInTheDocument();
  });

  it("renders nothing when freshness is absent (deploy skew / error)", () => {
    mockStatus(null, false);
    render(<LiveFreshnessBadge />);
    expect(screen.queryByTestId("live-freshness-badge")).not.toBeInTheDocument();
  });

  it("renders the idle state distinctly when paused", () => {
    mockStatus({
      state: "paused",
      newest_reading_at: null,
      age_seconds: null,
      is_stale: false,
      threshold_seconds: 180,
    });
    render(<LiveFreshnessBadge />);

    const badge = screen.getByTestId("live-freshness-badge");
    expect(badge).toHaveAttribute("data-freshness-state", "off");
    expect(badge).toHaveTextContent("Live feed paused");
    // Off state shows no age line — there is nothing to report on.
    expect(screen.queryByTestId("live-freshness-age")).not.toBeInTheDocument();
  });

  it("renders the idle state for a never-configured scope", () => {
    mockStatus({
      state: "not_configured",
      newest_reading_at: null,
      age_seconds: null,
      is_stale: false,
      threshold_seconds: 180,
    });
    render(<LiveFreshnessBadge />);

    expect(screen.getByTestId("live-freshness-badge")).toHaveAttribute(
      "data-freshness-state",
      "off",
    );
  });

  it("renders the stale (fault) state distinctly from healthy, with age and threshold", () => {
    mockStatus({
      state: "live",
      newest_reading_at: null,
      age_seconds: null,
      is_stale: true,
      threshold_seconds: 180,
    });
    render(<LiveFreshnessBadge />);

    const badge = screen.getByTestId("live-freshness-badge");
    expect(badge).toHaveAttribute("data-freshness-state", "stale");
    expect(badge).toHaveTextContent("Live feed stalled");
    expect(screen.getByTestId("live-freshness-age")).toHaveTextContent(
      "No reading has ever arrived for this scope",
    );
    expect(screen.getByTestId("live-freshness-age")).toHaveTextContent("stale after 3 minutes");
  });

  it("renders a 27-day-old stale feed as the production incident, not an empty state", () => {
    mockStatus({
      state: "live",
      newest_reading_at: "2026-09-19T09:00:00+00:00",
      age_seconds: 27 * 86400,
      is_stale: true,
      threshold_seconds: 180,
    });
    render(<LiveFreshnessBadge />);

    expect(screen.getByTestId("live-freshness-age")).toHaveTextContent("Last reading 27 days ago");
  });

  it("renders the healthy state distinctly, with a pulsing indicator", () => {
    mockStatus({
      state: "live",
      newest_reading_at: "2026-10-16T09:00:00+00:00",
      age_seconds: 12,
      is_stale: false,
      threshold_seconds: 180,
    });
    render(<LiveFreshnessBadge />);

    const badge = screen.getByTestId("live-freshness-badge");
    expect(badge).toHaveAttribute("data-freshness-state", "healthy");
    expect(badge).toHaveTextContent("Live feed healthy");
    expect(screen.getByTestId("live-freshness-age")).toHaveTextContent(
      "Last reading 12 seconds ago",
    );
  });

  it("never renders '-Ns ago' for a future-dated (negative age) healthy reading", () => {
    mockStatus({
      state: "live",
      newest_reading_at: "2027-01-01T00:00:00+00:00",
      age_seconds: -42,
      is_stale: false,
      threshold_seconds: 180,
    });
    render(<LiveFreshnessBadge />);

    const ageText = screen.getByTestId("live-freshness-age").textContent ?? "";
    expect(ageText).not.toContain("-42");
    expect(ageText).toContain("future-dated");
  });

  it("renders three mutually distinct data-freshness-state values across the three states", () => {
    const states: Array<[Freshness, string]> = [
      [
        {
          state: "paused",
          newest_reading_at: null,
          age_seconds: null,
          is_stale: false,
          threshold_seconds: 180,
        },
        "off",
      ],
      [
        {
          state: "live",
          newest_reading_at: null,
          age_seconds: null,
          is_stale: true,
          threshold_seconds: 180,
        },
        "stale",
      ],
      [
        {
          state: "live",
          newest_reading_at: "2026-10-16T09:00:00+00:00",
          age_seconds: 1,
          is_stale: false,
          threshold_seconds: 180,
        },
        "healthy",
      ],
    ];
    const seen = new Set<string>();
    for (const [freshness, expected] of states) {
      mockStatus(freshness);
      const { unmount } = render(<LiveFreshnessBadge />);
      const actual = screen
        .getByTestId("live-freshness-badge")
        .getAttribute("data-freshness-state");
      expect(actual).toBe(expected);
      seen.add(actual ?? "");
      unmount();
    }
    expect(seen.size).toBe(3);
  });
});
