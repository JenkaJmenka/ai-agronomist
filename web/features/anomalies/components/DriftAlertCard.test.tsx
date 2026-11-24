import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AnomalyEpisode } from "@/features/anomalies/api/episodes";
import {
  DriftAlertCard,
  isDriftWatchEpisode,
} from "@/features/anomalies/components/DriftAlertCard";
import { EpisodeTable } from "@/features/anomalies/components/EpisodeTable";

const HREF = "/drift-watch?tenant=t1&gh=gh_A";

const driftEpisode: AnomalyEpisode = {
  episode_id: "drift_watch:abc",
  channel: "temperature",
  detector: "drift_watch",
  direction: null,
  start_ts: "2026-11-20T04:00:00Z",
  end_ts: "2026-11-20T09:00:00Z",
  duration_seconds: 18000,
  sample_count: 0,
  peak_score: 0,
  peak_ts: "2026-11-20T04:00:00Z",
  severity: "warning",
  first_value: null,
  last_value: null,
  peak_value: null,
  trend: "flat",
  movement: "steady",
  slope_per_hour: null,
  affected_sensors: [],
  headline: "Calibration drift",
  is_truncated_start: false,
  is_truncated_end: false,
  samples: [],
  frozen_sample_count: null,
  raw_slot_count: null,
  drift_offset: null,
  drift_input_id: null,
  drift_first_detected_ts: null,
  drift_input_kind: null,
  drift_message: null,
};

vi.mock("@/features/anomalies/hooks/useAnomalyEpisodes", () => ({
  useAnomalyEpisodes: () => ({
    episodes: [driftEpisode],
    anomalyCount: 0,
    isLoading: false,
    isRefetching: false,
    isError: false,
    error: null,
  }),
}));
vi.mock("@/features/anomalies/hooks/useDriftWatchHref", () => ({
  useDriftWatchHref: () => "/drift-watch?tenant=t1&gh=gh_A",
}));

afterEach(cleanup);

describe("DriftAlertCard", () => {
  it("falls back to neutral text, start_ts and no offset when drift fields are null", () => {
    render(<DriftAlertCard episode={driftEpisode} href={HREF} />);
    const card = screen.getByTestId("drift-alert-card").textContent ?? "";
    expect(card).toContain("temperature");
    expect(card).toContain("First detected 2026-11-20 04:00");
    const text = screen.getByTestId("drift-alert-text").textContent ?? "";
    expect(text).toContain("cannot tell which");
    expect(text).not.toMatch(/probe (is|has) (faulty|drifted)/i);
    expect(screen.queryByTestId("drift-alert-offset")).toBeNull();
    const chip = screen.getByTestId("drift-alert-severity").firstElementChild;
    expect(chip?.getAttribute("data-severity")).toBe("warning");
    expect(screen.getByTestId("drift-alert-severity").textContent).toBe("Advisory");
  });

  it("renders drift_message as given and prefers drift_first_detected_ts", () => {
    const message = "Probe A reads 1.2 °C lower than probe B. Check A first.";
    render(
      <DriftAlertCard
        episode={{
          ...driftEpisode,
          drift_message: message,
          drift_first_detected_ts: "2026-11-19T22:30:00Z",
        }}
        href={HREF}
      />,
    );
    expect(screen.getByTestId("drift-alert-text").textContent).toBe(message);
    expect(screen.getByTestId("drift-alert-card").textContent).toContain(
      "First detected 2026-11-19 22:30",
    );
  });

  it.each([
    ["temperature", -1.24, "−1.2 °C"],
    ["humidity", 3.5, "+3.5 %RH"],
    ["measured_hd", -0.8, "−0.8 g/m³"],
  ])("shows the signed %s offset with its unit", (channel, offset, expected) => {
    render(
      <DriftAlertCard episode={{ ...driftEpisode, channel, drift_offset: offset }} href={HREF} />,
    );
    expect(screen.getByTestId("drift-alert-offset").textContent).toBe(`Offset ${expected}`);
  });

  it("links to Drift Watch and only drift_watch rows are gated in", () => {
    render(<DriftAlertCard episode={driftEpisode} href={HREF} />);
    expect(screen.getByTestId("drift-alert-link").getAttribute("href")).toBe(HREF);
    expect(isDriftWatchEpisode({ detector: "drift_watch" })).toBe(true);
    expect(isDriftWatchEpisode({ detector: "statistical" })).toBe(false);
  });

  it("EpisodeTable renders a drift episode with no samples or direction, without a score badge", () => {
    render(<EpisodeTable windowStart="a" windowEnd="b" threshold={0.5} anomalyCount={0} />);
    expect(screen.getByTestId("drift-alert-card")).toBeTruthy();
    const summary = screen.getByTestId("episode-summary").textContent ?? "";
    expect(summary).toContain("Temperature drift");
    expect(summary).not.toContain("0.000");
    expect(summary).not.toMatch(/0 samples/);
    expect(screen.queryByText(/highest-scoring/)).toBeNull();
  });
});
