"use client";

import { useLiveStatus } from "@/features/monitoring/hooks/useLiveStatus";
import {
  describeReadingAge,
  describeStaleThreshold,
} from "@/features/monitoring/lib/freshnessFormat";

/**
 * Three states an operator must be able to tell apart at a glance — none of
 * them may look like a quiet/empty greenhouse (MIGRATION_PLAN live-feed fix):
 *
 * - `off`     — paused or never configured; Go Live is the next action.
 * - `stale`   — Go Live is on but no row has arrived inside the threshold
 *   (including a scope that has never written one at all). This is the
 *   fault state: a production audit found zero `source='live'` rows ever
 *   written for any tenant while the toggle read "live" the whole time.
 * - `healthy` — Go Live is on and a reading landed inside the threshold.
 */
type DisplayState = "off" | "stale" | "healthy";

function resolveDisplayState(isLive: boolean, isStale: boolean): DisplayState {
  if (!isLive) return "off";
  return isStale ? "stale" : "healthy";
}

interface BadgeStyle {
  readonly label: string;
  readonly pillClass: string;
  readonly dotClass: string;
}

const BADGE_STYLE: Record<DisplayState, Omit<BadgeStyle, "label">> = {
  off: { pillClass: "bg-text-muted/15 text-text-muted", dotClass: "bg-text-muted" },
  // Deliberately NOT a pulsing dot and NOT accent/green — a stale feed must
  // never visually read as a success state (live.py:183-186 applies the same
  // rule to the Go Live toggle itself: no green verdict that would lie).
  stale: { pillClass: "bg-sev-critical/15 text-sev-critical", dotClass: "bg-sev-critical" },
  healthy: {
    pillClass: "bg-sev-normal/15 text-sev-normal",
    dotClass: "motion-safe:animate-pulse bg-sev-normal",
  },
};

/**
 * Surfaces `freshness` from `GET /api/bff/live/status` — "is data actually
 * arriving", as opposed to `LiveBadge`'s SSE-transport-level `stale` flag
 * (which only detects a dropped *connection*, not a connected feed that
 * simply never writes a row). Renders regardless of the local `live` URL
 * toggle so "off" is visible before the operator ever turns Go Live on.
 */
export function LiveFreshnessBadge() {
  const { freshness, isLoading } = useLiveStatus();

  // Loading or an older API that hasn't shipped `freshness` yet (deploy
  // skew) — show nothing rather than guess at a state.
  if (isLoading || freshness === null) return null;

  const isLive = freshness.state === "live";
  const display = resolveDisplayState(isLive, freshness.is_stale);
  const { pillClass, dotClass } = BADGE_STYLE[display];

  const label =
    display === "off"
      ? freshness.state === "paused"
        ? "Live feed paused"
        : "Live feed off"
      : display === "stale"
        ? "Live feed stalled"
        : "Live feed healthy";

  return (
    <div
      className="flex flex-col items-start gap-0.5"
      data-testid="live-freshness-badge"
      data-freshness-state={display}
    >
      <span
        className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold ${pillClass}`}
      >
        <span className={`h-2 w-2 rounded-full ${dotClass}`} aria-hidden="true" />
        {label}
      </span>
      {display !== "off" && (
        <span className="text-xs text-text-muted" data-testid="live-freshness-age">
          {describeReadingAge(freshness.age_seconds)}
          {display === "stale" ? ` — ${describeStaleThreshold(freshness.threshold_seconds)}` : ""}
        </span>
      )}
    </div>
  );
}
