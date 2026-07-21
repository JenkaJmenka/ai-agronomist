"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { parseAsFloat, parseAsString, useQueryStates } from "nuqs";

import { Button } from "@/components/ui/Button";
import { pickDashboardSearchParams } from "@/lib/dashboardQuery";
import { DEFAULT_SCORE_THRESHOLD } from "@/lib/severity";

const JUMP_WINDOW_HOURS = 12;

interface MonitoringJumpLinkProps {
  readonly timestamp: string;
  /** Affected sensor channels — pre-selected in the Monitoring toggles. */
  readonly channels?: readonly string[] | null;
}

/**
 * Anomaly → Monitoring jump: centers a window on the anomaly and carries
 * shared dashboard params (tenant/gh/live/refresh/threshold) plus channel=.
 */
export function MonitoringJumpLink({ timestamp, channels }: MonitoringJumpLinkProps) {
  const search = useSearchParams();
  const [{ tenant, gh, threshold }] = useQueryStates({
    tenant: parseAsString.withDefault("tenant_1"),
    gh: parseAsString.withDefault("gh_A"),
    threshold: parseAsFloat.withDefault(DEFAULT_SCORE_THRESHOLD),
  });

  const center = new Date(timestamp).getTime();
  const from = new Date(center - JUMP_WINDOW_HOURS * 3_600_000).toISOString();
  const to = new Date(center + JUMP_WINDOW_HOURS * 3_600_000).toISOString();
  const query = pickDashboardSearchParams(search);
  query.set("tenant", tenant);
  query.set("gh", gh);
  query.set("from", from);
  query.set("to", to);
  query.set("threshold", String(threshold));
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
