import { Suspense } from "react";

import { Card } from "@/components/ui/Card";
import { Chip } from "@/components/ui/Chip";
import { DateRangeControls } from "@/components/ui/DateRangeControls";
import { Skeleton } from "@/components/ui/Skeleton";
import { DemoChart } from "@/features/home/DemoChart";

export default function HomePage() {
  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold">AI Agronomist</h1>
        <p className="text-sm text-text-muted">
          Greenhouse monitoring &amp; anomaly detection — Next.js frontend (P0 scaffold).
        </p>
      </div>

      <Card title="Severity semantics">
        <div className="flex flex-wrap gap-2">
          <Chip severity="normal">Normal</Chip>
          <Chip severity="warning">Warning</Chip>
          <Chip severity="lstm">LSTM / drift</Chip>
          <Chip severity="critical">Critical</Chip>
        </div>
      </Card>

      <Card title="Window controls (URL-driven)">
        <Suspense fallback={<Skeleton className="h-8 w-full" />}>
          <DateRangeControls pageId="home" />
        </Suspense>
      </Card>

      <Card title="Chart pipeline (uPlot demo)">
        <DemoChart />
      </Card>
    </div>
  );
}
