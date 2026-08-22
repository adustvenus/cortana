/* Dusk palette engine v2 — derive a whole colour scheme from a background image.
 *
 * ENGINE FILE. Not a module; see MODULES.md. Cortana's self-edit layer must not
 * touch this (selfedit.py PROTECTED).
 *
 * WHY THIS REPLACED THE OLD EXTRACTOR
 * The v1 extractor binned hues 20 degrees wide, took the single strongest bin,
 * and produced the other four colours by fixed rotations off it (+330, +45,
 * +18, +70). Measured against real images that failed four ways:
 *   - a solid black or white background returned null and reset the board to
 *     the shipped coral palette;
 *   - a dark navy came out as #3e5df9 neon, because saturation was forced to a
 *     floor the picture never had;
 *   - random colour static and a desert photo produced BYTE-IDENTICAL palettes,
 *     since nothing measured whether a dominant hue existed at all;
 *   - three of the five colours were rotations, so a sunset's real navy ridge
 *     was discarded and the orb came out olive green.
 *
 * THE PIPELINE
 *   1. 64x64 downsample                       -> 4096 samples
 *   2. sRGB -> OKLab, so rotations and lightness clamps behave perceptually
 *   3. deterministic k-means (k=6)            -> the image's actual colours
 *   4. statistics -> a scheme: rich | mono | chaotic | achromatic
 *   5. surfaces built from the image's own depth, accents per scheme
 *   6. every text/accent role contrast-fitted before it is returned
 *
 * The thresholds in chooseScheme() are measured, not guessed - the table of
 * images each one separates is in PALETTE.md.
 *
 * Determinism matters and is deliberate: no Math.random anywhere, and k-means
 * is seeded by farthest-point init, so one image always yields one palette.
 */
(function (global) {
  'use strict';

  var K = 6;              // clusters
  var GRID = 64;          // sample resolution
  // Cortana's identity hues, used ONLY when the image offers no usable hue.
  var HOUSE = { accent: 26.0, accent2: 8.0, accent3: 300.0 };
  // Temperature anchors for placing a counterpoint accent in mono schemes.
  var WARM = 42.0, COOL = 250.0;
  // A TINTED NEUTRAL fills an accent slot the picture cannot supply: accent1's
  // hue at a fraction of the chroma. Deliberately not a pure grey - --text is
  // nearly achromatic, so a tint stays distinguishable from body copy at a
  // lightness where a grey would not.
  var NEUTRAL_C = [0.050, 0.030];
  // Lightness midway between --text and --text-dim (dark board, light board):
  // where a small label belongs - clearly under body copy, clearly over
  // secondary text.
  var NEUTRAL_L = [0.88, 0.34];

  // ── sRGB <-> linear ───────────────────────────────────────────────────────
  function lin(c) { return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); }
  function gam(c) { return c <= 0.0031308 ? c * 12.92 : 1.055 * Math.pow(c, 1 / 2.4) - 0.055; }
  function cbrt(x) { return x < 0 ? -Math.pow(-x, 1 / 3) : Math.pow(x, 1 / 3); }

  // ── sRGB <-> OKLab ────────────────────────────────────────────────────────
  function rgb2oklab(r, g, b) {
    r = lin(r); g = lin(g); b = lin(b);
    var l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b;
    var m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b;
    var s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b;
    var l_ = cbrt(l), m_ = cbrt(m), s_ = cbrt(s);
    return [0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_];
  }
  function oklab2rgb(L, a, b) {
    var l_ = L + 0.3963377774 * a + 0.2158037573 * b;
    var m_ = L - 0.1055613458 * a - 0.0638541728 * b;
    var s_ = L - 0.0894841775 * a - 1.2914855480 * b;
    var l = l_ * l_ * l_, m = m_ * m_ * m_, s = s_ * s_ * s_;
    return [gam(+4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
            gam(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
            gam(-0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)];
  }

  // ── OKLCh ─────────────────────────────────────────────────────────────────
  function lab2lch(L, a, b) {
    return { L: L, C: Math.sqrt(a * a + b * b), h: (Math.atan2(b, a) * 180 / Math.PI + 360) % 360 };
  }
  function lch2lab(L, C, h) {
    var r = h * Math.PI / 180;
    return [L, C * Math.cos(r), C * Math.sin(r)];
  }
  function inGamut(rgb) {
    for (var i = 0; i < 3; i++) if (rgb[i] < -1e-4 || rgb[i] > 1 + 1e-4) return false;
    return true;
  }
  /* OKLCh -> sRGB with the chroma reduced until the colour actually fits the
   * gamut. Never channel-clipped: clipping shifts hue and is exactly what made
   * the old palette look radioactive. We desaturate until the colour is real. */
  function lch2rgb(L, C, h) {
    var lo = 0, hi = C;
    if (inGamut(oklab2rgb.apply(null, lch2lab(L, C, h)))) lo = C;
    else {
      for (var i = 0; i < 24; i++) {
        var mid = (lo + hi) / 2;
        if (inGamut(oklab2rgb.apply(null, lch2lab(L, mid, h)))) lo = mid; else hi = mid;
      }
    }
    return oklab2rgb.apply(null, lch2lab(L, lo, h)).map(function (c) {
      return Math.min(1, Math.max(0, c));
    });
  }
  function lch2rgb255(L, C, h) {
    return lch2rgb(L, C, h).map(function (c) { return Math.round(c * 255); });
  }
  function rgbStr(a) { return a[0] + ',' + a[1] + ',' + a[2]; }

  // ── WCAG 2.1 contrast ─────────────────────────────────────────────────────
  function luminance(rgb01) {
    return 0.2126 * lin(rgb01[0]) + 0.7152 * lin(rgb01[1]) + 0.0722 * lin(rgb01[2]);
  }
  function contrast(a, b) {
    var la = luminance(a), lb = luminance(b);
    if (la < lb) { var t = la; la = lb; lb = t; }
    return (la + 0.05) / (lb + 0.05);
  }
  function to01(rgb255) { return [rgb255[0] / 255, rgb255[1] / 255, rgb255[2] / 255]; }

  /* Buy contrast with lightness only - hue and chroma are the colour's
   * identity. The test runs on the ROUNDED 8-bit colour: fitting in float and
   * quantising afterwards lands a hair under target, and the pixel that ships
   * is the rounded one. */
  function fitContrast(L, C, h, bg01, target, preferLight) {
    function ok(x) { return contrast(to01(lch2rgb255(x, C, h)), bg01) >= target; }
    if (ok(L)) return L;
    var lo = preferLight ? L : 0, hi = preferLight ? 1 : L, i;
    for (i = 0; i < 20; i++) {
      var mid = (lo + hi) / 2;
      if (ok(mid) === preferLight) hi = mid; else lo = mid;
    }
    var out = preferLight ? hi : lo;
    var step = preferLight ? 0.002 : -0.002;
    for (i = 0; i < 60; i++) {                 // quantisation is not perfectly
      if (ok(out)) return out;                 // monotone; walk the last stretch
      out = Math.max(0, Math.min(1, out + step));
    }
    return out;
  }

  // ── 1-2. sample ───────────────────────────────────────────────────────────
  function sample(img) {
    var c = document.createElement('canvas');
    c.width = GRID; c.height = GRID;
    var ctx = c.getContext('2d', { willReadFrequently: true });
    // Area-average the picture down rather than point-sample it: without this
    // a fine-grained image aliases and the cluster statistics become noise.
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(img, 0, 0, GRID, GRID);
    var d = ctx.getImageData(0, 0, GRID, GRID).data, pts = [];
    for (var i = 0; i < d.length; i += 4)
      pts.push(rgb2oklab(d[i] / 255, d[i + 1] / 255, d[i + 2] / 255));
    return pts;
  }

  // ── 3. deterministic k-means in OKLab ─────────────────────────────────────
  /* Lightness is deliberately down-weighted: two colours of one hue at
   * different exposure are the SAME paint to a viewer, and clustering by
   * brightness would just rediscover the image's gradient. */
  function d2(p, q) {
    var dl = (p[0] - q[0]) * 0.55;
    return dl * dl + (p[1] - q[1]) * (p[1] - q[1]) + (p[2] - q[2]) * (p[2] - q[2]);
  }
  function kmeans(pts) {
    var n = pts.length, i, j, mean = [0, 0, 0];
    for (i = 0; i < n; i++) { mean[0] += pts[i][0]; mean[1] += pts[i][1]; mean[2] += pts[i][2]; }
    mean = [mean[0] / n, mean[1] / n, mean[2] / n];
    var seed = pts[0], sd = Infinity;
    for (i = 0; i < n; i++) { var dd = d2(pts[i], mean); if (dd < sd) { sd = dd; seed = pts[i]; } }
    var cents = [seed];
    while (cents.length < K) {                       // farthest-point init
      var far = null, fard = -1;
      for (i = 0; i < n; i++) {
        var best = Infinity;
        for (j = 0; j < cents.length; j++) { var v = d2(pts[i], cents[j]); if (v < best) best = v; }
        if (best > fard) { fard = best; far = pts[i]; }
      }
      if (!far || fard <= 0) break;
      cents.push(far);
    }
    var acc = [], err = 0;
    for (var it = 0; it < 12; it++) {
      acc = cents.map(function () { return [0, 0, 0, 0]; });
      err = 0;
      for (i = 0; i < n; i++) {
        var bi = 0, bd = Infinity;
        for (j = 0; j < cents.length; j++) { var q = d2(pts[i], cents[j]); if (q < bd) { bd = q; bi = j; } }
        acc[bi][0] += pts[i][0]; acc[bi][1] += pts[i][1]; acc[bi][2] += pts[i][2]; acc[bi][3]++;
        err += bd;
      }
      cents = acc.map(function (a, k) {
        return a[3] ? [a[0] / a[3], a[1] / a[3], a[2] / a[3]] : cents[k];
      });
    }
    var clusters = [];
    for (i = 0; i < cents.length; i++) {
      if (!acc[i][3]) continue;
      var lch = lab2lch(cents[i][0], cents[i][1], cents[i][2]);
      lch.share = acc[i][3] / n;
      clusters.push(lch);
    }
    clusters.sort(function (a, b) { return b.share - a.share; });
    return { clusters: clusters, quantErr: Math.sqrt(err / n) };
  }

  // ── 4. statistics and the scheme decision ─────────────────────────────────
  function angDist(a, b) { var d = Math.abs(a - b) % 360; return d <= 180 ? d : 360 - d; }

  /* Chroma-and-area-weighted circular concentration of the cluster hues.
   * 1.0 = every colour points the same way; 0.0 = no agreement at all. */
  function hueSpread(clusters) {
    var sx = 0, sy = 0, w = 0;
    clusters.forEach(function (c) {
      var wt = c.C * Math.sqrt(c.share), r = c.h * Math.PI / 180;
      sx += wt * Math.cos(r); sy += wt * Math.sin(r); w += wt;
    });
    if (w <= 0) return { conc: 0, meanH: 0 };
    return { conc: Math.sqrt(sx * sx + sy * sy) / w,
             meanH: (Math.atan2(sy, sx) * 180 / Math.PI + 360) % 360 };
  }

  /* Single-link grouping of the chromatic clusters around the hue circle. A
   * photograph resolves to 1-3 hue families; confetti resolves to as many as
   * it has. This is what tells a two-colour image from a meaningless one. */
  function hueGroups(clusters) {
    var tol = 34, ch = clusters.filter(function (c) { return c.C >= 0.030; })
                              .sort(function (a, b) { return a.h - b.h; });
    if (!ch.length) return 0;
    var groups = [], cur = [ch[0]];
    for (var i = 1; i < ch.length; i++) {
      if (ch[i].h - cur[cur.length - 1].h <= tol) cur.push(ch[i]);
      else { groups.push(cur); cur = [ch[i]]; }
    }
    groups.push(cur);
    if (groups.length > 1) {
      var last = groups[groups.length - 1];
      if ((360 - last[last.length - 1].h) + groups[0][0].h <= tol) {
        groups[0] = last.concat(groups[0]);
        groups.pop();
      }
    }
    return groups.length;
  }

  function analyse(pts) {
    var km = kmeans(pts), n = pts.length, i;
    var Lsum = 0, chroma = [];
    for (i = 0; i < n; i++) {
      Lsum += pts[i][0];
      chroma.push(Math.sqrt(pts[i][1] * pts[i][1] + pts[i][2] * pts[i][2]));
    }
    chroma.sort(function (a, b) { return a - b; });
    var hs = hueSpread(km.clusters);
    // Salience of the single most assertive colour: chroma weighted by how much
    // of the picture it covers. This is the number that says whether the image
    // HAS a colour at all - it survives downsampling, unlike per-pixel chroma.
    var maxSal = 0;
    km.clusters.forEach(function (c) { maxSal = Math.max(maxSal, c.C * Math.sqrt(c.share)); });
    return {
      clusters: km.clusters, quantErr: km.quantErr,
      Lmean: Lsum / n,
      Chi: chroma[Math.floor(n * 0.90)],   // 90th-pct chroma: a mostly-grey image
      conc: hs.conc, meanH: hs.meanH,      // can still carry one emphatic colour
      maxSal: maxSal, groups: hueGroups(km.clusters)
    };
  }

  function chooseScheme(st) {
    // Black, white, grey - and per-pixel colour noise, which averages to grey
    // and IS grey to anyone standing back from the screen.
    if (st.maxSal < 0.020) return 'achromatic';
    // Many hue families that agree on nothing: confetti. BOTH halves are
    // needed - a sunset has 4 families too, but they agree (conc 0.46 vs 0.10).
    if (st.groups >= 4 && st.conc < 0.20) return 'chaotic';
    if (st.conc > 0.72 || st.groups <= 1) return 'mono';
    return 'rich';
  }

  // ── 5. accents ────────────────────────────────────────────────────────────
  function pickAccents(st, scheme, bgH) {
    var cl = st.clusters, c;
    if (scheme === 'achromatic' || scheme === 'chaotic') {
      // No trustworthy hue. Cortana's identity hues at restrained chroma, so a
      // colourless or busy background never fights the UI.
      c = scheme === 'chaotic' ? 0.075 : 0.105;
      return [[HOUSE.accent, c, false], [HOUSE.accent2, c * 0.85, false],
              [HOUSE.accent3, c * 0.8, false]];
    }
    if (scheme === 'mono') {
      var h = st.meanH;
      c = Math.max(0.085, Math.min(0.155, st.Chi * 1.15));
      // Counterpoint, not a fixed rotation. A blunt +194 off a blue lands on
      // yellow-green, which desaturates to khaki mud; steering to whichever
      // temperature anchor is FARTHER from the base gives a blue board a warm
      // amber second colour and a green board a cool violet one.
      var second = angDist(h, WARM) > angDist(h, COOL) ? WARM : COOL;
      return [[h, c, false], [second, c * 0.85, false], [(h + 32) % 360, c * 0.82, false]];
    }
    // rich: the image's own colours, most salient first, each >= 34 degrees
    // from the ones already taken.
    //
    // Salience weights CHROMA far above area. An accent is a small, vivid piece
    // of UI; the picture's most COMMON colour is already the background, and
    // ranking by area picked pale desert sand over the blue sky above it. The
    // hue penalty is that same rule from the other side: a colour painted ON
    // the board must not be the board's own colour.
    function sal(x) {
      return x.C * Math.pow(x.share, 0.25) * (angDist(x.h, bgH) < 30 ? 0.55 : 1);
    }
    var cand = cl.filter(function (x) { return x.C > 0.018; })
                 .sort(function (a, b) { return sal(b) - sal(a); });
    var out = [];
    for (var i = 0; i < cand.length && out.length < 3; i++) {
      var far = out.every(function (o) { return angDist(cand[i].h, o[0]) > 34; });
      if (far) out.push([cand[i].h, Math.max(0.075, Math.min(0.16, cand[i].C * 1.25)), false]);
    }
    // Out of real hues. Do NOT rotate off accent1 to manufacture more - that is
    // precisely what v1 did, and an invented hue is a lie about the picture: a
    // red/cyan duotone was given a lavender label, a blue-and-sand desert a
    // green one. Take a tinted neutral instead. It reads as deliberate - this
    // image has one usable colour, so the rest of the hierarchy is carried by
    // neutrals - and it can neither clash nor turn to mud.
    //
    // A second neutral is separated from the first by CHROMA, not lightness:
    // the band between --text-dim and --text is narrow, and stepping through
    // it collides with body text.
    var taken = out.length;
    for (var slot = taken; slot < 3; slot++) {
      var h0 = out.length ? out[0][0] : bgH;
      out.push([h0, NEUTRAL_C[Math.min(slot - taken, NEUTRAL_C.length - 1)], true]);
    }
    return out;
  }

  // ── 6. build ──────────────────────────────────────────────────────────────
  function build(img) {
    var st = analyse(sample(img));
    var scheme = chooseScheme(st), cl = st.clusters, i;
    // Only a genuinely bright picture earns a bright board: this design's home
    // turf is dark, so mid-bright images stay there.
    var dark = st.Lmean < 0.68;

    // Surfaces: the image's own colour, pulled to a usable lightness and nearly
    // stripped of chroma so text stays readable on top.
    var bgSrc = cl[0];
    for (i = 1; i < cl.length; i++)
      if (dark ? cl[i].L < bgSrc.L : cl[i].L > bgSrc.L) bgSrc = cl[i];
    var bgH = bgSrc.h;
    var bgC = Math.min(bgSrc.C, dark ? 0.022 : 0.014);
    // A warm hue held at low lightness reads as BROWN, not as a dark neutral -
    // a red/cyan duotone came out muddy maroon. Cool darks survive tinting;
    // warm ones have to give most of their chroma up.
    if (dark && bgSrc.h >= 18 && bgSrc.h <= 115) bgC *= 0.40;
    // The board follows the picture's own depth: a black wallpaper gets a black
    // board, a hazy one a softer charcoal. Clamped so the ramp below has room.
    var bgL = dark ? Math.max(0.10, Math.min(0.235, 0.50 * bgSrc.L + 0.085))
                   : Math.max(0.930, Math.min(0.985, 0.55 + 0.42 * bgSrc.L));
    var step = dark ? 0.052 : -0.038;
    var bg = lch2rgb255(bgL, bgC, bgH);
    var surface = lch2rgb255(bgL + step, bgC * 1.15, bgH);
    var surface2 = lch2rgb255(bgL + step * 2, bgC * 1.25, bgH);
    var border = lch2rgb255(bgL + step * 3, bgC * 1.3, bgH);

    // Contrast is fitted against BORDER - the far end of the surface ramp, and
    // so the least contrasty thing any colour can land on. Fitting against bg
    // alone let a colour clear 4.5:1 on the page and quietly fail it inside a
    // card; every role now clears its target on ALL FOUR surfaces.
    var ref = to01(border);

    // Text: neutral, faintly tinted by the board, contrast-locked.
    var text = lch2rgb255(fitContrast(dark ? 0.97 : 0.26, 0.012, bgH, ref, 9.0, dark), 0.012, bgH);
    var textDim = lch2rgb255(fitContrast(dark ? 0.72 : 0.52, 0.016, bgH, ref, 4.6, dark), 0.016, bgH);

    // Accents, each lightness-fitted to clear 4.5:1 wherever it lands.
    var acc = pickAccents(st, scheme, bgH);
    var L0 = dark ? [0.78, 0.74, 0.82] : [0.52, 0.50, 0.46];
    var roles = acc.map(function (a, k) {
      // A tinted neutral needs its own lightness: at the accent lightness a
      // near-grey reads as dirty text rather than as a label. The flag is
      // explicit rather than sniffed from chroma - inferring it from a
      // threshold breaks silently the moment a scheme's chroma dips under it,
      // turning a real accent into a neutral with nothing to notice it.
      var start = a[2] ? (dark ? NEUTRAL_L[0] : NEUTRAL_L[1]) : L0[k];
      return { L: fitContrast(start, a[1], a[0], ref, 4.5, dark), C: a[1], h: a[0] };
    });
    var a0 = roles[0];

    // Highlight: the loudest version of the primary. On a dark board that means
    // paler; on a light one paleness is invisible, so it goes RICHER instead
    // (and only slightly darker - a bigger drop turned it to mud).
    var ph = (a0.h + 12) % 360, pc = a0.C * (dark ? 0.72 : 1.30);
    var pL = dark ? Math.min(0.95, a0.L + 0.12) : Math.max(0.34, a0.L - 0.05);
    var peach = lch2rgb255(fitContrast(pL, pc, ph, ref, 4.5, dark), pc, ph);

    // The sphere's deep hue. In a rich image this is a REAL dark colour out of
    // the picture (the sunset's navy ridge), not a fixed rotation off the
    // accent - which is what put olive green under a coral board.
    var oh, oc, deep = null;
    if (scheme === 'rich') {
      cl.forEach(function (c) { if (c.C > 0.02 && (!deep || c.L < deep.L)) deep = c; });
    }
    if (deep) { oh = deep.h; oc = Math.min(0.11, Math.max(0.05, deep.C * 1.1)); }
    else {
      // Nothing dark and chromatic to borrow: sit the orb on the TERTIARY hue,
      // which the scheme has already placed in harmony.
      oh = roles[2].h; oc = Math.max(0.055, Math.min(0.11, roles[2].C * 0.95));
    }

    // ── the sphere's three gradient stops ────────────────────────────────────
    // These used to be the peach / accent2 / orb text colours doing double
    // duty. On a light board those roles pull opposite ways - text must go
    // dark - and the sphere turned into a mud-coloured blob. The orb is a
    // GRAPHIC: it owes only ~3:1 against the board and must read as lit from
    // the upper left in either polarity, so it gets its own stops, pinned
    // luminous, sharing the scheme's hues.
    var orbHi = lch2rgb255(0.93, Math.min(0.075, a0.C * 0.55), (a0.h + 12) % 360);
    var orbMid = lch2rgb255(0.72, Math.max(0.085, Math.min(0.15, roles[1].C * 1.1)), roles[1].h);
    var orb = lch2rgb255(fitContrast(0.50, oc, oh, to01(surface), 1.6, false), oc, oh);

    // Panel wash and hairline. Modules are frosted glass floating over the
    // blurred picture; both were hardcoded white, which is invisible on a light
    // board. The wash stays near-white in BOTH polarities (that is what
    // frosting looks like); only the hairline flips.
    var panel = lch2rgb255(0.96, Math.min(0.020, bgC * 0.8), bgH);
    var hairline = lch2rgb255(dark ? 0.97 : 0.30, 0.010, bgH);

    return {
      scheme: scheme,
      stats: { Lmean: st.Lmean, conc: st.conc, groups: st.groups,
               maxSal: st.maxSal, quantErr: st.quantErr, dark: dark },
      vars: {
        '--bg-rgb': rgbStr(bg),
        '--panel-rgb': rgbStr(panel), '--hairline-rgb': rgbStr(hairline),
        '--surface-rgb': rgbStr(surface),
        '--surface2-rgb': rgbStr(surface2), '--border-rgb': rgbStr(border),
        '--text-rgb': rgbStr(text), '--text-dim-rgb': rgbStr(textDim),
        '--accent-rgb': rgbStr(lch2rgb255(roles[0].L, roles[0].C, roles[0].h)),
        '--accent2-rgb': rgbStr(lch2rgb255(roles[1].L, roles[1].C, roles[1].h)),
        '--accent3-rgb': rgbStr(lch2rgb255(roles[2].L, roles[2].C, roles[2].h)),
        '--peach-rgb': rgbStr(peach),
        // Sphere stops, highlight to core. --orb-rgb keeps its old name and old
        // meaning (the deep stop) so pinned colours and COLOR_ROWS carry over.
        '--orb-hi-rgb': rgbStr(orbHi), '--orb-mid-rgb': rgbStr(orbMid),
        '--orb-rgb': rgbStr(orb)
      }
    };
  }

  global.duskPalette = {
    build: build,
    contrast: contrast,
    // Exposed so the dashboard can report a scheme in its toast, and so the
    // bubble/phone can be handed the sphere stops without re-deriving anything.
    ORB_STOPS: ['--orb-hi-rgb', '--orb-mid-rgb', '--orb-rgb']
  };
})(window);
