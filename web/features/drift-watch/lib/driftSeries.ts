import type uPlot from "uplot";

import type { DriftSeries, DriftSeriesEpisode } from "@/features/drift-watch/api/driftWatch";

const toSec = (iso: string): number => Date.parse(iso) / 1000;

/** Stable key shared by a chart band and its episode table row. */
export function episodeKey(inputId: string, direction: string, firstDetectedTs: string): string {
  return `${inputId}|${direction}|${Date.parse(firstDetectedTs)}`;
}

function xs(series: DriftSeries): number[] {
  return series.points.map((p) => toSec(p.ts));
}

const constant = (n: number, value: number): number[] => Array.from({ length: n }, () => value);

/** Chart A data: [x, dev, +alert_floor, -alert_floor]. */
export function residualData(series: DriftSeries): uPlot.AlignedData {
  const x = xs(series);
  return [
    x,
    series.points.map((p) => p.dev ?? null),
    constant(x.length, series.alert_floor),
    constant(x.length, -series.alert_floor),
  ];
}

/** Chart B data: [x, s_pos, s_neg] plus h when the input has one. */
export function statisticData(series: DriftSeries): uPlot.AlignedData {
  const x = xs(series);
  const base: uPlot.AlignedData = [
    x,
    series.points.map((p) => p.s_pos ?? null),
    series.points.map((p) => p.s_neg ?? null),
  ];
  return series.h == null ? base : [...base, constant(x.length, series.h)];
}

/** Chart C data (paired probes): [x, target, reference]. */
export function probeData(series: DriftSeries): uPlot.AlignedData {
  return [
    xs(series),
    series.points.map((p) => p.target ?? null),
    series.points.map((p) => p.reference ?? null),
  ];
}

export interface EpisodeBand {
  readonly key: string;
  readonly label: string;
  readonly direction: string;
  readonly suppressed: boolean;
  /** Fractions of the window, 0..1. */
  readonly left: number;
  readonly width: number;
}

const MIN_BAND_FRACTION = 0.004;

export function bandLabel(
  label: string | null | undefined,
): "confirmed_drift" | "false_alarm" | "unlabelled" {
  return label === "confirmed_drift" || label === "false_alarm" ? label : "unlabelled";
}

/** Tailwind classes per label; the fill is never the only cue (see legend + border style). */
export const BAND_CLASSES: Record<ReturnType<typeof bandLabel>, string> = {
  unlabelled: "bg-[color-mix(in_srgb,var(--sev-warning)_24%,transparent)] border-sev-warning",
  confirmed_drift: "bg-[color-mix(in_srgb,var(--sev-ok)_24%,transparent)] border-sev-ok",
  false_alarm: "bg-[color-mix(in_srgb,var(--text-muted)_24%,transparent)] border-text-muted",
};

export function episodeBands(inputId: string, series: DriftSeries): EpisodeBand[] {
  const start = toSec(series.window_start);
  const span = toSec(series.window_end) - start;
  if (!(span > 0)) return [];
  const bands: EpisodeBand[] = [];
  for (const ep of series.episodes as DriftSeriesEpisode[]) {
    const from = toSec(ep.onset_ts ?? ep.first_detected_ts);
    const to = toSec(ep.last_confirmed_ts ?? ep.first_detected_ts);
    if (!(Number.isFinite(from) && Number.isFinite(to))) continue;
    const l = Math.max(0, (Math.min(from, to) - start) / span);
    const r = Math.min(1, (Math.max(from, to) - start) / span);
    if (r < 0 || l > 1) continue;
    bands.push({
      key: episodeKey(inputId, ep.direction, ep.first_detected_ts),
      label: bandLabel(ep.label),
      direction: ep.direction,
      suppressed: Boolean(ep.suppressed_by),
      left: l,
      width: Math.max(MIN_BAND_FRACTION, r - l),
    });
  }
  return bands;
}

/** Scroll to and briefly highlight the episode table row for a band. */
export function focusEpisodeRow(key: string): void {
  const row = document.querySelector<HTMLElement>(`[data-episode-key="${CSS.escape(key)}"]`);
  if (!row) return;
  row.scrollIntoView?.({ behavior: "smooth", block: "center" });
  row.dataset.highlighted = "true";
  window.setTimeout(() => {
    delete row.dataset.highlighted;
  }, 2500);
}
