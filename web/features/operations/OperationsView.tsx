"use client";

import { Suspense } from "react";

import { Card } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { ClearDataPanel } from "@/features/operations/components/ClearDataPanel";
import { InjectAnomaliesPanel } from "@/features/operations/components/InjectAnomaliesPanel";
import { ModelRegistryPanel } from "@/features/operations/components/ModelRegistryPanel";
import { UploadPanel } from "@/features/operations/components/UploadPanel";
import { useScope } from "@/lib/useScope";

function OperationsBody() {
  const { tenant, gh } = useScope();

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Upload & Operations</h1>
        <p className="text-sm text-text-muted tabular-nums">
          {tenant} · {gh}
        </p>
      </div>

      <Card title="Upload sensor data">
        <UploadPanel tenant={tenant} gh={gh} />
      </Card>

      <Card title="Model registry">
        <ModelRegistryPanel tenant={tenant} />
      </Card>

      <Card title="Inject test anomalies">
        <InjectAnomaliesPanel tenant={tenant} gh={gh} />
      </Card>

      <Card title="Manage stored data">
        <ClearDataPanel tenant={tenant} gh={gh} />
      </Card>
    </div>
  );
}

export function OperationsView() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-6xl space-y-4">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-64 w-full" />
        </div>
      }
    >
      <OperationsBody />
    </Suspense>
  );
}
