import { describe, expect, it } from "vitest";

import type { AnomalyEpisode } from "@/features/anomalies/api/episodes";
import {
  channelUnit,
  directionArrow,
  formatChannel,
  formatDuration,
  formatEpisodeSpan,
  formatTrend,
  formatValueRange,
} from "@/features/anomalies/lib/formatEpisode";

function episode(overrides: Partial<AnomalyEpisode> = {}): AnomalyEpisode {
  return {
    episode_id: "humidity:statistical:drop:2026-12-25T09:55:00+00:00",
    channel: "humidity",
    detector: "statistical",
    direction: "drop",
    start_ts: "2026-12-25T09:55:00+00:00",
    end_ts: "2026-12-25T10:40:00+00:00",
    duration_seconds: 2700,
    sample_count: 10,
    peak_score: 0.85,
    peak_ts: "2026-12-25T10:00:00+00:00",
    severity: "warning",
    first_value: 32,
    last_value: 30,
    peak_value: 29,
    trend: "worsening",
    slope_per_hour: -2.7,
    affected_sensors: ["humidity"],
    headline: "Unexpected humidity drop: 29.0% (50% lower bound), worsening over 45 min",
    is_truncated_start: false,
    is_truncated_end: false,
    samples: [],
    ...overrides,
  };
}

describe("formatDuration", () => {
  it("renders sub-hour spans in minutes", () => {
    expect(formatDuration(2700)).toBe("45 min");
  });

  it("renders spans of an hour or more in hours", () => {
    expect(formatDuration(9000)).toBe("2.5 hr");
  });

  it("names a zero-length span rather than printing 0 min", () => {
    expect(formatDuration(0)).toBe("single reading");
  });
});

describe("formatEpisodeSpan", () => {
  it("collapses the repeated date when an episode stays within one day", () => {
    expect(formatEpisodeSpan("2026-12-25T09:55:00+00:00", "2026-12-25T10:40:00+00:00")).toBe(
      "2026-12-25 09:55 → 10:40",
    );
  });

  it("keeps both dates when an episode crosses midnight", () => {
    expect(formatEpisodeSpan("2026-12-25T23:50:00+00:00", "2026-12-26T00:10:00+00:00")).toBe(
      "2026-12-25 23:50 → 2026-12-26 00:10",
    );
  });

  it("renders a single timestamp for a one-sample episode", () => {
    const ts = "2026-12-25T09:55:00+00:00";
    expect(formatEpisodeSpan(ts, ts)).toBe("2026-12-25 09:55");
  });
});

describe("formatValueRange", () => {
  it("runs from the opening value to the extremum, not the closing value", () => {
    // The episode ends at 30% but bottomed at 29% — the low is what matters.
    expect(formatValueRange(episode(), "%")).toBe("32.0% → 29.0%");
  });

  it("renders a single value when the channel never moved", () => {
    expect(formatValueRange(episode({ first_value: 29, peak_value: 29 }), "%")).toBe("29.0%");
  });

  it("is empty when no trigger value was recorded", () => {
    expect(formatValueRange(episode({ first_value: null, peak_value: null }), "%")).toBe("");
  });

  it("falls back to whichever value exists", () => {
    expect(formatValueRange(episode({ first_value: null, peak_value: 29 }), "%")).toBe("29.0%");
  });
});

describe("directionArrow", () => {
  it("points down for a drop and up for a spike", () => {
    expect(directionArrow("drop")).toBe("↓");
    expect(directionArrow("spike")).toBe("↑");
  });

  it("uses a neutral mark when direction is unknown", () => {
    expect(directionArrow(null)).toBe("•");
  });
});

describe("formatTrend", () => {
  it("renders flat as steady so it reads as a state, not a missing value", () => {
    expect(formatTrend("flat")).toBe("steady");
    expect(formatTrend("worsening")).toBe("worsening");
    expect(formatTrend("easing")).toBe("easing");
  });
});

describe("channelUnit and formatChannel", () => {
  it("maps known channels to their units", () => {
    expect(channelUnit("humidity")).toBe("%");
    expect(channelUnit("temperature")).toBe("°C");
  });

  it("returns no unit for an unknown channel", () => {
    expect(channelUnit("measured_pipe_1_temp")).toBe("");
  });

  it("sentence-cases the channel name", () => {
    expect(formatChannel("humidity")).toBe("Humidity");
    expect(formatChannel("root_zone_temp")).toBe("Root zone temp");
  });
});
