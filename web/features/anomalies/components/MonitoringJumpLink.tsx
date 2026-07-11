"use client";

import Link from "next/link";
import { parseAsString, useQueryStates } from "nuqs";

import { Button } from "@/components/ui/Button";

const JUMP_WINDOW_HOURS = 12;

interface MonitoringJumpLinkProps {
  readonly timestamp: string;
}

/**
 * Anomaly → Monitoring jump: pure URL construction (plan §6.2
 * useMonitoringJump). Centers a 24 h window on the anomaly and carries
 * tenant/greenhouse — shareable, lossless, no session state.
 */
export function MonitoringJumpLink({ timestamp }: MonitoringJumpLinkProps) {
  const [{ tenant, gh }] = useQueryStates({
    tenant: parseAsString.withDefault("tenant_1"),
    gh: parseAsString.withDefault("gh_A"),
  });

  const center = new Date(timestamp).getTime();
  const from = new Date(center - JUMP_WINDOW_HOURS * 3_600_000).toISOString();
  const to = new Date(center + JUMP_WINDOW_HOURS * 3_600_000).toISOString();
  const query = new URLSearchParams({ tenant, gh, from, to });

  return (
    <Button asChild variant="secondary" size="sm">
      <Link href={`/monitoring?${query.toString()}`} aria-label={`Open ${timestamp} in Monitoring`}>
        Monitoring →
      </Link>
    </Button>
  );
}
