#!/usr/bin/env python3
"""
gen_assets.py — genera firmware/src/ui_assets.h

Convierte fuentes TrueType (Roboto, Apache 2.0) en mapas de bits con
anti-aliasing de 8 bits y rasteriza los iconos vectoriales (controles,
logo de Apple, nota musical) a máscaras alfa.  El firmware mezcla esas
máscaras sobre el degradado de fondo, de ahí el aspecto suave.

Uso:
    pip3 install pillow numpy freetype-py
    python3 tools/gen_assets.py                # escribe firmware/src/ui_assets.h
    python3 tools/gen_assets.py --preview      # además guarda tools/assets_preview.png

Para cambiar tamaños o tipografías edita FACES más abajo.
"""
import os
import re
import sys

import numpy as np
from PIL import Image, ImageDraw
import freetype

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_H = os.path.join(HERE, "..", "firmware", "src", "ui_assets.h")
FONT_DIR = os.path.join(HERE, "fonts")

# ── Caras tipográficas (nombre, archivo, tamaño en px, gamma del alfa) ─────────
# gamma < 1 engrosa un poco el trazo (texto claro sobre fondo oscuro se ve
# más fino de lo que es por la respuesta del ojo/panel).
FACES = [
    ("HDR",    "Roboto-Bold.ttf",    10, 0.85),   # "Apple Music" (cabecera)
    ("TITLE",  "Roboto-Bold.ttf",    12, 0.80),   # título de la canción
    ("ARTIST", "Roboto-Medium.ttf",  12, 0.80),   # artista / texto secundario
]
PHASES = 4   # posiciones subpíxel (0, ¼, ½, ¾ px) para un espaciado uniforme

# ── Juego de caracteres: ASCII + Latin-1 + Latin Extendido-A + puntuación ─────
CODEPOINTS = (
    list(range(0x20, 0x7F))
    + list(range(0xA0, 0x100))
    + list(range(0x100, 0x180))
    + [0x2013, 0x2014, 0x2018, 0x2019, 0x201A, 0x201C, 0x201D, 0x201E,
       0x2022, 0x2026, 0x20AC, 0x2122, 0x2212]
)
CODEPOINTS = sorted(set(CODEPOINTS))

SS = 16  # factor de supermuestreo para los iconos


# ═════════════════════════════════════════════════════════════════════════════
#  FUENTES  (FreeType: avances lineales + posicionamiento subpíxel)
# ═════════════════════════════════════════════════════════════════════════════
LOAD_FLAGS = freetype.FT_LOAD_TARGET_LIGHT | freetype.FT_LOAD_RENDER


def render_phase(face, ch, phase, gamma):
    """Rasteriza `ch` desplazado phase/PHASES de píxel a la derecha."""
    face.set_transform(freetype.Matrix(0x10000, 0, 0, 0x10000),
                       freetype.Vector(int(round(phase * 64 / PHASES)), 0))
    face.load_char(ch, LOAD_FLAGS)
    bm = face.glyph.bitmap
    w, h = bm.width, bm.rows
    if w == 0 or h == 0:
        return dict(w=0, h=0, xo=0, yo=0, data=b"")
    buf = np.array(bm.buffer, dtype=np.uint8).reshape(h, bm.pitch)[:, :w]
    arr = np.clip(np.power(buf / 255.0, gamma) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return dict(w=w, h=h, xo=face.glyph.bitmap_left, yo=-face.glyph.bitmap_top, data=arr.tobytes())


def build_face(fname, size, gamma):
    face = freetype.Face(os.path.join(FONT_DIR, fname))
    face.set_pixel_sizes(0, size)
    glyphs, adv16, blob = [], [], bytearray()
    for cp in CODEPOINTS:
        ch = chr(cp)
        face.set_transform(freetype.Matrix(0x10000, 0, 0, 0x10000), freetype.Vector(0, 0))
        face.load_char(ch, freetype.FT_LOAD_TARGET_LIGHT | freetype.FT_LOAD_NO_BITMAP)
        adv16.append(int(round(face.glyph.linearHoriAdvance / 65536.0 * 16)))
        for ph in range(PHASES):
            g = render_phase(face, ch, ph, gamma)
            g["off"] = len(blob)
            blob += g["data"]
            glyphs.append(g)
    asc = face.size.ascender >> 6
    desc = -(face.size.descender >> 6)
    return dict(glyphs=glyphs, adv16=adv16, blob=bytes(blob), lineH=asc + desc, ascent=asc)


# ═════════════════════════════════════════════════════════════════════════════
#  RASTERIZADO DE ICONOS
# ═════════════════════════════════════════════════════════════════════════════
def grid(w, h):
    ys, xs = np.mgrid[0:h * SS, 0:w * SS]
    return (xs + 0.5) / SS, (ys + 0.5) / SS


def to_alpha(mask, w, h):
    m = mask.astype(np.float32).reshape(h, SS, w, SS).mean(axis=(1, 3))
    return np.clip(m * 255.0 + 0.5, 0, 255).astype(np.uint8)


def rect_mask(X, Y, x0, y0, x1, y1, r):
    """Rectángulo redondeado (x0,y0)-(x1,y1) con radio r."""
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    hx, hy = (x1 - x0) / 2.0 - r, (y1 - y0) / 2.0 - r
    qx = np.abs(X - cx) - hx
    qy = np.abs(Y - cy) - hy
    outside = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0))
    inside = np.minimum(np.maximum(qx, qy), 0)
    return (outside + inside - r) <= 0


def rounded_poly(X, Y, verts, r):
    """Polígono convexo con esquinas redondeadas (erosión + dilatación)."""
    v = np.array(verts, dtype=np.float64)
    n = len(v)
    c = v.mean(axis=0)
    lines = []
    for i in range(n):
        p, q = v[i], v[(i + 1) % n]
        d = (q - p) / np.linalg.norm(q - p)
        nrm = np.array([-d[1], d[0]])
        if np.dot(nrm, c - p) < 0:
            nrm = -nrm
        lines.append((p + nrm * r, d))
    inner = []
    for i in range(n):
        p1, d1 = lines[i - 1]
        p2, d2 = lines[i]
        A = np.array([[d1[0], -d2[0]], [d1[1], -d2[1]]])
        t, _ = np.linalg.solve(A, p2 - p1)
        inner.append(p1 + t * d1)
    inner = np.array(inner)
    ic = inner.mean(axis=0)

    inside = np.ones(X.shape, dtype=bool)
    dist = np.full(X.shape, 1e9)
    for i in range(n):
        a, b = inner[i], inner[(i + 1) % n]
        e = b - a
        cr = e[0] * (Y - a[1]) - e[1] * (X - a[0])
        sc = e[0] * (ic[1] - a[1]) - e[1] * (ic[0] - a[0])
        inside &= (cr * np.sign(sc)) >= 0
        L2 = float(np.dot(e, e))
        t = np.clip(((X - a[0]) * e[0] + (Y - a[1]) * e[1]) / L2, 0, 1)
        dist = np.minimum(dist, np.hypot(X - (a[0] + t * e[0]), Y - (a[1] + t * e[1])))
    return inside | (dist <= r)


def ellipse_mask(X, Y, cx, cy, rx, ry, rot_deg):
    a = np.deg2rad(rot_deg)
    dx, dy = X - cx, Y - cy
    u = dx * np.cos(a) + dy * np.sin(a)
    v = -dx * np.sin(a) + dy * np.cos(a)
    return (u / rx) ** 2 + (v / ry) ** 2 <= 1.0


def icon_pause(w=16, h=19):
    X, Y = grid(w, h)
    bar = 6.5
    m = rect_mask(X, Y, 0, 0, bar, h, 1.9) | rect_mask(X, Y, w - bar, 0, w, h, 1.9)
    return to_alpha(m, w, h), w, h


def icon_play(w=16, h=19):
    X, Y = grid(w, h)
    m = rounded_poly(X, Y, [(0.0, 0.0), (0.0, h), (w, h / 2.0)], 2.1)
    return to_alpha(m, w, h), w, h


def icon_rewind(w=22, h=14):
    X, Y = grid(w, h)
    tw, ov = 11.6, 10.0
    m = np.zeros(X.shape, dtype=bool)
    for k in (0, 1):
        x0 = k * ov
        m |= rounded_poly(X, Y, [(x0, h / 2.0), (x0 + tw, 0.0), (x0 + tw, float(h))], 1.5)
    return to_alpha(m, w, h), w, h


def icon_forward(w=22, h=14):
    a, w, h = icon_rewind(w, h)
    return a[:, ::-1].copy(), w, h


def icon_squircle(size=28, radius=6.5):
    X, Y = grid(size, size)
    return to_alpha(rect_mask(X, Y, 0, 0, size, size, radius), size, size), size, size


def icon_note(size):
    """Nota doble unida por una barra (diseñada en rejilla de 28 unidades)."""
    X, Y = grid(size, size)
    s = 28.0 / size
    Xd, Yd = X * s, Y * s
    m = ellipse_mask(Xd, Yd, 7.2, 21.2, 4.6, 3.3, -22)
    m |= ellipse_mask(Xd, Yd, 20.2, 18.4, 4.6, 3.3, -22)
    m |= (Xd >= 10.2) & (Xd <= 12.2) & (Yd >= 6.0) & (Yd <= 20.6)
    m |= (Xd >= 23.2) & (Xd <= 25.2) & (Yd >= 2.5) & (Yd <= 17.8)
    # barra inclinada (paralelogramo)
    x0, x1 = 10.2, 25.2
    y_top0, y_top1 = 5.2, 1.6
    t = np.clip((Xd - x0) / (x1 - x0), 0, 1)
    top = y_top0 + (y_top1 - y_top0) * t
    m |= (Xd >= x0) & (Xd <= x1) & (Yd >= top) & (Yd <= top + 4.8)
    return to_alpha(m, size, size), size, size


# ── Logo de Apple a partir de un path SVG (viewBox 24×24, Simple Icons, CC0) ──
APPLE_PATH = (
    "M12.152 6.896c-.948 0-2.415-1.078-3.96-1.04-2.04.027-3.91 1.183-4.961 3.014"
    "-2.117 3.675-.546 9.103 1.519 12.09 1.013 1.454 2.208 3.09 3.792 3.039 1.52-.065 "
    "2.09-.987 3.935-.987 1.831 0 2.35.987 3.96.948 1.637-.026 2.676-1.48 3.676-2.948 "
    "1.156-1.688 1.636-3.325 1.662-3.415-.039-.013-3.182-1.221-3.22-4.857-.026-3.04 "
    "2.48-4.494 2.597-4.559-1.429-2.09-3.623-2.324-4.39-2.376-2-.156-3.675 1.09-4.61 "
    "1.09zM15.53 3.83c.843-1.012 1.4-2.427 1.245-3.83-1.207.052-2.662.805-3.532 "
    "1.818-.78.896-1.454 2.338-1.273 3.714 1.338.104 2.715-.688 3.559-1.701"
)


def parse_svg_path(d):
    toks = re.findall(r"[a-zA-Z]|-?\d*\.?\d+(?:e-?\d+)?", d)
    i, subs, cur = 0, [], []
    x = y = sx = sy = 0.0
    cmd, lastc = None, None

    def num():
        nonlocal i
        v = float(toks[i])
        i += 1
        return v

    def bez(p0, p1, p2, p3):
        for t in np.linspace(0, 1, 25)[1:]:
            a, b, c, e = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
            cur.append((a * p0[0] + b * p1[0] + c * p2[0] + e * p3[0],
                        a * p0[1] + b * p1[1] + c * p2[1] + e * p3[1]))

    while i < len(toks):
        if re.match(r"[a-zA-Z]", toks[i]):
            cmd = toks[i]
            i += 1
        if cmd in "Zz":
            if cur:
                subs.append(cur)
                cur = []
            x, y = sx, sy
            continue
        if cmd in "Mm":
            if cur:
                subs.append(cur)
                cur = []
            dx, dy = num(), num()
            x, y = (x + dx, y + dy) if cmd == "m" else (dx, dy)
            sx, sy = x, y
            cur = [(x, y)]
            cmd = "l" if cmd == "m" else "L"
            lastc = None
        elif cmd in "Ll":
            dx, dy = num(), num()
            x, y = (x + dx, y + dy) if cmd == "l" else (dx, dy)
            cur.append((x, y))
            lastc = None
        elif cmd in "Cc":
            v = [num() for _ in range(6)]
            if cmd == "c":
                v = [x + v[0], y + v[1], x + v[2], y + v[3], x + v[4], y + v[5]]
            bez((x, y), (v[0], v[1]), (v[2], v[3]), (v[4], v[5]))
            x, y, lastc = v[4], v[5], (v[2], v[3])
        elif cmd in "Ss":
            v = [num() for _ in range(4)]
            if cmd == "s":
                v = [x + v[0], y + v[1], x + v[2], y + v[3]]
            p1 = (2 * x - lastc[0], 2 * y - lastc[1]) if lastc else (x, y)
            bez((x, y), p1, (v[0], v[1]), (v[2], v[3]))
            x, y, lastc = v[2], v[3], (v[0], v[1])
        else:
            raise ValueError("comando SVG no soportado: " + cmd)
    if cur:
        subs.append(cur)
    return subs


def icon_apple(w=8, h=10, height_px=9.2):
    subs = parse_svg_path(APPLE_PATH)
    pts = np.array([p for s in subs for p in s])
    mn, mx = pts.min(axis=0), pts.max(axis=0)
    scale = height_px / (mx[1] - mn[1])
    ox = (w - (mx[0] - mn[0]) * scale) / 2.0 - mn[0] * scale
    oy = (h - height_px) / 2.0 - mn[1] * scale
    im = Image.new("L", (w * SS, h * SS), 0)
    dr = ImageDraw.Draw(im)
    for s in subs:
        dr.polygon([((px * scale + ox) * SS, (py * scale + oy) * SS) for px, py in s], fill=255)
    m = np.asarray(im, dtype=np.float32).reshape(h, SS, w, SS).mean(axis=(1, 3))
    return np.clip(m + 0.5, 0, 255).astype(np.uint8), w, h


# ═════════════════════════════════════════════════════════════════════════════
#  SALIDA C
# ═════════════════════════════════════════════════════════════════════════════
def c_bytes(name, data, per_line=24):
    data = bytes(data)
    lines = []
    for i in range(0, len(data), per_line):
        lines.append("    " + ",".join("%d" % b for b in data[i:i + per_line]))
    return "static const uint8_t %s[] PROGMEM = {\n%s\n};\n" % (name, ",\n".join(lines) if lines else "    0")


def runs(cps):
    out, start, prev = [], cps[0], cps[0]
    for cp in cps[1:]:
        if cp != prev + 1:
            out.append((start, prev - start + 1))
            start = cp
        prev = cp
    out.append((start, prev - start + 1))
    return out


def main():
    preview = "--preview" in sys.argv
    o = []
    o.append("// ============================================================\n"
             "//  ui_assets.h — GENERADO por tools/gen_assets.py (no editar a mano)\n"
             "//  Fuentes: Roboto (Apache 2.0) · Iconos: rasterizados con anti-aliasing\n"
             "// ============================================================\n"
             "#pragma once\n#include <stdint.h>\n\n"
             "#ifndef PROGMEM\n#define PROGMEM\n#endif\n")
    o.append("// glyphs[indice * FONT_PHASES + fase]; adv16 = avance en 1/16 de píxel\n"
             "#define FONT_PHASES %d\n"
             "struct GlyphInfo { uint32_t off; uint8_t w, h; int8_t xo, yo; };\n"
             "struct FontDef {\n"
             "    const GlyphInfo* glyphs;\n"
             "    const uint16_t*  adv16;\n"
             "    const uint8_t*   bmp;\n"
             "    uint8_t          lineH;\n"
             "    int8_t           ascent;\n"
             "};\n" % PHASES)

    rr = runs(CODEPOINTS)
    base, bases = 0, []
    for st, cnt in rr:
        bases.append(base)
        base += cnt
    o.append("#define FONT_NRANGES %d\n" % len(rr))
    o.append("static const uint16_t FONT_RANGE_START[FONT_NRANGES] = {%s};\n" % ",".join(str(s) for s, _ in rr))
    o.append("static const uint16_t FONT_RANGE_COUNT[FONT_NRANGES] = {%s};\n" % ",".join(str(c) for _, c in rr))
    o.append("static const uint16_t FONT_RANGE_BASE[FONT_NRANGES]  = {%s};\n" % ",".join(str(b) for b in bases))
    o.append("#define FONT_QMARK_INDEX %d\n" % CODEPOINTS.index(ord("?")))

    faces_out = {}
    for name, fname, size, gamma in FACES:
        f = build_face(fname, size, gamma)
        faces_out[name] = f
        assert len(f["blob"]) < 2 ** 32
        o.append(c_bytes("FONT_%s_BMP" % name, f["blob"]))
        rows = ",\n".join(
            "    {%d,%d,%d,%d,%d}" % (g["off"], g["w"], g["h"], g["xo"], g["yo"])
            for g in f["glyphs"])
        o.append("static const GlyphInfo FONT_%s_GLYPHS[] PROGMEM = {\n%s\n};\n" % (name, rows))
        o.append("static const uint16_t FONT_%s_ADV[] PROGMEM = {%s};\n" % (name, ",".join(str(a) for a in f["adv16"])))
        o.append("static const FontDef FONT_%s = { FONT_%s_GLYPHS, FONT_%s_ADV, FONT_%s_BMP, %d, %d };\n"
                 % (name, name, name, name, f["lineH"], f["ascent"]))
        print("%-7s %-18s %2dpx  %6d bytes bitmap (%d fases)" % (name, fname, size, len(f["blob"]), PHASES))

    icons = {
        "PAUSE": icon_pause(),
        "PLAY": icon_play(),
        "REWIND": icon_rewind(),
        "FORWARD": icon_forward(),
        "APPLE": icon_apple(),
        "SQUIRCLE": icon_squircle(),
        "NOTE_S": icon_note(17),
        "NOTE_L": icon_note(28),
    }
    for name, (arr, w, h) in icons.items():
        o.append("#define ICON_%s_W %d\n#define ICON_%s_H %d\n" % (name, w, name, h))
        o.append(c_bytes("ICON_%s" % name, arr.tobytes(), per_line=w))

    with open(OUT_H, "w") as fh:
        fh.write("\n".join(o))
    print("→ %s" % os.path.normpath(OUT_H))

    if preview:
        sheet = Image.new("RGB", (640, 260), (70, 40, 55))
        d = ImageDraw.Draw(sheet)
        x = 8
        for name, (arr, w, h) in icons.items():
            ic = Image.fromarray(arr, "L").resize((w * 6, h * 6), Image.NEAREST)
            sheet.paste((255, 255, 255), (x, 8), ic)
            x += w * 6 + 10
            if x > 560:
                break
        sheet.save(os.path.join(HERE, "assets_preview.png"))


if __name__ == "__main__":
    main()
