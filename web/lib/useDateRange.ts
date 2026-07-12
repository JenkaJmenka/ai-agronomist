"use client";

import { parseAsIsoDateTime, useQueryStates } from "nuqs";

export interface DateRangeValue {
  readonly from: Date | null;
  readonly to: Date | null;
}

export type SetDateRange = (range: DateRangeValue) => Promise<URLSearchParams>;

/**
 * URL-backed from/to window (plan §7.4) — one parser config shared by the
 * range controls and chart drag-selection so both write the same params.
 */
export function useDateRange(): [DateRangeValue, SetDateRange] {
  return useQueryStates({
    from: parseAsIsoDateTime,
    to: parseAsIsoDateTime,
  });
}
