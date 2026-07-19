import { describe, expect, it } from "vitest";

import {
  anomalyDetailEventSchema,
  heartbeatEventSchema,
  liveAnomalyEventSchema,
  liveReadingEventSchema,
  resetEventSchema,
  sourceStatusEventSchema,
  statusResponseSchema,
  toSensorRow,
} from "@/features/monitoring/api/live";

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

const VALID_READING = {
  seq: "",
  timestamp: "2026-07-18T10:00:00Z",
  tenant_id: "tenant_1",
  greenhouse_id: "gh_A",
  channels: { temperature: 22.5, humidity: 65.0, co2: 420.0 },
  sources: { temperature: "src_1", humidity: "src_1", co2: "src_2" },
  anomaly_score: null,
};

const VALID_ANOMALY = {
  seq: "abc-123",
  timestamp: "2026-07-18T10:01:00Z",
  tenant_id: "tenant_1",
  greenhouse_id: "gh_A",
  score: 0.91,
  threshold: 0.15,
  affected_channels: ["co2", "temperature"],
  reasoning: "Co2 spike detected",
  window_start: "2026-07-18T09:41:00Z",
  window_end: "2026-07-18T10:01:00Z",
};

const VALID_ANOMALY_DETAIL = {
  anomaly_seq: "abc-123",
  tenant_id: "tenant_1",
  greenhouse_id: "gh_A",
  reasoning: "Co2 spike: fan failure suspected",
  fault_type: "spike_fault",
  root_cause: "Fan failure",
  // UUID string — matches the Incident model's UUID PK (CONTRACT 3 / N2).
  incident_id: "550e8400-e29b-41d4-a716-446655440000",
};

const VALID_SOURCE_STATUS = {
  source_id: "src_1",
  vendor: "acme",
  status: "online" as const,
  last_seen: "2026-07-18T10:00:00Z",
};

// ---------------------------------------------------------------------------
// liveReadingEventSchema
// ---------------------------------------------------------------------------

describe("liveReadingEventSchema", () => {
  it("parses a valid reading event", () => {
    const result = liveReadingEventSchema.safeParse(VALID_READING);
    expect(result.success).toBe(true);
    if (!result.success) return;
    expect(result.data.channels).toEqual(VALID_READING.channels);
    expect(result.data.anomaly_score).toBeNull();
  });

  it("accepts a non-null anomaly_score", () => {
    const result = liveReadingEventSchema.safeParse({
      ...VALID_READING,
      anomaly_score: 0.42,
    });
    expect(result.success).toBe(true);
    if (!result.success) return;
    expect(result.data.anomaly_score).toBe(0.42);
  });

  it("accepts a reading without anomaly_score (undefined)", () => {
    const { anomaly_score: _omit, ...withoutScore } = VALID_READING;
    const result = liveReadingEventSchema.safeParse(withoutScore);
    expect(result.success).toBe(true);
  });

  it("fails on missing required fields", () => {
    const { timestamp: _omit, ...missing } = VALID_READING;
    expect(liveReadingEventSchema.safeParse(missing).success).toBe(false);
  });

  it("fails when channels contains non-numeric value", () => {
    const result = liveReadingEventSchema.safeParse({
      ...VALID_READING,
      channels: { temperature: "hot" },
    });
    expect(result.success).toBe(false);
  });

  it("allows unknown extra fields (passthrough)", () => {
    const result = liveReadingEventSchema.safeParse({ ...VALID_READING, extra_field: true });
    expect(result.success).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// liveAnomalyEventSchema
// ---------------------------------------------------------------------------

describe("liveAnomalyEventSchema", () => {
  it("parses a valid anomaly event", () => {
    const result = liveAnomalyEventSchema.safeParse(VALID_ANOMALY);
    expect(result.success).toBe(true);
    if (!result.success) return;
    expect(result.data.affected_channels).toEqual(["co2", "temperature"]);
  });

  it("fails on missing seq", () => {
    const { seq: _omit, ...missing } = VALID_ANOMALY;
    expect(liveAnomalyEventSchema.safeParse(missing).success).toBe(false);
  });

  it("fails when score is not a number", () => {
    expect(liveAnomalyEventSchema.safeParse({ ...VALID_ANOMALY, score: "high" }).success).toBe(
      false,
    );
  });
});

// ---------------------------------------------------------------------------
// anomalyDetailEventSchema
// ---------------------------------------------------------------------------

describe("anomalyDetailEventSchema", () => {
  it("parses a valid anomaly-detail event", () => {
    const result = anomalyDetailEventSchema.safeParse(VALID_ANOMALY_DETAIL);
    expect(result.success).toBe(true);
    if (!result.success) return;
    expect(result.data.anomaly_seq).toBe("abc-123");
    expect(result.data.fault_type).toBe("spike_fault");
    // incident_id is a UUID string (CONTRACT 3).
    expect(result.data.incident_id).toBe("550e8400-e29b-41d4-a716-446655440000");
  });

  it("rejects a numeric incident_id (must be string per CONTRACT 3)", () => {
    const result = anomalyDetailEventSchema.safeParse({
      ...VALID_ANOMALY_DETAIL,
      incident_id: 42,
    });
    expect(result.success).toBe(false);
  });

  it("accepts null optional fields", () => {
    const result = anomalyDetailEventSchema.safeParse({
      ...VALID_ANOMALY_DETAIL,
      fault_type: null,
      root_cause: null,
      incident_id: null,
    });
    expect(result.success).toBe(true);
  });

  it("accepts missing optional fields", () => {
    const { fault_type: _f, root_cause: _r, incident_id: _i, ...minimal } = VALID_ANOMALY_DETAIL;
    expect(anomalyDetailEventSchema.safeParse(minimal).success).toBe(true);
  });

  it("fails on missing anomaly_seq", () => {
    const { anomaly_seq: _omit, ...missing } = VALID_ANOMALY_DETAIL;
    expect(anomalyDetailEventSchema.safeParse(missing).success).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// sourceStatusEventSchema
// ---------------------------------------------------------------------------

describe("sourceStatusEventSchema", () => {
  it("parses a valid source-status event", () => {
    const result = sourceStatusEventSchema.safeParse(VALID_SOURCE_STATUS);
    expect(result.success).toBe(true);
    if (!result.success) return;
    expect(result.data.status).toBe("online");
  });

  it("accepts all three status values", () => {
    for (const status of ["online", "degraded", "offline"] as const) {
      expect(sourceStatusEventSchema.safeParse({ ...VALID_SOURCE_STATUS, status }).success).toBe(
        true,
      );
    }
  });

  it("fails on an unknown status value", () => {
    expect(
      sourceStatusEventSchema.safeParse({ ...VALID_SOURCE_STATUS, status: "unknown" }).success,
    ).toBe(false);
  });

  it("accepts null last_seen", () => {
    expect(
      sourceStatusEventSchema.safeParse({ ...VALID_SOURCE_STATUS, last_seen: null }).success,
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// heartbeatEventSchema
// ---------------------------------------------------------------------------

describe("heartbeatEventSchema", () => {
  it("parses an empty object (ts is optional)", () => {
    expect(heartbeatEventSchema.safeParse({}).success).toBe(true);
  });

  it("parses a heartbeat with ts (backend field — routes/live.py:43 sends {ts: ...})", () => {
    const result = heartbeatEventSchema.safeParse({ ts: "2026-07-18T10:00:00Z" });
    expect(result.success).toBe(true);
    if (!result.success) return;
    expect(result.data.ts).toBe("2026-07-18T10:00:00Z");
  });

  it("allows unknown extra fields (passthrough)", () => {
    expect(heartbeatEventSchema.safeParse({ uptime: 42 }).success).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// resetEventSchema
// ---------------------------------------------------------------------------

describe("resetEventSchema", () => {
  it("parses an empty object", () => {
    expect(resetEventSchema.safeParse({}).success).toBe(true);
  });

  it("parses a reset with reason", () => {
    const result = resetEventSchema.safeParse({ reason: "config_reload" });
    expect(result.success).toBe(true);
    if (!result.success) return;
    expect(result.data.reason).toBe("config_reload");
  });
});

// ---------------------------------------------------------------------------
// statusResponseSchema  (GET /api/bff/live/status — CONTRACT 2)
// ---------------------------------------------------------------------------

describe("statusResponseSchema", () => {
  it("parses a well-formed status response", () => {
    const result = statusResponseSchema.safeParse({
      sources: [
        { source_id: "src_1", vendor: "acme", status: "online", last_seen: "2026-07-18T10:00:00Z" },
      ],
    });
    expect(result.success).toBe(true);
    if (!result.success) return;
    expect(result.data.sources).toHaveLength(1);
    expect(result.data.sources[0]?.status).toBe("online");
    expect(result.data.sources[0]?.vendor).toBe("acme");
  });

  it("accepts sources without last_seen (nullable/optional per CONTRACT 2)", () => {
    const result = statusResponseSchema.safeParse({
      sources: [{ source_id: "src_1", vendor: "acme", status: "online" }],
    });
    expect(result.success).toBe(true);
  });

  it("accepts last_seen: null", () => {
    const result = statusResponseSchema.safeParse({
      sources: [{ source_id: "src_1", vendor: "acme", status: "online", last_seen: null }],
    });
    expect(result.success).toBe(true);
  });

  it("accepts an empty sources array", () => {
    expect(statusResponseSchema.safeParse({ sources: [] }).success).toBe(true);
  });

  it("accepts all three status values across sources", () => {
    const statuses = ["online", "degraded", "offline"] as const;
    const result = statusResponseSchema.safeParse({
      sources: statuses.map((status, i) => ({
        source_id: `src_${i}`,
        vendor: "acme",
        status,
      })),
    });
    expect(result.success).toBe(true);
    if (!result.success) return;
    expect(result.data.sources).toHaveLength(3);
  });
});

// ---------------------------------------------------------------------------
// toSensorRow
// ---------------------------------------------------------------------------

describe("toSensorRow", () => {
  it("flattens channels onto the top-level object", () => {
    const row = toSensorRow({
      seq: "",
      timestamp: "2026-07-18T10:00:00Z",
      tenant_id: "t1",
      greenhouse_id: "gh_A",
      channels: { temperature: 22.5, humidity: 65.0 },
      sources: { temperature: "src_1", humidity: "src_1" },
      anomaly_score: 0.12,
    });
    expect(row.timestamp).toBe("2026-07-18T10:00:00Z");
    expect(row.temperature).toBe(22.5);
    expect(row.humidity).toBe(65.0);
    expect(row.anomaly_score).toBe(0.12);
  });

  it("maps undefined anomaly_score to null", () => {
    const row = toSensorRow({
      seq: "",
      timestamp: "2026-07-18T10:00:00Z",
      tenant_id: "t1",
      greenhouse_id: "gh_A",
      channels: { co2: 400 },
      sources: { co2: "src_2" },
      anomaly_score: undefined,
    });
    expect(row.anomaly_score).toBeNull();
  });

  it("maps null anomaly_score to null", () => {
    const row = toSensorRow({
      seq: "",
      timestamp: "2026-07-18T10:00:00Z",
      tenant_id: "t1",
      greenhouse_id: "gh_A",
      channels: { co2: 400 },
      sources: { co2: "src_2" },
      anomaly_score: null,
    });
    expect(row.anomaly_score).toBeNull();
  });

  it("includes all four channels when present", () => {
    const channels = { temperature: 22.0, humidity: 60.0, co2: 420.0, light: 800.0 };
    const row = toSensorRow({
      seq: "",
      timestamp: "2026-07-18T10:00:00Z",
      tenant_id: "t1",
      greenhouse_id: "gh_A",
      channels,
      sources: {},
      anomaly_score: null,
    });
    expect(row.temperature).toBe(22.0);
    expect(row.humidity).toBe(60.0);
    expect(row.co2).toBe(420.0);
    expect(row.light).toBe(800.0);
  });

  it("produces a shape that discoverChannels can consume", () => {
    const row = toSensorRow({
      seq: "",
      timestamp: "T",
      tenant_id: "t",
      greenhouse_id: "g",
      channels: { co2: 400 },
      sources: {},
      anomaly_score: null,
    });
    // discoverChannels checks typeof value === "number"
    expect(typeof (row as Record<string, unknown>)["co2"]).toBe("number");
  });
});
