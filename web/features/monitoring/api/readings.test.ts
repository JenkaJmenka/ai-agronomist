import { describe, expect, it } from "vitest";

import { discoverChannels, type SensorRow } from "@/features/monitoring/api/readings";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function canonicalRow(ts: string): SensorRow {
  return { timestamp: ts, temperature: 22, humidity: 65, co2: 420, light: 300 };
}

function fullRow(ts: string): SensorRow {
  return {
    timestamp: ts,
    temperature: 22,
    humidity: 65,
    co2: 420,
    light: 300,
    soil_moisture: 70,
    root_zone_temp: 21,
    vpd: 0.9,
  };
}

// ---------------------------------------------------------------------------
// Basic discovery
// ---------------------------------------------------------------------------

describe("discoverChannels — basic", () => {
  it("returns empty array for empty input", () => {
    expect(discoverChannels([])).toEqual([]);
  });

  it("discovers canonical channels from a single row", () => {
    const found = discoverChannels([canonicalRow("2024-01-01T00:00:00Z")]);
    expect(found).toContain("temperature");
    expect(found).toContain("humidity");
    expect(found).toContain("co2");
    expect(found).toContain("light");
  });

  it("discovers all extra channels when present", () => {
    const found = discoverChannels([fullRow("2024-01-01T00:00:00Z")]);
    expect(found).toContain("soil_moisture");
    expect(found).toContain("root_zone_temp");
    expect(found).toContain("vpd");
  });

  it("excludes non-channel keys (timestamp, anomaly_score, tenant_id, greenhouse_id, id)", () => {
    const row: SensorRow = {
      timestamp: "2024-01-01T00:00:00Z",
      temperature: 22,
      anomaly_score: 0.03,
      tenant_id: "t1",
      greenhouse_id: "gh1",
      id: 1,
    };
    const found = discoverChannels([row]);
    expect(found).not.toContain("timestamp");
    expect(found).not.toContain("anomaly_score");
    expect(found).not.toContain("tenant_id");
    expect(found).not.toContain("greenhouse_id");
    expect(found).not.toContain("id");
    expect(found).toContain("temperature");
  });

  it("excludes non-numeric channel values", () => {
    const row: SensorRow = {
      timestamp: "2024-01-01T00:00:00Z",
      temperature: 22,
      label: "some-string",
    };
    const found = discoverChannels([row]);
    expect(found).toContain("temperature");
    expect(found).not.toContain("label");
  });

  it("does not duplicate channels across rows", () => {
    const rows: SensorRow[] = [
      canonicalRow("2024-01-01T00:00:00Z"),
      canonicalRow("2024-01-01T00:00:02Z"),
    ];
    const found = discoverChannels(rows);
    expect(found).toHaveLength(new Set(found).size);
    expect(found).toContain("temperature");
  });
});

// ---------------------------------------------------------------------------
// F1 regression — extra channels after deep canonical-only history
// ---------------------------------------------------------------------------

describe("discoverChannels — F1 regression", () => {
  it("discovers extra channels even after 100 canonical-only rows precede them", () => {
    // Before the fix, rows.slice(0, 50) sampled only the first 50 historical
    // rows and never reached the live rows carrying extras. The fix samples
    // rows.slice(-50) (newest tail) so extras are always found.
    const canonicalRows: SensorRow[] = Array.from({ length: 100 }, (_, i) =>
      canonicalRow(`2024-01-01T${String(i).padStart(2, "0")}:00:00Z`),
    );
    const liveRows: SensorRow[] = [
      fullRow("2024-01-01T10:00:00Z"),
      fullRow("2024-01-01T10:00:02Z"),
    ];

    const found = discoverChannels([...canonicalRows, ...liveRows]);

    expect(found).toContain("soil_moisture");
    expect(found).toContain("root_zone_temp");
    expect(found).toContain("vpd");
    // Canonical channels must still be present.
    expect(found).toContain("temperature");
    expect(found).toContain("humidity");
    expect(found).toContain("co2");
    expect(found).toContain("light");
  });

  it("still works when there are exactly 50 canonical rows before a live row", () => {
    const canonicalRows: SensorRow[] = Array.from({ length: 50 }, (_, i) =>
      canonicalRow(`2024-01-01T${String(i).padStart(2, "0")}:00:00Z`),
    );
    const found = discoverChannels([...canonicalRows, fullRow("2024-01-01T10:00:00Z")]);
    expect(found).toContain("soil_moisture");
    expect(found).toContain("vpd");
  });

  it("returns only canonical channels when all rows are canonical-only", () => {
    const rows: SensorRow[] = Array.from({ length: 200 }, (_, i) =>
      canonicalRow(`2024-01-01T${String(i).padStart(3, "0")}:00:00Z`),
    );
    const found = discoverChannels(rows);
    expect(found).not.toContain("soil_moisture");
    expect(found).not.toContain("root_zone_temp");
    expect(found).not.toContain("vpd");
    expect(found).toContain("temperature");
  });
});
