import { describe, expect, it } from "vitest";

import {
  describeReadingAge,
  describeStaleThreshold,
  formatDurationSeconds,
} from "@/features/monitoring/lib/freshnessFormat";

describe("formatDurationSeconds", () => {
  it("formats sub-minute durations in seconds", () => {
    expect(formatDurationSeconds(42)).toBe("42 seconds");
  });

  it("singularizes a 1-second duration", () => {
    expect(formatDurationSeconds(1)).toBe("1 second");
  });

  it("formats minute-scale durations", () => {
    expect(formatDurationSeconds(125)).toBe("2 minutes");
  });

  it("formats hour-scale durations", () => {
    expect(formatDurationSeconds(3 * 3600 + 10)).toBe("3 hours");
  });

  it("formats day-scale durations", () => {
    // 27 days — the production incident this feature exists to surface.
    expect(formatDurationSeconds(27 * 86400)).toBe("27 days");
  });

  it("singularizes a 1-day duration", () => {
    expect(formatDurationSeconds(86400)).toBe("1 day");
  });

  it("floors fractional seconds instead of rounding", () => {
    expect(formatDurationSeconds(1.9)).toBe("1 second");
  });
});

describe("describeReadingAge", () => {
  it("reports a never-arrived reading distinctly from any numeric age", () => {
    expect(describeReadingAge(null)).toBe("No reading has ever arrived for this scope");
  });

  it("describes a positive age as time elapsed", () => {
    expect(describeReadingAge(27 * 86400)).toBe("Last reading 27 days ago");
  });

  it("describes a negative age as future-dated, never as an error", () => {
    const result = describeReadingAge(-42);
    expect(result).toContain("ahead of now");
    expect(result).toContain("future-dated");
    expect(result).not.toMatch(/error/i);
    // Never render the raw negative number / Math.abs sign flip as the message.
    expect(result).not.toContain("-42");
  });

  it("treats zero age as elapsed, not future", () => {
    expect(describeReadingAge(0)).toBe("Last reading 0 seconds ago");
  });
});

describe("describeStaleThreshold", () => {
  it("phrases the configured threshold in human units", () => {
    expect(describeStaleThreshold(180)).toBe("stale after 3 minutes with no reading");
  });
});
