import { describe, expect, it } from "vitest";

import { getSeverity } from "./severity";

describe("getSeverity", () => {
  it("returns normal below threshold regardless of detector", () => {
    expect(getSeverity({ score: 0.05, threshold: 0.15, agreement: true })).toBe("normal");
  });

  it("returns critical when detectors agree above threshold", () => {
    expect(getSeverity({ score: 0.2, threshold: 0.15, agreement: true })).toBe("critical");
  });

  it("returns lstm for lstm/drift detectors above threshold", () => {
    expect(getSeverity({ score: 0.2, threshold: 0.15, detector: "lstm" })).toBe("lstm");
    expect(getSeverity({ score: 0.2, threshold: 0.15, detector: "cusum_drift" })).toBe("lstm");
  });

  it("returns warning for single-sensor statistical flags", () => {
    expect(getSeverity({ score: 0.2, threshold: 0.15, detector: "statistical" })).toBe("warning");
  });

  it("treats a score equal to threshold as flagged (strict below-threshold check)", () => {
    expect(getSeverity({ score: 0.15, threshold: 0.15 })).toBe("warning");
  });
});
