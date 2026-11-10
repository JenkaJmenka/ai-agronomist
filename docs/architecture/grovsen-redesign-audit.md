# Grovsen redesign audit — operator UI (`web/`)

Architect, 2026-11-10. Measured on `main` @ `4556539`. Read-only audit; no
source was changed. Line numbers are as of that commit.

Scope: the Next.js operator UI under `web/`, light and dark themes. Known
out-of-scope branding, listed once and not pursued: `src/api/main.py:215`
`title="AI Agronomist API"` and the legacy Streamlit dashboard
(`src/dashboard/app.py:8`, `src/dashboard/home.py:7`).

Contrast ratios below are WCAG 2.x relative-luminance ratios computed from the
hex values; they are rounded to two decimals and were not measured in a
browser. Any ratio quoted for a tint (`/15`, `/16`) is for the tint blended
over the named surface.

---

## 1. Exact paths and the token → utility bridge

| Fact | Value |
|---|---|
| Next.js app root | `web/` (repo root is the FastAPI project) |
| Router | App Router at `web/app/`. **No `web/src/`.** Single root layout `web/app/layout.tsx`; no nested `layout.tsx` anywhere (so `/login` renders inside the same shell — see §5) |
| Global CSS / token file | `web/app/globals.css`, 103 lines — the only token file |
| Framework versions | `next@16.2.10`, `react@19.2.4`, `tailwindcss@^4`, `@tailwindcss/postcss@^4` (`web/package.json:25,29,39,49`) |
| Tailwind config | **None.** No `tailwind.config.*` exists. Tailwind v4 CSS-first: `@import "tailwindcss"` at `globals.css:1`, and the var → utility bridge is the `@theme inline { … }` block at `globals.css:54-68` |
| Chart library | `uplot@^1.6.32` only (`package.json:32`) |

### What `@theme inline` exposes (and therefore what has utility classes)

Exposed as colour utilities (`bg-*`, `text-*`, `border-*`, `outline-*`,
`accent-*`, with `/NN` opacity modifiers): `bg`, `bg-elevated`, `border`,
`text`, `text-muted`, `accent`, `sev-normal`, `sev-warning`, `sev-lstm`,
`sev-critical`, `sev-urgent`. Exposed as radius: `--radius-control`,
`--radius-card` (consumed as `rounded-[var(--radius-control)]`, i.e. arbitrary
values, not as a named radius utility).

**Not exposed** (consumed only as `var(--…)` / arbitrary values):
`--shadow-elevation` (`Card.tsx:12`, `SensorTile.tsx:127`) and
`--chart-select-overlay` (`globals.css:78-80`, the uPlot `.u-select` DOM
overlay).

**Referenced but defined nowhere** (these classes compile to nothing — the
element has no background):

- `bg-surface` — 10 sites: `features/configuration/ConfigurationView.tsx:77,116,132`,
  `features/configuration/components/ConfigEditors.tsx:47,125,134,143,154`,
  `features/app-configuration/AppConfigurationView.tsx:161`.
- `bg-muted-foreground/40` — `features/monitoring/components/LiveBadge.tsx:27`
  (the `paused` dot), pinned by `LiveBadge.test.tsx:96`.
- `--color-brand` — `ConfigEditors.tsx:193` `accent-[var(--color-brand,#16a34a)]`;
  the literal fallback is always what renders.

These are shadcn-vocabulary names (`surface`, `muted-foreground`, `brand`)
that never existed in this token set. They are cheap to fix in the rebrand
and are listed in §3 and §8.

---

## 2. Theme mechanism

### Where `data-theme` is set and persisted

- `features/shell/ThemeToggle.tsx:7` — `THEME_STORAGE_KEY = "theme"`;
  `:48` writes `document.documentElement.dataset.theme = next`; `:50`
  persists to `localStorage` inside try/catch (private mode still themes the
  session). Initial resolution `:27-31`: `data-theme` attribute, else
  `matchMedia("(prefers-color-scheme: dark)")`.
- `app/layout.tsx:18-25` — `THEME_INIT_SCRIPT`, a synchronous inline script
  that restores the stored value onto `<html>`; injected via
  `dangerouslySetInnerHTML` at `:42` as the **first child of `<body>`**.
- `app/globals.css` honours both `[data-theme="dark"]` (`:28-38`) and
  `@media (prefers-color-scheme: dark) { :root:not([data-theme]) }` (`:40-52`).
- `components/charts/chartTheme.ts:65-86` — `subscribeToThemeChange` watches
  the attribute (MutationObserver, `attributeFilter: ["data-theme"]`) and the
  media query, clearing the token memo on either.
- e2e pins: `test/e2e/smoke.spec.ts:362-367` (toggle → dark, survives reload,
  toggles back) and `test/e2e/chart-theme.spec.ts:94-95` ("the two themes
  must not render the same ink").

### Verdict 1 — flash of wrong theme: **no, in practice; a cheap hardening exists**

The script is a classic (non-`async`, non-`defer`, non-module) inline script.
The HTML parser executes it synchronously the moment it reaches it, inside the
same parser task that consumed the `<body>` start tag, and nothing else in
`<body>` has been parsed yet. Rendering opportunities occur between tasks,
not mid-task, so there is no frame in which page content exists in the wrong
theme. The stylesheet (`globals.css` → a render-blocking `<link>` in `<head>`)
is already applied by then, so the attribute flip re-cascades the custom
properties before first content paint.

The one theoretical gap is a streamed-HTML chunk boundary falling exactly
between `<body>` and `<script>`, which could yield one frame of an *empty*
body painted in the OS-preference theme (via the `@media` twin). That frame
is only "wrong" for users whose stored choice differs from their OS
preference, and it contains no content. Not worth engineering around, but
moving the script into `<head>` (plain `<script>` child of `<head>` in the
root layout, or `next/script` with `strategy="beforeInteractive"`) closes it
for free and is the documented Next pattern. Recommend doing it in the shell
group (§8, G3) since `layout.tsx` is being edited anyway.

### Verdict 2 — wrong toggle *icon* on first paint: **yes, visibly, in dark mode**

`ThemeToggle.tsx:40` starts at `useState<Theme | null>(null)`; `:65` renders
`theme === "dark" ? <MoonIcon /> : <SunIcon />`. `null` is not `"dark"`, so
the server HTML and the first client paint always carry the **sun** icon and
`aria-label="Switch to dark theme"` (`:61`). The effect at `:42-44` corrects
it only after hydration. For a dark-theme user the colours are right from the
first frame but the sun icon sits in the sidebar until the client bundle has
loaded and hydrated — on a cold load that is hundreds of milliseconds, and it
is a visible swap. The `aria-label` is also wrong for that window.

Fix (for the shell group): render both icons and let CSS pick, keyed on the
attribute the init script already set —

```
.theme-icon-moon { display: none }
[data-theme="dark"] .theme-icon-sun { display: none }
[data-theme="dark"] .theme-icon-moon { display: inline }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme]) .theme-icon-sun { display: none }
  :root:not([data-theme]) .theme-icon-moon { display: inline }
}
```

— the `aria-label` can stay state-driven (it is not painted) or be replaced by
a static "Toggle colour theme". The e2e assertions in `smoke.spec.ts:362-367`
read the `html[data-theme]` attribute, not the icon, so they are unaffected.

### The duplicated dark block, and what it means for the rebrand

`globals.css:28-38` and `:40-52` carry the **same 8 declarations verbatim**
(`color-scheme`, `--bg`, `--bg-elevated`, `--border`, `--text`,
`--text-muted`, `--accent`, `--shadow-elevation`, `--chart-select-overlay`).
There is no shared source; they drift silently if either is edited alone.
Every dark-token change in this rebrand must be made twice, and the review
gate for the token group (§8, G1) should diff the two blocks. The structural
fix is to make the OS-preference branch set the attribute instead of
duplicating the values — but `THEME_INIT_SCRIPT` deliberately leaves the
attribute unset when nothing is stored, and `chartTheme.ts:79-80` relies on
the media-query path, so collapsing the duplication is a behaviour change,
not a refactor. Recommend keeping both blocks for the rebrand and adding a
one-line comment on each pointing at its twin.

Note also that `--sev-*` and the two radii are never overridden for dark, so
today "normal" green (`#16a34a`) equals the *light* accent and differs from
the dark accent (`#22c55e`). §3 and §9 resolve this.

---

## 3. Every hard-coded colour outside the token file

Proposed tokens are defined in §3.3. "No change" means the literal is correct
to keep as a literal (e.g. a colour-blind-safe chart palette) but the row is
listed so the sweep is complete.

### 3.1 Implementation targets (non-test source)

| file | line | value | what it styles | proposed token |
|---|---|---|---|---|
| `lib/colors.ts` | 10-17 | `#0072b2 #e69f00 #009e73 #cc79a7 #56b4e9 #d55e00 #f0e442 #767676` | `CHART_PALETTE` — Okabe-Ito series strokes (canvas) | **No change.** Colour-blind-safe categorical palette, brand-neutral by design; the 8th slot is already tuned for both backgrounds (header comment). Do not brand-tint series colours. |
| `lib/colors.ts` | 25 | `rgba(239, 68, 68, 0.15)` | `ANOMALY_BAND_FILL` — canvas anomaly band (`SensorTimeSeriesChart.tsx:46`) | `chartTokens().anomalyBand`, resolved from new `--chart-anomaly-band` (both themes `color-mix(in srgb, var(--sev-critical) 15%, transparent)`). Not a rebrand blocker since `--sev-critical` does not change; do it in G5 so the last canvas literal that shadows a token goes through the memo. |
| `lib/colors.ts` | 28 | `rgba(22, 163, 74, 0.1)` | `OPTIMAL_BAND_FILL` — **dead export, zero consumers** (confirmed by grep) | **Delete.** Its comment says "translucent `--accent`", which would become a near-black wash under the new accent; nothing reads it. |
| `components/charts/chartTheme.ts` | 15 | `#5d6b60` | `LIGHT_FALLBACK.text` (SSR/jsdom) — mirrors light `--text-muted` | Keep as a literal (canvas needs a concrete value with no DOM); must equal light `--text-muted`. **Unchanged if light neutrals are unchanged** (recommended, §3.3). |
| `components/charts/chartTheme.ts` | 16 | `#e2e8e2` | `LIGHT_FALLBACK.line` — mirrors light `--border` | Same as above. |
| `features/anomalies/components/ScoreTimeline.tsx` | 76 | `#ff8c00` | uPlot series stroke, "Anomaly score" — duplicates `--sev-lstm` | `chartTokens().sevLstm` (new, reads `--sev-lstm`), handed to uPlot as a function like the axis colours. |
| `features/anomalies/components/ScoreTimeline.tsx` | 82 | `#ef4444` | uPlot series stroke, "Threshold" — duplicates `--sev-critical` | `chartTokens().sevCritical` (new, reads `--sev-critical`). |
| `features/configuration/components/ConfigEditors.tsx` | 193 | `accent-[var(--color-brand,#16a34a)]` | CO2-enrichment checkbox tick colour; `--color-brand` is undefined so `#16a34a` always renders | `accent-accent` (Tailwind `accent-*` utility on the `accent` colour token) — **not** a fixed brand value: `#0D3631` is invisible as a checkbox on dark `--bg-elevated` (1.29:1). |
| `app/globals.css` | 91 | `rgba(22, 163, 74, 0.2)` | `@keyframes tile-flash` 0% — live-value glow on `SensorTile` (`:69-73`) | `--accent-flash` = `color-mix(in srgb, var(--accent) 20%, transparent)`; declare once in `:root`, it follows `--accent` per theme automatically. The flash is a "fresh data" cue, not a severity, so it stays on the accent, not on `--sev-normal`. |
| `app/globals.css` | 94 | `rgba(22, 163, 74, 0)` | `tile-flash` 100% | `transparent`. |
| `components/ui/Button.tsx` | 15 | `text-white` | primary button label on `bg-accent` | `text-on-accent` (new `--on-accent`). White on the dark-mode accent `#4FC3A1` is **2.18:1** — illegible; the dark label must be dark. |
| `components/ui/CalendarDropdown.tsx` | 49 | `text-white` (with `bg-accent`) | selected day cell | `text-on-accent`. Same reasoning. **Missed by the sweep appendix**, which placed `text-white` only in `Button.tsx`. |
| `features/monitoring/components/LiveToggle.tsx` | 153 | `animate-pulse bg-green-400` | live-status dot inside the Live button | `bg-sev-normal` (its sibling `:151` already uses `bg-sev-critical`; `LiveBadge.tsx:166` uses `motion-safe:animate-pulse bg-sev-normal` for the same meaning — match it, including `motion-safe:`). Note the dot sits on `bg-accent` when the button is `primary`; see §9 for why `--sev-normal` on `--accent` still reads. |
| `features/monitoring/components/LiveAnomalyStrip.tsx` | 60 | `border-amber-500/30 bg-amber-500/5` | strip container | `border-sev-warning/30 bg-sev-warning/5` |
| `features/monitoring/components/LiveAnomalyStrip.tsx` | 63 | `text-amber-600` | "Live anomalies" label | `text-sev-warning` |
| `features/monitoring/components/LiveAnomalyStrip.tsx` | 87 | `text-amber-600` | affected-channel names | `text-sev-warning` |
| `features/monitoring/components/LiveBadge.tsx` | 27 | `bg-muted-foreground/40` | `paused` source dot — **undefined token, renders no background** | `bg-text-muted/40` (the comment at `:26` says "muted rather than a severity colour", which is exactly `--text-muted`). Pinned by `LiveBadge.test.tsx:96`. |
| `features/configuration/ConfigurationView.tsx` | 77, 116, 132 | `bg-surface` | crop `<select>` and two numeric inputs — undefined, transparent | Either add `--color-surface: var(--bg-elevated)` to `@theme inline` (zero class edits, 10 sites fixed) or rewrite to `bg-bg-elevated`. Recommend the alias in G1 and the class rewrite as a follow-up. |
| `features/configuration/components/ConfigEditors.tsx` | 47, 125, 134, 143, 154 | `bg-surface` | season `<select>`, four range inputs | Same. |
| `features/app-configuration/AppConfigurationView.tsx` | 161 | `bg-surface` | default-period `<select>` | Same. |

Confirmed against the appendix: §3c's Tailwind-palette list (4 sites) is
complete. The additional rows above are not palette classes; they are
undefined-token classes and a `text-white` the appendix did not list.

### 3.2 Test rows that pin literals

| file | line(s) | value | what it pins | impact of the rebrand |
|---|---|---|---|---|
| `components/charts/chartTheme.test.ts` | 31 | `#5d6b60` | `LIGHT_FALLBACK.text` — the **only** assertion against a source constant | Breaks only if light `--text-muted` (and hence the fallback) changes. Recommended plan keeps it. |
| `components/charts/chartTheme.test.ts` | 26 | `toEqual({ text, line })` | the **key shape** of `ChartTokens` | **Breaks when `chartTokens()` gains `sevLstm` / `sevCritical` / `anomalyBand`** (G5). Must be edited in the same run. |
| `components/charts/chartTheme.test.ts` | 25, 35-38, 44, 49, 53-54, 58, 80-81, 88, 90 | `#93a197 #26302a #5d6b60 #e2e8e2` | self-consistent: `setTokens()` writes these onto `documentElement.style` (`:10-14`) and then reads them back | **Do not break** on any `globals.css` change — they never read the stylesheet. Cosmetic to update. |
| `components/charts/chartTheme.test.ts` | 60-61 | `#000`, `#000000` | "never hands uPlot black" | Unaffected. |
| `features/monitoring/components/LiveBadge.test.tsx` | 96 | `"bg-muted-foreground/40"` | the undefined class string | Breaks when `LiveBadge.tsx:27` is fixed; edit together (G6). |
| `lib/auth/emailProvider.test.ts` | 60, 70, 76 | `"AI Agronomist <no-reply@example.com>"` | a stubbed `AUTH_RESEND_FROM` round-trips to `body.from` | Fixture text only; passes regardless of brand. Optional cosmetic rename. |
| `lib/auth/emailProvider.test.ts` | 95-99 | `"ai-agronomist.app"` | the unowned domain never appears in provider source | Must keep passing; do not introduce any `grovsen.*` domain literal into `emailProvider.ts` either — the sender stays env-driven. |

So the appendix's "16 assertion sites" overstates the blast radius: one line
(`:31`) pins a source value, one line (`:26`) pins the key shape, the rest
are closed-loop.

### 3.3 Proposed token additions (both themes)

Declare in `:root`, override in **both** `[data-theme="dark"]` and the
`@media` twin, and expose through `@theme inline` where a utility is needed.

| token | light | dark | utility | purpose |
|---|---|---|---|---|
| `--accent` | `#0D3631` | `#4FC3A1` | `*-accent` (exists) | primary brand action colour: primary buttons, active nav text, links, focus rings, selects' focus |
| `--on-accent` | `#ffffff` | `#0e1310` | `text-on-accent` (new) | text/icon on an `--accent` fill |
| `--accent-soft` | `#E3EFEB` | `rgb(79 195 161 / 0.16)` | `bg-accent-soft` (new) | tinted surfaces: active nav, selected table row, chip-style brand badges. Replaces `bg-accent/15` wherever the tint is *brand*, not severity |
| `--accent-flash` | `color-mix(in srgb, var(--accent) 20%, transparent)` | (inherits) | none — used by `@keyframes tile-flash` | live-update glow |
| `--brand` | `#0D3631` | `#0D3631` | `*-brand` (new, optional) | the fixed brand green for the logo tile / marketing surfaces only. **Never for text or controls in dark mode** (1.42:1 on `--bg`, 1.29:1 on `--bg-elevated`) |
| `--chart-anomaly-band` | `color-mix(in srgb, var(--sev-critical) 15%, transparent)` | (inherits) | none — read by `chartTokens()` | canvas band fill |
| `--color-surface` | `var(--bg-elevated)` alias in `@theme inline` only | — | `bg-surface` | makes the 10 dangling `bg-surface` sites render |

Neutrals (`--bg`, `--bg-elevated`, `--border`, `--text`, `--text-muted`) and
the `--sev-*` scale: **recommended unchanged** in this rebrand. The neutrals
are already green-tinted toward the brand hue (hue ≈ 173° for `#0D3631`;
`#f8faf8`/`#e2e8e2`/`#17211a`/`#5d6b60` all sit in the same family), and
keeping them avoids the `chartTheme.ts:15-16` / `chartTheme.test.ts:31`
edits. If the user wants a warmer or cooler neutral set later, that is a
separate, contained change to those two blocks plus the fallback pair.

Contrast evidence for the ramp:

| pairing | ratio | verdict |
|---|---|---|
| `#0D3631` text on light `--bg` `#f8faf8` | 12.60:1 | AAA |
| `#0D3631` text on light `--bg-elevated` `#ffffff` | 13.21:1 | AAA |
| `#ffffff` on `#0D3631` (light primary button) | 13.21:1 | AAA |
| `#0D3631` text on `--accent-soft` `#E3EFEB` (light active nav) | 11.21:1 | AAA |
| `#0D3631` text on today's `bg-accent/15` over white (≈ `#DBE1E0`) | 9.98:1 | legible, but the tint is a grey-green wash with no brand read — the reason for a dedicated `--accent-soft` |
| `#4FC3A1` text on dark `--bg` `#0e1310` | 8.62:1 | AAA |
| `#4FC3A1` text on dark `--bg-elevated` `#171e19` | 7.81:1 | AAA |
| `#0e1310` text on `#4FC3A1` (dark primary button, `--on-accent`) | 8.62:1 | AAA |
| `#ffffff` text on `#4FC3A1` (what `text-white` would give) | 2.18:1 | **fails** — this is why `--on-accent` exists |
| `#4FC3A1` text on dark `--accent-soft` blended over `#171e19` (≈ `#203830`) | 5.79:1 | AA |
| `#0D3631` on dark `--bg` / `--bg-elevated` | 1.42:1 / 1.29:1 | invisible — never use `--brand` for ink in dark |

`#4FC3A1` was chosen as the same hue family (≈ 165-173°) lifted to L≈60%;
a designer may substitute any value that keeps ≥ 7:1 on `#171e19` and
≥ 4.5:1 for `#0e1310` text on it. Those two numbers are the acceptance gate,
not the specific hex.

A light-mode caveat the implementation should carry: `#0D3631` as *link*
colour (`text-accent` at `HomeStatusBar.tsx:50`, `HomeAnomalyList.tsx:60`,
`LiveAnomalyStrip.tsx:94`) is close to body text `#17211a` in value, so link
affordance will rest on the existing `underline-offset-2 hover:underline`.
Recommend adding a persistent `underline` (not hover-only) to those three
anchors in G2/G6, or accept the reduced affordance — a user decision.

---

## 4. Chart library and where colours are set

**uPlot only** (`uplot@^1.6.32`). No Recharts/Chart.js/D3/Victory, and
neither `@radix-ui/react-tooltip` nor `@radix-ui/react-select` is imported
anywhere despite being in `package.json:18,20`.

| concern | where | mechanism |
|---|---|---|
| axis text, grid, ticks, border | `components/charts/chartTheme.ts:51-58` `themedAxisDefaults()` → merged per axis in `components/charts/UPlotChart.tsx:64-92` | **functions** that call `chartTokens()`, so uPlot re-resolves at every draw; `chartTokens()` (`:33-40`) reads `--text-muted` / `--border` via `getComputedStyle` and memoises; `subscribeToThemeChange` (`:65-86`) clears the memo |
| series strokes — sensor chart | `features/monitoring/components/SensorTimeSeriesChart.tsx:107` `stroke: channelColor(i)` ← `lib/colors.ts:20-22` | literal Okabe-Ito palette; **no change** |
| series strokes — score timeline | `features/anomalies/components/ScoreTimeline.tsx:76,82` | literal `#ff8c00` / `#ef4444`; must move to `chartTokens()` |
| anomaly band | `SensorTimeSeriesChart.tsx:43-53` `drawAnomalyBands` — raw canvas `ctx.fillStyle = ANOMALY_BAND_FILL` (`:46`) | literal from `lib/colors.ts:25` |
| drag-select overlay | DOM, not canvas: `.u-select` in `globals.css:78-80` reading `--chart-select-overlay` | already themed |
| tooltip / hover readout | **none.** Both charts set `legend: { show: false }` (`SensorTimeSeriesChart.tsx:114`, `ScoreTimeline.tsx:88`) and the only `cursor` option is `SELECT_ONLY_CURSOR` for drag-select. There is no hover value readout anywhere. Confirmed by grep for `legend|tooltip|setCursor|u-legend|u-cursor` across `app/ components/ features/`. | nothing to retheme; if the redesign adds a readout it starts from zero |

**Constraint, stated plainly:** canvas 2D cannot read CSS custom properties
(`chartTheme.ts:1-9`). Adding `--accent` or any new token to `globals.css`
does nothing for a chart. A chart-visible token must be (a) declared in
`globals.css`, (b) read in `chartTokens()` with a `LIGHT_FALLBACK` entry for
SSR/jsdom, and (c) handed to uPlot as a *function* so a theme flip repaints
without a rebuild. The memo is cleared by `subscribeToThemeChange`, which
already covers both the attribute and the media query. Whether the brand
accent ever needs to reach a chart is open — today no chart element is
accent-coloured, and the recommendation is to keep it that way (series =
Okabe-Ito, bands = severity).

---

## 5. Every place "AI Agronomist" is rendered as branding

| file | line | surface | visible? | rename action |
|---|---|---|---|---|
| `app/layout.tsx` | 12 | `metadata.title: "AI Agronomist"` → `<title>` on every page | yes | `"Grovsen"`; consider `title: { default: "Grovsen", template: "%s · Grovsen" }` so pages can set their own |
| `app/layout.tsx` | 13 | `metadata.description` | meta tag | keep or reword; no brand string in it today |
| `features/shell/Sidebar.tsx` | 56 | `<p className="text-sm font-bold text-accent">AI Agronomist</p>` — **the logo slot; there is no logo/mark component in the repo** | yes, every page including `/login` | replace with a lockup component (§8, G3) |
| `features/home/HomeView.tsx` | 265 | `<h1 className="text-2xl font-bold">AI Agronomist</h1>` — Home H1 | yes | **decision needed** (§10): rename to `Grovsen`, or make the H1 a page title ("Overview") now that the sidebar carries the brand |
| `features/auth/LoginView.tsx` | — | **No brand string.** It is a `Card title="Sign in"` with an email input and a primary `Button` (`:25-48`); all classes are token-driven (`text-text`, `text-text-muted`, `border-border`, `bg-bg-elevated`). | — | the only brand on `/login` is the sidebar wordmark, because `app/login/page.tsx` renders inside the root layout and there is no `app/login/layout.tsx`. The redesign should add the mark above "Sign in" and almost certainly a login-specific layout without the nav — see §10 |
| `components/charts/chartBaseline.ts` | 12 | `STORAGE_KEY_PREFIX = "ai-agronomist.chart-baseline"` | no | **Leave it.** Note: this is `sessionStorage` (`:21`), not `localStorage` as the brief said — renaming would only lose baselines in tabs open across a deploy, not persisted state. Still zero benefit to renaming an invisible key |
| `test/e2e/smoke.spec.ts` | 119 | `getByRole("heading", { name: "AI Agronomist" })` after `goto("/")` | — | **breaks on H1 rename**; edit with G3 |
| `test/e2e/perf.spec.ts` | 91 | same assertion, used as the cold-load gate | — | **breaks on H1 rename**; edit with G3 |
| `test/e2e/global-setup.ts` | 25 | `email: "e2e@ai-agronomist.test"` | — | fixture; no change |
| `web/AGENTS.md` | 7 | `# AI Agronomist — web/` | docs | cosmetic |

### Magic-link email — actual state

`lib/auth/emailProvider.ts:91` calls the stock
`next-auth/providers/resend` `sendVerificationRequest`. There is **no custom
HTML**: the email body is Auth.js's default template, which renders the bare
host name as the "sign in to …" subject/heading. Brand colours and the
wordmark do not reach the inbox. The sender display name is whatever
`AUTH_RESEND_FROM` holds (`:70`, `:83`) — a Railway variable, not code — and
the provider refuses to send when it is unset (`:86`). So the rebrand of the
email has two independent parts:

1. Sender name: change the Railway variable to `Grovsen <…>` on a domain the
   project owns. No code; `emailProvider.test.ts:95-99` forbids baking any
   domain into source, and that must stay true.
2. Template: a branded body needs a custom `sendVerificationRequest` with
   inline-styled HTML (email clients ignore CSS variables; use the literal
   `#0D3631` and white). That is a feature, not part of the UI rebrand;
   flagged as an open item.

---

## 6. Current favicon / icon setup

- `web/app/favicon.ico` — the only icon on disk under `app/` (25,931 bytes,
  4 images: 16×16 and 32×32 at 32bpp). Picked up by Next's **file
  convention** only; no `metadata.icons` key exists (`layout.tsx:11-14` has
  exactly `title` and `description`).
- No `app/icon.*`, `apple-icon.*`, `opengraph-image.*`, `manifest.*`, and no
  `metadata.openGraph`.
- `web/public/` holds only the untouched create-next-app scaffold:
  `file.svg`, `globe.svg`, `next.svg`, `vercel.svg`, `window.svg` — none
  referenced by any source file. Safe to delete in G4.
- Brand assets exist only at repo root `assets/brand/` (untracked, not
  gitignored): `favicon.ico`, `grovsen-icon.svg`, `grovsen-lockup.svg`,
  `grovsen-lockup-white.svg`, and `.webp` rasters at 512/300 (icon), 16/32/64
  (favicon), plus colour/white lockups.

Facts about the SVGs that matter for implementation (read from the files):

- `grovsen-icon.svg` is a 512×512 `#0D3631` rounded square (`rx=112`) with a
  white seedling (`fill="#FFFFFF"` paths). On dark `--bg-elevated` the tile
  is 1.29:1 — the square disappears and only the white leaf shows. Dark mode
  needs either the white lockup or a mark whose tile uses `--accent`.
- `grovsen-lockup.svg` carries the wordmark as **outlined paths**
  (`<path d="…">` groups at line 9), not `<text>`. **No Poppins download is
  needed to render the lockup.** Poppins is only required if headings or
  CSS-set text adopt the brand face. The wordmark fill is `#0D3631` baked in;
  for theming, inline the SVG as a component and set `fill="currentColor"`.
- `README.txt` discrepancy **confirmed**: it names `grovsen-icon-300.png` and
  `grovsen-lockup-color.png`; only `.webp` rasters exist. Relevant because
  Next's `apple-icon` convention accepts `.jpg/.jpeg/.png` and `icon` accepts
  `.ico/.jpg/.jpeg/.png/.svg` — **`.webp` is not accepted for either**, so an
  `apple-icon.png` needs a conversion step the agent cannot do with
  Read/Write alone (needs Bash + an image tool, or the user exports a PNG).

---

## 7. Components carrying colour today

| primitive | file | colour-bearing class strings today | rebrand change |
|---|---|---|---|
| Button | `components/ui/Button.tsx:15-18,36` | `primary: "bg-accent text-white hover:opacity-90 disabled:opacity-50"`; `secondary: "border border-border bg-bg-elevated text-text hover:border-accent disabled:opacity-50"`; `ghost: "text-text-muted hover:text-text disabled:opacity-50"`; base `focus-visible:outline-2 focus-visible:outline-accent` | `text-white` → `text-on-accent`; rest follows `--accent` automatically |
| Chip (badge) | `components/ui/Chip.tsx:10-19` | `unclassified: "bg-text-muted/15 text-text-muted"`, `normal: "bg-sev-normal/15 text-sev-normal"`, `warning/lstm/critical/urgent` same pattern on their `sev-*` | none — severity scale unchanged (§9) |
| Live pill | `features/monitoring/components/LiveBadge.tsx:22-28,146-175` | `STATUS_DOT` `bg-sev-normal` / `bg-sev-warning` / `bg-sev-critical` / **`bg-muted-foreground/40`**; pills `bg-sev-critical/15 text-sev-critical`, `bg-sev-warning/15 text-sev-warning`, `bg-sev-normal/15 text-sev-normal`, dot `motion-safe:animate-pulse bg-sev-normal` | `:27` → `bg-text-muted/40` |
| Freshness badge | `features/monitoring/components/LiveFreshnessBadge.tsx:38-41` | `sev-*` pattern (not re-read; appendix) | none |
| Card | `components/ui/Card.tsx:12,15` | `rounded-[var(--radius-card)] border border-border bg-bg-elevated p-4 shadow-[var(--shadow-elevation)]`; title `text-text-muted` | none |
| KPI tile | `features/monitoring/components/SensorTile.tsx:93-110,126-127,146,160,168,179` | anomaly `border-l-sev-critical bg-sev-critical/10 text-sev-critical`; excursion `border-l-sev-warning bg-sev-warning/5 text-sev-warning`; ok `border-l-sev-normal bg-bg-elevated text-text`; no_range `border-l-border`; badge `bg-sev-critical/15 text-sev-critical`; derived `bg-border text-text-muted`; shell `border-border border-l-4 shadow-[var(--shadow-elevation)]`; flash overlay via `.tile-flash` | none in classes; `tile-flash` keyframes retokenised in G1 |
| Selects (all native) | `components/ui/DateRangeControls.tsx:140,163` (`SELECT_CLASS`); `features/shell/GreenhouseSelector.tsx:26` `rounded-[var(--radius-control)] border border-border bg-bg-elevated px-2 py-1.5 text-sm text-text`; `features/operations/components/InjectAnomaliesPanel.tsx:65,127` same minus `text-text`; `features/configuration/ConfigurationView.tsx:77`, `ConfigEditors.tsx:47`, `AppConfigurationView.tsx:161` `border border-border bg-surface …` | 7 `<select>` sites across 6 files, three class variants, one of them using the undefined `bg-surface`. A rebrand that restyles selects is either 6 edits or one `components/ui/Select.tsx` primitive. `@radix-ui/react-select` is installed and unused; a native-`<select>` primitive with a shared class string is the smaller change and keeps `color-scheme` native rendering |
| Active nav | `features/shell/Sidebar.tsx:38-40` | `isActive ? "bg-accent/15 font-medium text-accent" : "text-text hover:bg-border/50"`, `aria-current="page"` at `:33`; section labels `:23` `text-text-muted`; shell `:54` `border-r border-border bg-bg-elevated` | `bg-accent/15` → `bg-accent-soft`; recommend adding a `border-l-2 border-accent` so the active state does not rest on a dark-on-tint pairing alone |
| Calendar | `components/ui/CalendarDropdown.tsx:48-52` | selected `bg-accent text-white font-semibold`; today `border border-accent text-accent`; default `text-text hover:bg-border/50` | `text-white` → `text-on-accent` |
| Data table rows | `components/ui/DataTable.tsx:218-219` | `hover:bg-accent/5 focus-visible:bg-accent/10`, selected `bg-accent/15` | selected → `bg-accent-soft`; hover/focus tints can stay on `--accent` |
| Two-step confirm | `components/ui/TwoStepConfirm.tsx:53-61` | `border-sev-critical/40 bg-sev-critical/5`, `text-sev-critical`, armed button `bg-sev-critical` | none |
| DateTimeField | `components/ui/DateTimeField.tsx:168-169` | `bg-bg … focus-within:outline-accent`, error `border-sev-critical` | none |
| Links | `features/home/components/HomeStatusBar.tsx:50`, `features/home/components/HomeAnomalyList.tsx:60`, `features/monitoring/components/LiveAnomalyStrip.tsx:94` | `text-accent underline-offset-2 hover:underline` | see the light-mode link caveat in §3.3 |
| Theme toggle | `features/shell/ThemeToggle.tsx:63` | `text-text-muted hover:bg-border/50 hover:text-text focus-visible:outline-accent` | icon fix per §2 |

---

## 8. Implementation groups — one agent run each, in landing order

All `web/` groups are **frontend-engineer** (`web/AGENTS.md`). The five gates
are `corepack pnpm lint`, `typecheck`, `test`, `build`, and
`corepack pnpm exec playwright test` run from `web/` with pnpm 9.15.9 via
corepack. Each group is designed to be green on its own with `main` as base.

### G1 — tokens (additive, no visual change until consumers move)

Files: `web/app/globals.css`.

Changes: add `--accent` new values, `--on-accent`, `--accent-soft`,
`--accent-flash`, `--brand`, `--chart-anomaly-band` to `:root`; the dark
overrides to **both** `[data-theme="dark"]` and the `@media` twin; expose
`--color-on-accent`, `--color-accent-soft`, `--color-brand`,
`--color-surface: var(--bg-elevated)` in `@theme inline`; rewrite
`@keyframes tile-flash` to `var(--accent-flash)` / `transparent`.

Forces test edits: none. Gate: all five; `test/e2e/chart-theme.spec.ts` must
still show the two themes differ. Reviewer diff-checks the two dark blocks
line by line.

Note: with G1 alone the UI *already* changes — `--accent` is consumed
everywhere — and primary buttons will be white-on-`#4FC3A1` in dark mode
until G2 lands. Land G1 and G2 in the same PR or accept one intermediate
commit with the 2.18:1 pairing; the former is recommended.

### G2 — primitives

Files: `web/components/ui/Button.tsx`, `web/components/ui/CalendarDropdown.tsx`,
`web/components/ui/DataTable.tsx`.

Changes: `text-white` → `text-on-accent` (Button:15, CalendarDropdown:49);
selected row `bg-accent/15` → `bg-accent-soft` (DataTable:219).

Forces test edits: none found (no unit test asserts these class strings —
the agent must confirm with a grep for `text-white` under `web/**/*.test.*`
before claiming so). Gate: all five.

### G3 — shell and branding

Files: new `web/components/brand/GrovsenLockup.tsx` and
`web/components/brand/GrovsenMark.tsx` (inline SVG from
`assets/brand/grovsen-lockup.svg` / `grovsen-icon.svg`, `fill="currentColor"`
on the wordmark, tile on `--accent`), `web/features/shell/Sidebar.tsx`,
`web/features/shell/ThemeToggle.tsx`, `web/app/layout.tsx`,
`web/features/home/HomeView.tsx`, `web/features/auth/LoginView.tsx`,
`web/test/e2e/smoke.spec.ts`, `web/test/e2e/perf.spec.ts`.

Changes: Sidebar `:56` → `<GrovsenLockup className="text-accent" />` with a
proper `<a href="/">` or heading role; active nav `:39` → `bg-accent-soft`
(+ optional left border); ThemeToggle CSS-driven icon (§2); layout
`metadata.title` → `Grovsen` (+ template), move `THEME_INIT_SCRIPT` into
`<head>`; HomeView `:265` per the §10 decision; LoginView adds the mark above
"Sign in"; the two e2e heading assertions updated to whatever the new H1 says.

Forces test edits: **yes** — `smoke.spec.ts:119`, `perf.spec.ts:91`. Gate:
all five, with the perf budget (cold < 2 s) explicitly re-measured because the
inline lockup adds bytes to every page. If Poppins is adopted for headings
(§10), it is added here via `next/font/google` `Poppins({ weight: ["600"] })`
and the cold-load budget must be re-measured again.

### G4 — icons and metadata

Files: `web/app/favicon.ico` (replace from `assets/brand/favicon.ico`), new
`web/app/icon.svg` (from `grovsen-icon.svg`), new `web/app/apple-icon.png`
(**blocked** until a 180×180 PNG exists — `.webp` is not accepted by the
convention; see §6), delete `web/public/{file,globe,next,vercel,window}.svg`.

Forces test edits: none. Gate: `build` + a manual check that `/favicon.ico`
and `/icon.svg` are served; e2e unaffected. Owner is frontend-engineer; the
PNG export is a user or devops step because it needs a tool outside the
agent's toolset.

### G5 — charts

Files: `web/components/charts/chartTheme.ts`,
`web/features/anomalies/components/ScoreTimeline.tsx`, `web/lib/colors.ts`,
`web/features/monitoring/components/SensorTimeSeriesChart.tsx`,
`web/components/charts/chartTheme.test.ts`.

Changes: `ChartTokens` gains `sevLstm`, `sevCritical`, `anomalyBand` with
`LIGHT_FALLBACK` entries (`#ff8c00`, `#ef4444`, `rgba(239, 68, 68, 0.15)`);
ScoreTimeline strokes become `() => chartTokens().sevLstm` etc.;
`drawAnomalyBands` reads `chartTokens().anomalyBand`; delete
`OPTIMAL_BAND_FILL` and `ANOMALY_BAND_FILL` from `colors.ts` once nothing
imports them.

Forces test edits: **yes** — `chartTheme.test.ts:26` (`toEqual` key shape);
add a case that the new keys resolve and fall back. Gate: all five, including
`chart-theme.spec.ts`.

### G6 — monitoring stragglers and the undefined tokens

Files: `web/features/monitoring/components/LiveToggle.tsx`,
`web/features/monitoring/components/LiveAnomalyStrip.tsx`,
`web/features/monitoring/components/LiveBadge.tsx`,
`web/features/monitoring/components/LiveBadge.test.tsx`,
`web/features/configuration/components/ConfigEditors.tsx`.

Changes: per §3.1 rows — `bg-green-400` → `motion-safe:animate-pulse
bg-sev-normal`; the three amber classes → `sev-warning`; `:27` →
`bg-text-muted/40`; `:193` → `accent-accent`. Optionally rewrite the 10
`bg-surface` sites to `bg-bg-elevated` and drop the G1 alias.

Forces test edits: **yes** — `LiveBadge.test.tsx:96`. Gate: all five.

### G7 — email sender (no code)

Owner: the user via Railway, or devops-lead if the variable is managed in
`.railway/railway.ts`. Change `AUTH_RESEND_FROM` to `Grovsen <no-reply@<owned
domain>>`. `emailProvider.test.ts:95-99` must keep passing — nothing in
`emailProvider.ts` changes. A branded template is a separate feature (§5).

### Not touched

`.github/` — no workflow depends on the brand string (devops-lead has nothing
to do unless the Railway variable is codified). `lib/colors.ts` palette rows,
`Chip.tsx`, `Card.tsx`, `SensorTile.tsx` classes, the `--sev-*` scale, the
`chartBaseline` storage key.

---

## 9. The two colour decisions

### 9.1 Per-theme accent ramp — which surface moves where

`#0D3631` cannot be the dark-mode accent: 1.42:1 against `--bg` and 1.29:1
against `--bg-elevated`. The ramp in §3.3 keeps the brand green as the
*light* accent and lifts the same hue to `#4FC3A1` for dark.

| surface | today | after |
|---|---|---|
| primary button fill / label | `bg-accent text-white` | `bg-accent text-on-accent` |
| secondary button hover border, focus rings, DateTimeField focus, ThemeToggle focus | `*-accent` | unchanged token, new value |
| sidebar active item | `bg-accent/15 text-accent` | `bg-accent-soft text-accent` (+ `border-l-2 border-accent` recommended) |
| sidebar wordmark | `text-accent` on a `<p>` | lockup component, `text-accent` → `#0D3631` light / `#4FC3A1` dark; the icon tile uses `--accent` as well so it does not vanish in dark |
| selected table row | `bg-accent/15` | `bg-accent-soft` |
| table hover/focus | `bg-accent/5`, `/10` | unchanged (ratios irrelevant — no text colour change) |
| calendar selected / today | `bg-accent text-white` / `border-accent text-accent` | `text-on-accent` / unchanged |
| checkbox tick | literal `#16a34a` | `accent-accent` |
| live-value flash | literal `rgba(22,163,74,.2)` | `--accent-flash` |
| links | `text-accent` | unchanged token; see the light-mode affordance caveat |
| logo tile in marketing/email contexts | — | `--brand` fixed `#0D3631` (light surfaces only) |

The tint-on-tint problem the brief raised is real for the *existing* `/15`
pattern: 15% of a near-black green over white is a grey wash
(≈ `#DBE1E0`), legible at 9.98:1 but with no brand identity. That is why
`--accent-soft` is a hand-picked mint (`#E3EFEB`, 11.21:1 under `#0D3631`)
rather than an opacity of the accent. In dark mode the inverse holds — 16%
of a light mint over `#171e19` is a pleasant tinted surface and `#4FC3A1`
text on it reads at 5.79:1 — so there the alpha form is fine, and it is
declared that way so it tracks the accent if a designer changes it.

### 9.2 Brand green vs `--sev-normal`: **deliberately diverge**

Call: `--sev-normal` stays `#16a34a` (light, 3.14:1 on `--bg`, as today) and
is **not** overridden for dark (so `#16a34a` on `#0e1310`, 5.69:1, as today).
It does not follow the brand.

Reasoning:

1. Severity colours are semantics, not chrome. If a healthy sensor tile's
   left bar, the "Live · SSE" pill and the `normal` chip were brand green, a
   healthy reading would look like decoration and a brand element would look
   like a verdict. The `Chip.tsx:11-12` comment already draws this line for
   `unclassified` ("not a reassuring low-severity verdict") — the same
   principle says brand ≠ normal.
2. The brand green is too dark to function as a status colour on the light
   theme: a `bg-sev-normal/15 text-sev-normal` chip at `#0D3631` is a
   grey-green chip, and a `border-l-sev-normal` tile bar at that value reads
   as neutral, not as "ok".
3. Today the coupling is accidental (light accent happened to equal
   `--sev-normal`; dark did not). Breaking it is a correction, not a loss —
   the dark theme has never had them equal and nobody has reported the
   mismatch.
4. The grass green (`#16a34a` / hue ≈ 142°) and the brand teal-green
   (`#0D3631` / `#4FC3A1`, hue ≈ 165-173°) are far enough apart in hue and
   value that they will not be read as the same colour side by side — for
   example the live dot (`bg-sev-normal`) on the primary Live button
   (`bg-accent`): `#16a34a` on `#0D3631` is 4.0:1 in light, and on `#4FC3A1`
   in dark it is 1.8:1, which is low for a 2px dot. G6 should move the dot to
   `bg-on-accent` when the button is `primary` — a two-line conditional — or
   accept the dim dot. Flagged, not decided here.

Pre-existing issue, not caused by the rebrand and not fixed by it:
`#16a34a` text on `#f8faf8` is **3.14:1**, below AA for the `text-xs` chips.
Worth its own backlog item; raising `--sev-normal` to e.g. `#15803d`
(≈ 4.6:1) would fix it and is a one-token change, but it is a severity-scale
decision outside this brand brief.

---

## 10. Open questions for the user

1. **Home H1** (`HomeView.tsx:265`): "Grovsen", or a page title such as
   "Overview" now that the sidebar carries the brand? Determines the two e2e
   edits. Recommendation: page title; the brand appears once, in the shell.
2. **Typography**: adopt Poppins SemiBold for headings (and load it via
   `next/font/google`, re-measuring the cold-load budget), or keep Inter
   everywhere and let the SVG lockup be the only Poppins on screen?
   Recommendation: Inter stays; the lockup is already outlined and needs no
   font.
3. **Login layout**: today `/login` renders inside the full shell with the
   sidebar, nav links and greenhouse selector, because there is no
   `app/login/layout.tsx` and `Sidebar` does not check the route. Should the
   redesign give `/login` a bare branded layout (mark + card, no nav)? This
   is a structural change beyond colours and belongs in G3 if yes. I could
   not confirm in a browser what an anonymous user sees; `proxy.ts` is said
   to fail-close non-public routes, so the sidebar's data components may
   render empty rather than erroring.
4. **Link affordance in light mode**: accept near-black links that underline
   on hover, or add a persistent underline / use `--accent-strong`?
5. **`--sev-normal` legibility** (3.14:1): take the one-token fix now, or
   defer to a separate severity-scale item?
6. **Apple touch icon**: who exports a 180×180 PNG from the `.webp`/SVG? The
   agent cannot.
7. **Email**: sender rename only (Railway variable), or also a branded
   template (new feature)?
8. **Dark-mode dot on the primary Live button** (§9.2 item 4): `bg-on-accent`
   or leave as is?
9. **Should `assets/brand/` be committed**, or do the SVGs move under
   `web/components/brand/` as inline components and the directory stays
   untracked? Either way the `.png` references in `README.txt` should be
   corrected to `.webp` by whoever owns that directory (it has no owner row in
   `CLAUDE.md`).

---

## Appendix — discrepancies against the orchestrator's sweep

1. `chartBaseline.ts` uses **`sessionStorage`** (`:21`), not `localStorage`.
   Renaming the prefix loses only in-tab state across a deploy, not persisted
   baselines. Recommendation unchanged (leave it).
2. "`chartTheme.test.ts` pins the exact token hexes at 16 assertion sites" is
   overstated: the test writes its own values with `setTokens()` and reads
   them back. Only `:31` pins a source constant (`LIGHT_FALLBACK.text`) and
   `:26` pins the key shape. The key-shape line is the real edit site.
3. The §3c palette-class list is complete, but the sweep missed a second
   category of colour defects: undefined tokens that render as nothing —
   `bg-surface` at 10 sites and `bg-muted-foreground/40` at `LiveBadge.tsx:27`
   (pinned by `LiveBadge.test.tsx:96`).
4. `text-white` is not only in `Button.tsx:15`; it is also at
   `CalendarDropdown.tsx:49`. `DataTable.tsx:218-219` accent tints were also
   absent from the §7 inventory.
5. §4's open item on tooltips is settled: there is no tooltip or hover
   readout in either chart.
6. §8 confirmed: `README.txt` names `.png` rasters that do not exist; only
   `.webp` exist. Additionally, the lockup SVG wordmark is outlined paths, so
   no Poppins load is needed for it.
7. §5's "login renders no AI Agronomist string" is true of `LoginView.tsx`
   but not of the `/login` page as seen, which includes the sidebar wordmark
   via the single root layout.
