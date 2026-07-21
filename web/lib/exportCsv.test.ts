import { describe, expect, it } from "vitest";

import { toCsv } from "@/lib/exportCsv";

describe("toCsv", () => {
  it("joins headers and rows with commas", () => {
    expect(
      toCsv(
        ["a", "b"],
        [
          [1, 2],
          ["x", "y"],
        ],
      ),
    ).toBe("a,b\r\n1,2\r\nx,y\r\n");
  });

  it("quotes cells that contain commas or quotes", () => {
    expect(toCsv(["note"], [['say "hi", please']])).toBe('note\r\n"say ""hi"", please"\r\n');
  });

  it("renders null/undefined as empty cells", () => {
    expect(toCsv(["a", "b"], [[null, undefined]])).toBe("a,b\r\n,\r\n");
  });
});
