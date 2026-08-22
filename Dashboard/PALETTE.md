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

## Verifying a change

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
