import { describe, expect, it } from "vitest";

import {
  detectDelimiter,
  MAX_FILE_SIZE_MB,
  splitCsvLine,
  validateCsvFile,
  validateCsvSample,
} from "@/lib/upload-validator";

const HEADER = "timestamp,temperature,humidity";

describe("splitCsvLine", () => {
  it("splits plain fields", () => {
    expect(splitCsvLine("a,b,c", ",")).toEqual(["a", "b", "c"]);
  });

  it("keeps quoted delimiters inside a field", () => {
    expect(splitCsvLine('a,"b,inner",c', ",")).toEqual(["a", "b,inner", "c"]);
  });

  it("unescapes doubled quotes", () => {
    expect(splitCsvLine('a,"say ""hi""",c', ",")).toEqual(["a", 'say "hi"', "c"]);
  });
});

describe("detectDelimiter", () => {
  it("detects commas", () => {
    expect(detectDelimiter(HEADER)).toBe(",");
  });

  it("detects semicolons", () => {
    expect(detectDelimiter("timestamp;temperature;humidity")).toBe(";");
  });

  it("detects tabs", () => {
    expect(detectDelimiter("timestamp\ttemperature\thumidity")).toBe("\t");
  });
});

describe("validateCsvSample", () => {
  it("accepts a consistent file", () => {
    const sample = `${HEADER}\n2026-06-01T00:00:00,21.5,60\n2026-06-01T01:00:00,21.7,61`;
    expect(validateCsvSample(sample)).toEqual({ isValid: true, errors: [], warnings: [] });
  });

  it("rejects an empty file", () => {
    const result = validateCsvSample("");
    expect(result.isValid).toBe(false);
    expect(result.errors[0]).toMatch(/empty/i);
  });

  it("rejects a header-only file", () => {
    const result = validateCsvSample(`${HEADER}\n`);
    expect(result.isValid).toBe(false);
    expect(result.errors[0]).toMatch(/no data rows/i);
  });

  it("warns on inconsistent rows but stays valid", () => {
    const sample = `${HEADER}\n2026-06-01T00:00:00,21.5,60\nbroken,row,with,extra,columns`;
    const result = validateCsvSample(sample);
    expect(result.isValid).toBe(true);
    expect(result.warnings[0]).toMatch(/inconsistent column counts/i);
    expect(result.warnings[0]).toMatch(/line 3/);
  });

  it("rejects when every sample row mismatches the header", () => {
    const sample = `${HEADER}\na,b\nc,d`;
    const result = validateCsvSample(sample);
    expect(result.isValid).toBe(false);
    expect(result.errors[0]).toMatch(/no sample row matches/i);
  });
});

describe("validateCsvFile", () => {
  it("rejects an empty File", async () => {
    const result = await validateCsvFile(new File([], "empty.csv"));
    expect(result.isValid).toBe(false);
    expect(result.errors[0]).toMatch(/empty/i);
  });

  it("rejects a file over the size cap", async () => {
    const oversized = new File([new Uint8Array((MAX_FILE_SIZE_MB + 1) * 1024 * 1024)], "big.csv");
    const result = await validateCsvFile(oversized);
    expect(result.isValid).toBe(false);
    expect(result.errors[0]).toMatch(/too large/i);
  });

  it("accepts a small well-formed file", async () => {
    const csv = `${HEADER}\n2026-06-01T00:00:00,21.5,60`;
    const result = await validateCsvFile(new File([csv], "ok.csv", { type: "text/csv" }));
    expect(result).toEqual({ isValid: true, errors: [], warnings: [] });
  });
});
