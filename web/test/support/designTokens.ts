/**
 * Reading the design-token layer from disk, for `test/themeTokens.test.ts`.
 *
 * A stylesheet parser and a WCAG contrast implementation are both too large to
 * sit inside the test file and keep it readable, and neither belongs in `lib/`
 * — nothing shipped to the browser reads `globals.css` as text. The split is
 * mechanism here, assertions there.
 */

import { readFileSync } from "node:fs";
import path from "node:path";

export const GLOBALS_CSS = "app/globals.css";
export const CHART_THEME_TS = "components/charts/chartTheme.ts";
export const LOGO_TSX = "components/brand/GrovsenLogo.tsx";

/** Paths are relative to `web/`, which is where every test command runs. */
export function readWebFile(relativePath: string): string {
  return readFileSync(path.resolve(process.cwd(), relativePath), "utf-8");
}

/** Comments are stripped first, so a commented-out token never parses as live. */
const css = readWebFile(GLOBALS_CSS).replace(/\/\*[\s\S]*?\*\//g, "");

export type Declaration = readonly [property: string, value: string];

/**
 * The custom-property declarations of the block introduced by `selector`, in
 * source order. Brace-matched rather than regex-delimited so the nested
 * `@media (prefers-color-scheme: dark)` twin parses exactly like a top-level
 * block does. Throws rather than returning empty: a selector that no longer
 * matches has to fail loudly, not quietly assert nothing.
 */
export function customPropertiesOf(selector: string): Declaration[] {
  const selectorAt = css.indexOf(selector);
  if (selectorAt < 0) throw new Error(`${GLOBALS_CSS} has no ${selector} block`);

  const open = css.indexOf("{", selectorAt);
  let depth = 0;
  let close = -1;
  for (let index = open; index < css.length; index++) {
    if (css[index] === "{") depth++;
    else if (css[index] === "}" && --depth === 0) {
      close = index;
      break;
    }
  }
  if (close < 0) throw new Error(`${selector} block is unterminated`);

  return [...css.slice(open + 1, close).matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map(
    (match) => [match[1], match[2].trim()] as const,
  );
}

export const propertiesOf = (declarations: Declaration[]): string[] =>
  declarations.map(([property]) => property);

// ---------------------------------------------------------------------------
// WCAG 2.x relative luminance and contrast
// ---------------------------------------------------------------------------

const SRGB_LINEAR_THRESHOLD = 0.03928;
const RED_WEIGHT = 0.2126;
const GREEN_WEIGHT = 0.7152;
const BLUE_WEIGHT = 0.0722;
const CONTRAST_OFFSET = 0.05;

const HEX_COLOR = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i;

function channelToLinear(channel: number): number {
  return channel <= SRGB_LINEAR_THRESHOLD ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
}

function relativeLuminance(hex: string): number {
  const digits = hex.replace("#", "");
  const sixDigit = digits.length === 3 ? digits.replace(/./g, (char) => char + char) : digits;
  const [red, green, blue] = [0, 2, 4].map((offset) =>
    channelToLinear(Number.parseInt(sixDigit.slice(offset, offset + 2), 16) / 255),
  );
  return RED_WEIGHT * red + GREEN_WEIGHT * green + BLUE_WEIGHT * blue;
}

export function contrastRatio(foreground: string, background: string): number {
  const [lighter, darker] = [relativeLuminance(foreground), relativeLuminance(background)].sort(
    (a, b) => b - a,
  );
  return (lighter + CONTRAST_OFFSET) / (darker + CONTRAST_OFFSET);
}

/**
 * A token as a concrete hex, following one level of `var()` aliasing — enough
 * for `--sev-normal: var(--sev-ok)`, which would otherwise drop out of the
 * severity coverage entirely. `color-mix()` values (`--accent-soft`,
 * `--accent-flash`) have no colour a parser can reach and return null; the
 * caller asserts that nothing it means to check resolves to null.
 */
export function resolveColor(theme: Map<string, string>, property: string): string | null {
  const raw = theme.get(property);
  if (raw === undefined) return null;
  const alias = raw.match(/^var\((--[\w-]+)\)$/);
  const value = alias ? theme.get(alias[1]) : raw;
  return value !== undefined && HEX_COLOR.test(value) ? value : null;
}
