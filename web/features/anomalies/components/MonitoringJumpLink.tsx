"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { parseAsFloat, parseAsString, useQueryStates } from "nuqs";

import { Button } from "@/components/ui/Button";
import { anomalyJumpWindow } from "@/features/anomalies/lib/anomalyJumpWindow";
import { useReadingsBounds } from "@/features/monitoring/hooks/useReadingsBounds";
import { pickDashboardSearchParams } from "@/lib/dashboardQuery";
import { DEFAULT_SCORE_THRESHOLD } from "@/lib/severity";

interface MonitoringJumpLinkProps {
  readonly timestamp: string;
  /** Affected sensor channels — pre-selected in the Monitoring toggles. */
  readonly channels?: readonly string[] | null;
}

/**
 * Anomaly → Monitoring jump: opens a lookback centered on the anomaly (clamped
 * so the window never extends into empty future), carries shared dashboard
 * params, and forces live off so Live rolling does not replace the jump window.
 */
export function MonitoringJumpLink({ timestamp, channels }: MonitoringJumpLinkProps) {
  const search = useSearchParams();
  const [{ tenant, gh, threshold }] = useQueryStates({
    tenant: parseAsString.withDefault("tenant_1"),
    gh: parseAsString.withDefault("gh_A"),
    threshold: parseAsFloat.withDefault(DEFAULT_SCORE_THRESHOLD),
  });

  // Exports can be timestamped ahead of the clock; clamping to `now` alone
  // would slide the window off the anomaly entirely.
  const { maxTimestamp } = useReadingsBounds({ tenant, gh });
  const window = anomalyJumpWindow(timestamp, { datasetEnd: maxTimestamp });
  const query = pickDashboardSearchParams(search);
  query.set("tenant", tenant);
  query.set("gh", gh);
  if (window) {
    query.set("from", window.from.toISOString());
    query.set("to", window.to.toISOString());
  }
  query.set("threshold", String(threshold));
  // Historical investigation — Live lookback would overwrite the jump window.
  query.set("live", "false");
  if (channels && channels.length > 0) query.set("channel", channels.join(","));
  else query.delete("channel");

  return (
    <Button asChild variant="secondary" size="sm">
      <Link href={`/monitoring?${query.toString()}`} aria-label={`Open ${timestamp} in Monitoring`}>
        Monitoring →
      </Link>
    </Button>
  );
}
