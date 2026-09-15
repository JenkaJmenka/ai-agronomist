import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AnomalyEpisode } from "@/features/anomalies/api/episodes";
import { EpisodeSummary } from "@/features/anomalies/components/EpisodeSummary";

// The shared vitest setup has no RTL auto-cleanup (see SensorTile.test.tsx).
afterEach(cleanup);

function buildEpisode(overrides: Partial<AnomalyEpisode> = {}): AnomalyEpisode {
  return {
    episode_id: "humidity:statistical:drop:2026-01-01T00:00:00Z",
    channel: "humidity",
    detector: "statistical",
    direction: "drop",
    start_ts: "2026-01-01T00:00:00Z",
    end_ts: "2026-01-01T00:45:00Z",
    duration_seconds: 2700,
    sample_count: 10,
    peak_score: 0.85,
    peak_ts: "2026-01-01T00:30:00Z",
    severity: "warning",
    first_value: 32,
    last_value: 30,
    peak_value: 29,
    trend: "worsening",
    movement: "falling",
    slope_per_hour: -2.7,
    affected_sensors: ["humidity"],
    headline: "Unexpected humidity drop: 29.0% (50% lower bound), worsening over 45 min",
    is_truncated_start: false,
    is_truncated_end: false,
    samples: [],
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Expand vs. zoom decoupling — the regression: expanding an episode used to
// zoom the chart and silently replace the operator's date range.
// ---------------------------------------------------------------------------

describe("EpisodeSummary — expand/zoom decoupling", () => {
  it("clicking the summary body toggles the disclosure without zooming", () => {
    const onZoomToSpan = vi.fn();
    render(
      <details data-testid="episode-details">
        <EpisodeSummary episode={buildEpisode()} onZoomToSpan={onZoomToSpan} />
      </details>,
    );
    const details = screen.getByTestId("episode-details") as HTMLDetailsElement;
    expect(details.open).toBe(false);

    fireEvent.click(screen.getByTestId("episode-summary"));

    expect(details.open).toBe(true);
    expect(onZoomToSpan).not.toHaveBeenCalled();
  });

  it('clicking "Zoom to span" calls the callback without toggling the disclosure', () => {
    const onZoomToSpan = vi.fn();
    render(
      <details data-testid="episode-details">
        <EpisodeSummary episode={buildEpisode()} onZoomToSpan={onZoomToSpan} />
      </details>,
    );
    const details = screen.getByTestId("episode-details") as HTMLDetailsElement;

    fireEvent.click(screen.getByTestId("episode-zoom-button"));

    expect(onZoomToSpan).toHaveBeenCalledTimes(1);
    // The button's own click handler prevents the native toggle, so the
    // explicit act of zooming must not also expand the episode.
    expect(details.open).toBe(false);
  });

  it("omits the zoom control when no callback is wired up (e.g. Table view)", () => {
    render(
      <details>
        <EpisodeSummary episode={buildEpisode()} />
      </details>,
    );
    expect(screen.queryByTestId("episode-zoom-button")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Severity rendering — "unclassified" must render its own distinguishable
// chip, not fall through to the unstyled score span.
// ---------------------------------------------------------------------------

describe("EpisodeSummary — severity rendering", () => {
  it("renders a styled chip for a known severity", () => {
    render(
      <details>
        <EpisodeSummary episode={buildEpisode({ severity: "critical" })} />
      </details>,
    );
    const chip = screen.getByText("0.850");
    expect(chip.getAttribute("data-severity")).toBe("critical");
  });

  it("renders 'unclassified' as its own chip instead of the unstyled fallback", () => {
    render(
      <details>
        <EpisodeSummary episode={buildEpisode({ severity: "unclassified" })} />
      </details>,
    );
    const chip = screen.getByText("0.850");
    expect(chip.getAttribute("data-severity")).toBe("unclassified");
    // Not styled with a real-severity color — a muted/neutral chip instead.
    expect(chip.className).toContain("text-text-muted");
  });

  it("falls back to the unstyled score when the server sends an unrecognized value", () => {
    render(
      <details>
        <EpisodeSummary episode={buildEpisode({ severity: "totally-unknown" })} />
      </details>,
    );
    const score = screen.getByText("0.850");
    expect(score.getAttribute("data-severity")).toBeNull();
  });
});
