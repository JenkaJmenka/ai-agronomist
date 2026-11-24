import Link from "next/link";

import { Chip } from "@/components/ui/Chip";
import type { AnomalyEpisode } from "@/features/anomalies/api/episodes";
import { formatChannel } from "@/features/anomalies/lib/formatEpisode";

/** Detector name the backend writes for drift-watch alerts (ADR-013 §4). */
export const DRIFT_WATCH_DETECTOR = "drift_watch";

export function isDriftWatchEpisode(episode: Pick<AnomalyEpisode, "detector">): boolean {
  return episode.detector === DRIFT_WATCH_DETECTOR;
}

const DRIFT_UNITS: Readonly<Record<string, string>> = {
  temperature: "°C",
  humidity: "%RH",
  measured_hd: "g/m³",
};

/** Signed offset with the channel unit, e.g. "-1.2 °C"; unit omitted when unknown. */
export function formatDriftOffset(offset: number, channel: string): string {
  const unit = DRIFT_UNITS[channel];
  const sign = offset > 0 ? "+" : offset < 0 ? "\u2212" : "";
  const value = `${sign}${Math.abs(offset).toFixed(1)}`;
  return unit ? `${value} ${unit}` : value;
}

/**
 * Advisory card for a drift-watch episode in the anomaly feed (ADR-013 §4).
 *
 * The wording is the backend's `drift_message`, rendered as given: this card
 * never composes probe-naming text itself. When the message is absent (older
 * cached response) it falls back to neutral text that names no probe. The
 * false-alarm figures and feedback buttons stay on the Drift Watch page.
 * Severity is always advisory, never urgent.
 */
export function DriftAlertCard({
  episode,
  href,
}: {
  readonly episode: Pick<AnomalyEpisode, "channel" | "start_ts"> &
    Partial<Pick<AnomalyEpisode, "drift_offset" | "drift_first_detected_ts" | "drift_message">>;
  readonly href: string;
}) {
  const channel = formatChannel(episode.channel).toLowerCase();
  const firstDetected = episode.drift_first_detected_ts ?? episode.start_ts;
  const offset = episode.drift_offset ?? null;
  return (
    <div
      className="space-y-1 border-t border-border px-4 py-2.5 text-sm"
      data-testid="drift-alert-card"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">Calibration drift: {channel}</span>
        <span data-testid="drift-alert-severity">
          <Chip severity="warning">Advisory</Chip>
        </span>
        {offset !== null ? (
          <span className="tabular-nums text-text-muted" data-testid="drift-alert-offset">
            Offset {formatDriftOffset(offset, episode.channel)}
          </span>
        ) : null}
      </div>
      <p className="text-text-muted" data-testid="drift-alert-text">
        {episode.drift_message ??
          `A sensor and its reference disagree on ${channel}. One of them may have drifted. The monitor cannot tell which one.`}
      </p>
      <p className="text-xs text-text-muted">
        First detected {firstDetected.replace("T", " ").slice(0, 16)}.{" "}
        <Link
          href={href}
          className="font-medium text-accent hover:underline"
          data-testid="drift-alert-link"
        >
          Open Drift Watch
        </Link>{" "}
        for the false-alarm rate and to confirm or dismiss.
      </p>
    </div>
  );
}
