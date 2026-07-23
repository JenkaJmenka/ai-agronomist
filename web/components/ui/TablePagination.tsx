"use client";

import { Button } from "@/components/ui/Button";

/** Default page size for all dashboard data tables. */
export const TABLE_PAGE_SIZE = 10;

interface TablePaginationProps {
  readonly pageIndex: number;
  readonly pageCount: number;
  readonly totalRows: number;
  readonly pageSize?: number;
  readonly onPageChange: (pageIndex: number) => void;
  readonly label?: string;
}

/**
 * Compact prev/next pager shown under tables. Hidden when everything fits
 * on a single page.
 */
export function TablePagination({
  pageIndex,
  pageCount,
  totalRows,
  pageSize = TABLE_PAGE_SIZE,
  onPageChange,
  label = "rows",
}: TablePaginationProps) {
  if (pageCount <= 1) return null;

  const from = pageIndex * pageSize + 1;
  const to = Math.min(totalRows, (pageIndex + 1) * pageSize);

  return (
    <div
      className="flex flex-wrap items-center justify-between gap-2 text-sm text-text-muted"
      data-testid="table-pagination"
    >
      <span className="tabular-nums">
        {from}–{to} of {totalRows.toLocaleString()} {label}
      </span>
      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="secondary"
          size="sm"
          disabled={pageIndex <= 0}
          onClick={() => onPageChange(pageIndex - 1)}
          aria-label="Previous page"
        >
          Previous
        </Button>
        <span className="tabular-nums" data-testid="table-pagination-page">
          Page {pageIndex + 1} of {pageCount}
        </span>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          disabled={pageIndex >= pageCount - 1}
          onClick={() => onPageChange(pageIndex + 1)}
          aria-label="Next page"
        >
          Next
        </Button>
      </div>
    </div>
  );
}

/** Slice `items` to the current page; clamps an out-of-range page index. */
export function paginateItems<T>(
  items: readonly T[],
  pageIndex: number,
  pageSize: number = TABLE_PAGE_SIZE,
): { pageItems: T[]; pageIndex: number; pageCount: number } {
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));
  const safeIndex = Math.min(Math.max(0, pageIndex), pageCount - 1);
  const start = safeIndex * pageSize;
  return {
    pageItems: items.slice(start, start + pageSize) as T[],
    pageIndex: safeIndex,
    pageCount,
  };
}
