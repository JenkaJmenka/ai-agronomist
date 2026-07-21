"use client";

import { Button } from "@/components/ui/Button";
import { exportCsv } from "@/lib/exportCsv";

interface ExportCsvButtonProps {
  readonly filename: string;
  readonly headers: readonly string[];
  readonly rows: readonly (readonly unknown[])[];
  readonly disabled?: boolean;
  readonly testId?: string;
  readonly label?: string;
}

/** Download the supplied table snapshot as CSV. */
export function ExportCsvButton({
  filename,
  headers,
  rows,
  disabled = false,
  testId = "export-csv",
  label = "Export CSV",
}: ExportCsvButtonProps) {
  return (
    <Button
      type="button"
      variant="secondary"
      size="sm"
      disabled={disabled || rows.length === 0}
      data-testid={testId}
      onClick={() => exportCsv({ filename, headers, rows })}
    >
      {label}
    </Button>
  );
}
