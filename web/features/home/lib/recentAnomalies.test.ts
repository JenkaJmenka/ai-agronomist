import { describe, expect, it } from "vitest";

import type { AnomalyRow } from "@/features/anomalies/hooks/useAnomalies";
import { buildRecentAnomalies, countSeverities } from "@/features/home/lib/recentAnomalies";
import type { LiveAnomalyItem } from "@/features/monitoring/hooks/useLiveReadings";

const liveItem = (overrides: Partial<LiveAnomalyItem> = {}): LiveAnomalyItem => ({
  seq: "s1",
  timestamp: "2026-09-08T12:00:00.000Z",
  score: 0.4,
  threshold: 0.15,
  affected_channels: ["temperature"],
  reasoning: "",
  window_start: "2026-09-08T11:30:00.000Z",
  window_end: "2026-09-08T12:30:00.000Z",
  ...overrides,
});

const flagged = (overrides: Partial<AnomalyRow> = {}): AnomalyRow =>
  ({
    timestamp: "2026-09-08T10:00:00.000Z",
    anomaly_score: 0.3,
    score: 0.3,
    severity: "warning",
    temperature: 30,
    humidity: 60,
    ...overrides,
  }) as AnomalyRow;

describe("buildRecentAnomalies", () => {
  it("prefers live anomalies when live and non-empty", () => {
    const items = buildRecentAnomalies({
      live: true,
      liveAnomalies: [liveItem()],
      flagged: [flagged()],
      threshold: 0.15,
    });
    expect(items).toHaveLength(1);
    expect(items[0]?.id).toBe("s1");
    expect(items[0]?.sensors).toEqual(["temperature"]);
  });

  it("uses flagged rows when not live", () => {
    const items = buildRecentAnomalies({
      live: false,
      liveAnomalies: [liveItem()],
      flagged: [flagged()],
      threshold: 0.15,
    });
    expect(items).toHaveLength(1);
    expect(items[0]?.id).toContain("flagged:");
    expect(items[0]?.sensors).toContain("temperature");
  });

  it("caps at limit", () => {
    const items = buildRecentAnomalies({
      live: false,
      liveAnomalies: [],
      flagged: [flagged(), flagged({ timestamp: "2026-09-08T09:00:00.000Z", score: 0.2 })],
      threshold: 0.15,
      limit: 1,
    });
    expect(items).toHaveLength(1);
  });
});

describe("countSeverities", () => {
  it("counts non-normal severities", () => {
    expect(
      countSeverities([
        {
          id: "a",
          timestamp: "t",
          score: 1,
          severity: "critical",
          sensors: [],
          windowStart: "t",
          windowEnd: "t",
        },
        {
          id: "b",
          timestamp: "t",
          score: 1,
          severity: "warning",
          sensors: [],
          windowStart: "t",
          windowEnd: "t",
        },
        {
          id: "c",
          timestamp: "t",
          score: 1,
          severity: "lstm",
          sensors: [],
          windowStart: "t",
          windowEnd: "t",
        },
      ]),
    ).toEqual({ critical: 1, warning: 1, lstm: 1 });
  });
});
