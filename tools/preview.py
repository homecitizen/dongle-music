#!/usr/bin/env python3
"""
preview.py — Vista previa del diseño en el PC (sin flashear la placa).

Compila el MISMO motor de dibujo del firmware (firmware/src/ui_render.h) y
genera un PNG ampliado.  Los colores de fondo salen de la misma función que
usa mac_music_bridge.py, así que lo que ves es lo que dibujará el dongle.

    python3 tools/preview.py portada.jpg --title "Blinding Lights" --artist "The Weeknd"
    python3 tools/preview.py portada.jpg --paused
    python3 tools/preview.py --standby
Requisitos: g++, pillow, pyserial (por el import del bridge).
"""
import argparse, os, struct, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("cover", nargs="?", help="imagen de portada (opcional)")
ap.add_argument("--title", default="Blinding Lights")
ap.add_argument("--artist", default="The Weeknd")
ap.add_argument("--paused", action="store_true")
ap.add_argument("--standby", action="store_true")
ap.add_argument("--scroll", type=int, default=0, help="desplazamiento del texto largo (px)")
ap.add_argument("--out", default=os.path.join(HERE, "preview.png"))
a = ap.parse_args()

exe = os.path.join(tempfile.gettempdir(), "dongle_preview")
subprocess.run(["g++", "-O2", "-std=c++17", "-I" + os.path.join(ROOT, "firmware", "src"),
                os.path.join(HERE, "preview.cpp"), "-o", exe], check=True, stderr=subprocess.DEVNULL)

cmd = [exe, "--out", exe + ".ppm", "--title", a.title, "--artist", a.artist,
       "--state", "2" if a.paused else "1", "--toff", str(a.scroll)]
if a.standby:
    cmd.append("--standby")
elif a.cover:
    import mac_music_bridge as br
    img = Image.open(a.cover).convert("RGB")
    mesh = br.extract_bg_mesh(img)
    small = img.resize((48, 48), Image.Resampling.LANCZOS)
    raw = bytearray()
    for y in range(48):
        for x in range(48):
            r, g, b = small.getpixel((x, y))
            raw += struct.pack("<H", ((b & 0xF8) << 8) | ((g & 0xFC) << 3) | (r >> 3))
    open(exe + ".cover", "wb").write(raw)
    cmd += ["--cover", exe + ".cover", "--mesh", "|".join(f"{r}|{g}|{b}" for r, g, b in mesh)]
subprocess.run(cmd, check=True)
Image.open(exe + ".ppm").resize((640, 320), Image.LANCZOS).save(a.out)
print("→", a.out)
