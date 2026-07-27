import { describe, expect, it } from "vitest";

import { SERVER_GROUPING_THRESHOLD } from "@/features/anomalies/api/flaggedWindow";
import { resolveGroupingMode } from "@/features/anomalies/lib/groupingDecision";

describe("resolveGroupingMode", () => {
  it("returns 'loading' when count is null — never commits to client path prematurely", () => {
    // Regression guard: while the cheap count fetch is in flight, the caller
    // must not render the client path (which would show blank enrichment cells
    // because its row subset diverges from the enriched top-500 set).
    expect(resolveGroupingMode(null)).toBe("loading");
  });

  it("returns 'server' when count meets the threshold", () => {
    expect(resolveGroupingMode(SERVER_GROUPING_THRESHOLD)).toBe("server");
  });

  it("returns 'server' when count exceeds the threshold", () => {
    expect(resolveGroupingMode(SERVER_GROUPING_THRESHOLD + 1)).toBe("server");
    expect(resolveGroupingMode(2649)).toBe("server");
  });

  it("returns 'client' when count is below the threshold", () => {
    expect(resolveGroupingMode(SERVER_GROUPING_THRESHOLD - 1)).toBe("client");
  });

  it("returns 'client' for small counts", () => {
    expect(resolveGroupingMode(0)).toBe("client");
    expect(resolveGroupingMode(1)).toBe("client");
    expect(resolveGroupingMode(35)).toBe("client");
  });
});
