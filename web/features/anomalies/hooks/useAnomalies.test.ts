import { describe, expect, it } from "vitest";

import { getSeverity } from "@/lib/severity";

// Pure flagging logic mirrored from useAnomalies (hook itself needs providers;
// the ordering/threshold contract is what must not regress).
function flag(rows: { timestamp: string; anomaly_score?: number | null }[], threshold: number) {
  return rows
    .filter(
      (r): r is { timestamp: string; anomaly_score: number } =>
        typeof r.anomaly_score === "number" && r.anomaly_score >= threshold,
    )
    .map((r) => ({ ...r, severity: getSeverity({ score: r.anomaly_score, threshold }) }))
    .sort((a, b) => b.anomaly_score - a.anomaly_score);
}

describe("anomaly flagging", () => {
  const rows = [
    { timestamp: "2026-07-01T00:00:00", anomaly_score: 0.01 },
    { timestamp: "2026-07-01T01:00:00", anomaly_score: 0.2 },
    { timestamp: "2026-07-01T02:00:00", anomaly_score: null },
    { timestamp: "2026-07-01T03:00:00", anomaly_score: 0.16 },
    { timestamp: "2026-07-01T04:00:00" },
  ];

  it("flags only scored rows at or above threshold", () => {
    const flagged = flag(rows, 0.15);
    expect(flagged.map((f) => f.timestamp)).toEqual(["2026-07-01T01:00:00", "2026-07-01T03:00:00"]);
  });

  it("orders most severe first", () => {
    const flagged = flag(rows, 0.15);
    expect(flagged[0]?.anomaly_score).toBe(0.2);
  });

  it("empty when threshold above every score", () => {
    expect(flag(rows, 0.9)).toHaveLength(0);
  });
});
