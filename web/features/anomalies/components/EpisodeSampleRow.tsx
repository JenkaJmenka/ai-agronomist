import type { AnomalyEpisodeSample } from "@/features/anomalies/api/episodes";
import { MonitoringJumpLink } from "@/features/anomalies/components/MonitoringJumpLink";
import {
  formatSensorList,
  formatSensorValues,
} from "@/features/anomalies/lib/formatAnomalyDetails";
import { toDisplayChannel } from "@/features/monitoring/api/readings";
import type { Setpoints } from "@/features/monitoring/hooks/useSetpoints";
import { formatSetpointContext } from "@/features/monitoring/lib/setpointContext";

/** One raw 5-minute sample inside an expanded episode. */
export function EpisodeSampleRow({
  sample,
  fallbackChannel,
  episodeSpan,
  setpoints,
}: {
  readonly sample: AnomalyEpisodeSample;
  readonly fallbackChannel: string;
  /** Frames the Monitoring jump on the whole episode instead of this sample. */
  readonly episodeSpan: { readonly start_ts: string; readonly end_ts: string };
  /** Effective setpoints — when present, shows an inline setpoint context line. */
  readonly setpoints?: Setpoints;
}) {
  const sensors = sample.affected_sensors.length > 0 ? sample.affected_sensors : [fallbackChannel];

  // Build a setpoint context line for the primary channel.
  // The channel may be a raw SCADA alias — map through the display alias first.
  const primarySensor = sensors[0] ?? fallbackChannel;
  const displayCh = toDisplayChannel(primarySensor);
  const rawValue = sample.values[displayCh] ?? sample.values[primarySensor];
  const sensorValue = typeof rawValue === "number" ? rawValue : null;
  const setpointLine = setpoints ? formatSetpointContext(displayCh, sensorValue, setpoints) : null;

  return (
    <li className="flex flex-col gap-1 px-4 py-2 text-sm" data-testid="episode-sample-row">
      <div className="flex flex-wrap items-center gap-3">
        <span className="tabular-nums">{sample.timestamp.replace("T", " ").slice(0, 19)}</span>
        {sample.anomaly_score !== null && sample.anomaly_score !== undefined ? (
          <span className="tabular-nums text-text-muted">{sample.anomaly_score.toFixed(3)}</span>
        ) : null}
        <span className="ml-auto">
          <MonitoringJumpLink
            timestamp={sample.timestamp}
            channels={sensors}
            episode={episodeSpan}
          />
        </span>
      </div>
      <div className="grid gap-0.5 text-xs text-text-muted sm:grid-cols-[7rem_1fr]">
        <span className="font-medium text-text">Sensors</span>
        <span>{formatSensorList(sensors)}</span>
        <span className="font-medium text-text">Values</span>
        <span className="tabular-nums">{formatSensorValues(sample.values, sensors) || "—"}</span>
        <span className="font-medium text-text">Reasoning</span>
        <span>{sample.reasoning?.trim() || "—"}</span>
        {setpointLine ? (
          <>
            <span className="font-medium text-text">Setpoint</span>
            <span data-testid="sample-setpoint-context" className="tabular-nums">
              {setpointLine}
            </span>
          </>
        ) : null}
      </div>
    </li>
  );
}
