"use client";

import Link from "next/link";
import { parseAsFloat, parseAsString, useQueryStates } from "nuqs";

import { Button } from "@/components/ui/Button";
import { DEFAULT_SCORE_THRESHOLD } from "@/lib/severity";

const JUMP_WINDOW_HOURS = 12;

interface MonitoringJumpLinkProps {
  readonly timestamp: string;
  /** Affected sensor channels — pre-selected in the Monitoring toggles. */
  readonly channels?: readonly string[] | null;
}

/**
 * Anomaly → Monitoring jump: pure URL construction (plan §6.2
 * useMonitoringJump). Centers a 24 h window on the anomaly and carries
 * tenant/greenhouse — shareable, lossless, no session state. Affected
 * channels ride along as a comma-separated `channel=` so Monitoring opens
 * focused on those traces, and `threshold=` so it highlights the same rows.
 */
export function MonitoringJumpLink({ timestamp, channels }: MonitoringJumpLinkProps) {
  const [{ tenant, gh, threshold }] = useQueryStates({
    tenant: parseAsString.withDefault("tenant_1"),
    gh: parseAsString.withDefault("gh_A"),
    threshold: parseAsFloat.withDefault(DEFAULT_SCORE_THRESHOLD),
  });

  const center = new Date(timestamp).getTime();
  const from = new Date(center - JUMP_WINDOW_HOURS * 3_600_000).toISOString();
  const to = new Date(center + JUMP_WINDOW_HOURS * 3_600_000).toISOString();
  const query = new URLSearchParams({ tenant, gh, from, to, threshold: String(threshold) });
  if (channels && channels.length > 0) query.set("channel", channels.join(","));

  return (
    <Button asChild variant="secondary" size="sm">
      <Link href={`/monitoring?${query.toString()}`} aria-label={`Open ${timestamp} in Monitoring`}>
        Monitoring →
      </Link>
    </Button>
  );
}
