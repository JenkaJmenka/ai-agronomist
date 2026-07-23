/**
 * Shared PROFILE_LOGGING_ENABLED parser — mirrors Python `_env_flag` in
 * `src/config.py` (truthy: 1/true/yes/on, case-insensitive).
 */

export function parseProfileLoggingEnabled(
  raw: string | undefined | null,
  defaultValue = false,
): boolean {
  if (raw == null) return defaultValue;
  const normalized = raw.trim().toLowerCase();
  if (normalized === "") return defaultValue;
  return normalized === "1" || normalized === "true" || normalized === "yes" || normalized === "on";
}

/** Server-side read of PROFILE_LOGGING_ENABLED (default off when unset). */
export function isProfileLoggingEnabled(): boolean {
  return parseProfileLoggingEnabled(process.env.PROFILE_LOGGING_ENABLED, false);
}
