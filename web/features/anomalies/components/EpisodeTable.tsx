"use client";

import { useEffect, useState } from "react";

import { Chip } from "@/components/ui/Chip";
import { ExportCsvButton } from "@/components/ui/ExportCsvButton";
import { RefetchingBlock } from "@/components/ui/RefetchingBlock";
import { Skeleton } from "@/components/ui/Skeleton";
import { paginateItems, TABLE_PAGE_SIZE, TablePagination } from "@/components/ui/TablePagination";
import type { AnomalyEpisode, AnomalyEpisodeSample } from "@/features/anomalies/api/episodes";
import { MonitoringJumpLink } from "@/features/anomalies/components/MonitoringJumpLink";
import { useAnomalyEpisodes } from "@/features/anomalies/hooks/useAnomalyEpisodes";
import { fetchErrorHint } from "@/features/anomalies/lib/classifyFetchError";
import {
  formatSensorList,
  formatSensorValues,
} from "@/features/anomalies/lib/formatAnomalyDetails";
import {
  channelUnit,
  directionArrow,
  formatChannel,
  formatDuration,
  formatEpisodeSpan,
  formatTrend,
  formatValueRange,
} from "@/features/anomalies/lib/formatEpisode";
import { windowFromSelectedTimestamps } from "@/lib/selectionWindow";
import { SEVERITY_ORDER, type Severity } from "@/lib/severity";
import type { SelectionWindowHandler } from "@/lib/useTimestampRowSelection";

interface EpisodeTableProps {
  readonly windowStart: string | null;
  readonly windowEnd: string | null;
  readonly threshold: number;
  /** When set, clicking an episode zooms the chart to its span. */
  readonly onWindowSelect?: SelectionWindowHandler;
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

function asSeverity(value: string): Severity | undefined {
  return value in SEVERITY_ORDER ? (value as Severity) : undefined;
}

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

/** One merged event: span, span length, channel movement, and trend. */
function EpisodeSummary({ episode }: { readonly episode: AnomalyEpisode }) {
  const unit = channelUnit(episode.channel);
  const severity = asSeverity(episode.severity);
  const valueRange = formatValueRange(episode, unit);
  const isClipped = episode.is_truncated_start || episode.is_truncated_end;

  return (
    <summary className="cursor-pointer px-4 py-2.5 text-sm" data-testid="episode-summary">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium tabular-nums">
          {formatEpisodeSpan(episode.start_ts, episode.end_ts)}
        </span>
        <span className="text-text-muted tabular-nums">
          {/* A clipped episode ran past the queried window, so its span is a floor. */}
          {isClipped ? "≥ " : ""}
          {formatDuration(episode.duration_seconds)}
        </span>
        <span className="text-text-muted tabular-nums">
          {episode.sample_count.toLocaleString()} sample
          {episode.sample_count === 1 ? "" : "s"}
        </span>
        {severity ? (
          <Chip severity={severity}>{episode.peak_score.toFixed(3)}</Chip>
        ) : (
          <span className="tabular-nums text-text-muted">{episode.peak_score.toFixed(3)}</span>
        )}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-text-muted">
        <span className="font-medium text-text">
          {formatChannel(episode.channel)} {episode.direction ?? "anomaly"}
        </span>
        <span aria-hidden="true">{directionArrow(episode.direction)}</span>
        {valueRange ? <span className="tabular-nums">{valueRange}</span> : null}
        <span>·</span>
        <span data-testid="episode-trend">{formatTrend(episode.trend)}</span>
        {episode.detector !== "unknown" ? (
          <>
            <span>·</span>
            <span>{episode.detector}</span>
          </>
        ) : null}
      </div>
    </summary>
  );
}

/** One raw 5-minute sample inside an expanded episode. */
function EpisodeSampleRow({
  sample,
  fallbackChannel,
}: {
  readonly sample: AnomalyEpisodeSample;
  readonly fallbackChannel: string;
}) {
  const sensors = sample.affected_sensors.length > 0 ? sample.affected_sensors : [fallbackChannel];
  return (
    <li className="flex flex-col gap-1 px-4 py-2 text-sm" data-testid="episode-sample-row">
      <div className="flex flex-wrap items-center gap-3">
        <span className="tabular-nums">{sample.timestamp.replace("T", " ").slice(0, 19)}</span>
        {sample.anomaly_score !== null && sample.anomaly_score !== undefined ? (
          <span className="tabular-nums text-text-muted">{sample.anomaly_score.toFixed(3)}</span>
        ) : null}
        <span className="ml-auto">
          <MonitoringJumpLink timestamp={sample.timestamp} channels={sensors} />
        </span>
      </div>
      <div className="grid gap-0.5 text-xs text-text-muted sm:grid-cols-[7rem_1fr]">
        <span className="font-medium text-text">Sensors</span>
        <span>{formatSensorList(sensors)}</span>
        <span className="font-medium text-text">Values</span>
        <span className="tabular-nums">{formatSensorValues(sample.values, sensors) || "—"}</span>
        <span className="font-medium text-text">Reasoning</span>
        <span>{sample.reasoning?.trim() || "—"}</span>
      </div>
    </li>
  );
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
  const sampleTotal = state.anomalyCount ?? 0;

  return (
    <div className="space-y-3" data-testid="episode-table">
      <div className="flex flex-wrap items-center gap-2 text-xs text-text-muted tabular-nums">
        <span>
          {state.episodes.length.toLocaleString()} episode
          {state.episodes.length === 1 ? "" : "s"}
          {sampleTotal > 0 ? ` merged from ${sampleTotal.toLocaleString()} samples` : ""}
          {onWindowSelect ? " · click an episode to zoom the chart to its span" : ""}
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
            // onToggle on <details> rather than onClick on <summary>: the
            // summary is natively interactive, so an onClick there trips the
            // static-element a11y rule. Zoom on open only, not on collapse.
            onToggle={(event) => {
              if (!onWindowSelect || !event.currentTarget.open) return;
              const win = episodeChartWindow(episode);
              if (win) onWindowSelect(win.from, win.to);
            }}
          >
            <EpisodeSummary episode={episode} />
            <ul className="divide-y divide-border border-t border-border">
              {episode.samples.map((sample) => (
                <EpisodeSampleRow
                  key={sample.timestamp}
                  sample={sample}
                  fallbackChannel={episode.channel}
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
