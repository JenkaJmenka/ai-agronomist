/**
 * Guard that the rest of the tree actually goes through the token layer.
 *
 * `app/globals.css` can be perfect and the UI still wrong: a component that
 * hard-codes `#ef4444`, or names a token that does not exist, bypasses the
 * whole theme and only shows up when someone flips to dark and looks. This
 * walks the trees from disk and asserts that nothing does — plus that the
 * canvas fallback palette still mirrors the light block, and that the brand
 * assets are present.
 *
 * The stylesheet's own invariants are `test/themeTokens.test.ts`.
 */

import { readdirSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import {
  CHART_THEME_TS,
  customPropertiesOf,
  GLOBALS_CSS,
  LOGO_TSX,
  readWebFile,
  resolveColor,
} from "./support/designTokens";

/** Needed by the fallback-mirror assertion: the light block's own values. */
const LIGHT_THEME = new Map(customPropertiesOf('[data-theme="light"]'));

// ---------------------------------------------------------------------------
// Stragglers — raw colours that bypass the token layer
// ---------------------------------------------------------------------------

/**
 * `features/` is included deliberately and matters most: every literal the
 * Grovsen rebrand actually found lived there — `bg-green-400` in
 * `LiveToggle.tsx`, amber classes in `LiveAnomalyStrip.tsx`, `#ff8c00` and
 * `#ef4444` in `ScoreTimeline.tsx`, and seven of the nine `bg-surface` sites.
 * A guard that watched only `app/` and `components/` would have watched the
 * two trees that were already clean.
 */
const SCANNED_TREES = ["app", "components", "features"];
const SCANNED_EXTENSIONS = [".ts", ".tsx", ".css", ".svg"];

/**
 * Files allowed to hold raw colours, each for a reason that cannot be a token:
 * `globals.css` is the token layer itself; `icon.svg` is a static favicon with
 * no stylesheet to read from; `GrovsenLogo.tsx` paints a white sprout that is
 * correct in both themes; `chartTheme.ts` needs concrete colours for a canvas
 * during SSR — and rather than trust that last exemption, the mirror test
 * below asserts each of its literals still equals the light token it names.
 */
const RAW_COLOR_EXEMPTIONS = [GLOBALS_CSS, "app/icon.svg", LOGO_TSX, CHART_THEME_TS];

const TAILWIND_PALETTE =
  "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose";

const RAW_COLOR_PATTERNS: readonly { readonly label: string; readonly pattern: RegExp }[] = [
  { label: "hex literal", pattern: /#[0-9a-fA-F]{3,8}\b/g },
  { label: "rgb()/hsl() literal", pattern: /\b(?:rgba?|hsla?)\(/g },
  {
    label: "Tailwind palette class",
    pattern: new RegExp(
      `\\b(?:bg|text|border|fill|stroke|ring|from|via|to|divide|outline|shadow|decoration)-(?:${TAILWIND_PALETTE})-\\d{2,3}\\b`,
      "g",
    ),
  },
  {
    label: "undefined or pre-rebrand token class",
    pattern: /\b(?:text-white|bg-surface|bg-muted-foreground)\b/g,
  },
];

function scannedFiles(): string[] {
  return SCANNED_TREES.flatMap((tree) =>
    readdirSync(path.resolve(process.cwd(), tree), { recursive: true, encoding: "utf-8" })
      .map((entry) => path.posix.join(tree, entry.split(path.sep).join("/")))
      .filter((file) => SCANNED_EXTENSIONS.includes(path.extname(file)))
      // Tests set and assert concrete colours on purpose — that is how token
      // resolution gets exercised at all — so only shipped source is scanned.
      .filter((file) => !/\.test\.tsx?$/.test(file))
      .filter((file) => !RAW_COLOR_EXEMPTIONS.includes(file)),
  );
}

describe("no raw colours outside the token layer", () => {
  const files = scannedFiles();

  it("walks both trees to their leaves", () => {
    // A readdir that silently returned nothing, or only the top level, would
    // make every assertion below vacuous. Asserted as a shape — files exist,
    // and some of them are nested — rather than as a count, which would need
    // editing every time a component is added or removed.
    expect(files.length).toBeGreaterThan(0);
    expect(files.some((file) => file.split("/").length >= 3)).toBe(true);
  });

  it("exempts only files that are still there", () => {
    // A stale exemption is how a file quietly stops being scanned; the paths
    // are matched exactly, so a moved file must be re-justified, not inherited.
    for (const exemption of RAW_COLOR_EXEMPTIONS) {
      expect(statSync(path.resolve(process.cwd(), exemption)).size, exemption).toBeGreaterThan(0);
      expect(files, exemption).not.toContain(exemption);
    }
  });

  it.each(RAW_COLOR_PATTERNS)("finds no $label", ({ pattern }) => {
    // matchAll, not match: every offender in a file is reported in one run, so
    // a file with three of them does not take three iterations to clean up.
    // The patterns carry /g for that reason — matchAll throws without it.
    const offenders = files.flatMap((file) =>
      [...readWebFile(file).matchAll(pattern)].map((hit) => `${file}: ${hit[0]}`),
    );
    expect(offenders).toEqual([]);
  });
});

describe("canvas fallback palette", () => {
  it("mirrors the light value of every token its comments name", () => {
    // This pair drifted once already during the rebrand: light --text-muted
    // moved and LIGHT_FALLBACK.text kept the old value, which only jsdom and
    // SSR could see. Each literal is annotated with the token it mirrors, so
    // the annotation is what gets checked — including that every literal in
    // the block carries one.
    const source = readWebFile(CHART_THEME_TS);
    const start = source.indexOf("const LIGHT_FALLBACK");
    const end = source.indexOf("} as const;", start);
    expect(start, `${CHART_THEME_TS} has no LIGHT_FALLBACK block`).toBeGreaterThanOrEqual(0);
    expect(end, "LIGHT_FALLBACK block is unterminated").toBeGreaterThan(start);

    const block = source.slice(start, end);
    const annotated = [...block.matchAll(/(\w+):\s*"(#[0-9a-fA-F]{3,8})",\s*\/\/\s*(--[\w-]+)/g)];
    expect(annotated.length, "no annotated fallbacks parsed").toBeGreaterThan(0);
    expect(annotated.length, "a fallback literal names no token").toBe(
      (block.match(/#[0-9a-fA-F]{3,8}\b/g) ?? []).length,
    );

    for (const [, key, literal, token] of annotated) {
      expect(literal.toLowerCase(), `${key} mirrors ${token}`).toBe(
        resolveColor(LIGHT_THEME, token)?.toLowerCase(),
      );
    }
  });
});

describe("brand assets", () => {
  it.each(["app/icon.svg", "app/favicon.ico", "app/apple-icon.png"])("ships %s", (asset) => {
    expect(statSync(path.resolve(process.cwd(), asset)).size).toBeGreaterThan(0);
  });

  it("gives the inline logo an accessible name", () => {
    // biome's a11y/noSvgWithoutTitle is switched off repo-wide, so the <title>
    // on the inline brand mark has no linter guarding it.
    expect(readWebFile(LOGO_TSX)).toContain("<title>");
  });
});
