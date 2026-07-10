import { Card } from "@/components/ui/Card";

export default function ThresholdsPage() {
  return (
    <div className="mx-auto max-w-6xl">
      <h1 className="mb-6 text-2xl font-bold">Thresholds</h1>
      <Card>
        <p className="text-sm text-text-muted">
          Thresholds migrates in a later phase — see MIGRATION_PLAN.md §8.
        </p>
      </Card>
    </div>
  );
}
