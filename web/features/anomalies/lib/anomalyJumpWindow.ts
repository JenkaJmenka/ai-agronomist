/** Build the Monitoring lookback for an Anomaly → Monitoring jump. */

const DEFAULT_PAD_HOURS = 12;

/**
 * Center a window on ``timestamp``, then clamp so ``to`` never exceeds ``now``.
 * When clamping would shrink the span, shift ``from`` earlier to keep the
 * intended duration so the chart stays filled with data (no empty future half).
 */
export function anomalyJumpWindow(
  timestamp: string | Date,
  options: { readonly padHours?: number; readonly now?: Date } = {},
): { from: Date; to: Date } | null {
  const center = timestamp instanceof Date ? timestamp.getTime() : Date.parse(timestamp);
  if (!Number.isFinite(center)) return null;

  const padHours = options.padHours ?? DEFAULT_PAD_HOURS;
  const padMs = padHours * 3_600_000;
  const spanMs = padMs * 2;
  const nowMs = (options.now ?? new Date()).getTime();

  let fromMs = center - padMs;
  let toMs = center + padMs;

  if (toMs > nowMs) {
    toMs = nowMs;
    fromMs = toMs - spanMs;
  }

  // Guard against inverted / zero windows (e.g. clock skew).
  if (!(toMs > fromMs)) {
    toMs = nowMs;
    fromMs = toMs - spanMs;
  }

  return { from: new Date(fromMs), to: new Date(toMs) };
}
