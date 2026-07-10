import { Card } from "@/components/ui/Card";

export default function MonitoringPage() {
  return (
    <div className="mx-auto max-w-6xl">
      <h1 className="mb-6 text-2xl font-bold">Monitoring</h1>
      <Card>
        <p className="text-sm text-text-muted">
          Monitoring migrates in a later phase — see MIGRATION_PLAN.md §8.
        </p>
      </Card>
    </div>
  );
}
