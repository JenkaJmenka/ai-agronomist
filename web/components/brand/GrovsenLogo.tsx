/**
 * Grovsen tile mark + wordmark, inlined as SVG so the brand fills can resolve
 * CSS tokens (`--brand-tile`, `--logo-wordmark`) per theme — an `<img>` or
 * `next/image` reference can't repaint when `[data-theme]` flips.
 *
 * Geometry is copied verbatim from `assets/brand/grovsen-icon.svg` and
 * `assets/brand/grovsen-lockup.svg`. The wordmark is 7 outlined glyph paths
 * (no `<text>`, no font dependency); their `d`/`transform` attributes were
 * extracted from the source SVG with a small regex script rather than typed
 * by hand, to rule out a mistyped coordinate silently deforming a glyph.
 */

type GrovsenLogoVariant = "full" | "icon";

interface GrovsenLogoProps {
  readonly variant?: GrovsenLogoVariant;
  readonly height: number;
  readonly "aria-hidden"?: boolean;
}

// Tight crop of grovsen-lockup.svg's own viewBox ("0 0 1006 290"). Bounds
// computed by walking every absolute path command (M/L/H/V/Q/Z, including
// implicit post-M LineTos) in the 7 wordmark glyphs below, applying each
// glyph's own translate(tx 0), then the wordmark group's
// translate(294 204) scale(0.16833 -0.16833); the tile's bound is
// translate(40 40) scale(0.41016) applied to its 512x512 rect. Result:
// tile x/y [40, 250.00192], wordmark x [299.55, 959.2098606], wordmark y
// [109.23, 249.95409] — so the union is x [40, 959.2098606], y [40, 250.00192],
// i.e. the source's 40px left/top padding with no further margin needed.
const FULL_VIEW_BOX_MIN = 40;
const FULL_VIEW_BOX_WIDTH = 919.21;
const FULL_VIEW_BOX_HEIGHT = 210;
const FULL_VIEW_BOX = `${FULL_VIEW_BOX_MIN} ${FULL_VIEW_BOX_MIN} ${FULL_VIEW_BOX_WIDTH} ${FULL_VIEW_BOX_HEIGHT}`;
const FULL_ASPECT_RATIO = FULL_VIEW_BOX_WIDTH / FULL_VIEW_BOX_HEIGHT;

const ICON_VIEW_BOX_SIZE = 512;
const ICON_VIEW_BOX = `0 0 ${ICON_VIEW_BOX_SIZE} ${ICON_VIEW_BOX_SIZE}`;
const ICON_ASPECT_RATIO = 1;

/** `width` follows from `height` so the two numbers can't drift apart. */
function computeWidth(height: number, aspectRatio: number): number {
  return Math.round(height * aspectRatio * 100) / 100;
}

/**
 * The rounded tile + white sprout glyph, on the tile's own 512x512 viewBox.
 * White is a literal here (correct in both themes); the tile fill is a token
 * — the flat brand green is 1.42:1 on the dark background and would
 * disappear without `--brand-tile` swapping lighter in dark mode.
 */
function SproutOnTile() {
  return (
    <>
      <rect width={512} height={512} rx={112} fill="var(--brand-tile)" />
      <g transform="translate(256 328) scale(4.4)">
        <circle cx={0} cy={0} r={5} fill="#FFFFFF" />
        <path d="M0 0 L0 -34" fill="none" stroke="#FFFFFF" strokeWidth={6} strokeLinecap="round" />
        <path d="M0 -14 C-11 -13 -19 -19 -24 -28 C-13 -30 -4 -25 0 -18 Z" fill="#FFFFFF" />
        <path d="M0 -20 C11 -19 19 -25 24 -34 C13 -36 4 -31 0 -24 Z" fill="#FFFFFF" />
      </g>
    </>
  );
}

/** The "grovsen" wordmark — 7 outlined glyph paths, copied exactly from the source SVG. */
function Wordmark() {
  return (
    <g transform="translate(294.0 204.0)" fill="var(--logo-wordmark)">
      <g transform="scale(0.16833 -0.16833)">
        <path
          d="M469 475V554H610V-4Q610 -81 579.0 -141.5Q548 -202 486.0 -237.5Q424 -273 336 -273Q218 -273 142.5 -218.0Q67 -163 57 -68H196Q207 -106 243.5 -128.5Q280 -151 332 -151Q393 -151 431.0 -114.5Q469 -78 469 -4V82Q442 43 394.5 17.0Q347 -9 286 -9Q216 -9 158.0 27.0Q100 63 66.5 128.5Q33 194 33 279Q33 363 66.5 428.0Q100 493 157.5 528.0Q215 563 286 563Q348 563 395.0 538.5Q442 514 469 475ZM322 440Q283 440 250.0 421.0Q217 402 196.5 365.5Q176 329 176 279Q176 229 196.5 191.5Q217 154 250.5 134.0Q284 114 322 114Q361 114 395.0 133.5Q429 153 449.0 189.5Q469 226 469 277Q469 328 449.0 364.5Q429 401 395.0 420.5Q361 440 322 440Z"
          transform="translate(0.00 0)"
        />
        <path
          d="M379 562V415H342Q276 415 242.5 384.0Q209 353 209 276V0H69V554H209V468Q236 512 279.5 537.0Q323 562 379 562Z"
          transform="translate(656.97 0)"
        />
        <path
          d="M34 277Q34 362 71.5 427.0Q109 492 174.0 527.5Q239 563 319 563Q399 563 464.0 527.5Q529 492 566.5 427.0Q604 362 604 277Q604 192 565.5 127.0Q527 62 461.5 26.5Q396 -9 315 -9Q235 -9 171.0 26.5Q107 62 70.5 127.0Q34 192 34 277ZM460 277Q460 356 418.5 398.5Q377 441 317 441Q257 441 216.5 398.5Q176 356 176 277Q176 198 215.5 155.5Q255 113 315 113Q353 113 386.5 131.5Q420 150 440.0 187.0Q460 224 460 277Z"
          transform="translate(1039.94 0)"
        />
        <path d="M300 129 440 554H589L384 0H214L10 554H160Z" transform="translate(1656.91 0)" />
        <path
          d="M39 175H180Q184 143 211.5 122.0Q239 101 280 101Q320 101 342.5 117.0Q365 133 365 158Q365 185 337.5 198.5Q310 212 250 228Q188 243 148.5 259.0Q109 275 80.5 308.0Q52 341 52 397Q52 443 78.5 481.0Q105 519 154.5 541.0Q204 563 271 563Q370 563 429.0 513.5Q488 464 494 380H360Q357 413 332.5 432.5Q308 452 267 452Q229 452 208.5 438.0Q188 424 188 399Q188 371 216.0 356.5Q244 342 303 327Q363 312 402.0 296.0Q441 280 469.5 246.5Q498 213 499 158Q499 110 472.5 72.0Q446 34 396.5 12.5Q347 -9 281 -9Q213 -9 159.0 15.5Q105 40 73.5 82.0Q42 124 39 175Z"
          transform="translate(2234.88 0)"
        />
        <path
          d="M580 235H175Q180 175 217.0 141.0Q254 107 308 107Q386 107 419 174H570Q546 94 478.0 42.5Q410 -9 311 -9Q231 -9 167.5 26.5Q104 62 68.5 127.0Q33 192 33 277Q33 363 68.0 428.0Q103 493 166.0 528.0Q229 563 311 563Q390 563 452.5 529.0Q515 495 549.5 432.5Q584 370 584 289Q584 259 580 235ZM439 329Q438 383 400.0 415.5Q362 448 307 448Q255 448 219.5 416.5Q184 385 176 329Z"
          transform="translate(2758.85 0)"
        />
        <path
          d="M597 325V0H457V306Q457 372 424.0 407.5Q391 443 334 443Q276 443 242.5 407.5Q209 372 209 306V0H69V554H209V485Q237 521 280.5 541.5Q324 562 376 562Q475 562 536.0 499.5Q597 437 597 325Z"
          transform="translate(3354.82 0)"
        />
      </g>
    </g>
  );
}

/**
 * Grovsen brand mark. `"full"` (default) renders the tile + sprout + wordmark
 * lockup; `"icon"` renders the tile + sprout alone, on its own square
 * viewBox. Pass `height` in px — `width` is derived from the viewBox's own
 * aspect ratio. Pass `aria-hidden` when this logo sits next to a visible
 * "Grovsen" wordmark elsewhere and would otherwise be announced twice.
 */
export function GrovsenLogo({
  variant = "full",
  height,
  "aria-hidden": ariaHidden,
}: GrovsenLogoProps) {
  const isIcon = variant === "icon";
  const viewBox = isIcon ? ICON_VIEW_BOX : FULL_VIEW_BOX;
  const aspectRatio = isIcon ? ICON_ASPECT_RATIO : FULL_ASPECT_RATIO;

  return (
    <svg
      role="img"
      aria-label="Grovsen"
      aria-hidden={ariaHidden}
      width={computeWidth(height, aspectRatio)}
      height={height}
      viewBox={viewBox}
      xmlns="http://www.w3.org/2000/svg"
    >
      <title>Grovsen</title>
      {isIcon ? (
        <SproutOnTile />
      ) : (
        <>
          <g transform="translate(40 40) scale(0.41016)">
            <SproutOnTile />
          </g>
          <Wordmark />
        </>
      )}
    </svg>
  );
}
