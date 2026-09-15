import { Chip } from "@/components/ui/Chip";
import type { AnomalyEpisode } from "@/features/anomalies/api/episodes";
import {
  channelUnit,
  directionArrow,
  formatChannel,
  formatDevelopment,
  formatDuration,
  formatEpisodeSpan,
  formatValueRange,
  movementArrow,
} from "@/features/anomalies/lib/formatEpisode";
import { SEVERITY_ORDER, type Severity } from "@/lib/severity";

function asSeverity(value: string): Severity | undefined {
  return value in SEVERITY_ORDER ? (value as Severity) : undefined;
}

/** One merged event: span, span length, channel movement, and trend. */
export function EpisodeSummary({
  episode,
  onZoomToSpan,
}: {
  readonly episode: AnomalyEpisode;
  /**
   * Present only when the page wired up chart zoom. Expanding/collapsing
   * this <details> must stay a pure disclosure action — it never calls this;
   * only an explicit click on the button below does.
   */
  readonly onZoomToSpan?: () => void;
}) {
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
        {onZoomToSpan ? (
          <button
            type="button"
            data-testid="episode-zoom-button"
            onClick={(event) => {
              // Clicking anywhere in <summary> toggles the native disclosure;
              // stop that AND the bubble so this button is a distinct act,
              // not a side effect of expanding.
              event.preventDefault();
              event.stopPropagation();
              onZoomToSpan();
            }}
            className="ml-auto rounded-[var(--radius-control)] border border-border px-2 py-0.5 text-xs text-text-muted hover:border-accent hover:text-accent"
          >
            Zoom to span
          </button>
        ) : null}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-text-muted">
        <span className="font-medium text-text">
          {formatChannel(episode.channel)} {episode.direction ?? "anomaly"}
        </span>
        {/* Band breach direction when there is one; otherwise which way the
            value travelled, so an unbounded channel still reads as moving. */}
        <span aria-hidden="true">
          {episode.direction ? directionArrow(episode.direction) : movementArrow(episode.movement)}
        </span>
        {valueRange ? <span className="tabular-nums">{valueRange}</span> : null}
        <span>·</span>
        <span data-testid="episode-trend">{formatDevelopment(episode)}</span>
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
