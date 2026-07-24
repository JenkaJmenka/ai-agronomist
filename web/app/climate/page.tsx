import { MonitoringView } from "@/features/monitoring/MonitoringView";

export default function ClimatePage() {
  return <MonitoringView title="Climate Data" channelFilter={["temp", "humid"]} pageId="climate" />;
}
