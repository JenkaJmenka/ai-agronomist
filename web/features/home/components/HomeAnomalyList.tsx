"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { Chip } from "@/components/ui/Chip";
import type { HomeAnomalyItem } from "@/features/home/lib/recentAnomalies";
import { pickDashboardSearchParams } from "@/lib/dashboardQuery";
import { formatTime } from "@/lib/formatTime";

interface HomeAnomalyListProps {
  readonly items: readonly HomeAnomalyItem[];
  readonly threshold: number;
  readonly isLoading: boolean;
}

export function HomeAnomalyList({ items, threshold, isLoading }: HomeAnomalyListProps) {
  const search = useSearchParams();

  if (isLoading) {
    return (
      <p className="text-sm text-text-muted" data-testid="home-anomaly-list-loading">
        Loading anomalies…
      </p>
    );
  }

  if (items.length === 0) {
    return (
      <p className="text-sm text-text-muted" data-testid="home-anomaly-list-empty">
        No anomalies in the selected window.
      </p>
    );
  }

  return (
    <ul className="divide-y divide-border" data-testid="home-anomaly-list">
      {items.map((item) => {
        const q = pickDashboardSearchParams(search);
        if (item.sensors.length > 0) q.set("channel", item.sensors.join(","));
        q.set("threshold", String(threshold));
        q.set("from", item.windowStart);
        q.set("to", item.windowEnd);
        const href = `/anomalies?${q.toString()}`;

        return (
          <li
            key={item.id}
            className="flex flex-wrap items-center gap-2 py-2 text-sm"
            data-testid={`home-anomaly-row-${item.id}`}
          >
            <span className="tabular-nums text-text-muted">{formatTime(item.timestamp)}</span>
            <span className="tabular-nums font-medium">{item.score.toFixed(2)}</span>
            <Chip severity={item.severity}>{item.severity}</Chip>
            <span className="text-text-muted">
              {item.sensors.length > 0 ? item.sensors.join(", ") : "—"}
            </span>
            <Link
              href={href}
              className="ml-auto text-accent underline-offset-2 hover:underline"
              data-testid={`home-anomaly-link-${item.id}`}
            >
              View
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
