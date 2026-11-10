GROVSEN — brand assets (deep green)
Color: deep green #0D3631  |  Wordmark: Poppins SemiBold, lowercase, letter-spacing -0.03em
(Wordmark is set in the brand green itself, not black — cohesive for a dark brand colour.)

WHERE TO USE
  LinkedIn company logo  -> grovsen-icon-512.webp (or grovsen-icon-300.webp for smaller slots)
  Website favicon        -> favicon.ico
  Website header         -> grovsen-lockup.svg (or icon.svg + text in CSS)
  Email signature        -> grovsen-lockup-color.webp (keep ~180px wide)
  Dark backgrounds       -> grovsen-lockup-white.*  (white wordmark)

LIGHT VS. DARK VARIANTS
  grovsen-lockup.svg / grovsen-icon.svg        -> light backgrounds (tile #0D3631)
  grovsen-lockup-dark.svg / grovsen-icon-dark.svg -> dark backgrounds (tile lifted to #2A7768)
  grovsen-lockup-white.svg                     -> dark backgrounds where the tile stays
                                                   #0D3631 and only the wordmark turns white

  Why the dark variants exist: the flat brand green #0D3631 measures only 1.42:1
  contrast against the app's dark background (#081614) — effectively invisible
  there. #2A7768 measures 3.47:1 against the same background. Measured directly;
  use the -dark tile whenever the surface behind it is dark, use grovsen-lockup-white
  instead when the tile itself should stay #0D3631 and only the wordmark needs to
  read against a dark surface.

The in-app logo is the inline React component
`web/components/brand/GrovsenLogo.tsx`, not these files — these are the source
of truth for exports (favicons, raster logos, email/doc use), but the app does
not render them directly.

These are working assets to launch with. Hand this direction to a designer later
for final polish.
