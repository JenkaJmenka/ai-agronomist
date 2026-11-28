"use client";

import { parseAsStringLiteral, useQueryState } from "nuqs";
import { useMemo } from "react";

import { Skeleton } from "@/components/ui/Skeleton";
import { DRIFT_WINDOW_WEEKS, type DriftSeries } from "@/features/drift-watch/api/driftWatch";
import { DriftChart, type DriftLineSpec } from "@/features/drift-watch/components/DriftChart";
import { useDriftWatchSeries } from "@/features/drift-watch/hooks/useDriftWatch";
import {
  BAND_CLASSES,
  episodeBands,
  probeData,
  residualData,
  statisticData,
} from "@/features/drift-watch/lib/driftSeries";
import { useScope } from "@/lib/useScope";

const WEEK_VALUES = DRIFT_WINDOW_WEEKS.map(String) as ["13", "26", "52"];

const RESIDUAL_LINES: readonly DriftLineSpec[] = [
  { label: "Deviation", color: "chart1" },
  { label: "+ alert floor", color: "chart3", dash: [6, 4] },
  { label: "- alert floor", color: "chart3", dash: [2, 3] },
];
const STAT_LINES_BASE: readonly DriftLineSpec[] = [
  { label: "Upward statistic (s+)", color: "chart2" },
  { label: "Downward statistic (s-)", color: "chart1", dash: [2, 3] },
];
const STAT_LINES_H: readonly DriftLineSpec[] = [
  ...STAT_LINES_BASE,
  { label: "Alert threshold (h)", color: "chart3", dash: [6, 4] },
];
const PROBE_LINES: readonly DriftLineSpec[] = [
  { label: "Target probe", color: "chart1" },
  { label: "Reference probe", color: "chart2", dash: [6, 4] },
];

const BAND_LEGEND = [
  ["unlabelled", "Episode, not labelled"],
  ["confirmed_drift", "Confirmed drift"],
  ["false_alarm", "False alarm"],
] as const;

const MISMATCH_NOTE = (n: number) =>
  `The chart is a replay from calibration. It found ${n} episode starts that drift watch did not record (for example, before the first upload). Shaded bands show the recorded episodes.`;

function WindowControl({
  weeks,
  onChange,
}: {
  readonly weeks: string;
  readonly onChange: (w: (typeof WEEK_VALUES)[number]) => void;
}) {
  return (
    <fieldset className="flex items-center gap-1 text-xs" data-testid="window-control">
      <legend className="sr-only">Chart window</legend>
      {WEEK_VALUES.map((w) => (
        <button
          key={w}
          type="button"
          aria-pressed={weeks === w}
          data-testid={`window-${w}`}
          onClick={() => onChange(w)}
          className={`rounded border px-2 py-1 ${weeks === w ? "border-accent bg-accent-soft text-text" : "border-border text-text-muted hover:text-text"}`}
        >
          {w} weeks
        </button>
      ))}
    </fieldset>
  );
}

function Charts({ inputId, series }: { readonly inputId: string; readonly series: DriftSeries }) {
  const bands = useMemo(() => episodeBands(inputId, series), [inputId, series]);
  const xRange = useMemo(
    () => [Date.parse(series.window_start) / 1000, Date.parse(series.window_end) / 1000] as const,
    [series.window_start, series.window_end],
  );
  const residual = useMemo(() => residualData(series), [series]);
  const stat = useMemo(() => statisticData(series), [series]);
  const probes = useMemo(() => probeData(series), [series]);
  const syncKey = `drift:${inputId}`;
  const channel = series.target_channel;

  return (
    <div className="space-y-4">
      <DriftChart
        testId="chart-residual"
        ariaLabel={`Deviation of ${channel} from its reference over time, in ${series.unit}, with the alert floor and ${bands.length} recorded episodes shaded.`}
        data={residual}
        lines={RESIDUAL_LINES}
        axisLabel={series.unit}
        xRange={xRange}
        syncKey={syncKey}
        bands={bands}
      />
      <ul
        className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-muted"
        data-testid="band-legend"
      >
        {BAND_LEGEND.map(([kind, text]) => (
          <li key={kind} className="flex items-center gap-1.5">
            <span
              className={`inline-block h-3 w-4 border-x ${BAND_CLASSES[kind]}`}
              aria-hidden="true"
            />
            {text}
          </li>
        ))}
        <li className="flex items-center gap-1.5">
          <span
            className="inline-block h-3 w-4 border-x border-dashed border-text-muted"
            aria-hidden="true"
          />
          Dashed outline: suppressed (stuck sensor)
        </li>
      </ul>
      <DriftChart
        testId="chart-statistic"
        ariaLabel={`Drift statistic for ${channel} over time, in sigma units${series.h == null ? "" : ", with the alert threshold"}.`}
        data={stat}
        lines={series.h == null ? STAT_LINES_BASE : STAT_LINES_H}
        axisLabel="Drift statistic (σ)"
        xRange={xRange}
        syncKey={syncKey}
        height={160}
      />
      {series.kind === "paired_probes" ? (
        <DriftChart
          testId="chart-probes"
          ariaLabel={`Raw readings of the target and reference probes for ${channel} over time, in ${series.unit}.`}
          data={probes}
          lines={PROBE_LINES}
          axisLabel={series.unit}
          xRange={xRange}
          syncKey={syncKey}
        />
      ) : null}
    </div>
  );
}

/** Fetches and renders the drift charts for one input. Mounted only while the panel is open. */
export function DriftSeriesCharts({ inputId }: { readonly inputId: string }) {
  const { tenant, gh } = useScope();
  const [weeks, setWeeks] = useQueryState(
    "driftWeeks",
    parseAsStringLiteral(WEEK_VALUES).withDefault("13"),
  );
  const query = useDriftWatchSeries(tenant, gh, inputId, Number(weeks), true);
  const series = query.data;

  return (
    <div className="space-y-3" data-testid="drift-series-panel">
      <WindowControl weeks={weeks} onChange={(w) => void setWeeks(w)} />
      {query.isError ? (
        <p role="alert" className="text-sm text-sev-critical">
          Could not load the chart: {query.error.message}
        </p>
      ) : !series ? (
        <Skeleton className="h-48 w-full" />
      ) : series.reason ? (
        <p className="text-sm text-text-muted" data-testid="series-reason">
          {series.reason}
        </p>
      ) : series.points.length === 0 ? (
        <p className="text-sm text-text-muted" data-testid="series-empty">
          No data in this window.
        </p>
      ) : (
        <>
          {series.replay_mismatch > 0 ? (
            <p className="text-sm text-text-muted" data-testid="series-mismatch-note">
              {MISMATCH_NOTE(series.replay_mismatch)}
            </p>
          ) : null}
          <Charts inputId={inputId} series={series} />
        </>
      )}
    </div>
  );
}
