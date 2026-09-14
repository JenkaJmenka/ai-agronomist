/** Build the Monitoring lookback for an Anomaly → Monitoring jump. */

const DEFAULT_PAD_HOURS = 12;

function toMillis(value: string | Date | null | undefined): number | null {
  if (value == null) return null;
  const ms = value instanceof Date ? value.getTime() : Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

/**
 * Center a window on ``timestamp``, then clamp ``to`` to the data horizon.
 * When clamping would shrink the span, shift ``from`` earlier to keep the
 * intended duration so the chart stays filled with data (no empty future half).
 *
 * The horizon is ``max(now, datasetEnd)``, not wall-clock ``now`` alone. An
 * uploaded export can be timestamped ahead of the clock, and clamping such an
 * anomaly to ``now`` does not shrink the window — it slides the whole span
 * back, silently landing the operator months away from the anomaly they
 * clicked, on a chart that looks plausible and carries no scores. The backend
 * anchors ``/api/readings/recent`` to the newest stored row for the same
 * reason (src/api/routes/readings.py).
 */
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
  const spanMs = padMs * 2;
  const nowMs = (options.now ?? new Date()).getTime();
  const datasetEndMs = toMillis(options.datasetEnd);
  const horizonMs = datasetEndMs === null ? nowMs : Math.max(nowMs, datasetEndMs);

  let fromMs = center - padMs;
  let toMs = center + padMs;

  if (toMs > horizonMs) {
    toMs = horizonMs;
    fromMs = toMs - spanMs;
  }

  // Guard against inverted / zero windows (e.g. clock skew).
  if (!(toMs > fromMs)) {
    toMs = horizonMs;
    fromMs = toMs - spanMs;
  }

  return { from: new Date(fromMs), to: new Date(toMs) };
}
