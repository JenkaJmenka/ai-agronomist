/**
 * Guard for the design-token contract (MIGRATION_PLAN §7.1, Grovsen rebrand).
 *
 * Every value is parsed from disk, never snapshotted: changing a token means
 * editing `app/globals.css` and nothing else. What is asserted are the
 * invariants no single file shows you — that the two dark blocks stay in step,
 * that every ink clears WCAG AA on every surface it is painted on, that no raw
 * colour literal has crept back into `app/` or `components/`, and that the
 * canvas fallback palette still mirrors the light block.
 *
 * It lives in `test/` rather than beside a component because it has no single
 * subject: its inputs are a stylesheet, two directory trees and the brand
 * assets. Parsing and the contrast maths are in `test/support/designTokens.ts`.
 */

import { readdirSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import {
  CHART_THEME_TS,
  contrastRatio,
  customPropertiesOf,
  GLOBALS_CSS,
  LOGO_TSX,
  propertiesOf,
  readWebFile,
  resolveColor,
} from "./support/designTokens";

const LIGHT = customPropertiesOf('[data-theme="light"]');
const DARK = customPropertiesOf('[data-theme="dark"]');
const DARK_MEDIA_TWIN = customPropertiesOf(":root:not([data-theme])");

/**
 * Declared once, in `:root`, deliberately: radii are theme-invariant, so the
 * dark blocks must NOT repeat them. If the light-vs-dark assertion below ever
 * fails on these two, the fix is to leave `globals.css` alone — copying radii
 * into both dark blocks adds two more places to drift and changes nothing on
 * screen. Anything else missing from dark is a real gap.
 */
const THEME_INVARIANT_PROPERTIES = ["--radius-control", "--radius-card"];

describe("globals.css token blocks", () => {
  it("keeps the two dark blocks identical in property, value and order", () => {
    // Plain CSS cannot dedupe them — the media twin applies only when no
    // [data-theme] is set at all — so they drift silently. This is the
    // assertion that catches it.
    expect(DARK_MEDIA_TWIN).toEqual(DARK);
  });

  it("defines every dark token in the light block too", () => {
    const lightProperties = propertiesOf(LIGHT);
    expect(propertiesOf(DARK).filter((property) => !lightProperties.includes(property))).toEqual(
      [],
    );
  });

  it("defines every theme-varying light token in the dark blocks too", () => {
    const darkProperties = propertiesOf(DARK);
    const missing = propertiesOf(LIGHT).filter(
      (property) =>
        !THEME_INVARIANT_PROPERTIES.includes(property) && !darkProperties.includes(property),
    );
    expect(missing).toEqual([]);
  });

  it("declares each property at most once per block", () => {
    for (const [name, declarations] of [
      ["light", LIGHT],
      ["dark", DARK],
      ["dark media twin", DARK_MEDIA_TWIN],
    ] as const) {
      const properties = propertiesOf(declarations);
      expect(new Set(properties).size, `${name} block`).toBe(properties.length);
    }
  });
});

// ---------------------------------------------------------------------------
// Contrast
// ---------------------------------------------------------------------------

const AA_NORMAL_TEXT_RATIO = 4.5;
const AA_LARGE_GRAPHIC_RATIO = 3;
const SURFACE_PROPERTIES = ["--bg", "--bg-elevated", "--bg-sidebar"];

/** Dark is the cascade the browser resolves: the light block, then overrides. */
const LIGHT_THEME = new Map(LIGHT);
const DARK_THEME = new Map([...LIGHT, ...DARK]);

const THEMES = [
  { name: "light", tokens: LIGHT_THEME },
  { name: "dark", tokens: DARK_THEME },
] as const;

for (const theme of THEMES) {
  describe(`${theme.name} theme contrast`, () => {
    const inks = [
      "--text",
      "--text-muted",
      "--accent",
      ...propertiesOf(LIGHT).filter((property) => property.startsWith("--sev-")),
    ];
    const resolvableInks = inks.filter((ink) => resolveColor(theme.tokens, ink) !== null);

    it("resolves every ink under test to a concrete colour", () => {
      // An unresolvable value would silently vanish from the coverage below
      // rather than fail it, so the excluded set is asserted to be empty: a new
      // color-mix() ink fails here instead of going unchecked.
      expect(inks.filter((ink) => !resolvableInks.includes(ink))).toEqual([]);
    });

    const pairs = resolvableInks.flatMap((ink) =>
      SURFACE_PROPERTIES.map((surface) => ({ ink, surface })),
    );

    it.each(pairs)("reads $ink on $surface at AA", ({ ink, surface }) => {
      const inkHex = resolveColor(theme.tokens, ink) as string;
      const surfaceHex = resolveColor(theme.tokens, surface) as string;
      const ratio = contrastRatio(inkHex, surfaceHex);
      expect(ratio, `${inkHex} on ${surfaceHex} is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(
        AA_NORMAL_TEXT_RATIO,
      );
    });
  });
}

describe("filled-surface contrast", () => {
  /** Asserts a foreground/background token pair, naming the measured ratio. */
  function expectRatio(
    theme: Map<string, string>,
    pair: { readonly foreground: string; readonly background: string },
    minimum: number,
  ): void {
    const foregroundHex = resolveColor(theme, pair.foreground) as string;
    const backgroundHex = resolveColor(theme, pair.background) as string;
    const ratio = contrastRatio(foregroundHex, backgroundHex);
    const detail = `${pair.foreground} ${foregroundHex} on ${pair.background} ${backgroundHex} is ${ratio.toFixed(2)}:1`;
    expect(ratio, detail).toBeGreaterThanOrEqual(minimum);
  }

  it("reads --brand-fg on the light primary-button fill at AA", () => {
    expectRatio(
      LIGHT_THEME,
      { foreground: "--brand-fg", background: "--brand" },
      AA_NORMAL_TEXT_RATIO,
    );
  });

  it("reads --accent-fg on the dark primary-button fill at AA", () => {
    expectRatio(
      DARK_THEME,
      { foreground: "--accent-fg", background: "--accent" },
      AA_NORMAL_TEXT_RATIO,
    );
  });

  it("keeps the dark logo tile visible against the page", () => {
    // A graphic, not text, so 3:1. The flat brand green measures 1.42:1 here,
    // which is why --brand-tile swaps lighter in dark at all.
    expectRatio(
      DARK_THEME,
      { foreground: "--brand-tile", background: "--bg" },
      AA_LARGE_GRAPHIC_RATIO,
    );
  });
});

// ---------------------------------------------------------------------------
// Stragglers — raw colours that bypass the token layer
// ---------------------------------------------------------------------------

const SCANNED_TREES = ["app", "components"];
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
  { label: "hex literal", pattern: /#[0-9a-fA-F]{3,8}\b/ },
  { label: "rgb()/hsl() literal", pattern: /\b(?:rgba?|hsla?)\(/ },
  {
    label: "Tailwind palette class",
    pattern: new RegExp(
      `\\b(?:bg|text|border|fill|stroke|ring|from|via|to|divide|outline|shadow|decoration)-(?:${TAILWIND_PALETTE})-\\d{2,3}\\b`,
    ),
  },
  {
    label: "undefined or pre-rebrand token class",
    pattern: /\b(?:text-white|bg-surface|bg-muted-foreground)\b/,
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
    const offenders = files.flatMap((file) => {
      const found = readWebFile(file).match(pattern);
      return found === null ? [] : [`${file}: ${found[0]}`];
    });
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
