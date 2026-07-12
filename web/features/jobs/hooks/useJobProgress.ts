"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import {
  isActiveJobStatus,
  type JobProgressEvent,
  jobProgressEventSchema,
  jobSchema,
} from "@/features/jobs/api/jobs";
import { STALE_TIME_MS } from "@/lib/query";

/** Polling cadence when the SSE stream is unavailable (plan §2.1 row 8). */
const FALLBACK_POLL_MS = 3000;

export type ProgressTransport = "sse" | "polling" | "idle";

export interface JobProgressState {
  readonly progress: JobProgressEvent | null;
  readonly transport: ProgressTransport;
}

/**
 * Live progress for one active job: SSE via the BFF relay first, dropping
 * to 3 s polling if the stream errors. Job status is never cached
 * (staleTime 0). Terminal events invalidate the jobs list.
 */
export function useJobProgress(jobId: string, status: string): JobProgressState {
  const queryClient = useQueryClient();
  const isActive = isActiveJobStatus(status);
  const [progress, setProgress] = useState<JobProgressEvent | null>(null);
  const [sseFailed, setSseFailed] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!isActive || sseFailed || typeof EventSource === "undefined") {
      return undefined;
    }
    const source = new EventSource(`/api/bff/jobs/${encodeURIComponent(jobId)}/events`);
    sourceRef.current = source;

    source.onmessage = (event: MessageEvent<string>) => {
      const parsed = jobProgressEventSchema.safeParse(JSON.parse(event.data));
      if (!parsed.success) return;
      setProgress(parsed.data);
      if (!isActiveJobStatus(parsed.data.status)) {
        source.close();
        void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      }
    };
    source.onerror = () => {
      // The server closes the stream after the terminal event too; only
      // treat it as a transport failure while the job is still running.
      source.close();
      setSseFailed(true);
    };
    return () => {
      source.close();
      sourceRef.current = null;
    };
  }, [jobId, isActive, sseFailed, queryClient]);

  const fallbackQuery = useQuery({
    queryKey: ["job-progress", jobId],
    queryFn: async () => {
      const res = await fetch(`/api/bff/jobs/${encodeURIComponent(jobId)}`);
      if (!res.ok) throw new Error(`Job status request failed (${res.status})`);
      const job = jobSchema.parse(await res.json());
      if (!isActiveJobStatus(job.status)) {
        void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      }
      return {
        status: job.status,
        progress_pct: job.progress_pct ?? null,
        completed_portions: job.completed_portions,
        total_portions: job.total_portions ?? null,
      } satisfies JobProgressEvent;
    },
    enabled: isActive && sseFailed,
    staleTime: STALE_TIME_MS.jobStatus,
    refetchInterval: FALLBACK_POLL_MS,
  });

  if (!isActive) {
    return { progress: null, transport: "idle" };
  }
  if (sseFailed) {
    return { progress: fallbackQuery.data ?? progress, transport: "polling" };
  }
  return { progress, transport: "sse" };
}
