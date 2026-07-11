import { MonitoringView } from "@/features/monitoring/MonitoringView";

export default function GasPage() {
  return <MonitoringView title="Gas Monitoring" channelFilter={["co2", "gas"]} />;
}
