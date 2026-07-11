"use client";

import { Suspense } from "react";

import { Card } from "@/components/ui/Card";
import { Skeleton } from "@/components/ui/Skeleton";
import { JobCard } from "@/features/jobs/components/JobCard";
import { useJobs } from "@/features/jobs/hooks/useJobs";
import { useScope } from "@/lib/useScope";

function JobsBody() {
  const { tenant, gh } = useScope();
  const { jobs, isLoading, isError, error, terminate, isTerminating, terminateError } = useJobs(
    tenant,
    gh,
  );

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Job Monitoring</h1>
        <p className="text-sm text-text-muted tabular-nums">
          {tenant} · {gh} · active jobs stream live, list refreshes every 5 s
        </p>
      </div>

      {terminateError ? (
        <p role="alert" className="text-sm text-sev-critical">
          Failed to terminate job: {terminateError.message}
        </p>
      ) : null}

      {isLoading ? (
        <div className="space-y-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : isError ? (
        <Card>
          <p role="alert" className="py-4 text-center text-sm text-sev-critical">
            Could not load jobs: {error?.message}
          </p>
        </Card>
      ) : jobs.length === 0 ? (
        <Card>
          <p className="py-8 text-center text-sm text-text-muted">
            No recent jobs for this tenant and greenhouse. Uploads with persistence or training
            queue jobs that appear here.
          </p>
        </Card>
      ) : (
        <ul className="space-y-3">
          {jobs.map((job) => (
            <JobCard key={job.id} job={job} onTerminate={terminate} isTerminating={isTerminating} />
          ))}
        </ul>
      )}
    </div>
  );
}

export function JobsView() {
  return (
    <Suspense
      fallback={
        <div className="mx-auto max-w-6xl space-y-4">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-24 w-full" />
        </div>
      }
    >
      <JobsBody />
    </Suspense>
  );
}
