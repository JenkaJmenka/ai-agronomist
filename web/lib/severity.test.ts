import { describe, expect, it } from "vitest";

import { getSeverity, resolveRowSeverity, SEVERITY_ORDER, type Severity } from "./severity";

// ─── getSeverity (fallback path) ─────────────────────────────────────────────

describe("getSeverity", () => {
  it("returns normal below threshold", () => {
    expect(getSeverity({ score: 0.05, threshold: 0.15 })).toBe("normal");
  });

  it("returns warning when score meets threshold", () => {
    expect(getSeverity({ score: 0.2, threshold: 0.15 })).toBe("warning");
  });

  it("treats a score equal to threshold as flagged (strict below-threshold check)", () => {
    expect(getSeverity({ score: 0.15, threshold: 0.15 })).toBe("warning");
  });
});

// ─── resolveRowSeverity ───────────────────────────────────────────────────────

describe("resolveRowSeverity", () => {
  it("prefers detected_severity: urgent over score-based classification", () => {
    // Without server override this would be "warning"; with it, must be "urgent".
    const result = resolveRowSeverity({
      score: 0.9,
      threshold: 0.15,
      detectedSeverity: "urgent",
    });
    expect(result).toBe<Severity>("urgent");
  });

  it("prefers detected_severity: critical", () => {
    expect(
      resolveRowSeverity({ score: 0.2, threshold: 0.15, detectedSeverity: "critical" }),
    ).toBe<Severity>("critical");
  });

  it("prefers detected_severity: lstm", () => {
    expect(
      resolveRowSeverity({ score: 0.2, threshold: 0.15, detectedSeverity: "lstm" }),
    ).toBe<Severity>("lstm");
  });

  it("falls back to score/threshold when detectedSeverity is null", () => {
    expect(
      resolveRowSeverity({ score: 0.2, threshold: 0.15, detectedSeverity: null }),
    ).toBe<Severity>("warning");
  });

  it("falls back to score/threshold when detectedSeverity is undefined", () => {
    expect(resolveRowSeverity({ score: 0.2, threshold: 0.15 })).toBe<Severity>("warning");
  });

  it("falls back to normal via score path when detectedSeverity is absent and score is low", () => {
    expect(resolveRowSeverity({ score: 0.05, threshold: 0.15 })).toBe<Severity>("normal");
  });

  it("ignores unknown detected_severity strings and falls back", () => {
    // An unknown string from a future API version must not produce undefined.
    const result = resolveRowSeverity({
      score: 0.2,
      threshold: 0.15,
      detectedSeverity: "supercritical",
    });
    expect(result).toBe<Severity>("warning");
  });
});

// ─── SEVERITY_ORDER ───────────────────────────────────────────────────────────

describe("SEVERITY_ORDER", () => {
  it("places urgent above critical", () => {
    expect(SEVERITY_ORDER.urgent).toBeGreaterThan(SEVERITY_ORDER.critical);
  });

  it("places critical above lstm", () => {
    expect(SEVERITY_ORDER.critical).toBeGreaterThan(SEVERITY_ORDER.lstm);
  });

  it("places lstm above warning", () => {
    expect(SEVERITY_ORDER.lstm).toBeGreaterThan(SEVERITY_ORDER.warning);
  });

  it("places warning above normal", () => {
    expect(SEVERITY_ORDER.warning).toBeGreaterThan(SEVERITY_ORDER.normal);
  });

  it("urgent is the highest severity", () => {
    const allOrders = Object.values(SEVERITY_ORDER);
    expect(SEVERITY_ORDER.urgent).toBe(Math.max(...allOrders));
  });
});

// ─── Severity label coverage ──────────────────────────────────────────────────

describe("urgent label and style coverage", () => {
  it("SEVERITY_ORDER has an entry for urgent", () => {
    expect(SEVERITY_ORDER.urgent).toBeDefined();
    expect(typeof SEVERITY_ORDER.urgent).toBe("number");
  });

  it("all five severity levels have a defined order weight", () => {
    const severities: Severity[] = ["normal", "warning", "lstm", "critical", "urgent"];
    for (const sev of severities) {
      expect(SEVERITY_ORDER[sev]).toBeDefined();
    }
  });
});

// ─── Severity group ordering (captures the original bug) ─────────────────────

describe("severity group ordering", () => {
  /**
   * Simulates the groupClientSide logic for the severity groupBy case.
   * A fixture with mixed severities must produce more than one group.
   * Before the fix, getSeverity could only return "normal" or "warning" for
   * rows without detector/agreement, so all flagged rows landed in "warning" —
   * one group — which made the severity groupBy useless.
   */
  it("fixture list containing mixed detected_severities produces multiple groups", () => {
    const rows = [
      { score: 0.9, threshold: 0.15, detectedSeverity: "urgent" },
      { score: 0.8, threshold: 0.15, detectedSeverity: "critical" },
      { score: 0.7, threshold: 0.15, detectedSeverity: "warning" },
      { score: 0.6, threshold: 0.15, detectedSeverity: "warning" },
    ];
    const severities = rows.map(resolveRowSeverity);
    const groups = new Set(severities);
    expect(groups.size).toBeGreaterThan(1);
  });

  it("without server severity all rows fall into at most two groups (pre-migration rows)", () => {
    // Rows predating migration 024 have no detected_severity; score-based
    // classification can only produce "normal" or "warning".
    const rows = [
      { score: 0.9, threshold: 0.15 },
      { score: 0.8, threshold: 0.15 },
      { score: 0.05, threshold: 0.15 },
    ];
    const severities = rows.map(resolveRowSeverity);
    const groups = new Set(severities);
    // normal + warning = 2 groups maximum.
    expect(groups.size).toBeLessThanOrEqual(2);
  });

  it("urgent sorts before critical in descending SEVERITY_ORDER", () => {
    const urgentOrder = SEVERITY_ORDER.urgent;
    const criticalOrder = SEVERITY_ORDER.critical;
    // Descending sort: urgent group should appear first.
    const sorted = [
      { key: "warning", order: SEVERITY_ORDER.warning },
      { key: "urgent", order: urgentOrder },
      { key: "critical", order: criticalOrder },
    ].sort((a, b) => b.order - a.order);
    expect(sorted[0]?.key).toBe("urgent");
    expect(sorted[1]?.key).toBe("critical");
  });
});
