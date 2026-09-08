"use client";

import { DateRangeControls } from "@/components/ui/DateRangeControls";
import { LiveToggle } from "@/features/monitoring/components/LiveToggle";

interface HomeControlsProps {
  readonly pageId: string;
}

/** Slim Home toolbar — period/live/refresh only (no setpoints editor). */
export function HomeControls({ pageId }: HomeControlsProps) {
  return (
    <div className="flex flex-wrap items-end gap-2" data-testid="home-controls">
      <LiveToggle pageId={pageId} />
      <DateRangeControls pageId={pageId} />
    </div>
  );
}
