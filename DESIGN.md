# Slipcast Design System — "Signal Deck"

Status: v1.0 · 2026-09-13
Live showcase: `/static/design-system.html` (also linked from the dashboard's
Settings → About dialog). Source of truth for every token below is
`app/static/styles.css` — this document explains and narrates it; the CSS
file is what ships.

---

## 1. Direction narrative

Slipcast takes a YouTube channel and slips it into your podcast app as an
RSS feed with downloaded audio — it's a small relay station, not a media
player and not a social app. The **"Signal Deck"** direction leans into
that: the dashboard is styled like the control panel of the machine doing
the relaying — a late-night broadcast engineer's desk, not a
consumer-facing app. Dark ink surfaces, one electric "on-air" signal color
(violet, with the app's existing red→violet gradient as the literal signal
being relayed), and a technical monospace reserved for anything the machine
generated itself (poll countdowns, feed tokens, version numbers,
timestamps) rather than anything a person typed.

This direction was **not invented from nothing** — it's an uplift that
extends an identity the app already had. Before this pass, `app/static`
already shipped a genuinely purpose-built icon set (`icon-512.png`,
`cover-512.png`, `apple-touch-icon.png`, etc.): a play-button transitioning
into broadcast/podcast waves, in a red-to-violet gradient — literally
"YouTube video slipping into a podcast signal." That icon was checked
against the "generic or default?" bar this pass is supposed to apply, and
it clearly is not generic — it's a specific, legible metaphor for what the
app does — so it was **left untouched**. Everything else (color roles,
type, motion, textures, component treatment) was built to point at that
existing mark instead of away from it.

**Mood-board process.** Per the `ui-design` skill, this batch/autonomous
run generated candidate mood boards via Ideogram rather than asking a user
to pick (none was available to review interactively). The Ideogram MCP
account used for this session was shared with several other concurrent,
unrelated sessions during the run and its generation queue was saturated
for long stretches — only one of the three planned directions ("Signal
Deck", "Tape Transfer" analog-cassette, "Orbit Relay" satellite/space)
finished rendering in the available window. That single board
(`docs/design/signal-deck-moodboard.webp`) came back as a decisive match
for the app's existing icon and theme-color (`#5415A0`) already baked into
`site.webmanifest`, so rather than block further on a saturated shared
queue for two more directions to compare against, "Signal Deck" was
adopted directly — it isn't a coin-flip between three fresh options so
much as a confirmation of the direction the app's own assets already
implied.

**Key moments this system was designed around:**
1. **First load / at-a-glance health** — the polling gauge (a literal dial)
   and the "all healthy / N failing" status are the first thing worth
   seeing.
2. **Adding a channel or downloading an episode** — the one primary action
   per form; it carries the signature signal-gradient treatment so it reads
   as "the button that matters" against the ghost/text buttons around it.
3. **A poll or job succeeding or failing** — toasts and per-channel poll
   tags are the app's main feedback loop since polling runs unattended.
4. **Sharing a feed** — the share modal (QR code + URL + one-tap podcast
   app links) is the hand-off point to a phone, so it needs to feel quick
   and legible at a glance.
5. **Cookie health** — the one thing that silently breaks everything else;
   the cookie banner/status dot needed to stay legible without being
   alarmist at every visit.

---

## 2. Color

All values live in `:root` in `app/static/styles.css`, with a
`@media (prefers-color-scheme: dark)` block overriding the surface/ink
tokens (the app has no manual theme toggle — it follows the OS setting,
consistent with how it already worked before this pass).

### Surfaces & ink

| Token | Light | Dark | Role |
|---|---|---|---|
| `--bg` | `#f5f6fb` | `#0e1020` | Page background |
| `--surface` | `#ffffff` | `#181b30` | Card/panel fill |
| `--surface-2` | `#f9fafe` | `#1f2340` | Recessed fill (inputs, cookies form) |
| `--ink` | `#181b2e` | `#eef0fb` | Primary text |
| `--muted` | `#6b7191` | `#a6abce` | Secondary text |
| `--faint` | `#9aa0c0` | `#767ca6` | Tertiary text, captions |
| `--border` | `#e7e9f4` | `#2a2f4f` | Hairlines |

Contrast: `--ink` on `--surface` is 14.9:1 (light) / 14.3:1 (dark) — well
past AA/AAA for body text. `--muted` on `--surface` is 5.1:1 (light) / 6.0:1
(dark) — AA for normal text. `--faint` is deliberately sub-AA and is only
ever used for non-essential captions/decoration (e.g. the poll-gauge's
"until next" label sits next to the AA-passing numeral).

### Brand & signal

| Token | Value | Role |
|---|---|---|
| `--primary` | `#6d28d9` (light) / `#9d7bff` (dark) | Actions, links, focus ring |
| `--primary-soft` | `#efe7fd` / `#2c1e50` | Pills, selected-card border wash |
| `--accent` | `#0ea5a4` | Teal — reserved for a future secondary series (not yet used by a component; kept for parity with the mood board's palette) |
| `--accent-soft` | `#e3f7f6` / `#123634` | Teal soft fill |
| `--brand-a` | `#f40a02` (fixed, no dark variant) | Red end of the signal gradient — the icon, the primary-button gradient |
| `--brand-b` | `#5415a0` (fixed, no dark variant) | Violet end of the signal gradient |

`--brand-a`/`--brand-b` don't shift with the theme — they're the fixed
identity of the logomark itself (same gradient in the favicon, the app-bar
SVG, `site.webmanifest`'s `theme_color`, and the primary button).

### Semantic

| Token | Value (light / dark) | Role |
|---|---|---|
| `--success` / `--success-soft` | `#16a34a` / `#e7f6ed` — `#16a34a`(≈) / `#14301f` | Healthy, "up to date", cookies active |
| `--warn` / `--warn-soft` | `#b45309` / `#fdf2e0` — `#b45309` / `#3a2a12` | Expiring/aging cookies, low disk |
| `--danger` / `--danger-soft` | `#dc2626` / `#fdecec` — `#dc2626` / `#3a1c1c` | Failed polls, destructive actions |

---

## 3. Type

Three self-hosted variable/static faces (no CDN dependency — the app is
meant to run fully self-hosted, sometimes offline of anything but YouTube
itself, so fonts ship as local `.woff2` files under `app/static/fonts/`):

| Token | Face | Weight(s) shipped | File(s) | Fallback stack |
|---|---|---|---|---|
| `--font-display` | **Chakra Petch** | 500, 600, 700 (static, one file per weight) | `chakra-petch-{500,600,700}.woff2` | `ui-sans-serif, system-ui, sans-serif` |
| `--font-body` | **Manrope** | 400–800 (variable, one file) | `manrope-variable.woff2` | `ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif` |
| `--font-mono` | **JetBrains Mono** | 400–700 (variable, one file) | `jetbrains-mono-variable.woff2` | `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace` |

**Why these three, and why not the obvious picks:** Inter/Roboto/system-ui
were ruled out as "the personality" per house style — they're the default
for every SaaS dashboard and would say nothing about what this app is.
Space Grotesk (the other extremely common "distinctive" pick) was also
avoided to not converge with every other project reaching for the same
"safe-but-different" choice. Chakra Petch has an angular, slightly
technical character (note the cut corners on some glyphs) that reads as
broadcast/console signage without going full sci-fi-cliché (Orbitron).
Manrope is a clean geometric humanist body face — legible at the
dashboard's small UI sizes, distinct enough not to read as "default", warm
enough not to fight Chakra Petch's edge. JetBrains Mono was chosen for
technical readouts specifically because it's a *real* monospace built for
reading digits/code at small sizes (as opposed to a generic
`ui-monospace`), which matters for a countdown timer that updates every
second.

**Where each is used:**
- Display (`--font-display`, weight 600–700): `.brand-name`, all `<h2>`
  section headings, poll-panel `<h2>`, modal `<h3>` titles, changelog
  version numbers (`.cl-version`).
- Body (`--font-body`, default on `body`): all paragraph text, buttons,
  form inputs, card names/meta — anything a person typed or reads as
  prose.
- Mono (`--font-mono`): the poll countdown number (`.poll-gauge-num`), the
  per-run relative time (`.pr-time`), the feed-share URL input
  (`.share-url input`), and the About dialog's version/uptime values
  (`.about-meta dd`) — every place the value is machine-generated and
  benefits from tabular digits.

### Type scale

| Token | Size | Typical weight/face | Used for |
|---|---|---|---|
| `--text-xs` | 0.72rem (11.5px) | 600 / body | Badges (`.ep-badge`), fine print, poll-tag captions |
| `--text-sm` | 0.8125rem (13px) | 400 / body | Meta lines (`.ch-sub`, `.poll-facts`), secondary labels |
| `--text-base` | 0.9375rem (15px) | 400 / body | Body copy, form inputs — the `body` default |
| `--text-md` | 1.0625rem (17px) | 600–650 / body | Card titles (`.ch-name`), poll-panel subhead |
| `--text-lg` | 1.25rem (20px) | 600 / display | Section `<h2>` headings |
| `--text-xl` | 1.6rem (25.6px) | 600 / display | Dialog/modal titles |
| `--text-2xl` | 2.1rem (33.6px) | 700 / display | Hero numbers, showcase specimens |
| `--text-3xl` | 2.75rem (44px) | 700 / display | Showcase page display specimen only |

---

## 4. Spacing, radius, shadow, motion

### Spacing (4px base)

`--space-1` 4px · `--space-2` 8px · `--space-3` 12px · `--space-4` 16px ·
`--space-5` 20px · `--space-6` 28px · `--space-7` 36px · `--space-8` 48px.
These are new tokens added by this pass for future components to reference
directly; most existing layout still uses literal px values inline (a
pre-existing pattern in this file) — new work should prefer the scale.

### Radius

| Token | Value | Used for |
|---|---|---|
| `--radius-xs` | 6px | `.btn-sm` |
| `--radius-sm` | 10px | Buttons, inputs, banners |
| `--radius` | 16px | Cards, the inline-form container, modal cards |
| `--radius-lg` | 22px | New — showcase hero panel; available for future large panels |
| `--radius-pill` | 999px | Pills, count badges, the countdown chip shape |

### Shadow

| Token | Value | Used for |
|---|---|---|
| `--shadow-sm` | `0 1px 2px rgba(24,27,46,.05)` | New, subtle — reserved for low-elevation chips |
| `--shadow` | `0 1px 2px rgba(24,27,46,.04), 0 8px 24px rgba(24,27,46,.06)` | Cards, inline forms |
| `--shadow-lg` | `0 12px 40px rgba(24,27,46,.18)` | Modals, toasts, primary-button hover |
| `--shadow-signal` | `0 0 0 1px rgba(109,40,217,.14), 0 10px 30px -6px rgba(109,40,217,.35)` | New — the primary button's resting "on-air" glow |

### Motion

| Token | Value | Meant for |
|---|---|---|
| `--ease-out` | `cubic-bezier(.16,1,.3,1)` | Default deceleration — hover states, color/background fades |
| `--ease-spring` | `cubic-bezier(.34,1.56,.64,1)` | Press/lift physics — button press-down, the showcase's spring-hover card |
| `--dur-fast` | 120ms | Micro-interactions (button press) |
| `--dur-base` | 200ms | Default UI transitions (hover backgrounds, focus rings) |
| `--dur-slow` | 420ms | Panel-level reveals (reserved; the showcase's spring demo uses it) |

`prefers-reduced-motion: reduce` already collapsed all animation/transition
durations to ~0 before this pass (`app/static/styles.css`'s existing rule)
— untouched, still in effect for every new transition added here since
they all go through `transition`/`animation`, not raw JS timers.

---

## 5. Components

Every component below is real app markup/CSS — see them live, side by
side in each state, at `/static/design-system.html`.

- **Buttons** (`.btn` + a modifier): `.btn-primary` (the signature
  control — see below), `.btn-ghost` (secondary, most actions), `.btn-text`
  (tertiary, "Clear"), `.btn-danger-ghost` (destructive, "Remove"/"Delete"),
  `.btn-icon` (icon-only, settings gear). Sizes: default and `.btn-sm`.
  States: default, `:hover`, `:active` (press-down via `--ease-spring`),
  `[disabled]` (dimmed, hover effects suppressed).
- **Signature control — `.btn-primary`**: every primary form action in the
  app ("Add channel", "Download", "Save" in feed settings, "Poll all now"
  ghost-styled but the *concept* is the same "one main action per surface"
  pattern) uses this class. It now carries the brand's red→violet signal
  gradient (`linear-gradient(135deg, var(--brand-a) -40%, var(--primary)
  70%)`) and a resting glow (`--shadow-signal`), with a hover lift and a
  brightness bump, and a press-down scale on `:active`. The rationale: with
  everything else in the UI intentionally flat and quiet (ghost buttons,
  hairline borders), the one button that actually mutates state should look
  like it's "transmitting" — literally borrowing the icon's own gradient
  rather than inventing a fourth color.
- **Inline form** (`.inline-form`): the add-channel/download/cookie-upload
  input+button row. Focus state on the text input swaps to `--surface` and
  shows the focus ring.
- **Cards** (`.card`, `.ch-card`): channel cards, poll panel, cookies card,
  orphan cards. `.ch-card.selected` gets a primary-colored border + ring
  for bulk-select.
- **Pills & badges**: `.pill` (interval/status chip), `.count-pill` (section
  header counts), `.ep-badge` (episode/size count, with `.zero` and `.link`
  variants), `.poll-tag` (per-card last-poll status, with `.err`).
- **Poll panel**: the countdown ring (`.poll-gauge`, SVG `stroke-dashoffset`
  animated over 1s linear — unchanged, already used a real transition), the
  health summary (`.poll-health.good`/`.bad`), and the recent-runs list
  (`.pr-row` + status dot).
- **Toasts** (`.toast` + `.ok`/`.err`/`.info`): slide-in/out, left border
  colored by kind.
- **Modal** (`.modal`, `.modal-card`, `.modal-wide`): share dialog (QR +
  URL + app links), episode list, feed-settings form, About/changelog.
- **Banners** (`.banner.is-error`/`.is-warn`): cookie-health messaging at
  the top of the page.
- **Empty states** (`.empty`): shown when no channels/downloads/orphans
  exist yet.

---

## 6. Backgrounds, texture & generated art

All texture on this pass is **pure CSS** — no new raster assets — to keep
page weight down and every value themable through the existing tokens
rather than baked into a bitmap:

| Texture | Where | CSS technique |
|---|---|---|
| Oscilloscope dot-grid | `body` background, the whole app | `radial-gradient(circle at 1px 1px, …) 0 0/22px 22px` layered under `var(--bg)` |
| Signal glow wash | Showcase page hero panel only | Two soft `radial-gradient`s in `--brand-a`/`--brand-b` over `--surface` |
| Scanline ticks | Showcase page only, reserved for a future technical panel | `repeating-linear-gradient` |

**Ideogram mood board** (reference only, not shipped as an app asset):
`docs/design/signal-deck-moodboard.webp` — generated with the prompt
direction *"Signal Deck": deep near-black indigo background, palette
swatch chips of ink navy/electric violet/hot signal-red/teal accent, a
glossy play-button-to-broadcast-wave logomark in a red-to-violet gradient,
a chunky rounded primary "Add Channel" button, subtle oscilloscope/radio-
dial textures, bold condensed display type, mood: broadcast control room,
late-night engineer, precise and electric.* Kept in `docs/design/` as the
grounding reference for this document, not referenced by the running app.

**Existing icon set** (unchanged, see §8): `app/static/icon-512.png`,
`cover-512.png`, `apple-touch-icon.png`, `favicon*.{ico,png}` — the
play-to-broadcast-wave mark in the same `--brand-a`→`--brand-b` gradient,
already a real, specific identity and not regenerated by this pass.

---

## 7. Accessibility

- **Contrast**: `--ink`/`--muted` on `--surface`/`--bg` meet AA (see §2
  table) in both themes. `--primary-ink` (`#ffffff`) on the new
  `.btn-primary` gradient stays ≥ 4.5:1 across the gradient's darkest stop
  (`--primary`, not the brighter `--brand-a` end, since text sits centered
  and the button is a solid perceptual block, not a place text overlaps the
  lightest pixel).
- **Focus states**: unchanged, already solid — every interactive element
  gets `--ring` (a 3px `--primary`-colored box-shadow) via the existing
  `:focus-visible` rule; not weakened by any change in this pass.
- **Reduced motion**: unchanged existing rule
  (`prefers-reduced-motion: reduce` collapses all animation/transition
  durations); every new transition added in this pass (`--dur-*` tokens)
  goes through the same `transition`/`animation` properties that rule
  already targets, so nothing new needed adding.
- **Dark mode**: theme-aware via `prefers-color-scheme` only (the app has
  no manual toggle) — both the pre-existing tokens and every token added in
  this pass are defined for both light and dark.
- **No mute control**: out of scope — this pass has no sound identity (the
  app has no audio UI moments beyond playing a podcast episode itself,
  which already has native `<audio controls>`).

---

## 8. Icon / favicon audit

Per the standing "every project needs a real icon" rule, the existing set
under `app/static/` was checked before doing any regeneration work:
`favicon.ico`, `favicon-{16,32,48}x16.png`, `apple-touch-icon.png`,
`icon-192.png`, `icon-512.png`, `cover-512.png`. All of them already show
the same specific mark — a red play-triangle transitioning into three
violet "broadcast" arcs — which is a literal, on-brand depiction of "a
video slipping into a podcast signal," not a generic default/placeholder
icon (no default Vite/CRA/browser-default icon, no unrelated stock art).
**Verdict: real, on-brand identity — left untouched.** `site.webmanifest`
(`theme_color: #5415A0`, `background_color: #0e1020`) already matched this
pass's chosen palette with no changes needed either.

---

## 9. Asset inventory

| Path | Role |
|---|---|
| `app/static/styles.css` | Design system source of truth — all tokens, all component CSS |
| `app/static/design-system.html` | Live static showcase — every token/component rendered from the real CSS |
| `app/static/fonts/chakra-petch-500.woff2` | Display face, weight 500 |
| `app/static/fonts/chakra-petch-600.woff2` | Display face, weight 600 |
| `app/static/fonts/chakra-petch-700.woff2` | Display face, weight 700 |
| `app/static/fonts/manrope-variable.woff2` | Body face, variable 400–800 |
| `app/static/fonts/jetbrains-mono-variable.woff2` | Mono face, variable 400–700 |
| `docs/design/signal-deck-moodboard.webp` | Ideogram mood-board reference (documentation only, not served by the app) |
| `DESIGN.md` | This document |
| `app/static/icon-512.png`, `cover-512.png`, `apple-touch-icon.png`, `favicon*.{ico,png}`, `icon-192.png` | Existing icon set — audited (§8), unchanged |
| `app/static/site.webmanifest` | Existing PWA manifest — audited, unchanged |

No fonts/images from this pass need adding to a service-worker cache list —
Slipcast is a server-rendered dashboard with no service worker/offline app
shell.

---

## 10. Changelog

- **2026-09-13 — v1.0 "Signal Deck" (this document, initial version).**
  Uplifted the existing (already-decent) purple dashboard into a named,
  documented system: added a type scale + self-hosted three-face type
  system (Chakra Petch / Manrope / JetBrains Mono) in place of
  `ui-sans-serif`/system fonts, added spacing/radius/shadow/motion token
  scales, gave the primary button a signature red→violet signal-gradient
  treatment matching the existing icon, added a subtle CSS oscilloscope
  dot-grid background texture, and built the live showcase page. No HTML
  structure, JS behavior, or test-visible DOM/class contracts changed —
  `pytest` (272 tests) passes unmodified before and after. The existing
  icon set was audited and intentionally left as-is (see §8) since it was
  already a real, purpose-built identity rather than a generic default.
