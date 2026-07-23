/**
 * Anomaly → Monitoring jump focus (`channel=` URL) and when to paint
 * anomaly bands / cell highlights on Monitoring / Climate / Gas.
 */

/** `channel=` is a comma-separated list when an anomaly touches several sensors. */
export function parseChannelParam(param: string): string[] {
  return param
    .split(",")
    .map((w) => w.trim())
    .filter(Boolean);
}

function matchOneChannel(channels: readonly string[], wanted: string): string[] {
  const exact = channels.filter((ch) => ch === wanted);
  if (exact.length > 0) return exact;
  return channels.filter((ch) => ch.toLowerCase().includes(wanted.toLowerCase()));
}

/** Map jump/sensor names onto channels present on this page. */
export function matchChannelParam(channels: readonly string[], param: string): string[] {
  const matched = new Set(
    parseChannelParam(param).flatMap((wanted) => matchOneChannel(channels, wanted)),
  );
  return [...matched];
}

/**
 * Whether anomaly bands / score-cell emphasis should paint on this view.
 *
 * - No `channel=` → organic browsing; score-based highlight stays on.
 * - `channel=` set but none of those sensors exist on this page (e.g. Gas
 *   after a humidity jump) → never highlight.
 * - Otherwise only while at least one focus sensor is still toggled on.
 */
export function shouldPaintAnomalyHighlight(params: {
  readonly channelParam: string | null | undefined;
  readonly pageChannels: readonly string[];
  readonly visible: ReadonlySet<string>;
}): boolean {
  const raw = params.channelParam?.trim();
  if (!raw) return true;

  const focus = matchChannelParam(params.pageChannels, raw);
  if (focus.length === 0) return false;
  return focus.some((ch) => params.visible.has(ch));
}
