"use client";

import { Chip } from "@/components/ui/Chip";
import type { DriftWatchEpisode } from "@/features/drift-watch/api/driftWatch";
import { DriftFeedbackButtons } from "@/features/drift-watch/components/DriftFeedbackButtons";

/** Format an ISO datetime to "DD/MM/YY HH:MM" in local time. */
function formatTs(iso: string): string {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return iso;
  const dt = new Date(ms);
  const date = dt.toLocaleDateString(undefined, {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
  });
  const time = dt.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return `${date} ${time}`;
}

function signed(value: number): string {
  return `${value < 0 ? "-" : "+"}${Math.abs(value).toFixed(3)}`;
}

/** Prefer the signed robust offset; else the legacy magnitude with the direction's sign. */
function offsetText(episode: DriftWatchEpisode): string | null {
  if (episode.offset_robust != null) return signed(episode.offset_robust);
  if (episode.offset_estimate != null) {
    return `${episode.direction === "-" ? "-" : "+"}${Math.abs(episode.offset_estimate).toFixed(3)}`;
  }
  return null;
}

export function DriftEpisodeRow({ episode }: { readonly episode: DriftWatchEpisode }) {
  const isShadow = !episode.armed;
  const rowClass = isShadow ? "opacity-60" : "";
  const offsetStr = offsetText(episode);

  return (
    <>
      <tr
        data-testid="drift-episode-row"
        data-armed={String(episode.armed)}
        className={`border-b border-border last:border-0 ${rowClass}`}
      >
        <td className="py-2 pr-4 text-sm tabular-nums text-text-muted">
          {formatTs(episode.first_detected_ts)}
        </td>
        <td className="py-2 pr-4 text-sm font-medium text-text">{episode.target_channel}</td>
        <td className="py-2 pr-4 text-sm tabular-nums text-text">{episode.direction}</td>
        <td className="py-2 pr-4 text-sm tabular-nums text-text">
          {offsetStr ?? <span className="text-text-muted">—</span>}
        </td>
        <td className="py-2 pr-4 text-sm">
          {episode.ongoing ? (
            <span className="text-sev-warning font-medium">Yes</span>
          ) : (
            <span className="text-text-muted">No</span>
          )}
        </td>
        <td className="py-2 pr-4 text-sm">
          {episode.armed ? (
            <span data-testid="episode-armed-chip">
              <Chip severity="normal">Armed</Chip>
            </span>
          ) : (
            <span data-testid="episode-shadow-chip">
              <Chip severity="unclassified">Shadow (informational)</Chip>
            </span>
          )}
          {episode.suppressed_by ? (
            <div className="mt-1 text-xs text-text-muted" data-testid="episode-suppressed">
              suppressed (stuck sensor)
            </div>
          ) : null}
        </td>
        <td className="py-2">
          <DriftFeedbackButtons episode={episode} />
        </td>
      </tr>
      {episode.alert_message ? (
        <tr
          data-testid="drift-episode-message-row"
          className={`border-b border-border ${rowClass}`}
        >
          <td colSpan={7} className="pb-2 text-sm text-text-muted" data-testid="episode-message">
            {episode.alert_message}
          </td>
        </tr>
      ) : null}
    </>
  );
}
