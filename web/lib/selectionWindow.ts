/** Build a chart/URL time window from one or more selected row timestamps. */

/** Pad around a single selected timestamp (± each side). */
export const SINGLE_ROW_PAD_MS = 15 * 60 * 1000;

/** Extra pad outside the outermost timestamps when 2+ rows are selected. */
export const MULTI_ROW_EDGE_PAD_MS = 60 * 1000;

export interface SelectionWindowOptions {
  readonly singlePadMs?: number;
  readonly edgePadMs?: number;
}

/**
 * Derive ``[from, to]`` from selected ISO timestamps.
 *
 * - One row → centered ±{@link SINGLE_ROW_PAD_MS}.
 * - Two or more → span from earliest to latest with a small edge pad.
 *
 * Does **not** clamp to wall-clock now — demo / uploaded data often sits in the
 * future relative to the browser clock, and clamping would jump the chart away
 * from the row the operator just selected.
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
  const singlePad = options.singlePadMs ?? SINGLE_ROW_PAD_MS;
  const edgePad = options.edgePadMs ?? MULTI_ROW_EDGE_PAD_MS;

  let fromMs: number;
  let toMs: number;

  if (times.length === 1) {
    fromMs = times[0] - singlePad;
    toMs = times[0] + singlePad;
  } else {
    fromMs = times[0] - edgePad;
    toMs = times[times.length - 1] + edgePad;
  }

  // Duplicate timestamps → still give a usable pad around the point.
  if (!(toMs > fromMs)) {
    fromMs = times[0] - singlePad;
    toMs = times[0] + singlePad;
  }

  return { from: new Date(fromMs), to: new Date(toMs) };
}
