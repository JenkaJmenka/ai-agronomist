"use client";

import { useCallback, useState } from "react";

import { type SelectionWindowOptions, windowFromSelectedTimestamps } from "@/lib/selectionWindow";

export type TimestampRow = { readonly timestamp: string };

export type SelectionWindowHandler = (from: Date, to: Date) => void;

/**
 * Multi-select rows by timestamp and zoom the chart to that span.
 *
 * - Click → select only that row
 * - Meta/Ctrl+click → toggle row in the selection
 * - Shift+click → select the inclusive range between the anchor and this row
 *   (using the current display order)
 *
 * Callers should treat ``onWindowSelect`` as a chart-only focus (not a URL
 * period change) so the table keeps its current rows.
 */
export function useTimestampRowSelection(
  onWindowSelect: SelectionWindowHandler | undefined,
  windowOptions?: SelectionWindowOptions,
) {
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());
  const [anchor, setAnchor] = useState<string | null>(null);

  const clearSelection = useCallback(() => {
    setSelected(new Set());
    setAnchor(null);
  }, []);

  const isSelected = useCallback((row: TimestampRow) => selected.has(row.timestamp), [selected]);

  const handleRowClick = useCallback(
    (
      row: TimestampRow,
      event: { metaKey: boolean; ctrlKey: boolean; shiftKey: boolean },
      displayRows: readonly TimestampRow[],
    ) => {
      if (!onWindowSelect) return;

      const ts = row.timestamp;
      let next: Set<string>;

      if (event.shiftKey && anchor !== null) {
        const start = displayRows.findIndex((r) => r.timestamp === anchor);
        const end = displayRows.findIndex((r) => r.timestamp === ts);
        next = new Set(selected);
        if (start >= 0 && end >= 0) {
          const lo = Math.min(start, end);
          const hi = Math.max(start, end);
          for (let i = lo; i <= hi; i++) {
            next.add(displayRows[i].timestamp);
          }
        } else {
          next.add(ts);
        }
      } else if (event.metaKey || event.ctrlKey) {
        next = new Set(selected);
        if (next.has(ts)) next.delete(ts);
        else next.add(ts);
        setAnchor(ts);
      } else {
        next = new Set([ts]);
        setAnchor(ts);
      }

      setSelected(next);

      if (next.size === 0) return;
      const win = windowFromSelectedTimestamps([...next], windowOptions);
      if (win) onWindowSelect(win.from, win.to);
    },
    [anchor, onWindowSelect, selected, windowOptions],
  );

  return { selected, isSelected, handleRowClick, clearSelection };
}
