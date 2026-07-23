/**
 * Server-side PROFILE line emitter (stdout → Railway logs).
 * Format matches `src/utils/profiling.py`:
 *   PROFILE phase=<name> ok=1 elapsed_ms=12.3 key=value
 */

import { isProfileLoggingEnabled } from "@/lib/profileFlag";

export const PROFILE_PREFIX = "PROFILE";

export type ProfileFieldValue = string | number | boolean | null | undefined;

function formatProfileLine(
  phase: string,
  ok: boolean,
  elapsedMs: number | null,
  fields: Record<string, ProfileFieldValue>,
): string {
  const parts = [`phase=${phase}`, `ok=${ok ? 1 : 0}`];
  if (elapsedMs != null) {
    parts.push(`elapsed_ms=${elapsedMs.toFixed(1)}`);
  }
  for (const [key, value] of Object.entries(fields)) {
    if (value == null) continue;
    if (key === "phase" || key === "ok" || key === "elapsed_ms") continue;
    parts.push(`${key}=${value}`);
  }
  return `${PROFILE_PREFIX} ${parts.join(" ")}`;
}

/** Emit one greppable PROFILE line when PROFILE_LOGGING_ENABLED is on. */
export function emitProfileLog(
  phase: string,
  ok: boolean,
  elapsedMs: number | null,
  fields: Record<string, ProfileFieldValue> = {},
): void {
  if (!isProfileLoggingEnabled()) return;
  console.info(formatProfileLine(phase, ok, elapsedMs, fields));
}
