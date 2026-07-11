"use client";

interface ThresholdControlProps {
  readonly threshold: number;
  readonly onChange: (value: number) => void;
}

const MIN = 0.0;
const MAX = 0.5;
const STEP = 0.005;

/** Threshold slider — URL-backed via useAnomalies; flagging is client-side
 *  so dragging re-filters instantly without a refetch. */
export function ThresholdControl({ threshold, onChange }: ThresholdControlProps) {
  return (
    <label className="flex items-center gap-3 text-sm" data-testid="threshold-control">
      <span className="text-text-muted">Threshold</span>
      <input
        type="range"
        min={MIN}
        max={MAX}
        step={STEP}
        value={threshold}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-48 accent-[var(--accent)]"
      />
      <span className="w-14 font-medium tabular-nums">{threshold.toFixed(3)}</span>
    </label>
  );
}
