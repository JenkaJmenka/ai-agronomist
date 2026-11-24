import Link from "next/link";

import { Chip } from "@/components/ui/Chip";
import type { AnomalyEpisode } from "@/features/anomalies/api/episodes";
import { formatChannel } from "@/features/anomalies/lib/formatEpisode";

/** Detector name the backend writes for drift-watch alerts (ADR-013 §4). */
export const DRIFT_WATCH_DETECTOR = "drift_watch";

export function isDriftWatchEpisode(episode: Pick<AnomalyEpisode, "detector">): boolean {
  return episode.detector === DRIFT_WATCH_DETECTOR;
}

/**
 * Advisory card for a drift-watch episode in the anomaly feed (ADR-013 §4).
 *
 * The feed row carries no offset, input id or direction (those live only on
 * /api/drift-watch/episodes), and it cannot say whether the input is a paired
 * probe. So the wording stays neutral: it never names one probe as faulty, and
 * the offset, false-alarm figures and feedback buttons stay on the Drift Watch
 * page, which this card links to. Severity is always advisory, never urgent.
 */
export function DriftAlertCard({
  episode,
  href,
}: {
  readonly episode: Pick<AnomalyEpisode, "channel" | "start_ts">;
  readonly href: string;
}) {
  const channel = formatChannel(episode.channel).toLowerCase();
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
      </div>
      <p className="text-text-muted" data-testid="drift-alert-text">
        A sensor and its reference disagree on {channel}. One of them may have drifted. The monitor
        cannot tell which one.
      </p>
      <p className="text-xs text-text-muted">
        First detected {episode.start_ts.replace("T", " ").slice(0, 16)}.{" "}
        <Link
          href={href}
          className="font-medium text-accent hover:underline"
          data-testid="drift-alert-link"
        >
          Open Drift Watch
        </Link>{" "}
        for the offset, the false-alarm rate and to confirm or dismiss.
      </p>
    </div>
  );
}
