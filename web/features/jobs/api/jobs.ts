/** Job monitoring data access via the BFF (plan §8 P4, G5/G6). */

import { z } from "zod";

export const ACTIVE_JOB_STATUSES = ["pending", "preparing", "running", "finalizing"] as const;

export const jobSchema = z
  .object({
    id: z.string(),
    tenant_id: z.string(),
    greenhouse_id: z.string().nullable().optional(),
    job_type: z.string(),
    status: z.string(),
    total_portions: z.number().nullable().optional(),
    completed_portions: z.number().default(0),
    failed_portions: z.number().default(0),
    progress_pct: z.number().nullable().optional(),
    error: z.string().nullable().optional(),
    created_at: z.string().nullable().optional(),
    finished_at: z.string().nullable().optional(),
  })
  .loose();

export type JobSummary = z.infer<typeof jobSchema>;

const jobListSchema = z.object({ jobs: z.array(jobSchema) }).loose();

export const jobProgressEventSchema = z.object({
  status: z.string(),
  progress_pct: z.number().nullable(),
  completed_portions: z.number(),
  total_portions: z.number().nullable(),
});

export type JobProgressEvent = z.infer<typeof jobProgressEventSchema>;

export function isActiveJobStatus(status: string): boolean {
  return (ACTIVE_JOB_STATUSES as readonly string[]).includes(status);
}

export async function fetchJobs(params: {
  tenant: string;
  gh: string;
  limit?: number;
}): Promise<JobSummary[]> {
  const query = new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
    limit: String(params.limit ?? 20),
  });
  const res = await fetch(`/api/bff/jobs?${query}`);
  if (!res.ok) {
    throw new Error(`Job list request failed (${res.status})`);
  }
  return jobListSchema.parse(await res.json()).jobs;
}

export async function cancelJob(jobId: string): Promise<void> {
  const res = await fetch(`/api/bff/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
  });
  if (!res.ok) {
    throw new Error(`Terminate request failed (${res.status})`);
  }
}
