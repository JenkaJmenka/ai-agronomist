"use client";

import type { ReactNode } from "react";

import { DateRangeControls } from "@/components/ui/DateRangeControls";
import { LiveFreshnessBadge } from "@/features/monitoring/components/LiveFreshnessBadge";
import { LiveToggle } from "@/features/monitoring/components/LiveToggle";
import { SetpointsPanel } from "@/features/monitoring/components/SetpointsPanel";
import type { SetpointRange, Setpoints } from "@/features/monitoring/hooks/useSetpoints";

interface SharedViewControlsProps {
  /**
   * Page identifier (e.g. `"monitoring"`, `"anomalies"`, `"thresholds"`).
   * Threaded to LiveToggle and DateRangeControls so they can scope chart
   * baselines by page + tenant + greenhouse.
   */
  readonly pageId: string;
  /** Channels for the setpoints editor (empty hides editable rows). */
  readonly channels?: readonly string[];
  readonly setpoints: Setpoints;
  readonly onUpdateSetpoint: (channel: string, range: SetpointRange) => void;
  readonly onResetSetpoints: () => void;
  /** Extra controls after Live (optional). */
  readonly trailing?: ReactNode;
}

/**
 * Shared Setpoints + Live + Period + Refresh toolbar.
 * URL owns live/from/to/refresh; setpoints stay in the parent hook
 * (localStorage) so chart tiles and the editor stay in sync.
 */
export function SharedViewControls({
  pageId,
  channels = [],
  setpoints,
  onUpdateSetpoint,
  onResetSetpoints,
  trailing,
}: SharedViewControlsProps) {
  return (
    <div className="flex flex-wrap items-end gap-2" data-testid="shared-view-controls">
      <SetpointsPanel
        channels={[...channels]}
        setpoints={setpoints}
        onUpdate={onUpdateSetpoint}
        onReset={onResetSetpoints}
      />
      <LiveToggle pageId={pageId} />
      <LiveFreshnessBadge />
      {trailing}
      <DateRangeControls pageId={pageId} />
    </div>
  );
}
