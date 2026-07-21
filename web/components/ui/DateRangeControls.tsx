"use client";

import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { CustomDateRangeForm } from "@/components/ui/CustomDateRangeForm";
import {
  DEFAULT_PERIOD_ID,
  matchPeriodId,
  PERIOD_PRESETS,
  periodHours,
  REFRESH_PRESETS,
  windowForPeriod,
} from "@/lib/periodPresets";
import { useDateRange } from "@/lib/useDateRange";
import { useRefreshInterval } from "@/lib/useRefreshInterval";

const SELECT_CLASS =
  "h-8 rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 text-sm text-text";

/**
 * Period + refresh dropdowns (URL-driven). Replaces the old chip row so
 * Setpoints / Go Live share the same baseline as these controls.
 */
export function DateRangeControls() {
  const [{ from, to }, setRange] = useDateRange();
  const [refresh, setRefresh] = useRefreshInterval();
  const [isCustomOpen, setIsCustomOpen] = useState(false);

  const periodValue = isCustomOpen ? "custom" : from && to ? matchPeriodId(from, to) : "";

  function applyPeriod(periodId: string): void {
    if (periodId === "custom") {
      setIsCustomOpen(true);
      return;
    }
    if (!periodId) return;
    setIsCustomOpen(false);
    const hours = periodHours(periodId) ?? periodHours(DEFAULT_PERIOD_ID) ?? 24;
    const next = windowForPeriod(hours);
    void setRange(next);
  }

  function applyCustomRange(start: Date, end: Date): void {
    setIsCustomOpen(false);
    void setRange({ from: start, to: end });
  }

  return (
    <div className="relative flex items-center gap-2" data-testid="date-range-controls">
      <label className="flex items-center gap-1.5 text-sm text-text-muted">
        <span className="sr-only">Period</span>
        <select
          className={SELECT_CLASS}
          value={periodValue}
          onChange={(e) => applyPeriod(e.target.value)}
          data-testid="period-select"
          aria-label="Time period"
        >
          <option value="" disabled>
            Period
          </option>
          {PERIOD_PRESETS.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
          <option value="custom">Custom</option>
        </select>
      </label>

      <label className="flex items-center gap-1.5 text-sm text-text-muted">
        <span className="sr-only">Refresh</span>
        <select
          className={SELECT_CLASS}
          value={String(refresh)}
          onChange={(e) => void setRefresh(Number(e.target.value))}
          data-testid="refresh-select"
          aria-label="Refresh interval"
        >
          {REFRESH_PRESETS.map((p) => (
            <option key={p.id} value={p.id}>
              Refresh {p.label}
            </option>
          ))}
        </select>
      </label>

      {isCustomOpen && (
        <div className="absolute right-0 top-full z-20 mt-1 w-max">
          <CustomDateRangeForm from={from} to={to} onApply={applyCustomRange} />
          <div className="mt-1 flex justify-end">
            <Button variant="ghost" size="sm" onClick={() => setIsCustomOpen(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
