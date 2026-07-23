"use client";

import {
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { type KeyboardEvent, useEffect, useRef, useState } from "react";

import { TABLE_PAGE_SIZE, TablePagination } from "@/components/ui/TablePagination";
import { useTableDragPan } from "@/components/ui/useTableDragPan";

/** Row count above which the body renders through the virtualizer. */
const VIRTUALIZE_THRESHOLD = 100;
const ESTIMATED_ROW_HEIGHT_PX = 37;
const OVERSCAN_ROWS = 12;
const VIRTUAL_VIEWPORT_MAX_HEIGHT = "max-h-[560px]";

/** Modifier keys used for multi-select (avoids React MouseEvent vs DOM clash). */
export interface RowClickModifiers {
  readonly metaKey: boolean;
  readonly ctrlKey: boolean;
  readonly shiftKey: boolean;
}

interface DataTableProps<T> {
  readonly columns: ColumnDef<T, unknown>[];
  readonly rows: T[];
  readonly emptyMessage?: string;
  /** Extra classes for a body row. */
  readonly rowClassName?: (row: T) => string | undefined;
  /** Extra classes for a body cell (e.g. per-column anomaly highlight). */
  readonly cellClassName?: (row: T, columnId: string) => string | undefined;
  /** Top-align cells so rows with wrapping content (e.g. multi-line "Why") read cleanly. */
  readonly alignTop?: boolean;
  /** Keep columns at their natural width (no wrap) so wide tables scroll horizontally. */
  readonly scrollX?: boolean;
  /** Cap the body height so short (non-virtualized) tables scroll vertically too. */
  readonly scrollY?: boolean;
  /** Rows per page. Defaults to {@link TABLE_PAGE_SIZE}. */
  readonly pageSize?: number;
  /**
   * When set, rows are clickable (chart/window sync). Interactive children
   * (links, buttons) still receive clicks without selecting the row.
   * Event exposes modifier keys for multi-select.
   */
  readonly onRowClick?: (row: T, event: RowClickModifiers) => void;
  /** Marks the row as selected for styling / aria-selected. */
  readonly isRowSelected?: (row: T) => boolean;
}

/**
 * Sticky-header table shell (TanStack Table) with client-side pagination.
 * Large single-page row sets still render through TanStack Virtual so a
 * 50k-row window stays smooth when pagination is disabled or oversized.
 *
 * Pointer drag pans the scrollport horizontally (grab + drag); wheel/trackpad
 * still scrolls vertically when the viewport is capped.
 */
export function DataTable<T>({
  columns,
  rows,
  emptyMessage = "No records in the selected filters.",
  rowClassName,
  cellClassName,
  alignTop = false,
  scrollX = false,
  scrollY = false,
  pageSize = TABLE_PAGE_SIZE,
  onRowClick,
  isRowSelected,
}: DataTableProps<T>) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pageIndex, setPageIndex] = useState(0);

  const table = useReactTable({
    data: rows,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    state: {
      pagination: {
        pageIndex: Math.min(pageIndex, Math.max(0, Math.ceil(rows.length / pageSize) - 1)),
        pageSize,
      },
    },
    onPaginationChange: (updater) => {
      const current = {
        pageIndex: Math.min(pageIndex, Math.max(0, Math.ceil(rows.length / pageSize) - 1)),
        pageSize,
      };
      const next = typeof updater === "function" ? updater(current) : updater;
      setPageIndex(next.pageIndex);
    },
    autoResetPageIndex: false,
  });

  // Reset to first page when the row *count* changes (filter / window).
  // Do not depend on `rows` identity — live SSE flushes a new array every
  // 500ms with a similar length and was snapping the user back to page 0.
  const rowCount = rows.length;
  useEffect(() => {
    setPageIndex(0);
  }, [rowCount]);

  const tableRows = table.getRowModel().rows;
  const pageCount = table.getPageCount();
  const isVirtual = tableRows.length > VIRTUALIZE_THRESHOLD;
  const isEmpty = rows.length === 0;

  const virtualizer = useVirtualizer({
    count: tableRows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ESTIMATED_ROW_HEIGHT_PX,
    overscan: OVERSCAN_ROWS,
    enabled: isVirtual,
  });

  useTableDragPan(scrollRef, !isEmpty);

  if (isEmpty) {
    return <p className="py-8 text-center text-sm text-text-muted">{emptyMessage}</p>;
  }

  const virtualItems = isVirtual ? virtualizer.getVirtualItems() : null;
  const paddingTop = virtualItems?.[0]?.start ?? 0;
  const paddingBottom = virtualItems
    ? virtualizer.getTotalSize() - (virtualItems.at(-1)?.end ?? 0)
    : 0;

  // Clamp displayed page if rows shrank under the current index.
  const safePageIndex = Math.min(pageIndex, Math.max(0, pageCount - 1));

  return (
    <div className="space-y-2">
      <div
        ref={scrollRef}
        data-testid="data-table"
        data-virtualized={isVirtual || undefined}
        className={`overflow-auto overscroll-contain rounded-[var(--radius-card)] border border-border ${
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
              const selected = isRowSelected?.(row.original) ?? false;
              const clickable = Boolean(onRowClick);
              return (
                <tr
                  key={row.id}
                  // Measured (not estimated) heights: anomaly rows with chips
                  // run taller than 37px, and estimate drift makes rows jump
                  // while scrolling. data-index + measureElement keep the
                  // virtualizer's offsets exact.
                  data-index={index}
                  ref={isVirtual ? virtualizer.measureElement : undefined}
                  aria-selected={clickable ? selected : undefined}
                  data-selected={selected || undefined}
                  onClick={
                    onRowClick
                      ? (event) => {
                          const target = event.target as HTMLElement | null;
                          if (target?.closest("a, button, input, select, textarea, label")) {
                            return;
                          }
                          onRowClick(row.original, event);
                        }
                      : undefined
                  }
                  onKeyDown={
                    onRowClick
                      ? (event: KeyboardEvent<HTMLTableRowElement>) => {
                          if (event.key !== "Enter" && event.key !== " ") return;
                          const target = event.target as HTMLElement | null;
                          if (target?.closest("a, button, input, select, textarea, label")) {
                            return;
                          }
                          event.preventDefault();
                          onRowClick(row.original, {
                            metaKey: false,
                            ctrlKey: false,
                            shiftKey: false,
                          });
                        }
                      : undefined
                  }
                  tabIndex={clickable ? 0 : undefined}
                  className={`border-b border-border last:border-b-0 ${
                    clickable ? "cursor-pointer hover:bg-accent/5 focus-visible:bg-accent/10" : ""
                  } ${selected ? "bg-accent/15" : ""} ${rowClassName?.(row.original) ?? ""}`}
                >
                  {row.getVisibleCells().map((cell) => (
                    <td
                      key={cell.id}
                      className={`px-3 py-2 ${alignTop ? "align-top" : ""} ${
                        scrollX ? "whitespace-nowrap" : ""
                      } ${cellClassName?.(row.original, cell.column.id) ?? ""}`}
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
      <TablePagination
        pageIndex={safePageIndex}
        pageCount={pageCount}
        totalRows={rows.length}
        pageSize={pageSize}
        onPageChange={setPageIndex}
      />
    </div>
  );
}
