/** Build the Monitoring lookback for an Anomaly → Monitoring jump. */

const DEFAULT_PAD_HOURS = 12;

/** Proportional pad applied to an episode's own duration (§episodeJumpWindow). */
const EPISODE_PAD_FRACTION = 0.5;
/** Total window floor/ceiling regardless of episode duration, in hours. */
const EPISODE_MIN_SPAN_HOURS = 2;
const EPISODE_MAX_SPAN_HOURS = 48;

function toMillis(value: string | Date | null | undefined): number | null {
  if (value == null) return null;
  const ms = value instanceof Date ? value.getTime() : Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

/**
 * Clamp ``to`` to the data horizon, shifting ``from`` earlier to preserve the
 * intended span so the chart stays filled with data (no empty future half).
 *
 * The horizon is ``max(now, datasetEnd)``, not wall-clock ``now`` alone. An
 * uploaded export can be timestamped ahead of the clock, and clamping such an
 * anomaly to ``now`` does not shrink the window — it slides the whole span
 * back, silently landing the operator months away from the anomaly they
 * clicked, on a chart that looks plausible and carries no scores. The backend
 * anchors ``/api/readings/recent`` to the newest stored row for the same
 * reason (src/api/routes/readings.py). Shared by every framing strategy
 * below so this reasoning applies uniformly.
 */
function clampToHorizon(fromMs: number, toMs: number, horizonMs: number): { from: Date; to: Date } {
  const spanMs = toMs - fromMs;
  let clampedFrom = fromMs;
  let clampedTo = toMs;

  if (clampedTo > horizonMs) {
    clampedTo = horizonMs;
    clampedFrom = clampedTo - spanMs;
  }

  // Guard against inverted / zero windows (e.g. clock skew).
  if (!(clampedTo > clampedFrom)) {
    clampedTo = horizonMs;
    clampedFrom = clampedTo - spanMs;
  }

  return { from: new Date(clampedFrom), to: new Date(clampedTo) };
}

function resolveHorizonMs(now: Date | undefined, datasetEnd: string | Date | null | undefined) {
  const nowMs = (now ?? new Date()).getTime();
  const datasetEndMs = toMillis(datasetEnd);
  return datasetEndMs === null ? nowMs : Math.max(nowMs, datasetEndMs);
}

/** Center a window on a single sample timestamp — used when no episode
 * context is available (e.g. a top/grouped anomaly row, not an episode). */
export function anomalyJumpWindow(
  timestamp: string | Date,
  options: {
    readonly padHours?: number;
    readonly now?: Date;
    /** Newest stored reading for the scope; widens the clamp horizon. */
    readonly datasetEnd?: string | Date | null;
  } = {},
): { from: Date; to: Date } | null {
  const center = toMillis(timestamp);
  if (center === null) return null;

  const padHours = options.padHours ?? DEFAULT_PAD_HOURS;
  const padMs = padHours * 3_600_000;
  const horizonMs = resolveHorizonMs(options.now, options.datasetEnd);

  return clampToHorizon(center - padMs, center + padMs, horizonMs);
}

/**
 * Frame an episode's own ``[start_ts, end_ts]``, padded by a fraction of its
 * duration so the event fills a useful portion of the chart.
 *
 * A fixed ±12h window centered on one sample inside the episode (the old
 * behaviour) ignores the episode's actual span: a two-hour episode ends up
 * occupying a sliver of a 24h chart, and a multi-day episode can be clipped
 * entirely. Padding is proportional instead — short episodes still get a
 * usable amount of surrounding context (floored at
 * {@link EPISODE_MIN_SPAN_HOURS} total), long ones are not padded past
 * {@link EPISODE_MAX_SPAN_HOURS} (the episode's own span wins if it is
 * already longer — the event itself must never be clipped to fit the pad).
 */
export function episodeJumpWindow(
  episode: { readonly start_ts: string | Date; readonly end_ts: string | Date },
  options: {
    readonly now?: Date;
    /** Newest stored reading for the scope; widens the clamp horizon. */
    readonly datasetEnd?: string | Date | null;
  } = {},
): { from: Date; to: Date } | null {
  const startMs = toMillis(episode.start_ts);
  const endMs = toMillis(episode.end_ts);
  if (startMs === null || endMs === null || endMs < startMs) return null;

  const durationMs = endMs - startMs;
  const minSpanMs = EPISODE_MIN_SPAN_HOURS * 3_600_000;
  const maxSpanMs = EPISODE_MAX_SPAN_HOURS * 3_600_000;

  const padMs = Math.min(
    Math.max(durationMs * EPISODE_PAD_FRACTION, (minSpanMs - durationMs) / 2),
    Math.max(0, (maxSpanMs - durationMs) / 2),
  );

  const horizonMs = resolveHorizonMs(options.now, options.datasetEnd);
  return clampToHorizon(startMs - padMs, endMs + padMs, horizonMs);
}

/**
 * Resolve the Monitoring jump window: frame the episode when one is given
 * (see {@link episodeJumpWindow}), otherwise fall back to centering on the
 * clicked sample (see {@link anomalyJumpWindow}) — the case for a sample with
 * no episode context, e.g. a row in the Top/Grouped anomalies tables.
 */
export function monitoringJumpWindow(
  params: {
    readonly timestamp: string | Date;
    readonly episode?: { readonly start_ts: string | Date; readonly end_ts: string | Date } | null;
  },
  options: {
    readonly now?: Date;
    readonly datasetEnd?: string | Date | null;
  } = {},
): { from: Date; to: Date } | null {
  if (params.episode) {
    const framed = episodeJumpWindow(params.episode, options);
    if (framed) return framed;
  }
  return anomalyJumpWindow(params.timestamp, options);
}
