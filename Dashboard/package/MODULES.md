# Dusk Dashboard — Module Author Guide (for AI agents)

---

## ⚡ BOOTSTRAP PROMPT — paste this into any new AI chat, along with the files

> You are a module author for **Dusk Dashboard**, a finished single-file
> modular dashboard product. You will receive these files:
> `Dusk Dashboard.dc.html` (the product), `support.js` (runtime — never edit),
> `assets/bg-dusk.png` (default background), and `MODULES.md` (the contract).
>
> **Before writing anything: read MODULES.md in full.** It defines the only
> approved way to work. Then:
>
> 1. **Your job is to ADD or MODIFY modules — nothing else.** You make exactly
>    three kinds of edit, all defined in MODULES.md §3: a `DEFS` entry, one
>    `<sc-if>` template block, and `renderVals` data. You may also seed
>    `DEFAULT_MODULES` if the user wants your modules on screen at first load.
> 2. **HARD STOPS (MODULES.md §2) are absolute.** Never touch the grid engine,
>    drag/link/surface/remove systems, sanitizers, persistence keys, or the
>    module wrapper markup. Never rename localStorage keys. Never edit
>    `support.js`. If a request seems to require breaking a hard stop, say so
>    and propose a module-level alternative instead.
> 3. **Match the house style:** inline styles only, `rem` font sizes,
>    `'JetBrains Mono'` for data / `'Space Grotesk'` for prose, and colors ONLY
>    via the theme variables in §4 (e.g. `rgb(var(--accent-rgb))`,
>    `rgba(var(--text-rgb),.6)`). One hardcoded hex = broken theming.
> 4. **User content must be editable where asked.** For editable fields (a
>    stock symbol, a title), store them in state, persist small data to your
>    own `dusk.<module>.v1` localStorage key (try/catch), and render an
>    `<input>` styled per existing modules. Live data: poll in
>    `componentDidMount`, clear intervals in `componentWillUnmount`, keep the
>    last good value on failure (§5).
> 5. **Before declaring done, run the full checklist in §8** (add / drag /
>    resize-to-min / surface cycle / link / double-tap remove / reload persist /
>    background swap recolor / zero console errors). Fix failures silently and
>    re-run.
>
> Example requests you should handle in ONE pass — the pattern is always the
> same regardless of content:
> - *"Create a dashboard with 8 stock tickers that are editable"* → add a
>   `stock` module type (editable symbol input + price + delta, polling per
>   §5), seed 8 in `DEFAULT_MODULES` in a clean grid arrangement.
> - *"Add an image slideshow module"* → add a `slideshow` type (multi-file
>   upload like the IMAGE module, interval-advanced `background-image`
>   crossfade, dots or arrows ≥1.7rem).
> - Any other content — inbox, RSS feed, countdown, webcam, sensor readout —
>   is the same three edits (§3) + the matching data pattern (§5) + edge-case
>   rules (§6).
> Always finish by running the §8 checklist and reporting anything that needs
> a key/URL from the user.

---

You are editing `Dusk Dashboard.dc.html`, a single-file modular dashboard.
This document is the contract. Follow it exactly and your module will drop in
cleanly: it will move, resize, link, animate, persist, and theme itself with
zero extra work from you.

---

## 1. Architecture in 60 seconds

- **One file.** Template (HTML between `<x-dc>` tags) + logic (`class Component extends DCLogic`). No external CSS, no CSS classes — **inline styles only**.
- **Grid engine.** 24 columns × 16 rows, percentage-based (scales laptop → 4K). Every module is `{ id, type, x, y, w, h, surface, link, img? }` in `state.modules`. Wrappers get `padding: gutter/2`, which is why modules can never visually touch.
- **Surfaces.** Every module renders on one of three surfaces the user cycles with ◐: `light` (warm glass), `dark`, `clear` (no background). Your content must be legible on all three — that means never rely on the card background for contrast.
- **Links.** Modules sharing a `link` string move as one group. The engine handles it; you never special-case it.
- **Theme.** All colors come from CSS variables set on `:root`. When the user changes the background image, a code-only palette extractor rewrites these vars. **Never hardcode accent hex values** — use the vars (§4).
- **Persistence.** localStorage keys: `dusk.layout.v3` (layout), `dusk.boot` (uptime epoch), `dusk.bg.v1` (custom background + extracted palette), `dusk.theme.v1` (user-pinned colors). Saved layouts pass through `sanitizeLayout()` on load — unknown types are silently dropped, coordinates clamped, module count capped at 40.

## 2. HARD STOPS — never modify

These are verified-good. Changing them breaks move/link/persist behavior for
every module, including yours.

| Never touch | Why |
|---|---|
| `COLS`, `ROWS`, `MAX_MODULES`, `LS_KEY`, `LS_BG` | Grid math + saved layouts depend on them |
| `bindDrag / unbindDrag / startDrag / cellSize / snapTarget / commitDrag` | The entire drag/resize/snap/group-move engine |
| `sanitizeLayout`, `migrateV2`, `persist`, `applyTheme`, `persistTheme` | Data safety on load/save + theme resolution order |
| `onLink`, `groupOf`, `cycleSurface`, `requestRemove`, `findFreeSlot`, `addModule` guards | Link/surface/remove/add semantics incl. double-tap remove and "no room" toast |
| The module wrapper `<div style="{{ m.wrapStyle }}" onPointerDown="{{ m.drag }}">` and the EDIT CONTROLS block | Every module inherits drag + controls from this shell |
| `wrapStyle` computation (transitions, popIn animation, z-index rules) | The "flawless add/remove/move" animations |
| `try/catch` around every localStorage call | Quota / privacy-mode resilience |
| Never call `localStorage.clear()` or remove keys you didn't write | Shared storage |
| Never use `scrollIntoView` | Breaks the host app |

Also fixed by design: resize minimum is 3w × 2h; grid lines only appear as a
fading halo during drag (no permanent grid lines); gutters are the only
spacing mechanism.

## 3. Adding a module type — the 3-step recipe

Example: a "QUOTE" module. Steps are always the same three edits.

**Step 1 — register it** in `DEFS` (default size + tray label):

```js
quote: { w: 6, h: 3, label: 'QUOTE' },
```

Add `'quote'` to the `addables` array in `renderVals` so it appears in the
edit tray.

**Step 2 — template block**, inside the `<sc-for list="{{ modules }}">` loop,
alongside the other `<sc-if>` blocks (before the EDIT CONTROLS comment):

```html
<!-- ═══ QUOTE ═══ -->
<sc-if value="{{ m.t_quote }}" hint-placeholder-val="{{ false }}">
  <div style="{{ m.cardStyle }}">
    <div style="font-family:'JetBrains Mono',monospace;font-size:.72rem;letter-spacing:.18em;color:rgb(var(--accent3-rgb))">QUOTE OF THE DAY</div>
    <div style="margin-top:.7rem;font-size:.95rem;line-height:1.7;color:rgba(253,243,236,.85)">{{ quoteText }}</div>
  </div>
</sc-if>
```

**Step 3 — logic**, in `renderVals`:

```js
t_quote: m.type === 'quote',   // add to the flags list in the modules .map()
// ...and at top level of the returned object:
quoteText: s.quote || 'No quote loaded yet',
```

That's it. Drag, resize, link, surface cycling, remove, persistence, and the
pop-in animation all work automatically.

### Card style menu (pick one, don't invent wrappers)
- `m.cardStyle` — padded block (most modules)
- `m.cardStyleCol` — padded flex column (charts: header + growing body)
- `m.cardStyleCenter` — centered column (orbs, big single values)
- `m.cardStyleRow` — centered row (clock-style)
- `m.cardStyleTicker` — horizontal strip, hidden overflow
- `m.cardStyleImage` — full-bleed media frame (dashed border when empty)

All include `overflow:hidden` — your content will clip, not leak, at small sizes.

## 4. Colors, type, sizing

**Theme variables** (RGB triplets, so alpha works: `rgba(var(--accent-rgb),.5)`):

| Var | Role | Default |
|---|---|---|
| `--accent-rgb` | Primary accent (coral) | 255,171,143 |
| `--accent2-rgb` | Secondary / warning (rose) | 240,138,155 |
| `--accent3-rgb` | Labels / tertiary (lavender) | 201,184,232 |
| `--peach-rgb` | Highlight | 255,217,196 |
| `--orb-rgb` | Orb depth tone | 143,123,184 |
| `--text-rgb` | Primary text (use `rgba(var(--text-rgb),.4–.85)` for dim text) | 253,243,236 |

Fixed (theme-independent): positive-delta green `#9be8b8`, destructive red
`rgba(240,80,90,…)`.

**Resolution order:** defaults ← auto-extracted from the background image ←
user-pinned colors (edit mode → COLORS panel, stored in `dusk.theme.v1`).
Pinned colors survive background swaps by design — never overwrite
`state.themeCustom` from code, and always route theme changes through
`applyTheme()` so pins keep winning.

**Type:** `'Space Grotesk'` (inherited) for prose; `'JetBrains Mono',monospace`
for data, labels, numbers. Section labels: `.72rem`, `letter-spacing:.18em`,
uppercase, `--accent3-rgb`. Root font scales with viewport
(`clamp(12px, .78vw, 22px)`) — **always use `rem`, never `px`, for font sizes**.

**Sizing:** design for your DEFS default, and make sure nothing critical is lost
at the 3×2 minimum (a title + one value must survive). Interactive targets ≥
`1.7rem`.

## 5. Importing data

Never fetch inside `renderVals` (it runs every render). Pattern:

```js
// componentDidMount — poll
this._myPoll = setInterval(async () => {
  try {
    const r = await fetch('https://api.example.com/data');
    if (!r.ok) throw new Error(r.status);
    this.setState({ myData: await r.json() });
  } catch (e) { /* keep last good value; optionally log to console module */ }
}, 60000);
// componentWillUnmount — ALWAYS clear
clearInterval(this._myPoll);
```

- **Keep last good value** on failure; show `—` only if you never got data.
- Reasonable poll rates: markets 15–60s, weather 10–15min, clock 1s.
- **From another agent / window:** listen for `postMessage`:

```js
this._onMsg = (e) => {
  const d = e.data;
  if (!d || d.source !== 'my-agent' || typeof d.payload !== 'object') return; // validate!
  this.setState({ agentFeed: d.payload });
};
window.addEventListener('message', this._onMsg);   // remove on unmount
```

- **User files:** follow the IMAGE module pattern (hidden `<input type="file">`
  inside a `<label>`, FileReader → dataURL). Cap at 3.5MB (`setImage` shows why).
- **Persisting your data:** small state (a note, a symbol list) may go in its
  own localStorage key `dusk.<yourmodule>.v1`, always in try/catch. Do NOT
  stuff large data into the layout objects — layout must stay small.

## 6. Edge cases — supported, with rules

**Images / GIFs** — render as `background-image` on a div (never a raw `<img>`
with a template hole in `src`). Animated GIFs work. ≤3.5MB dataURL, or reference
a project file / URL.

**Video** — supported with constraints:
```html
<video src="clips/loop.mp4" muted autoplay loop playsinline
  style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover;border-radius:13px"></video>
```
Must be `muted` (browsers block autoplay with sound). Never persist video data
to localStorage — reference files/URLs only. Add controls only for h ≥ 3 rows.

**Bar charts** — divs with `%` heights in a flex row (see CHART). Zero deps.

**Line graphs** — one simple SVG `polyline` is acceptable; compute points in
`renderVals`:
```js
linePts: s.series.map((v, i) =>
  `${i / (s.series.length - 1) * 100},${100 - v}`).join(' '),
```
```html
<svg viewBox="0 0 100 100" preserveAspectRatio="none" style="flex:1;width:100%">
  <polyline points="{{ linePts }}" fill="none" stroke="rgb(var(--accent-rgb))" stroke-width="2" vector-effect="non-scaling-stroke"></polyline>
</svg>
```
No charting libraries, no complex hand-drawn SVG illustrations.

**Tables** — CSS grid, mono font, `.8rem`, cap visible rows to what fits
(`m.h * 2 - 2` rows is a good rule); let overflow clip.

**Email / inbox / feeds** — list pattern like TASKS: unread dot
(`--accent-rgb`), sender in `#fdf3ec`, subject dim, `white-space:nowrap;
overflow:hidden;text-overflow:ellipsis` per row. Never render raw HTML email
bodies — text only.

**Web viewer (iframe)** — works with caveats you must handle:
```html
<iframe src="{{ m.url }}" sandbox="allow-scripts allow-same-origin"
  style="{{ m.frameStyle }}"></iframe>
```
- Compute `frameStyle` with `pointerEvents: s.edit ? 'none' : 'auto'` — otherwise
  the iframe swallows drag events in edit mode. This applies to ANY interactive
  embed (iframe, video with controls, canvas apps).
- Many sites (google.com, most banks/socials) refuse embedding
  (`X-Frame-Options`). You cannot detect this reliably — always show a visible
  URL label + "open ↗" fallback link so a blank frame isn't a dead end.

**Long text / scrollable content** — `overflow-y:auto` on an inner div (not the
card), plus a fade-out mask at the bottom:
`maskImage: 'linear-gradient(180deg, #000 85%, transparent)'`.

**Canvas / custom drawing** — use a `ref` created in the class, draw in
`componentDidMount` / on state change. Size via CSS `100%`; re-read
`canvas.clientWidth` before drawing.

## 7. NOT supported — don't attempt

- Audio autoplay, or any sound without an explicit user click.
- Storing media (video, large images) in localStorage — ~5MB quota total.
- Heavy WebGL/three.js scenes — this is a persistent ambient dashboard;
  budget ≈ zero idle CPU per module (one interval is fine).
- Browser permission APIs (notifications, geolocation, camera) without a
  user-gesture button that requests them.
- npm imports / `import` statements in the logic class. Plain JS only.
- New fonts. The two loaded families cover everything.
- Emoji as UI. Use the existing glyph set (◐ ∞ ✕ ◢ ⏮ ▶ ⏭ ▲ ▼ ▌).
- Absolute-positioning your content to escape the card — everything lives
  inside the card style you chose.

## 8. Pre-ship checklist (run every item)

1. Add your module from the tray — pops in with spring animation, lands in a
   free slot.
2. Drag it — fading grid halo appears, snap preview shows valid/invalid, drop
   commits or reverts.
3. Resize to 3×2 minimum — nothing important disappears; nothing overflows.
4. Cycle ◐ through light / dark / clear — text legible on all three.
5. Link it to another module (∞, then ∞) — they move together; unlink works.
6. Remove it — first ✕ arms (red), second removes, fade-out plays.
7. Reload — layout, surface, and link state persist; no console errors.
8. Change the background image — your module recolors via the vars
   (if any color didn't change, you hardcoded a hex — fix it).
9. Narrow window (~900px tall) — page scrolls, nothing overlaps the edit pill.
10. Check DevTools console — zero errors, zero uncleared intervals after remove.

## 9. Existing module types (reference implementations)

| type | size | demonstrates |
|---|---|---|
| clock, date | 4–5×2 | live intervals, centered row/column cards |
| ticker | 15×2 | nested `sc-for`, horizontal strip, up/down coloring |
| weather | 6×4 | static block layout (swap in a real API per §5) |
| cpu, ram | 6×2 | progress bars, linked-pair pattern |
| music | 6×3 | interactive buttons inside a draggable card (`stopPropagation` via handlers) |
| iris | 6×5 | clear surface, animated orb, computed live text |
| tasks | 6×4 | clickable list rows mutating state |
| agenda, notes | 6×3–4 | text list patterns |
| image | 6×3 | file upload, dataURL, empty-state with `+` |
| chart | 6×3 | div bar chart, theme-var gradients |
| timer | 6×3 | mono counter |
| console | 24×3 | log feed, blinking caret, dark surface default |
| git | 7×5 | bridge-backed polling (`duskBridge.gitStatus`), list + status dot |
| youtube | 8×5 | imperatively-mounted iframe (§6), editable URL input |
| mobile | 6×4 | loopback `fetch` to a local service, canvas QR, "?" help overlay |

**Note on `<select>`:** the template is parsed through `innerHTML`, and HTML
parsers strip `sc-for` out of a `<select>`, which corrupts the whole render.
Use a click-to-cycle control instead (see the MIC picker in the `cortana`
module) — never a native dropdown.
