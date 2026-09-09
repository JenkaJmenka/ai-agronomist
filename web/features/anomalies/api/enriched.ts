/** G3/G4 anomaly aggregation endpoints via the BFF (plan §5). */

import { z } from "zod";

const enrichedRowSchema = z
  .object({
    timestamp: z.string(),
    anomaly_score: z.number().nullable().optional(),
    fault_type: z.string().nullable().optional(),
    root_cause: z.string().nullable().optional(),
    reasoning: z.string().nullable().optional(),
    pattern_type: z.string().nullable().optional(),
    affected_sensors: z.array(z.string()).default([]),
    incident_id: z.string().nullable().optional(),
    /** Persisted by migration 024; null for rows predating the migration. */
    detected_severity: z.string().nullable().optional(),
  })
  .loose();

export type EnrichedAnomalyRow = z.infer<typeof enrichedRowSchema>;

const enrichedResponseSchema = z
  .object({
    rows: z.array(enrichedRowSchema),
    count: z.number(),
    anomaly_count: z.number(),
  })
  .loose();

export interface AnomalyWindowParams {
  readonly tenant: string;
  readonly gh: string;
  readonly start: string;
  readonly end: string;
  readonly threshold: number;
}

export function windowQuery(params: AnomalyWindowParams): URLSearchParams {
  return new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
    start: params.start,
    end: params.end,
    threshold: String(params.threshold),
  });
}

export interface EnrichedAnomaliesResult {
  readonly rows: EnrichedAnomalyRow[];
  /** Full-window flagged count — the source of truth for anomaly totals
   * (client rows are downsampled, so counting them drifts per range). */
  readonly anomalyCount: number;
}

export async function fetchEnrichedAnomalies(
  params: AnomalyWindowParams & { limit?: number; signal?: AbortSignal },
): Promise<EnrichedAnomaliesResult> {
  const query = windowQuery(params);
  if (params.limit) query.set("limit", String(params.limit));
  const res = await fetch(`/api/bff/anomalies/enriched?${query}`, {
    signal: params.signal,
  });
  if (!res.ok) {
    throw new Error(`Enriched anomalies request failed (${res.status})`);
  }
  const body = enrichedResponseSchema.parse(await res.json());
  return { rows: body.rows, anomalyCount: body.anomaly_count };
}

export const SERVER_GROUP_BY = ["date", "type", "sensor"] as const;
export type ServerGroupBy = (typeof SERVER_GROUP_BY)[number];

const anomalyGroupSampleSchema = z.object({
  timestamp: z.string(),
  anomaly_score: z.number().nullable().optional(),
  affected_sensors: z.array(z.string()).default([]),
  reasoning: z.string().nullable().optional(),
  values: z.record(z.string(), z.number()).default({}),
  /** Persisted by migration 024; null for rows predating the migration. */
  detected_severity: z.string().nullable().optional(),
});

const anomalyGroupSchema = z.object({
  key: z.string(),
  count: z.number(),
  top_score: z.number(),
  sample_ids: z.array(z.string()),
  samples: z.array(anomalyGroupSampleSchema).default([]),
});

export type AnomalyGroupSample = z.infer<typeof anomalyGroupSampleSchema>;
export type AnomalyGroup = z.infer<typeof anomalyGroupSchema>;

const groupsResponseSchema = z
  .object({
    groups: z.array(anomalyGroupSchema),
    anomaly_count: z.number(),
  })
  .loose();

export interface AnomalyGroupsResult {
  readonly groups: AnomalyGroup[];
  readonly anomalyCount: number;
}

export async function fetchAnomalyGroups(
  params: AnomalyWindowParams & { groupBy: ServerGroupBy },
): Promise<AnomalyGroupsResult> {
  const query = windowQuery(params);
  query.set("group_by", params.groupBy);
  const res = await fetch(`/api/bff/anomalies/groups?${query}`);
  if (!res.ok) {
    throw new Error(`Anomaly groups request failed (${res.status})`);
  }
  const body = groupsResponseSchema.parse(await res.json());
  return { groups: body.groups, anomalyCount: body.anomaly_count };
}
