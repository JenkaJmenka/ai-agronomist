/**
 * Dashboard query keys shared across Monitoring, Climate, Gas, Anomalies,
 * and Thresholds. Sidebar navigation must carry these so period / Go Live /
 * refresh / threshold / chart window stay in sync between screens.
 *
 * Setpoints live in localStorage (tenant/gh scoped), not the URL.
 */
export const DASHBOARD_QUERY_KEYS = [
  "tenant",
  "gh",
  "from",
  "to",
  "live",
  "refresh",
  "threshold",
  "channel",
  "saved",
] as const;

export type DashboardQueryKey = (typeof DASHBOARD_QUERY_KEYS)[number];

/** Copy shared dashboard params from the current URL search string. */
export function pickDashboardSearchParams(search: URLSearchParams): URLSearchParams {
  const out = new URLSearchParams();
  for (const key of DASHBOARD_QUERY_KEYS) {
    const value = search.get(key);
    if (value !== null && value !== "") out.set(key, value);
  }
  return out;
}

/** Build `href?sharedParams` for sidebar / cross-page links. */
export function withDashboardQuery(href: string, search: URLSearchParams): string {
  const q = pickDashboardSearchParams(search);
  const suffix = q.toString();
  return suffix ? `${href}?${suffix}` : href;
}
