/**
 * Guard for the design-token contract in `app/globals.css`
 * (MIGRATION_PLAN §7.1, Grovsen rebrand).
 *
 * Every value is parsed from disk, never snapshotted: changing a token means
 * editing `globals.css` and nothing else. What is asserted are the invariants
 * reading one block cannot show you — that the two dark blocks stay in step,
 * that every ink clears WCAG AA on each surface it is painted on, that a
 * meaningful boundary clears 3:1, and that no two severity colours collapse
 * into the same hue.
 *
 * How the *rest* of the tree consumes these tokens is
 * `test/tokenAdoption.test.ts`. Parsing and the colour maths are in
 * `test/support/designTokens.ts`.
 */

import { describe, expect, it } from "vitest";

import {
  contrastRatio,
  customPropertiesOf,
  hueAndChromaOf,
  hueSeparation,
  isDirectHex,
  propertiesOf,
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
// Component boundaries — a border is a graphic, not text
// ---------------------------------------------------------------------------

describe("component boundary contrast", () => {
  // Only --border-strong. --border is a decorative hairline between rows and
  // measures ~1.2:1 by design; WCAG 1.4.11 applies to boundaries that *carry*
  // meaning — an input outline, a focused control — which is what
  // --border-strong is for. Do not add --border here and then lower the gate.
  const pairs = THEMES.flatMap((theme) =>
    SURFACE_PROPERTIES.map((surface) => ({ theme: theme.name, tokens: theme.tokens, surface })),
  );

  it.each(pairs)("shows --border-strong on $surface in $theme", ({ tokens, surface }) => {
    const borderHex = resolveColor(tokens, "--border-strong") as string;
    const surfaceHex = resolveColor(tokens, surface) as string;
    const ratio = contrastRatio(borderHex, surfaceHex);
    expect(ratio, `${borderHex} on ${surfaceHex} is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(
      AA_LARGE_GRAPHIC_RATIO,
    );
  });
});

// ---------------------------------------------------------------------------
// Severity palette — hue separation
// ---------------------------------------------------------------------------

/**
 * Minimum angular gap between any two severity hues.
 *
 * Read off the palette rather than taken from a standard. The tightest real
 * pairs are 38.2° (light --sev-warning vs --sev-critical) and 37.5° (dark
 * --sev-info vs --sev-urgent), while the defect this exists to catch — dark
 * --sev-lstm shipping 8.9° from --sev-warning, which no gate noticed until a
 * human looked — sat far below either. 30° leaves ~20% headroom over the real
 * floor and still flags that class with 3x margin. Note a threshold of 40°
 * would fail the palette as it stands, so this is not free to raise.
 */
const MIN_SEVERITY_HUE_SEPARATION = 30;

/** Under this saturation a colour is grey and its hue is arbitrary. */
const MIN_CHROMATIC_SATURATION = 0.3;

for (const theme of [
  { name: "light", declarations: LIGHT },
  { name: "dark", declarations: DARK },
] as const) {
  describe(`${theme.name} severity palette`, () => {
    // Aliases are the same colour deliberately (--sev-normal is var(--sev-ok))
    // and would register as a 0° collision, so only tokens declared as their
    // own hex are compared.
    const severities = theme.declarations.filter(
      ([property, value]) => property.startsWith("--sev-") && isDirectHex(value),
    );

    it("has more than one distinct severity colour to compare", () => {
      // Guards the filter above: if aliasing changed and this collapsed to one
      // entry, the pairwise assertion below would pass with nothing to check.
      expect(severities.length).toBeGreaterThan(1);
    });

    it("keeps every severity colour chromatic enough for hue to mean anything", () => {
      for (const [property, value] of severities) {
        const { saturation } = hueAndChromaOf(value);
        expect(saturation, `${property} ${value} sat=${saturation.toFixed(2)}`).toBeGreaterThan(
          MIN_CHROMATIC_SATURATION,
        );
      }
    });

    it(`separates every pair of severity hues by ${MIN_SEVERITY_HUE_SEPARATION} degrees`, () => {
      const collisions = severities
        .flatMap(([firstName, firstHex], index) =>
          severities.slice(index + 1).map(([secondName, secondHex]) => ({
            label: `${firstName} vs ${secondName}`,
            separation: hueSeparation(hueAndChromaOf(firstHex).hue, hueAndChromaOf(secondHex).hue),
          })),
        )
        .filter((pair) => pair.separation < MIN_SEVERITY_HUE_SEPARATION)
        .map((pair) => `${pair.label}: ${pair.separation.toFixed(1)} degrees`);

      expect(collisions).toEqual([]);
    });
  });
}
