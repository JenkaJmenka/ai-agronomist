import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchOperationalConfig } from "@/features/configuration/api/operationalConfig";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/** Minimal valid server config document; `co2_enrichment` is added per test. */
function configDocument(extra: Record<string, unknown> = {}) {
  return {
    crop: "tomato",
    setpoints: { temperature: { day: [18, 26], night: [15, 20] } },
    day_window: { start_hour: 6, end_hour: 22 },
    season_map: { "1": "winter", "6": "summer" },
    seasonal_overrides: {},
    ...extra,
  };
}

function mockConfigResponse(document: Record<string, unknown>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ config: document, source: {} }),
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// co2_enrichment
// ---------------------------------------------------------------------------

describe("operational config — co2_enrichment", () => {
  // Regression guard: documents stored before the flag existed must still parse.
  // Defaulting to false is what suppresses permanent CO2-low breaches in houses
  // without enrichment hardware — ambient CO2 (~420 ppm) sits below every crop
  // band minimum, so an enrichment-assuming default would alert on every read.
  it("defaults to false when the server document omits the key", async () => {
    mockConfigResponse(configDocument());

    const result = await fetchOperationalConfig({ tenant: "t1", gh: "gh1" });

    expect(result.config.co2_enrichment).toBe(false);
  });

  it("preserves an explicit true from the server", async () => {
    mockConfigResponse(configDocument({ co2_enrichment: true }));

    const result = await fetchOperationalConfig({ tenant: "t1", gh: "gh1" });

    expect(result.config.co2_enrichment).toBe(true);
  });

  it("preserves an explicit false from the server", async () => {
    mockConfigResponse(configDocument({ co2_enrichment: false }));

    const result = await fetchOperationalConfig({ tenant: "t1", gh: "gh1" });

    expect(result.config.co2_enrichment).toBe(false);
  });
});
