"use client";

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

function MonitoringBody({ title, channelFilter }: MonitoringViewProps) {
  const { rows, isLoading, isError, error, windowLabel, downsampled } = useReadings();

  const channels = useMemo(() => {
    const discovered = discoverChannels(rows);
    if (!channelFilter || channelFilter.length === 0) return discovered;
    return discovered.filter((ch) =>
      channelFilter.some((f) => ch.toLowerCase().includes(f.toLowerCase())),
    );
  }, [rows, channelFilter]);

  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set());
  const visible = useMemo(
    () => new Set(channels.filter((ch) => !hidden.has(ch))),
    [channels, hidden],
  );

  function toggleChannel(ch: string): void {
    setHidden((prev) => {
      const next = new Set(prev);
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
          <Card title={`Readings (latest ${Math.min(rows.length, 200)})`}>
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
