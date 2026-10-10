"use client";

import { type ReactNode, useEffect, useState } from "react";

import { ExportCsvButton } from "@/components/ui/ExportCsvButton";
import { RefetchingBlock } from "@/components/ui/RefetchingBlock";
import { Skeleton } from "@/components/ui/Skeleton";
import { paginateItems, TABLE_PAGE_SIZE, TablePagination } from "@/components/ui/TablePagination";
import { ANOMALY_EPISODE_MAX, type AnomalyEpisode } from "@/features/anomalies/api/episodes";
import { EpisodeSampleRow } from "@/features/anomalies/components/EpisodeSampleRow";
import { EpisodeSummary } from "@/features/anomalies/components/EpisodeSummary";
import { useAnomalyEpisodes } from "@/features/anomalies/hooks/useAnomalyEpisodes";
import { fetchErrorHint } from "@/features/anomalies/lib/classifyFetchError";
import { windowFromSelectedTimestamps } from "@/lib/selectionWindow";
import type { SelectionWindowHandler } from "@/lib/useTimestampRowSelection";

interface EpisodeTableProps {
  readonly windowStart: string | null;
  readonly windowEnd: string | null;
  readonly threshold: number;
  /** When set, the explicit "Zoom to span" control on each episode is shown. */
  readonly onWindowSelect?: SelectionWindowHandler;
  /**
   * Rendered instead of an error when /anomalies/episodes is unreachable.
   *
   * The web and api services deploy independently, so the UI can ship ahead of
   * the endpoint it calls. Without this the anomalies page is a dead end during
   * that window; with it the page silently serves the per-sample view until the
   * API catches up.
   */
  readonly fallback?: ReactNode;
  /**
   * Exact flagged-row count for the window, from the same source as the page
   * header (G2 /readings/anomalies-in-window) — reused here rather than this
   * endpoint's own `anomaly_count` so the two numbers on screen can never
   * drift apart from independent cache timing. Null while unresolved.
   */
  readonly anomalyCount: number | null;
}

const EXPORT_HEADERS = [
  "start",
  "end",
  "duration_seconds",
  "channel",
  "direction",
  "samples",
  "peak_score",
  "first_value",
  "peak_value",
  "trend",
  "headline",
] as const;

/**
 * Chart focus for an episode: its whole span, not a single instant.
 *
 * A one-sample episode has start === end; windowFromSelectedTimestamps pads
 * that to a usable window rather than a zero-width one.
 */
export function episodeChartWindow(
  episode: Pick<AnomalyEpisode, "start_ts" | "end_ts">,
): { from: Date; to: Date } | null {
  return windowFromSelectedTimestamps([episode.start_ts, episode.end_ts]);
}

function buildExportRows(episodes: readonly AnomalyEpisode[]): unknown[][] {
  return episodes.map((e) => [
    e.start_ts,
    e.end_ts,
    e.duration_seconds,
    e.channel,
    e.direction ?? "",
    e.sample_count,
    e.peak_score,
    e.first_value ?? "",
    e.peak_value ?? "",
    e.trend,
    e.headline,
  ]);
}

/**
 * Merged anomaly episodes — the default Table view.
 *
 * A continuous event is one row here instead of N five-minute samples;
 * expanding it reveals the raw samples the server merged.
 */
export function EpisodeTable({
  windowStart,
  windowEnd,
  threshold,
  onWindowSelect,
  fallback,
  anomalyCount,
}: EpisodeTableProps) {
  const state = useAnomalyEpisodes({
    windowStart,
    windowEnd,
    threshold,
    enabled: windowStart !== null && windowEnd !== null,
  });

  const [pageIndex, setPageIndex] = useState(0);
  useEffect(() => {
    setPageIndex(0);
  }, [windowStart, windowEnd, threshold]);

  if (state.isLoading) {
    return <Skeleton className="h-40 w-full" />;
  }

  if (state.isError) {
    if (fallback !== undefined) {
      return (
        <div data-testid="episode-fallback">
          <p className="mb-3 text-xs text-text-muted">
            Episode merging is unavailable right now — showing individual readings.
          </p>
          {fallback}
        </div>
      );
    }
    return (
      <p role="alert" className="py-4 text-center text-sm text-sev-critical">
        Could not load anomaly episodes ({fetchErrorHint(state.error)}).
      </p>
    );
  }

  if (state.episodes.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-text-muted">
        No anomaly episodes in this window.
      </p>
    );
  }

  const {
    pageItems,
    pageIndex: safePage,
    pageCount,
  } = paginateItems(state.episodes, pageIndex, TABLE_PAGE_SIZE);
  // Hitting the cap is the only signal more episodes exist — the endpoint
  // never reports an uncapped total (see ANOMALY_EPISODE_MAX).
  const isCapped = state.episodes.length >= ANOMALY_EPISODE_MAX;

  return (
    <div className="space-y-3" data-testid="episode-table">
      <div className="flex flex-wrap items-center gap-2 text-xs text-text-muted tabular-nums">
        <span>
          {state.episodes.length.toLocaleString()} episode
          {state.episodes.length === 1 ? "" : "s"}
          {isCapped
            ? // Capped: the shown episodes' samples no longer add up to the
              // window total, so state the exact total separately instead of
              // claiming these episodes "merged" all of it.
              ` (top ${ANOMALY_EPISODE_MAX} by peak score — more exist)${
                anomalyCount !== null
                  ? ` · ${anomalyCount.toLocaleString()} anomalies in this window (exact)`
                  : ""
              }`
            : anomalyCount !== null && anomalyCount > 0
              ? ` merged from ${anomalyCount.toLocaleString()} samples`
              : ""}
          {onWindowSelect ? ' · "Zoom to span" focuses the chart on an episode' : ""}
        </span>
        <span className="ml-auto">
          <ExportCsvButton
            filename="anomaly-episodes"
            headers={EXPORT_HEADERS}
            rows={buildExportRows(state.episodes)}
            testId="export-episodes-csv"
          />
        </span>
      </div>

      <RefetchingBlock isRefetching={state.isRefetching} className="space-y-3">
        {pageItems.map((episode) => (
          <details
            key={episode.episode_id}
            className="rounded-[var(--radius-card)] border border-border bg-bg-elevated"
          >
            <EpisodeSummary
              episode={episode}
              onZoomToSpan={
                onWindowSelect
                  ? () => {
                      const win = episodeChartWindow(episode);
                      if (win) onWindowSelect(win.from, win.to);
                    }
                  : undefined
              }
            />
            <ul className="divide-y divide-border border-t border-border">
              {episode.samples.map((sample) => (
                <EpisodeSampleRow
                  key={sample.timestamp}
                  sample={sample}
                  fallbackChannel={episode.channel}
                  episodeSpan={{ start_ts: episode.start_ts, end_ts: episode.end_ts }}
                />
              ))}
            </ul>
            {episode.samples.length < episode.sample_count ? (
              <p className="px-4 py-2 text-xs text-text-muted">
                Showing the {episode.samples.length} highest-scoring of{" "}
                {episode.sample_count.toLocaleString()} samples.
              </p>
            ) : null}
          </details>
        ))}
        <TablePagination
          pageIndex={safePage}
          pageCount={pageCount}
          totalRows={state.episodes.length}
          onPageChange={setPageIndex}
          label="episodes"
        />
      </RefetchingBlock>
    </div>
  );
}
