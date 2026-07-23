/** Build a chart/URL time window from one or more selected row timestamps. */

/** Pad around a single selected timestamp (± each side). */
export const SINGLE_ROW_PAD_MS = 15 * 60 * 1000;

/** Extra pad outside the outermost timestamps when 2+ rows are selected. */
export const MULTI_ROW_EDGE_PAD_MS = 60 * 1000;

export interface SelectionWindowOptions {
  readonly singlePadMs?: number;
  readonly edgePadMs?: number;
  readonly now?: Date;
}

/**
 * Derive ``[from, to]`` from selected ISO timestamps.
 *
 * - One row → centered ±{@link SINGLE_ROW_PAD_MS} (clamped so ``to`` ≤ now).
 * - Two or more → span from earliest to latest with a small edge pad.
 */
export function windowFromSelectedTimestamps(
  timestamps: readonly string[],
  options: SelectionWindowOptions = {},
): { from: Date; to: Date } | null {
  const times: number[] = [];
  for (const ts of timestamps) {
    const ms = Date.parse(ts);
    if (Number.isFinite(ms)) times.push(ms);
  }
  if (times.length === 0) return null;

  times.sort((a, b) => a - b);
  const nowMs = (options.now ?? new Date()).getTime();
  const singlePad = options.singlePadMs ?? SINGLE_ROW_PAD_MS;
  const edgePad = options.edgePadMs ?? MULTI_ROW_EDGE_PAD_MS;

  let fromMs: number;
  let toMs: number;

  if (times.length === 1) {
    const center = times[0];
    const span = singlePad * 2;
    fromMs = center - singlePad;
    toMs = center + singlePad;
    if (toMs > nowMs) {
      toMs = nowMs;
      fromMs = toMs - span;
    }
  } else {
    fromMs = times[0] - edgePad;
    toMs = times[times.length - 1] + edgePad;
    if (toMs > nowMs) toMs = nowMs;
  }

  if (!(toMs > fromMs)) {
    toMs = nowMs;
    fromMs = toMs - singlePad * 2;
  }

  return { from: new Date(fromMs), to: new Date(toMs) };
}
