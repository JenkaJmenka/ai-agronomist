import { MonitoringView } from "@/features/monitoring/MonitoringView";

/** Dedicated gas-panel columns only — never climate ``co2``. */
const GAS_CHANNELS = ["c2h4", "gas_co2", "no", "no2", "nox", "co"] as const;

export default function GasPage() {
  return (
    <MonitoringView
      title="Gas Monitoring"
      channelFilter={GAS_CHANNELS}
      channelMatch="exact"
      pageId="gas"
    />
  );
}
