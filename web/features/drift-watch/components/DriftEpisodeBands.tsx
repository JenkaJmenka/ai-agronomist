"use client";

import {
  BAND_CLASSES,
  bandLabel,
  type EpisodeBand,
  focusEpisodeRow,
} from "@/features/drift-watch/lib/driftSeries";

export interface PlotRect {
  readonly left: number;
  readonly top: number;
  readonly width: number;
  readonly height: number;
}

const LABEL_TEXT = {
  unlabelled: "not labelled",
  confirmed_drift: "confirmed drift",
  false_alarm: "false alarm",
} as const;

const HATCH =
  "repeating-linear-gradient(45deg, transparent 0 4px, color-mix(in srgb, currentColor 35%, transparent) 4px 5px)";

/**
 * DOM overlay aligned to the uPlot plot area. The band fill ignores the pointer
 * (so the chart cursor keeps working); only a small tab at the top is clickable
 * and jumps to the episode's table row, where the feedback buttons live.
 */
export function DriftEpisodeBands({
  bands,
  rect,
}: {
  readonly bands: readonly EpisodeBand[];
  readonly rect: PlotRect | null;
}) {
  const style = rect
    ? { left: rect.left, top: rect.top, width: rect.width, height: rect.height }
    : undefined;
  return (
    <div
      className={`pointer-events-none absolute ${rect ? "" : "inset-0"}`}
      style={style}
      data-testid="episode-bands"
    >
      {bands.map((band) => {
        const kind = bandLabel(band.label);
        const text = `${band.direction} episode, ${LABEL_TEXT[kind]}${band.suppressed ? ", suppressed (stuck sensor)" : ""}`;
        return (
          <div
            key={band.key}
            data-testid="episode-band"
            data-label={kind}
            data-suppressed={String(band.suppressed)}
            className={`absolute inset-y-0 border-x ${band.suppressed ? "border-dashed text-text" : ""} ${BAND_CLASSES[kind]}`}
            style={{
              left: `${band.left * 100}%`,
              width: `${band.width * 100}%`,
              ...(band.suppressed ? { backgroundImage: HATCH } : {}),
            }}
          >
            <button
              type="button"
              data-testid="episode-band-jump"
              aria-label={`Show ${text} in the table`}
              title={text}
              onClick={() => focusEpisodeRow(band.key)}
              className="pointer-events-auto absolute inset-x-0 top-0 h-3 cursor-pointer bg-current opacity-30 hover:opacity-60 focus-visible:opacity-70"
            />
          </div>
        );
      })}
    </div>
  );
}
