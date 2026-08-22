# The palette engine — how a background image becomes the board's colours

`package/palette.js` derives the dashboard's entire colour scheme from whatever
image you set as the background. This document is the *why*, because most of
the decisions in there look arbitrary until you see the numbers they came from.

## What was wrong with v1

The old extractor (inline in the `.dc.html`) put every pixel into one of 18
hue bins 20° wide, took the strongest bin, and manufactured the other four
colours by fixed rotations off it — `+330°`, `+45°`, `+18°`, `+70°`.

Run against real images it failed four separate ways:

| Input | v1 result |
|---|---|
| solid black | `null` → board silently **reset to the shipped coral palette** |
| solid white | `null` → same |
| dark navy `#0c163a` | `accent #3e5df9` — **neon**, from saturation forced to a floor the picture never had |
| random colour static | `accent #ddc2a6` |
| a desert photo | `accent #ddc2a6` — **byte-identical to the static** |
| sunset ridges | `orb #90a762` — **olive green**, a colour absent from the image |

Two of those are worth restating. Nothing measured whether a dominant hue
*existed*, so a meaningless image and a real photo produced the same palette.
And because three of five colours were rotations rather than observations, the
sunset's actual navy ridge was discarded in favour of an invented olive.

## The pipeline

1. **64×64 box downsample** → 4096 samples. Area-averaged, not point-sampled:
   without that, fine-grained images alias and every statistic below becomes
   noise.
2. **sRGB → OKLab.** Perceptually uniform, so a hue rotation rotates hue and a
   lightness clamp changes lightness only. In HSL neither is true.
3. **Deterministic k-means, k=6.** Farthest-point seeding, no `Math.random`
   anywhere — one image always yields exactly one palette.
4. **Statistics → a scheme.** See the table below.
5. **Surfaces from the image's own depth**, accents per scheme.
6. **Contrast fitting** on every text and accent role before it is returned.

## The four schemes, and the numbers that separate them

```
image                   conc   qErr    Chi  maxSal  grp   Lmean   -> scheme
sunset ridges           0.46  0.008  0.127  0.0600    4    0.56      rich
duotone (red/cyan)      0.29  0.000  0.205  0.1446    2    0.63      rich
desert photo            0.33  0.007  0.078  0.0339    2    0.84      rich
bg-dusk.png (shipped)   0.33  0.017  0.067  0.0326    2    0.59      rich
forest                  0.99  0.008  0.081  0.0307    1    0.35      mono
teal gradient           1.00  0.006  0.073  0.0304    1    0.38      mono
solid dark blue         1.00  0.000  0.071  0.0710    1    0.22      mono
random colour blocks    0.10  0.083  0.211  0.0775    5    0.63      chaotic
per-pixel colour static 0.04  0.017  0.043  0.0137    2    0.60      achromatic
solid black             0.00  0.000  0.000  0.0000    0    0.00      achromatic
solid white             1.00  0.000  0.000  0.0000    0    1.00      achromatic
```

- **`maxSal < 0.020` → achromatic.** Salience is chroma weighted by area
  coverage: does the image *have* a colour at all. Every real image above scores
  ≥ 0.0304; black, white and per-pixel static score ≤ 0.0137. Classifying
  per-pixel noise as achromatic is not a fudge — averaged over any real viewing
  distance that wallpaper **is** grey.
- **`groups ≥ 4 && conc < 0.20` → chaotic.** `groups` counts hue families
  (single-link, 34° tolerance); `conc` is how much those families agree.
  **Both halves are required**: the sunset also has 4 families, but they agree
  (0.46 vs 0.10). Neither number alone separates confetti from a sunset.
- **`conc > 0.72 || groups ≤ 1` → mono.** One hue family: a solid colour, a
  duotone photograph.
- **otherwise → rich.** Two or more genuine, separated hues — take the image's
  actual colours.

### When the image runs out of hues

`rich` takes up to three clusters, each **≥34°** from the ones already chosen.
Plenty of real images cannot supply three: a red/cyan duotone has two, and a
desert photo has one usable hue plus the board's own.

v1's answer — and this engine's, until it was measured — was to **rotate +40°
off accent1** for the remainder. That is the same fabrication the rewrite
existed to remove: it gave the red/cyan duotone a *lavender* label and the
blue-and-sand desert a *green* one, neither colour present anywhere in the
picture. It fired on 2 of the 5 `rich` test images.

The slot is now filled with a **tinted neutral**: accent1's own hue at 0.050
chroma (0.030 for a second one), at the lightness midway between `--text` and
`--text-dim`. It reads as deliberate — this image has one usable colour, so the
rest of the hierarchy is carried by neutrals — and it can neither clash nor turn
to mud.

Deliberately a *tint*, not a pure grey. Both sit at the same lightness and
therefore separate from body text equally by luminance (≈1.3:1), but `--text`
is nearly achromatic, so only the tinted version stays distinguishable by
**chroma** as well. A pure near-white/near-black label reads as a slightly
dimmer copy of the body text.

The flag is carried explicitly through `pickAccents`, not inferred from a
chroma threshold: sniffing it would break silently the first time any scheme's
chroma dipped below the threshold, turning a real accent into a neutral with
nothing to notice it.

Note this is *not* triggered by the 30° board-hue penalty, which fires on 4 of 5
rich images and is benign — lightness already separates an accent from the board
it sits on (the sunset's periwinkle `accent3` is 27° from the board hue, real,
and looks right).

One boundary is genuinely fuzzy, and deliberately so. Whether colour *noise*
reads as `achromatic` or as `chaotic` depends on how much of its chroma survives
the 64x64 downsample, which is a function of the source resolution: a 4K noise
wallpaper averages to grey, a 240x160 one keeps visible colour. Both branches
produce the same restrained house palette (they differ only in chroma), so the
distinction has no visual consequence - which is why `palette-check.html`
accepts either for a noise image. What must never happen is noise being read as
a *real* scheme and allowed to dictate a hue.

Achromatic and chaotic both fall back to Cortana's identity hues at restrained
chroma. That is the deliberate answer to "there is no colour here" and to "there
are too many colours here" alike: a colourless or a frantic background must
never dictate the UI, and a house colour is more honest than an average.

## Contrast is enforced, not hoped for

Every text and accent role is lightness-fitted until it clears its WCAG target
against **`--border-rgb`** — the far end of the surface ramp, and therefore the
least contrasty thing any colour can land on. Fitting against `--bg-rgb` alone
let colours clear 4.5:1 on the page and quietly fail inside a card.

The fit runs on the **rounded 8-bit** colour. Fitting in float and quantising
afterwards lands a hair under target, and the pixel that ships is the rounded
one.

Targets: text 9:1, dim text 4.6:1, every accent 4.5:1. Across all four schemes
and all eleven test images that is 0 failures on all four surfaces.

Out-of-gamut colours are **desaturated**, never channel-clipped. Clipping shifts
hue, and it is exactly what made v1 look radioactive.

## The tokens

| Token | Role |
|---|---|
| `--bg-rgb` | the board |
| `--surface-rgb` | a module |
| `--surface2-rgb` | a raised well inside a module |
| `--border-rgb` | solid divider; also the contrast reference |
| `--panel-rgb` | frosted-glass wash over the wallpaper (near-white in both polarities) |
| `--hairline-rgb` | 1px strokes; flips dark on a light board |
| `--text-rgb` / `--text-dim-rgb` | body and secondary text |
| `--accent-rgb` | primary |
| `--accent2-rgb` | rose / secondary |
| `--accent3-rgb` | lavender / small mono labels |
| `--peach-rgb` | highlight |
| `--orb-hi-rgb` / `--orb-mid-rgb` / `--orb-rgb` | the sphere's three gradient stops, highlight to core |

**The sphere has its own three stops on purpose.** It used to borrow the
`peach` / `accent2` / `orb` *text* colours. On a light board those roles have to
go dark, and the sphere turned into a mud-coloured blob. The orb is a graphic,
not text: it owes only ~3:1 against the board and must read as lit from the
upper left in either polarity. Those three stops are also exactly what the
bubble and the phone are handed, so all three spheres match without any of them
re-deriving anything.

A bright image (`Lmean ≥ 0.68`) produces a **light board** — dark text on a pale
ramp. The threshold is deliberately high: this design's home turf is dark, so
mid-bright images stay dark.

## Where the palette travels

The board is the only writer. Everything else reads.

```
  background image
        │
        ▼
  palette.js  ──▶  the page's localStorage  (dusk.bg.v1 / dusk.theme.v1)
        │
        ├── IPC  theme:set ──▶ main.js ──▶ userData/theme.json ──▶ bubble orb
        │                              └──▶ the window's own frame colour
        │
        └── board snapshot ──▶ cortana-bridge ──▶ phone: cards, labels,
                                                  spheres, 2x2 widget
```

Both hops validate the tokens with the same `^\d{1,3},\d{1,3},\d{1,3}$` shape,
in `Dashboard/app/main.js` and in `bridge/state.py`. `test_theme_tokens.py`
compares those two lists against the set `palette.js` actually emits *and*
against `THEME_DEFAULTS`, because a token added to the engine and missed in a
guard is dropped **silently** — and the only symptom is a phone or a bubble
wearing half the theme, on a machine this code isn't written on.

Three things deliberately do **not** travel:

- **The background image.** Colours cross the bridge; wallpaper does not. The
  phone gets a ~500 byte token blob, not a photo.
- **State colours.** Green means listening and grey means offline in every
  palette. A dead assistant must not look like a healthy one wearing today's
  colours.
- **The Android launcher icon.** Android resolves it from the manifest at
  install time and gives an app no way to repaint its own icon at runtime. The
  2×2 widget sphere *is* repainted, and it is the closest themed thing the home
  screen can have. `mobile/.../res/drawable/sphere.xml` survives only to feed
  that static icon; every sphere you can see while the app runs is built by
  `Theme.sphere()`.

The phone only receives a palette while the dashboard has the **MOBILE LINK**
module on the board — the tokens ride in that module's board snapshot, exactly
as tasks and the weather ZIP already do. The bridge keeps the last good palette,
so closing the dashboard leaves the phone's colours standing rather than
snapping it back to the built-in defaults.

## Verifying a change

The quickest check needs no tooling at all: open **`package/palette-check.html`**
in any browser, including on the Linux box. It runs the engine over eight
generated images covering all four schemes and both extremes, audits every role
against all four surfaces, and prints PASS or FAIL with the numbers. It also has
a drop zone - drag a wallpaper in to see exactly what palette it would produce
before committing to it on the board.

Its test images are generated in-canvas on purpose: loading a `file://` image
into a canvas taints it and `getImageData` throws, so a check that depended on
an asset would fail for reasons unrelated to the palette. Dropped files arrive
as data URLs, which do not taint.

The engine is browser JS, so it **can** be run and checked on the Windows dev
box — only the Electron shell can't be. Chrome and Edge are both present:

```bash
chrome.exe --headless=new --disable-gpu --no-sandbox \
  --virtual-time-budget=20000 --dump-dom "file:///.../harness.html" > dom.txt
```

Embed test images as **data: URIs** — a canvas drawn from a data URL is not
tainted, so `getImageData` works without loosening Chrome's `file://` policy.

You can also screenshot the real board under an injected palette:

```bash
chrome.exe --headless=new --disable-gpu --window-size=1280,820 \
  --screenshot=out.png "file:///.../Dusk%20Dashboard.dc.html"
```

One trap, and it cost a full round: **inject the palette as inline custom
properties on `documentElement`, not as a `<style>` block.** `applyThemeVars()`
sets them inline at mount, an inline property outranks any stylesheet rule, and
an injected `:root` is silently ignored — the screenshot then shows the defaults
while looking like it worked.

Copies of the page placed outside `package/` must have their relative resource
refs rewritten to absolute, or `support.js` and the vendored React 404, the DC
runtime never boots, and you screenshot a raw `{{ template }}`.
