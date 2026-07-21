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

export interface ReadingsBounds {
  readonly min_timestamp: string | null;
  readonly max_timestamp: string | null;
}

/** Newest/oldest stored timestamps for the scope — used to anchor period presets. */
export function fetchReadingsBounds(params: {
  tenant: string;
  gh: string;
}): Promise<ReadingsBounds> {
  const q = new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
  });
  return fetchJson(`/api/bff/readings/bounds?${q}`);
}

/** Numeric sensor channels present in the rows, order-stable. */
export function discoverChannels(rows: SensorRow[]): string[] {
  const channels: string[] = [];
  const seen = new Set<string>();

  function absorb(sample: readonly SensorRow[]): void {
    for (const row of sample) {
      for (const [key, value] of Object.entries(row)) {
        if (NON_CHANNEL_KEYS.has(key) || seen.has(key)) continue;
        if (typeof value === "number") {
          channels.push(key);
          seen.add(key);
        }
      }
    }
  }

  if (rows.length <= 200) {
    // Short windows (live 15/30 min): scan everything so intermittent
    // SCADA channels are not missed by a tail-only sample.
    absorb(rows);
  } else {
    // Head: channels that fall out of the newest ticks.
    // Tail: live-only extras after long canonical-only history (F1).
    absorb(rows.slice(0, 50));
    absorb(rows.slice(-50));
  }
  return channels;
}

/** Union two channel lists, preserving first-seen order. */
export function mergeChannelLists(previous: readonly string[], next: readonly string[]): string[] {
  if (previous.length === 0) return [...next];
  if (next.length === 0) return [...previous];
  const seen = new Set(previous);
  const out = [...previous];
  for (const ch of next) {
    if (seen.has(ch)) continue;
    out.push(ch);
    seen.add(ch);
  }
  return out;
}
