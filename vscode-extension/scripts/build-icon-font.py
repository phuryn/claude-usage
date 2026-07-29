#!/usr/bin/env python3
"""Build resources/claude-usage-icons.woff from resources/icon-vector.svg.

Why a font at all: VS Code only recolors icons that are glyphs in an icon font
(contributed via `contributes.icons` and referenced as a ThemeIcon). An SVG
handed to WebviewPanel.iconPath as a Uri is drawn as a plain image and keeps
whatever ink it was authored with, so it goes invisible on half the themes.
A glyph is painted with the live `icon.foreground` token instead.

Run from vscode-extension/:  python3 scripts/build-icon-font.py
Requires fonttools (pip install fonttools).
"""

import re
import sys
from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "resources" / "icon-vector.svg"
OUT = HERE.parent / "resources" / "claude-usage-icons.woff"

# Metrics are copied from VS Code's own codicon.ttf so our glyph sits in tabs
# exactly like the built-in icons. Measured from
# Resources/app/out/media/codicon.ttf: 300 upm, ascent 300 / descent 0, every
# glyph advance 300, and ink filling a 282x282 box anchored at the origin
# (`terminal` and `graph` are exactly 0..282 on both axes).
# Deviating here is what made the first build look small and left-padded.
UPM = 300
ASCENT, DESCENT = 300, 0
ADVANCE = 300
ICON_BOX = 282      # ink is fitted to this square, preserving aspect
CODEPOINT = 0xE001  # private use area; must match package.json's fontCharacter
GLYPH = "claudeUsage"
CURVE_STEPS = 24    # cubic flattening; the glyph renders at ~16px so this is ample


def flatten_cubic(p0, p1, p2, p3, n=CURVE_STEPS):
    pts = []
    for k in range(1, n + 1):
        t = k / n
        u = 1 - t
        pts.append((
            u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
        ))
    return pts


def parse_path(d):
    """Flatten an absolute M/L/C/Z path into a list of point-loops."""
    toks = re.findall(r"[MCLZmclz]|-?\d+\.?\d*(?:e-?\d+)?", d)
    loops, cur, pos, i, cmd = [], [], (0.0, 0.0), 0, None
    while i < len(toks):
        if toks[i] in "MCLZmclz":
            cmd = toks[i]
            i += 1
            if cmd in "Zz":
                if cur:
                    loops.append(cur)
                    cur = []
                continue
        nums = []
        while i < len(toks) and toks[i] not in "MCLZmclz":
            nums.append(float(toks[i]))
            i += 1
        if cmd == "M":
            if cur:
                loops.append(cur)
            pos = (nums[0], nums[1])
            cur = [pos]
            for j in range(2, len(nums), 2):
                pos = (nums[j], nums[j + 1])
                cur.append(pos)
        elif cmd == "L":
            for j in range(0, len(nums), 2):
                pos = (nums[j], nums[j + 1])
                cur.append(pos)
        elif cmd == "C":
            for j in range(0, len(nums), 6):
                p3 = (nums[j + 4], nums[j + 5])
                cur.extend(flatten_cubic(pos, (nums[j], nums[j + 1]),
                                         (nums[j + 2], nums[j + 3]), p3))
                pos = p3
        else:
            raise SystemExit(f"unsupported path command {cmd!r}; re-export as absolute M/L/C/Z")
    if cur:
        loops.append(cur)
    return loops


def main():
    if not SRC.exists():
        raise SystemExit(f"missing {SRC}")
    svg = SRC.read_text(encoding="utf-8")

    if "<image" in svg:
        raise SystemExit("source SVG embeds a raster; a font glyph needs real vector paths")
    ds = re.findall(r'\sd="([^"]+)"', svg)
    if not ds:
        raise SystemExit("source SVG has no path data")

    loops = []
    for d in ds:
        loops.extend(parse_path(d))

    # Fit the artwork's INK to the codicon box rather than mapping the viewBox.
    # The source art is inset within its viewBox, so scaling the viewBox would
    # bake that inset in as permanent margin: the icon renders small and sits
    # too far from the tab label.
    xs = [p[0] for lp in loops for p in lp]
    ys = [p[1] for lp in loops for p in lp]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    ink_w, ink_h = max_x - min_x, max_y - min_y

    scale = ICON_BOX / max(ink_w, ink_h)
    # Anchor x at 0 like the codicons; centre the shorter axis inside the box.
    off_x = (ICON_BOX - ink_w * scale) / 2
    off_y = (ICON_BOX - ink_h * scale) / 2

    # SVG is y-down, fonts are y-up, so flip about the ink's own bottom edge.
    def place(x, y):
        return (off_x + (x - min_x) * scale, off_y + (max_y - y) * scale)

    pen = TTGlyphPen(None)
    for loop in loops:
        if len(loop) < 3:
            continue
        pen.moveTo(place(*loop[0]))
        for pt in loop[1:]:
            pen.lineTo(place(*pt))
        pen.closePath()
    glyph = pen.glyph()

    fb = FontBuilder(UPM, isTTF=True)
    order = [".notdef", GLYPH]
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({CODEPOINT: GLYPH})
    fb.setupGlyf({".notdef": TTGlyphPen(None).glyph(), GLYPH: glyph})
    fb.setupHorizontalMetrics({".notdef": (ADVANCE, 0), GLYPH: (ADVANCE, 0)})
    fb.setupHorizontalHeader(ascent=ASCENT, descent=DESCENT)
    fb.setupNameTable({
        "familyName": "claude-usage-icons",
        "styleName": "Regular",
        "psName": "claude-usage-icons",
    })
    fb.setupOS2(sTypoAscender=ASCENT, sTypoDescender=DESCENT, usWinAscent=ASCENT,
                usWinDescent=-DESCENT)
    fb.setupPost()
    fb.font.flavor = "woff"
    fb.save(OUT)

    print(f"source   : {SRC.name} ({len(ds)} path(s), {len(loops)} contours, "
          f"{sum(len(l) for l in loops)} points)")
    print(f"src ink  : x {min_x:.0f}..{max_x:.0f}  y {min_y:.0f}..{max_y:.0f}  "
          f"({ink_w:.0f}x{ink_h:.0f})")
    print(f"glyph    : U+{CODEPOINT:04X} '{GLYPH}'  {UPM} upm, advance {ADVANCE}, "
          f"ink fitted to {ICON_BOX}")
    print(f"written  : resources/{OUT.name} ({OUT.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    sys.exit(main())
