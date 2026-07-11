"use client";

import { useMutation } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import {
  type AsyncUploadAccepted,
  type SyncUploadResult,
  type UploadOptions,
  uploadCsvAsync,
  uploadCsvSync,
} from "@/features/operations/api/operations";
import { type ValidationResult, validateCsvFile } from "@/lib/upload-validator";

export type UploadMode = "analyze" | "persist" | "persist_train";

export type UploadOutcome =
  | { readonly kind: "sync"; readonly result: SyncUploadResult }
  | { readonly kind: "job"; readonly accepted: AsyncUploadAccepted };

interface UploadArgs {
  readonly tenant: string;
  readonly gh: string;
  readonly file: File;
  readonly mode: UploadMode;
}

export interface CsvUploadState {
  readonly validation: ValidationResult | null;
  readonly isValidating: boolean;
  readonly validateFile: (file: File) => Promise<ValidationResult>;
  readonly resetValidation: () => void;
  readonly upload: (args: UploadArgs) => void;
  readonly isUploading: boolean;
  readonly outcome: UploadOutcome | null;
  readonly uploadError: Error | null;
  readonly resetUpload: () => void;
}

function toOptions(args: UploadArgs): UploadOptions {
  return {
    tenant: args.tenant,
    gh: args.gh,
    file: args.file,
    persistData: args.mode !== "analyze",
    trainModel: args.mode === "persist_train",
  };
}

/**
 * CSV pre-validation + upload mutation. Analyze-only files run the sync
 * inference path; persist/train files queue an async worker job (plan §8 P4).
 */
export function useCsvUpload(): CsvUploadState {
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [isValidating, setIsValidating] = useState(false);

  const mutation = useMutation<UploadOutcome, Error, UploadArgs>({
    mutationFn: async (args) => {
      if (args.mode === "analyze") {
        return { kind: "sync", result: await uploadCsvSync(toOptions(args)) };
      }
      return { kind: "job", accepted: await uploadCsvAsync(toOptions(args)) };
    },
  });

  const validateFile = useCallback(async (file: File): Promise<ValidationResult> => {
    setIsValidating(true);
    try {
      const result = await validateCsvFile(file);
      setValidation(result);
      return result;
    } finally {
      setIsValidating(false);
    }
  }, []);

  return {
    validation,
    isValidating,
    validateFile,
    resetValidation: () => setValidation(null),
    upload: (args) => mutation.mutate(args),
    isUploading: mutation.isPending,
    outcome: mutation.data ?? null,
    uploadError: mutation.error,
    resetUpload: () => mutation.reset(),
  };
}
