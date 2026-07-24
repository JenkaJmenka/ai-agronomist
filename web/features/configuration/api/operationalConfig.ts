/** Operational config access via the BFF (crop setpoints + season map). */

import { z } from "zod";

const bandSchema = z.object({
  day: z.array(z.number()).nullable().optional(),
  night: z.array(z.number()).nullable().optional(),
});

const dayWindowSchema = z.object({
  start_hour: z.number().int().min(0).max(23),
  end_hour: z.number().int().min(0).max(23),
});

const configDocumentSchema = z
  .object({
    crop: z.string(),
    setpoints: z.record(z.string(), bandSchema).default({}),
    day_window: dayWindowSchema,
    season_map: z.record(z.string(), z.enum(["summer", "winter"])).default({}),
    seasonal_overrides: z
      .record(z.string(), z.object({ detector_scale: z.number().nullable().optional() }).loose())
      .default({}),
  })
  .loose();

const operationalResponseSchema = z
  .object({
    config: configDocumentSchema,
    sources: z.record(z.string(), z.enum(["default", "override"])).default({}),
  })
  .loose();

export type OperationalConfigDocument = z.infer<typeof configDocumentSchema>;
export type OperationalConfigResponse = z.infer<typeof operationalResponseSchema>;

const cropProfilesSchema = z.record(z.string(), z.record(z.string(), bandSchema));

export type CropProfiles = z.infer<typeof cropProfilesSchema>;

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

export function fetchOperationalConfig(options: {
  readonly tenant: string;
  readonly gh: string;
  readonly signal?: AbortSignal;
}): Promise<OperationalConfigResponse> {
  const qs = new URLSearchParams({
    tenant_id: options.tenant,
    greenhouse_id: options.gh,
  });
  return requestJson(`/api/bff/config/operational?${qs}`, operationalResponseSchema, {
    signal: options.signal,
  });
}

export function putOperationalConfig(options: {
  readonly tenant: string;
  readonly gh: string;
  readonly config: OperationalConfigDocument;
}): Promise<OperationalConfigResponse> {
  const qs = new URLSearchParams({
    tenant_id: options.tenant,
    greenhouse_id: options.gh,
  });
  return requestJson(`/api/bff/config/operational?${qs}`, operationalResponseSchema, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(options.config),
  });
}

export function fetchCropProfiles(signal?: AbortSignal): Promise<CropProfiles> {
  return requestJson("/api/bff/config/crop-profiles", cropProfilesSchema, { signal });
}
