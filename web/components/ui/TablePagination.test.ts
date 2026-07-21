import { describe, expect, it } from "vitest";

import { TABLE_PAGE_SIZE, paginateItems } from "@/components/ui/TablePagination";

describe("paginateItems", () => {
  it("uses 24 as the default page size", () => {
    expect(TABLE_PAGE_SIZE).toBe(24);
  });

  it("returns the first page of items", () => {
    const items = Array.from({ length: 50 }, (_, i) => i);
    const { pageItems, pageIndex, pageCount } = paginateItems(items, 0);
    expect(pageItems).toEqual(items.slice(0, 24));
    expect(pageIndex).toBe(0);
    expect(pageCount).toBe(3);
  });

  it("clamps an out-of-range page index", () => {
    const items = Array.from({ length: 30 }, (_, i) => i);
    const { pageItems, pageIndex, pageCount } = paginateItems(items, 99);
    expect(pageIndex).toBe(1);
    expect(pageCount).toBe(2);
    expect(pageItems).toEqual(items.slice(24));
  });

  it("returns a single empty page for an empty list", () => {
    const { pageItems, pageIndex, pageCount } = paginateItems([], 3);
    expect(pageItems).toEqual([]);
    expect(pageIndex).toBe(0);
    expect(pageCount).toBe(1);
  });
});
