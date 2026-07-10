"use client";

import { type ColumnDef, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";

interface DataTableProps<T> {
  readonly columns: ColumnDef<T, unknown>[];
  readonly rows: T[];
  readonly emptyMessage?: string;
}

/** Sticky-header table shell (TanStack Table). Virtualization lands in P1. */
export function DataTable<T>({
  columns,
  rows,
  emptyMessage = "No records in the selected filters.",
}: DataTableProps<T>) {
  const table = useReactTable({
    data: rows,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  if (rows.length === 0) {
    return <p className="py-8 text-center text-sm text-text-muted">{emptyMessage}</p>;
  }

  return (
    <div className="overflow-x-auto rounded-[var(--radius-card)] border border-border">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-bg-elevated">
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id} className="border-b border-border text-left">
              {hg.headers.map((h) => (
                <th key={h.id} className="px-3 py-2 font-semibold text-text-muted">
                  {h.isPlaceholder ? null : flexRender(h.column.columnDef.header, h.getContext())}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            <tr key={row.id} className="border-b border-border last:border-b-0">
              {row.getVisibleCells().map((cell) => (
                <td key={cell.id} className="px-3 py-2">
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
