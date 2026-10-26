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

async function fetchJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, signal ? { signal } : undefined);
  if (!res.ok) {
    throw new Error(`Readings request failed (${res.status})`);
  }
  return (await res.json()) as T;
}

/**
 * Tell the server which rows the chart will band, so downsampling keeps them.
 * Without this the server guesses (top-decile scores only) and drops flagged
 * rows the client would have highlighted.
 */
function setDownsampleParams(
  q: URLSearchParams,
  params: { maxPoints?: number; threshold?: number },
): void {
  if (params.maxPoints) q.set("max_points", String(params.maxPoints));
  if (params.threshold !== undefined) q.set("anomaly_threshold", String(params.threshold));
}

export function fetchRecentReadings(params: {
  tenant: string;
  gh: string;
  hours?: number;
  maxPoints?: number;
  threshold?: number;
  signal?: AbortSignal;
}): Promise<RecentReadingsResponse> {
  const q = new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
    hours: String(params.hours ?? 168),
  });
  setDownsampleParams(q, params);
  return fetchJson(`/api/bff/readings/recent?${q}`, params.signal);
}

export function fetchWindowReadings(params: {
  tenant: string;
  gh: string;
  from: Date;
  to: Date;
  maxPoints?: number;
  threshold?: number;
  signal?: AbortSignal;
}): Promise<WindowReadingsResponse> {
  const q = new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
    start: params.from.toISOString(),
    end: params.to.toISOString(),
  });
  setDownsampleParams(q, params);
  return fetchJson(`/api/bff/readings/window?${q}`, params.signal);
}

export interface ReadingsBounds {
  readonly min_timestamp: string | null;
  readonly max_timestamp: string | null;
}

/** Newest/oldest stored timestamps for the scope — used to anchor period presets. */
export function fetchReadingsBounds(params: {
  tenant: string;
  gh: string;
  signal?: AbortSignal;
}): Promise<ReadingsBounds> {
  const q = new URLSearchParams({
    tenant_id: params.tenant,
    greenhouse_id: params.gh,
  });
  return fetchJson(`/api/bff/readings/bounds?${q}`, params.signal);
}

/** Map Connext / SCADA header aliases onto canonical Monitoring channel names. */
const DISPLAY_CHANNEL_ALIASES: Readonly<Record<string, string>> = {
  measured_greenhouse_temp: "temperature",
  greenhouse_temp: "temperature",
  measured_relative_humidity: "humidity",
  relative_humidity: "humidity",
  light_intensity: "light",
  measured_humidity_deficit: "measured_hd",
  humidity_deficit: "measured_hd",
};

/** Display channel name for a raw/detector sensor name (identity when unaliased). */
export function toDisplayChannel(name: string): string {
  return DISPLAY_CHANNEL_ALIASES[name] ?? name;
}

/**
 * Promote aliased raw columns onto canonical names and drop the aliases so
 * charts/tiles do not show duplicate mismatched series
 * (e.g. ``measured_greenhouse_temp`` + ``temperature``).
 */
export function applyDisplayChannelAliases(rows: readonly SensorRow[]): SensorRow[] {
  if (rows.length === 0) return [];
  return rows.map((row) => {
    let changed = false;
    const next: Record<string, unknown> = { ...row };
    for (const [alias, canonical] of Object.entries(DISPLAY_CHANNEL_ALIASES)) {
      if (!(alias in next)) continue;
      const value = next[alias];
      if (typeof value !== "number") continue;
      if (next[canonical] == null) {
        next[canonical] = value;
      }
      delete next[alias];
      changed = true;
    }
    return changed ? (next as SensorRow) : row;
  });
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
