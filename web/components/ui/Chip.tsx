import type { ReactNode } from "react";

import type { Severity } from "@/lib/severity";

interface ChipProps {
  readonly severity?: Severity;
  readonly children: ReactNode;
}

const SEVERITY_CLASSES: Record<Severity, string> = {
  normal: "bg-sev-normal/15 text-sev-normal",
  warning: "bg-sev-warning/15 text-sev-warning",
  lstm: "bg-sev-lstm/15 text-sev-lstm",
  critical: "bg-sev-critical/15 text-sev-critical",
};

/** Severity-coloured status chip; colors are the §7.2 single source of truth. */
export function Chip({ severity = "normal", children }: ChipProps) {
  return (
    <span
      data-severity={severity}
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${SEVERITY_CLASSES[severity]}`}
    >
      {children}
    </span>
  );
}
