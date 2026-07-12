/** Operator feedback data access via the BFF (plan §8 P5). */

import { z } from "zod";

const incidentSchema = z
  .object({
    id: z.string(),
    detected_at: z.string().nullable().optional(),
    anomaly_score: z.number().nullable().optional(),
    fault_type: z.string().nullable().optional(),
    root_cause: z.string().nullable().optional(),
    root_cause_details: z
      .object({
        pattern_type: z.string().nullable().optional(),
        affected_sensors: z.array(z.string()).optional(),
        hint_summary: z.string().nullable().optional(),
        reasoning: z.string().nullable().optional(),
      })
      .loose()
      .nullable()
      .optional(),
    resolved: z.boolean().nullable().optional(),
  })
  .loose();

export type OpenIncident = z.infer<typeof incidentSchema>;

export async function fetchOpenIncidents(params: {
  tenant: string;
  gh: string;
}): Promise<OpenIncident[]> {
  const query = new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
  });
  const res = await fetch(`/api/bff/incidents/open?${query}`);
  if (!res.ok) {
    throw new Error(`Incidents request failed (${res.status})`);
  }
  return z.array(incidentSchema).parse(await res.json());
}

export interface VerdictInput {
  readonly tenant: string;
  readonly incidentId: string;
  readonly isTrueAnomaly: boolean;
  readonly operatorId: string;
  readonly confidence: number;
  readonly patternObserved?: string;
  readonly actualRootCause?: string;
}

const verdictResponseSchema = z
  .object({
    status: z.string(),
    incident_id: z.string(),
    learning_triggered: z.boolean(),
    progress_to_retrain: z.string(),
  })
  .loose();

export type VerdictResponse = z.infer<typeof verdictResponseSchema>;

/** POST the operator verdict — body field names follow the API contract
 *  (customer_id is the documented legacy name for tenant, see G9). */
export async function submitVerdict(input: VerdictInput): Promise<VerdictResponse> {
  const res = await fetch("/api/bff/feedback/mark-anomaly", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      customer_id: input.tenant,
      incident_id: input.incidentId,
      is_true_anomaly: input.isTrueAnomaly,
      operator_id: input.operatorId,
      operator_confidence: input.confidence,
      pattern_type_observed: input.patternObserved || null,
      actual_root_cause: input.actualRootCause || null,
    }),
  });
  if (!res.ok) {
    const detail = await res
      .json()
      .then((body: { detail?: unknown }) => body.detail)
      .catch(() => undefined);
    throw new Error(typeof detail === "string" ? detail : `Verdict failed (${res.status})`);
  }
  return verdictResponseSchema.parse(await res.json());
}

const learningProgressSchema = z
  .object({
    progress_percent: z.number().optional(),
    samples_needed: z.number().optional(),
    high_quality_count: z.number().optional(),
    total_feedback: z.number().optional(),
    last_retrain: z.string().nullable().optional(),
  })
  .loose();

export type LearningProgress = z.infer<typeof learningProgressSchema>;

export async function fetchLearningProgress(tenant: string): Promise<LearningProgress> {
  const res = await fetch(`/api/bff/feedback/learning-progress/${encodeURIComponent(tenant)}`);
  if (!res.ok) {
    throw new Error(`Learning progress request failed (${res.status})`);
  }
  return learningProgressSchema.parse(await res.json());
}
