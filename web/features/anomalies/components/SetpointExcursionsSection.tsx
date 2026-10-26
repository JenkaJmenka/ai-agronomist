import type { SetpointExcursionSpan } from "@/features/anomalies/lib/setpointExcursions";
import { getChannelLabel } from "@/features/monitoring/lib/channelMeta";

interface SetpointExcursionsSectionProps {
  readonly spans: readonly SetpointExcursionSpan[];
}

function formatTs(ts: string): string {
  return ts.replace("T", " ").slice(0, 16);
}

function formatWorstValue(span: SetpointExcursionSpan): string {
  const dist = span.distanceFromRange.toFixed(1);
  const dir = span.direction === "below" ? "below" : "above";
  return `${span.worstValue} — ${dist} ${dir} setpoint ${span.rangeMin}–${span.rangeMax}`;
}

/**
 * Lists setpoint excursions the detector did NOT flag, grouped by channel.
 *
 * This section is the complement of the anomaly list: it shows what conventional
 * threshold alarms would have caught, alongside the note "likely the same event
 * as the X anomaly" when a coupled channel (RELATED_CHANNELS) had detector
 * anomalies in the same window.
 */
export function SetpointExcursionsSection({ spans }: SetpointExcursionsSectionProps) {
  return (
    <section data-testid="setpoint-excursions-section">
      <h2 className="mb-3 text-base font-semibold">
        Out of setpoint range — not flagged by the detector
      </h2>

      {spans.length === 0 ? (
        <p
          data-testid="setpoint-excursions-empty"
          className="py-6 text-center text-sm text-text-muted"
        >
          No setpoint excursions in this window.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="setpoint-excursions-table">
            <thead>
              <tr className="border-b border-border text-left text-xs text-text-muted">
                <th className="pb-2 pr-4 font-medium">Channel</th>
                <th className="pb-2 pr-4 font-medium">Start</th>
                <th className="pb-2 pr-4 font-medium">End</th>
                <th className="pb-2 pr-4 font-medium">Worst value</th>
                <th className="pb-2 font-medium">Note</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {spans.map((span, i) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: spans are ordered, deterministic
                <tr key={`${span.channel}:${span.startTs}:${i}`} data-testid="excursion-row">
                  <td className="py-2 pr-4 font-medium text-text">
                    {getChannelLabel(span.channel)}
                  </td>
                  <td className="py-2 pr-4 tabular-nums text-text-muted">
                    {formatTs(span.startTs)}
                  </td>
                  <td className="py-2 pr-4 tabular-nums text-text-muted">{formatTs(span.endTs)}</td>
                  <td className="py-2 pr-4 tabular-nums text-sev-warning">
                    {formatWorstValue(span)}
                  </td>
                  <td className="py-2 text-xs text-text-muted">
                    {span.relatedAnomalyChannel ? (
                      <span data-testid="excursion-related-note">
                        likely the same event as the{" "}
                        <span className="font-medium text-text">
                          {getChannelLabel(span.relatedAnomalyChannel)}
                        </span>{" "}
                        anomaly
                      </span>
                    ) : (
                      <span className="text-text-muted/60">setpoint excursion only</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
