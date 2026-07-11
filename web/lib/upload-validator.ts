/**
 * Client-side CSV pre-validation before upload (plan §6.2) — TS/zod port of
 * src/dashboard/services/upload_validator.py. Same contract: size and
 * structure errors block the upload; recoverable row inconsistencies pass
 * with a warning (the server skips bad rows the same way).
 */

import { z } from "zod";

export const MAX_FILE_SIZE_MB = 50;
const MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024;
const STRICT_SAMPLE_ROWS = 2000;
/** Bytes of the file head parsed for structural checks. */
const SAMPLE_BYTES = 1024 * 1024;

const CANDIDATE_DELIMITERS = [",", ";", "\t"] as const;
export type CsvDelimiter = (typeof CANDIDATE_DELIMITERS)[number];

export const validationResultSchema = z.object({
  isValid: z.boolean(),
  errors: z.array(z.string()),
  warnings: z.array(z.string()),
});

export type ValidationResult = z.infer<typeof validationResultSchema>;

function invalid(...errors: string[]): ValidationResult {
  return { isValid: false, errors, warnings: [] };
}

/** Pick the delimiter that splits the header into the most fields. */
export function detectDelimiter(headerLine: string): CsvDelimiter {
  let best: CsvDelimiter = ",";
  let bestCount = 0;
  for (const delimiter of CANDIDATE_DELIMITERS) {
    const count = splitCsvLine(headerLine, delimiter).length;
    if (count > bestCount) {
      best = delimiter;
      bestCount = count;
    }
  }
  return best;
}

/** Quote-aware single-line CSV field split (RFC-4180 double quotes). */
export function splitCsvLine(line: string, delimiter: CsvDelimiter): string[] {
  const fields: string[] = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];
    if (char === '"') {
      if (inQuotes && line[i + 1] === '"') {
        current += '"';
        i += 1;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (char === delimiter && !inQuotes) {
      fields.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  fields.push(current);
  return fields;
}

function structureWarning(badRowNumbers: number[]): string {
  const around = badRowNumbers[0];
  return (
    "Some rows have inconsistent column counts and will be skipped during " +
    `processing. CSV format error (around line ${around}): this usually means ` +
    "unquoted commas in a text field, mixed comma/semicolon delimiters, or " +
    "extra columns on some rows. Re-export as UTF-8 CSV with comma separators " +
    "and quote text fields, or fix the row and try again."
  );
}

/**
 * Structural checks over the file head (pure — unit-testable without File).
 * Mirrors the pandas strict-then-permissive parse: consistent rows pass,
 * inconsistent-but-parseable rows warn, and a data-less file errors.
 */
export function validateCsvSample(sampleText: string): ValidationResult {
  const lines = sampleText.split(/\r\n|\r|\n/).filter((line) => line.trim().length > 0);
  if (lines.length === 0) {
    return invalid("The file is empty.");
  }
  const header = lines[0];
  if (lines.length === 1) {
    return invalid("The CSV has no data rows (only a header or is completely empty).");
  }

  const delimiter = detectDelimiter(header);
  const expectedColumns = splitCsvLine(header, delimiter).length;
  const badRowNumbers: number[] = [];
  const dataLines = lines.slice(1, 1 + STRICT_SAMPLE_ROWS);
  for (const [index, line] of dataLines.entries()) {
    if (splitCsvLine(line, delimiter).length !== expectedColumns) {
      badRowNumbers.push(index + 2); // 1-based, counting the header line
    }
  }

  if (badRowNumbers.length === dataLines.length) {
    return invalid(
      "CSV format error: no sample row matches the header column count. " +
        "Re-export as UTF-8 CSV with comma separators and quote text fields.",
    );
  }
  return {
    isValid: true,
    errors: [],
    warnings: badRowNumbers.length > 0 ? [structureWarning(badRowNumbers)] : [],
  };
}

/** Full pre-upload validation for a browser File (size + structure). */
export async function validateCsvFile(file: File): Promise<ValidationResult> {
  if (file.size === 0) {
    return invalid("The file is empty.");
  }
  if (file.size > MAX_FILE_SIZE_BYTES) {
    const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
    return invalid(
      `File is too large (${sizeMb} MB). Maximum allowed size is ${MAX_FILE_SIZE_MB} MB.`,
    );
  }
  const sampleText = await file.slice(0, SAMPLE_BYTES).text();
  return validateCsvSample(sampleText);
}
