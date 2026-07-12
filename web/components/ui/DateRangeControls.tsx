"use client";

import { parseAsIsoDateTime, useQueryStates } from "nuqs";

import { Button } from "@/components/ui/Button";

const QUICK_RANGES = [
  { label: "24 h", hours: 24 },
  { label: "7 d", hours: 24 * 7 },
  { label: "1 mo", hours: 24 * 30 },
  { label: "6 mo", hours: 24 * 182 },
] as const;

/**
 * URL-driven window controls (plan §7.4): state lives in the URL so reloads
 * and interrupted fetches are lossless — the structural fix for the
 * Streamlit state-loss bug.
 */
export function DateRangeControls() {
  const [{ from, to }, setRange] = useQueryStates({
    from: parseAsIsoDateTime,
    to: parseAsIsoDateTime,
  });

  function applyQuickRange(hours: number): void {
    const end = new Date();
    const start = new Date(end.getTime() - hours * 3_600_000);
    void setRange({ from: start, to: end });
  }

  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="date-range-controls">
      {QUICK_RANGES.map((r) => (
        <Button
          key={r.label}
          variant="secondary"
          size="sm"
          onClick={() => applyQuickRange(r.hours)}
        >
          {r.label}
        </Button>
      ))}
      <span className="text-xs text-text-muted tabular-nums">
        {from && to
          ? `${from.toISOString().slice(0, 16)} → ${to.toISOString().slice(0, 16)}`
          : "No window selected"}
      </span>
    </div>
  );
}
