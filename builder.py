#!/usr/bin/env python3
"""
Zero typeface builder — converts 12×12 SVG glyph sources to an OTF (CFF) font.
Automatically pads the 12x12 glyphs to 16x16 (2px padding on all sides).

Usage:
  build_font.py --src GLYPHS_DIR --output OUT.otf [--mono]
                [--family NAME] [--style NAME]
"""

import argparse
import os
import re
import sys
from xml.etree import ElementTree as ET

from fontTools.fontBuilder import FontBuilder
from fontTools.misc.psCharStrings import T2CharString

# ── constants ─────────────────────────────────────────────────────────────────

SVG_H = 12  # base glyph height in SVG units
SCALE = 100  # 1 SVG unit = 100 font units
PAD_SVG = 2  # 2 units of padding on all sides
FONT_PAD = PAD_SVG * SCALE  # 200 font units

# total UPM is now (12 + 2 + 2) * 100 = 1600
UPM = (SVG_H + 2 * PAD_SVG) * SCALE

# Vertical metrics
ASCENDER = UPM
DESCENDER = 0
LINE_GAP = 0
CAP_HEIGHT = UPM
X_HEIGHT = UPM

# CFF Private dict — alignment zones + stem hints
PRIVATE_DICT = {
    "defaultWidthX": 0,
    "nominalWidthX": 0,
    # Alignment zones: [bottom, top] pairs — baseline zone and cap-height zone
    "BlueValues": [-10, 0, UPM - 10, UPM],
    # Stem widths (~1 px at target)
    "StdHW": 80,
    "StdVW": 80,
    "StemSnapH": [80, 120],
    "StemSnapV": [80, 120],
    "BlueFuzz": 1,
    "BlueScale": 0.039625,
    "BlueShift": 7,
    "ForceBold": False,
}

# ── glyph table ───────────────────────────────────────────────────────────────

_DIGIT_NAMES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
]


def make_glyph_table():
    """
    Returns list of (glyph_name, unicode_or_None, svg_filename_or_None).
    Uppercase A–Z reuse the lowercase SVG outlines.
    """
    table = [
        (".notdef", None, None),
        ("space", 0x0020, None),
        ("period", 0x002E, "dot.svg"),
        ("colon", 0x003A, "colon.svg"),
    ]
    for i, name in enumerate(_DIGIT_NAMES):
        table.append((name, 0x0030 + i, f"{i}.svg"))
    for cp in range(ord("a"), ord("z") + 1):
        table.append((chr(cp), cp, f"{chr(cp)}.svg"))
    for cp in range(ord("A"), ord("Z") + 1):
        lc_svg = f"{chr(cp + 32)}.svg"  # reuse lowercase outline
        table.append((chr(cp), cp, lc_svg))
    return table


# ── SVG path tokeniser ────────────────────────────────────────────────────────

_NUM_RE = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_TOK_RE = re.compile(rf"([MLHVCZmlhvcz])|({_NUM_RE})")

_ARGC = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "Z": 0}


def _iter_commands(d: str):
    """
    Yield (cmd_upper, is_relative, [float, ...]) for each drawing command in
    an SVG path `d` attribute.  Only M, L, H, V, C, Z (plus lowercase) needed.
    """
    tokens = [(m.group(1), m.group(2)) for m in _TOK_RE.finditer(d)]
    i = 0
    cur_cmd = None
    is_rel = False

    while i < len(tokens):
        cmd_char, num_str = tokens[i]

        if cmd_char is not None:
            cur_cmd = cmd_char.upper()
            is_rel = cmd_char.islower()
            i += 1
            n = _ARGC.get(cur_cmd, 0)
            if n == 0:
                yield (cur_cmd, is_rel, [])
            # For M, implicit subsequent coord pairs are L
        else:
            # Number token — belongs to cur_cmd
            n = _ARGC.get(cur_cmd, 0)
            if n == 0 or cur_cmd is None:
                i += 1
                continue
            args = []
            for j in range(n):
                if i + j >= len(tokens) or tokens[i + j][1] is None:
                    return
                args.append(float(tokens[i + j][1]))
            i += n
            yield (cur_cmd, is_rel, args)
            # After M, implicit repeats become L
            if cur_cmd == "M":
                cur_cmd = "L"


# ── SVG → CFF charstring ──────────────────────────────────────────────────────


def _r(v: float) -> int:
    return int(round(v))


def build_charstring(
    path_ds: list[str], svg_w: float, svg_h: float, advance_width: int, is_mono: bool
) -> T2CharString:
    """
    Convert a list of SVG path `d` strings to a Type 2 charstring.

    Coordinate transforms
    ─────────────────────
    SVG uses y-down; fonts use y-up.
    We also add 200 font units (2px) of padding everywhere.

      font_x(svg_x) = svg_x * SCALE + x_offset
      font_y(svg_y) = (svg_h - svg_y) * SCALE + y_offset

    For mono mode, every glyph is horizontally centred inside the UPM-wide cell.
    For standard mode, every glyph just gets the standard left padding.
    """
    if is_mono:
        x_offset = (UPM - svg_w * SCALE) / 2.0
    else:
        x_offset = FONT_PAD

    y_offset = FONT_PAD

    def to_fx(sx: float) -> float:
        return sx * SCALE + x_offset

    def to_fy(sy: float) -> float:
        return (svg_h - sy) * SCALE + y_offset

    # Charstring current position (font space). Starts at glyph origin (0, 0).
    cfx, cfy = 0.0, 0.0
    # SVG-space current position (for relative command resolution)
    sx_cur, sy_cur = 0.0, 0.0
    # Subpath start (for closepath resolution)
    sx_start, sy_start = 0.0, 0.0

    ops: list = [advance_width]  # width is first operand in Type 2

    def emit_moveto(ex: float, ey: float):
        nonlocal cfx, cfy, sx_cur, sy_cur, sx_start, sy_start
        nfx, nfy = to_fx(ex), to_fy(ey)
        ops.extend([_r(nfx - cfx), _r(nfy - cfy), "rmoveto"])
        cfx, cfy = nfx, nfy
        sx_cur, sy_cur = ex, ey
        sx_start, sy_start = ex, ey

    def emit_lineto(ex: float, ey: float):
        nonlocal cfx, cfy, sx_cur, sy_cur
        nfx, nfy = to_fx(ex), to_fy(ey)
        ops.extend([_r(nfx - cfx), _r(nfy - cfy), "rlineto"])
        cfx, cfy = nfx, nfy
        sx_cur, sy_cur = ex, ey

    def emit_curveto(x1: float, y1: float, x2: float, y2: float, ex: float, ey: float):
        nonlocal cfx, cfy, sx_cur, sy_cur
        fx1, fy1 = to_fx(x1), to_fy(y1)
        fx2, fy2 = to_fx(x2), to_fy(y2)
        fex, fey = to_fx(ex), to_fy(ey)
        ops.extend(
            [
                _r(fx1 - cfx),
                _r(fy1 - cfy),
                _r(fx2 - fx1),
                _r(fy2 - fy1),
                _r(fex - fx2),
                _r(fey - fy2),
                "rrcurveto",
            ]
        )
        cfx, cfy = fex, fey
        sx_cur, sy_cur = ex, ey

    def emit_closepath():
        nonlocal cfx, cfy, sx_cur, sy_cur
        # NOTE: Type 2 charstrings do NOT have a 'closepath' operator.
        # Subpaths are implicitly closed by the next move or endchar.
        # We just sync our SVG tracking state back to the start.
        cfx, cfy = to_fx(sx_start), to_fy(sy_start)
        sx_cur, sy_cur = sx_start, sy_start

    for d in path_ds:
        for cmd, is_rel, raw in _iter_commands(d):
            # Resolve relative → absolute (SVG space)
            def abs_xy(dx, dy):
                return (sx_cur + dx, sy_cur + dy) if is_rel else (dx, dy)

            def abs_x(dx):
                return (sx_cur + dx) if is_rel else dx

            def abs_y(dy):
                return (sy_cur + dy) if is_rel else dy

            if cmd == "M":
                emit_moveto(*abs_xy(raw[0], raw[1]))
            elif cmd == "L":
                emit_lineto(*abs_xy(raw[0], raw[1]))
            elif cmd == "H":
                emit_lineto(abs_x(raw[0]), sy_cur)
            elif cmd == "V":
                emit_lineto(sx_cur, abs_y(raw[0]))
            elif cmd == "C":
                x1, y1 = abs_xy(raw[0], raw[1])
                x2, y2 = abs_xy(raw[2], raw[3])
                ex, ey = abs_xy(raw[4], raw[5])
                emit_curveto(x1, y1, x2, y2, ex, ey)
            elif cmd == "Z":
                emit_closepath()

    ops.append("endchar")

    cs = T2CharString()
    cs.program = ops
    return cs


# ── per-glyph metrics ─────────────────────────────────────────────────────────


def glyph_metrics(svg_file: str | None, src_dir: str, is_mono: bool) -> tuple[int, int]:
    """Return (advance_width, lsb)."""
    if svg_file is None:
        # .notdef / space
        return (UPM, 0) if is_mono else (round(UPM * 0.5), 0)

    path = os.path.join(src_dir, svg_file)
    tree = ET.parse(path)
    root = tree.getroot()

    vb = root.get("viewBox", "")
    if vb:
        parts = vb.split()
        svg_w = float(parts[2])
    else:
        svg_w = float(root.get("width", "12").rstrip("px"))

    if is_mono:
        adv = UPM
        lsb = _r((UPM - svg_w * SCALE) / 2.0)
    else:
        # width of glyph + 2px padding on both sides
        adv = _r(svg_w * SCALE + 2 * FONT_PAD)
        lsb = FONT_PAD
    return adv, lsb


# ── SVG dimensions helper ─────────────────────────────────────────────────────


def svg_dims(svg_file: str, src_dir: str) -> tuple[float, float]:
    path = os.path.join(src_dir, svg_file)
    tree = ET.parse(path)
    root = tree.getroot()
    vb = root.get("viewBox", "")
    if vb:
        parts = vb.split()
        return float(parts[2]), float(parts[3])
    w = float(root.get("width", "12").rstrip("px"))
    h = float(root.get("height", "12").rstrip("px"))
    return w, h


def svg_path_ds(svg_file: str, src_dir: str) -> list[str]:
    path = os.path.join(src_dir, svg_file)
    tree = ET.parse(path)
    root = tree.getroot()
    ds = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "path":
            d = elem.get("d", "").strip()
            if d:
                ds.append(d)
    return ds


# ── font builder ──────────────────────────────────────────────────────────────


def build_font(
    src_dir: str,
    output: str,
    is_mono: bool,
    family: str = "Zero",
    style: str = "Regular",
):
    glyph_table = make_glyph_table()

    # ── metrics pass ──────────────────────────────────────────────────────────
    glyph_order = []
    cmap_dict = {}  # unicode → glyph_name
    hmtx_dict = {}  # glyph_name → (advance, lsb)
    charstrings = {}  # glyph_name → T2CharString

    for gname, uni, svg_file in glyph_table:
        glyph_order.append(gname)
        if uni is not None:
            cmap_dict[uni] = gname

        adv, lsb = glyph_metrics(svg_file, src_dir, is_mono)
        hmtx_dict[gname] = (adv, lsb)

        if svg_file is None:
            # Empty glyph (.notdef, space)
            cs = T2CharString()
            cs.program = [adv, "endchar"]
            charstrings[gname] = cs
        else:
            svg_w, svg_h = svg_dims(svg_file, src_dir)
            path_ds = svg_path_ds(svg_file, src_dir)
            charstrings[gname] = build_charstring(path_ds, svg_w, svg_h, adv, is_mono)

    # ── FontBuilder ───────────────────────────────────────────────────────────
    fb = FontBuilder(UPM, isTTF=False)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap(cmap_dict)
    fb.setupHorizontalMetrics(hmtx_dict)

    fb.setupHorizontalHeader(
        ascent=ASCENDER,
        descent=DESCENDER,
    )

    full_name = f"{family} {style}"
    ps_name = f"{family}-{style}".replace(" ", "")
    version_str = "Version 1.000"

    fb.setupNameTable(
        {
            "familyName": family,
            "styleName": style,
            "fullName": full_name,
            "psName": ps_name,
            "version": version_str,
            "copyright": f"Copyright 2025 neg-zero (-0)",
            "trademark": "",
            "manufacturer": "neg-zero",
            "designer": "doromiert",
            "description": "Zero pixel-display typeface",
            "vendorURL": "",
            "designerURL": "",
            "licenseDescription": "Licensed under the NAPALM anti-license",
        }
    )

    fb.setupOS2(
        sTypoAscender=ASCENDER,
        sTypoDescender=DESCENDER,
        sTypoLineGap=LINE_GAP,
        usWinAscent=ASCENDER,
        usWinDescent=abs(DESCENDER),
        sxHeight=X_HEIGHT,
        sCapHeight=CAP_HEIGHT,
        achVendID="NZRO",
        fsType=0,  # installable embedding
        fsSelection=0x40,  # REGULAR bit
        # Panose data must be a dict or a proper object for sstruct packing
        panose={
            "bFamilyType": 2,  # Latin Text
            "bSerifStyle": 0,  # any
            "bWeight": 0,  # any
            "bProportion": 9,  # monospaced
            "bContrast": 0,
            "bStrokeVariation": 0,
            "bArmStyle": 0,
            "bLetterForm": 0,
            "bMidline": 0,
            "bXHeight": 0,
        },
    )

    fb.setupPost(
        isFixedPitch=1 if is_mono else 0,
    )

    fb.setupHead(
        unitsPerEm=UPM,
        lowestRecPPEM=12,
    )

    fb.setupCFF(
        psName=ps_name,
        fontInfo={
            "version": "1.000",
            "Notice": "Copyright 2025 neg-zero (-0)",
            "FullName": full_name,
            "FamilyName": family,
            "Weight": style,
            "UnderlinePosition": -100,
            "UnderlineThickness": 80,
        },
        charStringsDict=charstrings,
        privateDict=PRIVATE_DICT,
    )

    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    fb.font.save(output)
    print(f"[zero-font] Saved {output!r} ({len(glyph_order)} glyphs, UPM={UPM})")


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(description="Build Zero typeface from SVG sources.")
    ap.add_argument("--src", required=True, help="Directory with *.svg glyph files")
    ap.add_argument("--output", required=True, help="Output .otf path")
    ap.add_argument(
        "--mono",
        action="store_true",
        help="Build mono variant (glyphs centred in 16×16 cell)",
    )
    ap.add_argument("--family", default="Zero", help="Font family name")
    ap.add_argument("--style", default="Regular", help="Font style name")
    args = ap.parse_args()

    if args.mono:
        args.family = args.family + " Mono"

    build_font(
        src_dir=args.src,
        output=args.output,
        is_mono=args.mono,
        family=args.family,
        style=args.style,
    )


if __name__ == "__main__":
    main()
