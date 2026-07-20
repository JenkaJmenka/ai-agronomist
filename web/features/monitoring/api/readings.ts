/** Readings data access via the BFF proxy — server keeps the API key. */

export interface SensorRow {
  readonly timestamp: string;
  readonly anomaly_score?: number | null;
  readonly [channel: string]: unknown;
}

export interface RecentReadingsResponse {
  readonly rows: SensorRow[];
  readonly count: number;
  readonly min_timestamp: string | null;
  readonly max_timestamp: string | null;
}

export interface WindowReadingsResponse {
  readonly rows: SensorRow[];
  readonly count: number;
  /** Present once backend G1 lands. */
  readonly downsampled?: boolean;
  readonly original_count?: number;
}

const NON_CHANNEL_KEYS = new Set([
  "timestamp",
  "anomaly_score",
  "tenant_id",
  "greenhouse_id",
  "id",
]);

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`Readings request failed (${res.status})`);
  }
  return (await res.json()) as T;
}

export function fetchRecentReadings(params: {
  tenant: string;
  gh: string;
  hours?: number;
  maxPoints?: number;
}): Promise<RecentReadingsResponse> {
  const q = new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
    hours: String(params.hours ?? 168),
  });
  if (params.maxPoints) q.set("max_points", String(params.maxPoints));
  return fetchJson(`/api/bff/readings/recent?${q}`);
}

export function fetchWindowReadings(params: {
  tenant: string;
  gh: string;
  from: Date;
  to: Date;
  maxPoints?: number;
}): Promise<WindowReadingsResponse> {
  const q = new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
    start: params.from.toISOString(),
    end: params.to.toISOString(),
  });
  if (params.maxPoints) q.set("max_points", String(params.maxPoints));
  return fetchJson(`/api/bff/readings/window?${q}`);
}

/** Numeric sensor channels present in the rows, order-stable. */
export function discoverChannels(rows: SensorRow[]): string[] {
  const channels: string[] = [];
  const seen = new Set<string>();
  // Sample the newest rows — extra channels (soil_moisture, root_zone_temp, vpd)
  // exist only in live stream rows. The merged array sorts historical rows first;
  // sampling from the oldest end silently drops extras once history accumulates
  // past 50 rows. Sampling the tail ensures newly-streamed channels are found
  // regardless of how much canonical-only history precedes them.
  const sample = rows.slice(-50);
  for (const row of sample) {
    for (const [key, value] of Object.entries(row)) {
      if (NON_CHANNEL_KEYS.has(key) || seen.has(key)) continue;
      if (typeof value === "number") {
        channels.push(key);
        seen.add(key);
      }
    }
  }
  return channels;
}
