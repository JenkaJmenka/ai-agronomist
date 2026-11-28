"use client";

import { useMemo, useRef, useState } from "react";
import type uPlot from "uplot";

import { chartTokens } from "@/components/charts/chartTheme";
import { UPlotChart } from "@/components/charts/UPlotChart";
import {
  DriftChartLegend,
  type LegendItem,
} from "@/features/drift-watch/components/DriftChartLegend";
import {
  DriftEpisodeBands,
  type PlotRect,
} from "@/features/drift-watch/components/DriftEpisodeBands";
import type { EpisodeBand } from "@/features/drift-watch/lib/driftSeries";

export type ChartColor = "chart1" | "chart2" | "chart3" | "chart8";

export interface DriftLineSpec {
  readonly label: string;
  readonly color: ChartColor;
  readonly dash?: readonly number[];
  readonly width?: number;
}

interface DriftChartProps {
  readonly testId: string;
  readonly ariaLabel: string;
  readonly data: uPlot.AlignedData;
  readonly lines: readonly DriftLineSpec[];
  readonly axisLabel: string;
  /** Fixed x range, unix seconds, so every chart in the stack shares one time axis. */
  readonly xRange: readonly [number, number];
  readonly syncKey: string;
  readonly height?: number;
  readonly bands?: readonly EpisodeBand[];
}

const DEFAULT_HEIGHT = 200;
const PX_FALLBACK = 1;

/** One themed uPlot chart with a line-style legend and optional episode bands. */
export function DriftChart({
  testId,
  ariaLabel,
  data,
  lines,
  axisLabel,
  xRange,
  syncKey,
  height = DEFAULT_HEIGHT,
  bands,
}: DriftChartProps) {
  const [rect, setRect] = useState<PlotRect | null>(null);
  const setRectRef = useRef(setRect);
  setRectRef.current = setRect;

  const [xMin, xMax] = xRange;
  const options = useMemo<Omit<uPlot.Options, "width" | "height">>(
    () => ({
      scales: { x: { time: true, range: () => [xMin, xMax] } },
      series: [
        {},
        ...lines.map((line) => ({
          label: line.label,
          stroke: () => chartTokens()[line.color],
          width: line.width ?? 1.5,
          ...(line.dash ? { dash: [...line.dash] } : {}),
          spanGaps: false,
          points: { show: false },
        })),
      ],
      axes: [{}, { label: axisLabel, size: 64 }],
      legend: { show: false },
      cursor: { sync: { key: syncKey } },
      hooks: {
        draw: [
          (u: uPlot) => {
            const px = window.devicePixelRatio || PX_FALLBACK;
            const next = {
              left: u.bbox.left / px,
              top: u.bbox.top / px,
              width: u.bbox.width / px,
              height: u.bbox.height / px,
            };
            setRectRef.current((prev) =>
              prev &&
              prev.left === next.left &&
              prev.top === next.top &&
              prev.width === next.width &&
              prev.height === next.height
                ? prev
                : next,
            );
          },
        ],
      },
    }),
    [lines, axisLabel, xMin, xMax, syncKey],
  );

  const legend: LegendItem[] = lines.map((l) => ({
    label: l.label,
    color: chartTokens()[l.color],
    dash: l.dash,
  }));

  return (
    <div className="space-y-1" data-testid={testId}>
      <div role="img" aria-label={ariaLabel} className="relative">
        <UPlotChart data={data} options={options} height={height} />
        {bands ? <DriftEpisodeBands bands={bands} rect={rect} /> : null}
      </div>
      <DriftChartLegend items={legend} />
    </div>
  );
}
