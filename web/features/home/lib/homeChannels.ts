/** Prefer climate channels on Home; keep discovery order for the rest. */

export const HOME_CLIMATE_PRIORITY: readonly string[] = [
  "temperature",
  "humidity",
  "co2",
  "light",
  "vpd",
  "soil_moisture",
  "root_zone_temp",
] as const;

export const HOME_DEFAULT_CHART_CHANNELS: readonly string[] = [
  "temperature",
  "humidity",
  "co2",
] as const;

/**
 * Order discovered channels: climate priority first, then remaining alphabetically.
 */
export function orderHomeChannels(discovered: readonly string[]): string[] {
  const set = new Set(discovered);
  const prioritized = HOME_CLIMATE_PRIORITY.filter((ch) => set.has(ch));
  const rest = discovered
    .filter((ch) => !HOME_CLIMATE_PRIORITY.includes(ch))
    .slice()
    .sort((a, b) => a.localeCompare(b));
  return [...prioritized, ...rest];
}

/**
 * Restrict to preferred primary channels when they exist in discovery;
 * fall back to ordered discovery when none match.
 */
export function selectHomeChannels(
  discovered: readonly string[],
  primaryChannels: readonly string[],
): string[] {
  const ordered = orderHomeChannels(discovered);
  if (primaryChannels.length === 0) return ordered;
  const preferred = primaryChannels.filter((ch) => ordered.includes(ch));
  return preferred.length > 0 ? preferred : ordered;
}

/** Visible set for the mini chart — prefer configured primaries intersecting discovery. */
export function homeChartVisible(
  channels: readonly string[],
  primaryChannels: readonly string[],
): Set<string> {
  const defaults = primaryChannels.length > 0 ? primaryChannels : HOME_DEFAULT_CHART_CHANNELS;
  const visible = defaults.filter((ch) => channels.includes(ch));
  if (visible.length > 0) return new Set(visible);
  return new Set(channels.slice(0, Math.min(3, channels.length)));
}
