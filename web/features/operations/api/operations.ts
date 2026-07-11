/** Upload & Operations data access via the BFF (plan §8 P4). */

import { z } from "zod";

async function requestJson<T>(url: string, schema: z.ZodType<T>, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    const detail = await res
      .json()
      .then((body: { detail?: unknown }) => body.detail)
      .catch(() => undefined);
    throw new Error(typeof detail === "string" ? detail : `Request failed (${res.status})`);
  }
  return schema.parse(await res.json());
}

/* ── Upload (sync + async job) ─────────────────────────────────────────── */

export interface UploadOptions {
  readonly tenant: string;
  readonly gh: string;
  readonly file: File;
  readonly persistData: boolean;
  readonly trainModel: boolean;
}

const syncUploadResultSchema = z
  .object({
    sequences_processed: z.number().optional(),
    anomalies_detected: z.number().optional(),
    threshold_used: z.number().nullable().optional(),
    data_persisted: z.boolean().optional(),
  })
  .loose();

export type SyncUploadResult = z.infer<typeof syncUploadResultSchema>;

const asyncUploadAcceptedSchema = z
  .object({
    job_id: z.string(),
    portion_count: z.number(),
    poll_url: z.string(),
  })
  .loose();

export type AsyncUploadAccepted = z.infer<typeof asyncUploadAcceptedSchema>;

function uploadFormData(options: UploadOptions): FormData {
  const form = new FormData();
  form.set("file", options.file, options.file.name);
  form.set("tenant_id", options.tenant);
  form.set("greenhouse_id", options.gh);
  form.set("persist_data", String(options.persistData));
  form.set("train_model", String(options.trainModel));
  return form;
}

/** Analyze-only path: inference runs inline and returns the result envelope. */
export function uploadCsvSync(options: UploadOptions): Promise<SyncUploadResult> {
  return requestJson("/api/bff/inference/upload", syncUploadResultSchema, {
    method: "POST",
    body: uploadFormData(options),
  });
}

/** Persist/train path: queues a portioned job on the worker. */
export function uploadCsvAsync(options: UploadOptions): Promise<AsyncUploadAccepted> {
  return requestJson("/api/bff/inference/upload-async", asyncUploadAcceptedSchema, {
    method: "POST",
    body: uploadFormData(options),
  });
}

/* ── Model registry ────────────────────────────────────────────────────── */

const modelVersionSchema = z
  .object({
    model_id: z.string(),
    is_active: z.boolean().optional(),
    model_type: z.string().optional(),
    created_at: z.string().nullable().optional(),
    metrics: z.record(z.string(), z.unknown()).nullable().optional(),
  })
  .loose();

export type ModelVersion = z.infer<typeof modelVersionSchema>;

export function fetchModelVersions(tenant: string): Promise<ModelVersion[]> {
  return requestJson(
    `/api/bff/models/${encodeURIComponent(tenant)}/versions`,
    z.array(modelVersionSchema),
  );
}

const statusResponseSchema = z.object({ status: z.string() }).loose();

export function activateModel(tenant: string, modelId: string): Promise<{ status: string }> {
  return requestJson(
    `/api/bff/models/${encodeURIComponent(tenant)}/activate`,
    statusResponseSchema,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ model_id: modelId }),
    },
  );
}

export function deleteModelVersion(params: {
  tenant: string;
  modelId: string;
  force: boolean;
}): Promise<{ status: string }> {
  const query = params.force ? "?force=true" : "";
  return requestJson(
    `/api/bff/models/${encodeURIComponent(params.tenant)}/versions/${encodeURIComponent(params.modelId)}${query}`,
    statusResponseSchema,
    { method: "DELETE" },
  );
}

/* ── Synthetic anomalies + destructive clear ───────────────────────────── */

export const INJECT_ANOMALY_TYPES = [
  "random",
  "temperature_spike",
  "temperature_drop",
  "humidity_spike",
  "humidity_drop",
  "co2_surge",
  "light_failure",
  "multi_sensor",
  "gradual_drift",
] as const;

export type InjectAnomalyType = (typeof INJECT_ANOMALY_TYPES)[number];

export const INJECT_TYPE_LABELS: Readonly<Record<InjectAnomalyType, string>> = {
  random: "Random (mixed)",
  temperature_spike: "Temperature spike",
  temperature_drop: "Temperature drop",
  humidity_spike: "Humidity spike",
  humidity_drop: "Humidity drop",
  co2_surge: "CO₂ surge",
  light_failure: "Light failure",
  multi_sensor: "Multi-sensor failure",
  gradual_drift: "Gradual drift",
};

const injectResultSchema = z
  .object({
    injected: z.number(),
    anomalies: z.number(),
    window_start: z.string().optional(),
    window_end: z.string().optional(),
    columns_used: z.array(z.string()).optional(),
  })
  .loose();

export type InjectResult = z.infer<typeof injectResultSchema>;

export function injectTestAnomalies(params: {
  tenant: string;
  gh: string;
  anomalyType: InjectAnomalyType;
  anomalyCount: number;
  normalCount: number;
}): Promise<InjectResult> {
  const body = new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
    anomaly_type: params.anomalyType,
    n_anomalies: String(params.anomalyCount),
    n_normal: String(params.normalCount),
  });
  return requestJson("/api/bff/upload/inject-test-anomalies", injectResultSchema, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
}

const removeResultSchema = z
  .object({
    deleted_sensor: z.number(),
    deleted_raw: z.number(),
    anomaly_type: z.string(),
  })
  .loose();

export type RemoveResult = z.infer<typeof removeResultSchema>;

export function removeTestAnomalies(params: {
  tenant: string;
  gh: string;
  anomalyType?: InjectAnomalyType;
}): Promise<RemoveResult> {
  const query = new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
  });
  if (params.anomalyType) query.set("anomaly_type", params.anomalyType);
  return requestJson(`/api/bff/upload/inject-test-anomalies?${query}`, removeResultSchema, {
    method: "DELETE",
  });
}

const clearResultSchema = z.object({}).loose();

export function clearTenantData(params: { tenant: string; gh: string }): Promise<unknown> {
  const query = new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
  });
  return requestJson(`/api/bff/upload/data?${query}`, clearResultSchema, {
    method: "DELETE",
  });
}
