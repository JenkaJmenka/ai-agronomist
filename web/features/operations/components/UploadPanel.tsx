"use client";

import Link from "next/link";
import { type ChangeEvent, useState } from "react";

import { Button } from "@/components/ui/Button";
import { type UploadMode, useCsvUpload } from "@/features/operations/hooks/useCsvUpload";

const MODE_LABELS: Readonly<Record<UploadMode, string>> = {
  analyze: "Find anomalies (no persistence)",
  persist: "Ingest data",
  persist_train: "Ingest data + train model",
};

interface UploadPanelProps {
  readonly tenant: string;
  readonly gh: string;
}

/**
 * CSV upload with client-side pre-validation and a two-step confirm: the
 * first click validates and shows a summary; only the explicit confirm
 * click POSTs (plan §8 P4 gate). Analyze-only runs sync inference;
 * persist/train queues a worker job surfaced on /jobs.
 */
export function UploadPanel({ tenant, gh }: UploadPanelProps) {
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<UploadMode>("analyze");
  const [isConfirming, setIsConfirming] = useState(false);
  const {
    validation,
    isValidating,
    validateFile,
    resetValidation,
    upload,
    isUploading,
    outcome,
    uploadError,
    resetUpload,
  } = useCsvUpload();

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const selected = event.target.files?.[0] ?? null;
    setFile(selected);
    setIsConfirming(false);
    resetValidation();
    resetUpload();
    if (selected) void validateFile(selected);
  };

  const handleUploadClick = () => setIsConfirming(true);

  const handleConfirm = () => {
    if (!file) return;
    setIsConfirming(false);
    upload({ tenant, gh, file, mode });
  };

  const canUpload = Boolean(file) && validation?.isValid === true && !isUploading;

  return (
    <div className="space-y-4">
      <label className="block text-sm">
        <span className="font-medium">CSV file</span>
        <input
          type="file"
          accept=".csv,text/csv"
          data-testid="upload-file-input"
          onChange={handleFileChange}
          className="mt-1 block w-full text-sm text-text-muted file:mr-3 file:rounded-[var(--radius-control)] file:border file:border-border file:bg-bg-elevated file:px-3 file:py-1.5 file:text-sm file:text-text"
        />
      </label>

      <fieldset className="space-y-1">
        <legend className="text-sm font-medium">After upload</legend>
        {(Object.keys(MODE_LABELS) as UploadMode[]).map((value) => (
          <label key={value} className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name="upload-mode"
              value={value}
              checked={mode === value}
              onChange={() => setMode(value)}
            />
            {MODE_LABELS[value]}
          </label>
        ))}
      </fieldset>

      {isValidating ? <p className="text-sm text-text-muted">Validating…</p> : null}
      {validation?.errors.map((message) => (
        <p key={message} role="alert" className="text-sm text-sev-critical">
          {message}
        </p>
      ))}
      {validation?.warnings.map((message) => (
        <p key={message} role="status" className="text-sm text-sev-warning">
          {message}
        </p>
      ))}

      {isConfirming && file ? (
        <div
          role="alertdialog"
          aria-label="Confirm upload"
          className="space-y-3 rounded-[var(--radius-control)] border border-accent/40 bg-accent/5 p-3"
        >
          <p className="text-sm">
            Upload <span className="font-semibold">{file.name}</span> (
            {(file.size / 1024).toFixed(1)} kB) for{" "}
            <span className="font-semibold tabular-nums">
              {tenant} / {gh}
            </span>{" "}
            — {MODE_LABELS[mode]}?
          </p>
          <div className="flex gap-2">
            <Button onClick={handleConfirm} data-testid="upload-confirm">
              Confirm upload
            </Button>
            <Button
              variant="secondary"
              onClick={() => setIsConfirming(false)}
              data-testid="upload-cancel"
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <Button onClick={handleUploadClick} disabled={!canUpload} data-testid="upload-submit">
          {isUploading ? "Uploading…" : "Upload"}
        </Button>
      )}

      {uploadError ? (
        <p role="alert" className="text-sm text-sev-critical">
          Upload failed: {uploadError.message}
        </p>
      ) : null}

      {outcome?.kind === "sync" ? (
        <div role="status" className="space-y-1 text-sm">
          <p className="font-medium text-sev-normal">Analysis complete.</p>
          <p className="tabular-nums text-text-muted">
            {outcome.result.sequences_processed ?? 0} sequences processed ·{" "}
            {outcome.result.anomalies_detected ?? 0} anomalies detected
          </p>
          <Link href="/anomalies" className="text-accent underline">
            Review on the Anomaly Detection page →
          </Link>
        </div>
      ) : null}
      {outcome?.kind === "job" ? (
        <div role="status" className="space-y-1 text-sm">
          <p className="font-medium text-sev-normal">
            Upload job accepted: <span className="font-mono">{outcome.accepted.job_id}</span>
          </p>
          <Link href="/jobs" className="text-accent underline">
            Follow live progress on Job Monitoring →
          </Link>
        </div>
      ) : null}
    </div>
  );
}
