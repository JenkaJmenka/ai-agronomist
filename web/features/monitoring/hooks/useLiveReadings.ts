"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import {
  anomalyDetailEventSchema,
  heartbeatEventSchema,
  liveAnomalyEventSchema,
  liveReadingEventSchema,
  resetEventSchema,
  resumeLiveScope,
  type SourceStatus,
  sourceStatusEventSchema,
  toSensorRow,
} from "@/features/monitoring/api/live";
import { fetchRecentReadings, type SensorRow } from "@/features/monitoring/api/readings";
import { useScope } from "@/lib/useScope";

/** Maximum rows kept in the live buffer (drop-oldest). */
const ROW_CAP = 2_000;
/** Maximum anomaly entries shown (newest-first). */
const ANOMALY_CAP = 50;
/** Batch rows into state this often to avoid per-event re-renders. */
const FLUSH_INTERVAL_MS = 500;
/** Mark stream stale after 2 × heartbeat interval (2 × 15 s = 30 s). */
const STALE_THRESHOLD_MS = 30_000;
/** Track onerror timestamps within this window. */
const ERROR_WINDOW_MS = 30_000;
/** Switch to polling after this many errors in ERROR_WINDOW_MS. */
const ERROR_FALLBACK_THRESHOLD = 3;
/** Fallback polling cadence. */
const POLLING_INTERVAL_MS = 5_000;

export interface LiveAnomalyItem {
  readonly seq: string;
  readonly timestamp: string;
  readonly score: number;
  readonly threshold: number;
  readonly affected_channels: readonly string[];
  readonly reasoning: string;
  /** Scoring window that produced this anomaly (for View deep-links). */
  readonly window_start: string;
  readonly window_end: string;
  readonly fault_type?: string | null;
  readonly root_cause?: string | null;
  readonly incident_id?: string | null;
}

export type LiveTransport = "sse" | "polling" | "idle";

export interface UseLiveReadingsResult {
  readonly rows: SensorRow[];
  readonly anomalies: LiveAnomalyItem[];
  readonly transport: LiveTransport;
  readonly stale: boolean;
  readonly sourceStatuses: SourceStatus[];
}

/**
 * Live sensor readings and anomalies via SSE (BFF relay), with automatic
 * fallback to 5 s polling if the stream errors ≥3 times in 30 s.
 *
 * @param enabled - controlled by the `live` URL param in MonitoringView
 */
export function useLiveReadings(enabled: boolean): UseLiveReadingsResult {
  const { tenant, gh } = useScope();
  const queryClient = useQueryClient();

  const [rows, setRows] = useState<SensorRow[]>([]);
  const [anomalies, setAnomalies] = useState<LiveAnomalyItem[]>([]);
  const [sourceStatuses, setSourceStatuses] = useState<SourceStatus[]>([]);
  const [stale, setStale] = useState(false);
  const [sseFailed, setSseFailed] = useState(false);

  // Accumulates incoming rows between flush ticks.
  const rowBufferRef = useRef<SensorRow[]>([]);
  // Error timestamps within the current window for fallback detection.
  const errorTimestampsRef = useRef<number[]>([]);
  // Stale-detection timer handle.
  const staleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // -------------------------------------------------------------------------
  // SSE connection
  // -------------------------------------------------------------------------
  useEffect(() => {
    if (!enabled || sseFailed || typeof EventSource === "undefined") {
      return undefined;
    }

    const url = new URL("/api/bff/live/events", window.location.origin);
    url.searchParams.set("tenant", tenant);
    url.searchParams.set("gh", gh);

    const source = new EventSource(url.toString());

    // Start the stale clock immediately so a connection that never delivers
    // any data is also flagged stale after STALE_THRESHOLD_MS.
    function resetStaleTimer(): void {
      if (staleTimerRef.current !== null) clearTimeout(staleTimerRef.current);
      setStale(false);
      staleTimerRef.current = setTimeout(() => setStale(true), STALE_THRESHOLD_MS);
    }

    // Begin stale detection immediately on connection open.
    staleTimerRef.current = setTimeout(() => setStale(true), STALE_THRESHOLD_MS);

    source.addEventListener("reading", (event: MessageEvent<string>) => {
      const parsed = liveReadingEventSchema.safeParse(JSON.parse(event.data));
      if (!parsed.success) return;
      rowBufferRef.current.push(toSensorRow(parsed.data));
      resetStaleTimer();
    });

    source.addEventListener("anomaly", (event: MessageEvent<string>) => {
      const parsed = liveAnomalyEventSchema.safeParse(JSON.parse(event.data));
      if (!parsed.success) return;
      const item: LiveAnomalyItem = {
        seq: parsed.data.seq,
        timestamp: parsed.data.timestamp,
        score: parsed.data.score,
        threshold: parsed.data.threshold,
        affected_channels: parsed.data.affected_channels,
        reasoning: parsed.data.reasoning,
        window_start: parsed.data.window_start,
        window_end: parsed.data.window_end,
      };
      setAnomalies((prev) => {
        // Re-emit: when the same episode seq arrives again (e.g. every 60 s),
        // update the existing entry in-place so the strip shows one row per
        // episode. Detail enrichment fields (fault_type etc.) are preserved.
        if (item.seq) {
          const idx = prev.findIndex((a) => a.seq === item.seq);
          if (idx >= 0) {
            const updated = [...prev];
            updated[idx] = { ...prev[idx], ...item };
            return updated;
          }
        }
        return [item, ...prev].slice(0, ANOMALY_CAP);
      });
    });

    source.addEventListener("anomaly-detail", (event: MessageEvent<string>) => {
      const parsed = anomalyDetailEventSchema.safeParse(JSON.parse(event.data));
      if (!parsed.success) return;
      const detail = parsed.data;
      setAnomalies((prev) =>
        prev.map((a) =>
          // Guard: never match when either side is an empty string — an empty
          // anomaly_seq (backend B2 bug) would otherwise corrupt every strip
          // entry. CONTRACT 1 guarantees non-empty seqs after the BE fix, but
          // the guard keeps the FE safe during transitional deploys.
          detail.anomaly_seq && a.seq === detail.anomaly_seq
            ? {
                ...a,
                reasoning: detail.reasoning,
                fault_type: detail.fault_type ?? null,
                root_cause: detail.root_cause ?? null,
                incident_id: detail.incident_id ?? null,
              }
            : a,
        ),
      );
    });

    source.addEventListener("source-status", (event: MessageEvent<string>) => {
      const parsed = sourceStatusEventSchema.safeParse(JSON.parse(event.data));
      if (!parsed.success) return;
      const updated = parsed.data;
      setSourceStatuses((prev) => {
        const without = prev.filter((s) => s.source_id !== updated.source_id);
        return [...without, updated];
      });
    });

    source.addEventListener("heartbeat", (event: MessageEvent<string>) => {
      const parsed = heartbeatEventSchema.safeParse(JSON.parse(event.data));
      if (!parsed.success) return;
      resetStaleTimer();
    });

    source.addEventListener("reset", (event: MessageEvent<string>) => {
      const parsed = resetEventSchema.safeParse(JSON.parse(event.data));
      if (!parsed.success) return;
      rowBufferRef.current = [];
      setRows([]);
      setAnomalies([]);
      void queryClient.invalidateQueries({ queryKey: ["readings"] });
    });

    source.onerror = () => {
      const now = Date.now();
      const recent = errorTimestampsRef.current.filter((t) => now - t < ERROR_WINDOW_MS);
      recent.push(now);
      errorTimestampsRef.current = recent;

      if (recent.length >= ERROR_FALLBACK_THRESHOLD) {
        source.close();
        setSseFailed(true);
      }
    };

    return () => {
      source.close();
      if (staleTimerRef.current !== null) {
        clearTimeout(staleTimerRef.current);
        staleTimerRef.current = null;
      }
    };
  }, [tenant, gh, enabled, sseFailed, queryClient]);

  // -------------------------------------------------------------------------
  // Flush interval — batches row-buffer into state on a timer so uPlot is
  // not re-rendered for every individual reading.
  // -------------------------------------------------------------------------
  useEffect(() => {
    if (!enabled) return undefined;

    const intervalId = setInterval(() => {
      if (rowBufferRef.current.length === 0) return;
      const incoming = rowBufferRef.current.splice(0);
      setRows((prev) => {
        const combined = [...prev, ...incoming];
        return combined.length > ROW_CAP ? combined.slice(combined.length - ROW_CAP) : combined;
      });
    }, FLUSH_INTERVAL_MS);

    return () => clearInterval(intervalId);
  }, [enabled]);

  // -------------------------------------------------------------------------
  // Reset local state when live mode is turned off.
  // -------------------------------------------------------------------------
  useEffect(() => {
    if (enabled) return;
    rowBufferRef.current = [];
    setRows([]);
    setAnomalies([]);
    setStale(false);
    setSseFailed(false);
    errorTimestampsRef.current = [];
    if (staleTimerRef.current !== null) {
      clearTimeout(staleTimerRef.current);
      staleTimerRef.current = null;
    }
  }, [enabled]);

  // -------------------------------------------------------------------------
  // One-shot resume for a live session that starts without a toggle click —
  // e.g. loading `?live=true` directly, or switching tenant/gh while live is
  // already on. Scopes now start muted (backend default + Clear all data),
  // so without this the SSE connection above would open and simply receive
  // nothing.
  //
  // WHY this can't live in the SSE effect: that effect also re-runs on
  // reconnect (its deps include `sseFailed`), and src/live/scope_control.py
  // documents that an automatic resume on reconnect would re-seed a database
  // the operator just wiped via Clear all data. This effect depends only on
  // [enabled, tenant, gh] — not on reconnect state — so it fires once per
  // mount/scope and never again for the same live session.
  // -------------------------------------------------------------------------
  useEffect(() => {
    if (!enabled) return;
    resumeLiveScope({ tenant, gh }).catch(() => {
      // Stream may still work if the scope was never muted.
    });
  }, [enabled, tenant, gh]);

  // -------------------------------------------------------------------------
  // Fallback polling when SSE is broken.
  // -------------------------------------------------------------------------
  const pollingQuery = useQuery({
    queryKey: ["readings-live-fallback", tenant, gh],
    queryFn: () => fetchRecentReadings({ tenant, gh, hours: 1 }),
    enabled: enabled && sseFailed,
    staleTime: 0,
    refetchInterval: POLLING_INTERVAL_MS,
  });

  // -------------------------------------------------------------------------
  // Derived transport state
  // -------------------------------------------------------------------------
  if (!enabled) {
    return { rows: [], anomalies: [], transport: "idle", stale: false, sourceStatuses: [] };
  }

  if (sseFailed) {
    return {
      rows: pollingQuery.data?.rows ?? rows,
      anomalies,
      transport: "polling",
      stale,
      sourceStatuses,
    };
  }

  return { rows, anomalies, transport: "sse", stale, sourceStatuses };
}
