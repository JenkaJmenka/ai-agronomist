"use client";

import { parseAsString, useQueryState } from "nuqs";
import { Suspense, useMemo, useState } from "react";

import { Card } from "@/components/ui/Card";
import { DateRangeControls } from "@/components/ui/DateRangeControls";
import { Skeleton } from "@/components/ui/Skeleton";
import { discoverChannels } from "@/features/monitoring/api/readings";
import { ChannelToggles } from "@/features/monitoring/components/ChannelToggles";
import { ReadingsTable } from "@/features/monitoring/components/ReadingsTable";
import { SensorTimeSeriesChart } from "@/features/monitoring/components/SensorTimeSeriesChart";
import { useReadings } from "@/features/monitoring/hooks/useReadings";

interface MonitoringViewProps {
  readonly title: string;
  /** Restrict to these channels (e.g. gas page → co2). Empty = all discovered. */
  readonly channelFilter?: readonly string[];
}

function matchChannel(channels: string[], wanted: string): string[] {
  const exact = channels.filter((ch) => ch === wanted);
  if (exact.length > 0) return exact;
  return channels.filter((ch) => ch.toLowerCase().includes(wanted.toLowerCase()));
}

function MonitoringBody({ title, channelFilter }: MonitoringViewProps) {
  const { rows, isLoading, isError, error, windowLabel, downsampled } = useReadings();
  // Anomaly-jump deep link: ?channel= pre-selects one trace (P2 follow-up).
  const [channelParam] = useQueryState("channel", parseAsString);

  const channels = useMemo(() => {
    const discovered = discoverChannels(rows);
    if (!channelFilter || channelFilter.length === 0) return discovered;
    return discovered.filter((ch) =>
      channelFilter.some((f) => ch.toLowerCase().includes(f.toLowerCase())),
    );
  }, [rows, channelFilter]);

  // null = untouched: the channel deep link applies until the first toggle.
  const [hidden, setHidden] = useState<ReadonlySet<string> | null>(null);
  const visible = useMemo(() => {
    if (hidden !== null) return new Set(channels.filter((ch) => !hidden.has(ch)));
    if (channelParam) {
      const matched = matchChannel(channels, channelParam);
      if (matched.length > 0) return new Set(matched);
    }
    return new Set(channels);
  }, [channels, hidden, channelParam]);

  function toggleChannel(ch: string): void {
    setHidden(() => {
      const next = new Set(channels.filter((candidate) => !visible.has(candidate)));
      if (next.has(ch)) next.delete(ch);
      else next.add(ch);
      return next;
    });
  }

  if (isError) {
    throw error ?? new Error("Failed to load readings");
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">{title}</h1>
          <p className="text-sm text-text-muted tabular-nums">
            {windowLabel}
            {downsampled ? " · downsampled" : ""}
          </p>
        </div>
        <DateRangeControls />
      </div>

      {isLoading ? (
        <div className="space-y-4">
          <Skeleton className="h-80 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      ) : rows.length === 0 ? (
        <Card>
          <p className="py-8 text-center text-sm text-text-muted">
            No sensor readings in the selected window. Widen the range or pick another
            tenant/greenhouse.
          </p>
        </Card>
      ) : (
        <>
          <Card title="Sensor channels">
            <ChannelToggles channels={channels} visible={visible} onToggle={toggleChannel} />
          </Card>
          <Card title="Time series">
            <SensorTimeSeriesChart rows={rows} channels={channels} visible={visible} />
          </Card>
          <Card title={`Readings (${rows.length.toLocaleString()} rows, newest first)`}>
            <ReadingsTable rows={rows} channels={channels} />
          </Card>
        </>
      )}
    </div>
  );
}

export function MonitoringView(props: MonitoringViewProps) {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-6xl space-y-4">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-80 w-full" />
        </div>
      }
    >
      <MonitoringBody {...props} />
    </Suspense>
  );
}
