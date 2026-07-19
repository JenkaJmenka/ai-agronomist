"use client";

import { type ColumnDef, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useRef } from "react";

/** Row count above which the body renders through the virtualizer. */
const VIRTUALIZE_THRESHOLD = 100;
const ESTIMATED_ROW_HEIGHT_PX = 37;
const OVERSCAN_ROWS = 12;
const VIRTUAL_VIEWPORT_MAX_HEIGHT = "max-h-[560px]";

interface DataTableProps<T> {
  readonly columns: ColumnDef<T, unknown>[];
  readonly rows: T[];
  readonly emptyMessage?: string;
  /** Extra classes for a body row (e.g. anomaly highlighting). */
  readonly rowClassName?: (row: T) => string | undefined;
  /** Top-align cells so rows with wrapping content (e.g. multi-line "Why") read cleanly. */
  readonly alignTop?: boolean;
  /** Keep columns at their natural width (no wrap) so wide tables scroll horizontally. */
  readonly scrollX?: boolean;
  /** Cap the body height so short (non-virtualized) tables scroll vertically too. */
  readonly scrollY?: boolean;
}

/**
 * Sticky-header table shell (TanStack Table). Large row sets render through
 * TanStack Virtual so a 50k-row window stays smooth (plan §3) — only the
 * visible slice plus overscan hits the DOM; spacer rows preserve scroll size.
 */
export function DataTable<T>({
  columns,
  rows,
  emptyMessage = "No records in the selected filters.",
  rowClassName,
  alignTop = false,
  scrollX = false,
  scrollY = false,
}: DataTableProps<T>) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const table = useReactTable({
    data: rows,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  const tableRows = table.getRowModel().rows;
  const isVirtual = tableRows.length > VIRTUALIZE_THRESHOLD;

  const virtualizer = useVirtualizer({
    count: tableRows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ESTIMATED_ROW_HEIGHT_PX,
    overscan: OVERSCAN_ROWS,
    enabled: isVirtual,
  });

  if (rows.length === 0) {
    return <p className="py-8 text-center text-sm text-text-muted">{emptyMessage}</p>;
  }

  const virtualItems = isVirtual ? virtualizer.getVirtualItems() : null;
  const paddingTop = virtualItems?.[0]?.start ?? 0;
  const paddingBottom = virtualItems
    ? virtualizer.getTotalSize() - (virtualItems.at(-1)?.end ?? 0)
    : 0;

  return (
    <div
      ref={scrollRef}
      data-testid="data-table"
      data-virtualized={isVirtual || undefined}
      className={`overflow-auto rounded-[var(--radius-card)] border border-border ${
        isVirtual || scrollY ? VIRTUAL_VIEWPORT_MAX_HEIGHT : ""
      }`}
    >
      <table className={`${scrollX ? "min-w-full" : "w-full"} text-sm`}>
        <thead className="sticky top-0 z-10 bg-bg-elevated">
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id} className="border-b border-border text-left">
              {hg.headers.map((h) => (
                <th
                  key={h.id}
                  className={`px-3 py-2 font-semibold text-text-muted ${
                    scrollX ? "whitespace-nowrap" : ""
                  }`}
                >
                  {h.isPlaceholder ? null : flexRender(h.column.columnDef.header, h.getContext())}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {paddingTop > 0 ? (
            <tr>
              <td colSpan={columns.length} style={{ height: paddingTop, padding: 0 }} />
            </tr>
          ) : null}
          {(virtualItems ?? tableRows.map((_, index) => ({ index }))).map(({ index }) => {
            const row = tableRows[index];
            return (
              <tr
                key={row.id}
                // Measured (not estimated) heights: anomaly rows with chips
                // run taller than 37px, and estimate drift makes rows jump
                // while scrolling. data-index + measureElement keep the
                // virtualizer's offsets exact.
                data-index={index}
                ref={isVirtual ? virtualizer.measureElement : undefined}
                className={`border-b border-border last:border-b-0 ${
                  rowClassName?.(row.original) ?? ""
                }`}
              >
                {row.getVisibleCells().map((cell) => (
                  <td
                    key={cell.id}
                    className={`px-3 py-2 ${alignTop ? "align-top" : ""} ${
                      scrollX ? "whitespace-nowrap" : ""
                    }`}
                  >
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            );
          })}
          {paddingBottom > 0 ? (
            <tr>
              <td colSpan={columns.length} style={{ height: paddingBottom, padding: 0 }} />
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}
