export interface LegendItem {
  readonly label: string;
  readonly color: string;
  readonly dash?: readonly number[];
}

/** Line-style legend: dash pattern distinguishes lines so colour is never the only cue. */
export function DriftChartLegend({ items }: { readonly items: readonly LegendItem[] }) {
  return (
    <ul
      className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-muted"
      data-testid="chart-legend"
    >
      {items.map((item) => (
        <li key={item.label} className="flex items-center gap-1.5">
          <svg width="28" height="8" aria-hidden="true">
            <line
              x1="0"
              y1="4"
              x2="28"
              y2="4"
              stroke={item.color}
              strokeWidth="2"
              strokeDasharray={item.dash?.join(" ")}
            />
          </svg>
          {item.label}
        </li>
      ))}
    </ul>
  );
}
