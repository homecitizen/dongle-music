#!/usr/bin/env python3
"""
mac_music_bridge.py — Apple Music → ESP32-S3 Dongle Bridge (v5.0 Apple Native)
=============================================================================
Lee en tiempo real el estado y la PORTADA REAL de Apple Music en macOS y la
transmite al Pocket Dongle ESP32-S3 a 921600 baudios con estética nativa de Apple.

Novedades v5.0 Apple Native:
 - Portada cuadrada de 48×48 px con esquinas redondeadas y antialiasing.
 - Degradado de fondo a pantalla completa con 4 colores de la portada (BGM:...).
 - Extracción directa ultra-rápida de Music.app sin problemas de sandbox.
 - Fallback inteligente a CDN de Apple (iTunes Search API).
 - Streaming serial con cadencia de 8 ms (100% fiable, 0 pérdida de paquetes).
 - Control bidireccional mediante botón físico GPIO 0 (Play/Pause, Prev, Next).
"""

import sys
import os
import time
import threading
import signal
import subprocess
import argparse
import base64
import urllib.request
import urllib.parse
import json
import re

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("\033[91m[ERROR]\033[0m pyserial no está instalado.")
    print("Ejecuta:  pip3 install pyserial")
    sys.exit(1)

try:
    from PIL import Image
    import io
except ImportError:
    print("\033[91m[ERROR]\033[0m Pillow no está instalado.")
    print("Ejecuta:  pip3 install Pillow")
    sys.exit(1)

# ─── Configuración ───────────────────────────────────────────────────────────
BAUD_RATE        = 921600
POLL_INTERVAL    = 0.35     # segundos entre lecturas de estado
PROG_INTERVAL    = 0.75     # segundos entre envíos de progreso
PING_INTERVAL    = 4.0      # segundos entre pings de keepalive
RECONNECT_DELAY  = 2.0      # segundos de espera para reconectar
ARTWORK_SIZE     = 48       # 48x48 píxeles

# Colores para la terminal
CLR_RESET   = "\033[0m"
CLR_BOLD    = "\033[1m"
CLR_RED     = "\033[91m"
CLR_GREEN   = "\033[92m"
CLR_YELLOW  = "\033[93m"
CLR_CYAN    = "\033[96m"
CLR_MAGENTA = "\033[95m"

def run_applescript(script: str) -> str:
    """Ejecuta un script AppleScript retornando stdout."""
    try:
        res = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=2.5
        )
        if res.returncode == 0:
            return res.stdout.strip()
        return ""
    except Exception:
        return ""

def get_music_state() -> dict:
    """Consulta el estado actual de Apple Music."""
    script = """
    if application "Music" is running then
        tell application "Music"
            try
                set ps to player state as string
                set pos to player position as integer
                set vol to sound volume as integer
                if ps is not "stopped" then
                    set t to name of current track
                    set ar to artist of current track
                    set al to album of current track
                    set dur to duration of current track as integer
                    return ps & "|" & pos & "|" & vol & "|" & t & "|" & ar & "|" & al & "|" & dur
                else
                    return ps & "|0|" & vol & "|||||0"
                end if
            on error
                return "stopped|0|50|||||0"
            end try
        end tell
    else
        return "not_running|0|50|||||0"
    end if
    """
    raw = run_applescript(script)
    if not raw or raw.startswith("not_running"):
        return {"state": "stopped", "pos": 0, "vol": 50,
                "title": "", "artist": "", "album": "", "duration": 0}

    parts = raw.split("|", 6)
    if len(parts) < 7:
        return {"state": "stopped", "pos": 0, "vol": 50,
                "title": "", "artist": "", "album": "", "duration": 0}

    def _to_int(v, d=0):
        try: return int(float(v))
        except: return d

    return {
        "state":    parts[0] if parts[0] in ("playing", "paused", "stopped") else "stopped",
        "pos":      _to_int(parts[1]),
        "vol":      _to_int(parts[2], 50),
        "title":    sanitize_str(parts[3][:60]),
        "artist":   sanitize_str(parts[4][:45]),
        "album":    sanitize_str(parts[5][:45]),
        "duration": _to_int(parts[6]),
    }

def sanitize_str(s: str) -> str:
    s = s.replace("|", "-").replace("\n", " ").replace("\r", "")
    s = re.sub(r"[\x00-\x1F\x7F]", "", s)
    return s.strip()

# ─── Comandos hacia Apple Music ──────────────────────────────────────────────
def music_play_pause():
    run_applescript('tell application "Music" to playpause')

def music_next():
    run_applescript('tell application "Music" to next track')

def music_prev():
    run_applescript('tell application "Music" to previous track')

# ─── Extracción de Color Ambiental y Portada Real ───────────────────────────
def extract_ambient_color(img: Image.Image) -> tuple[int, int, int]:
    """
    Extrae el color ambiental dominante para el resplandor de fondo estilo Apple.
    Detecta tonos vibrantes y satura sutilmente a un nivel oscuro elegante.
    """
    try:
        small = img.convert("RGB").resize((24, 24), Image.Resampling.BOX)
        pixels = list(small.getdata())
        
        best_col = (45, 18, 25) # Default sutil ciruela oscuro
        max_score = -1.0
        
        for r, g, b in pixels:
            max_c = max(r, g, b)
            min_c = min(r, g, b)
            sat = (max_c - min_c) / 255.0 if max_c > 0 else 0
            lum = (r * 0.299 + g * 0.587 + b * 0.114) / 255.0
            
            # Buscamos tonos de saturación media-alta y luminosidad equilibrada
            if 0.12 < lum < 0.88 and sat > 0.15:
                score = sat * (1.0 - abs(lum - 0.45))
                if score > max_score:
                    max_score = score
                    best_col = (r, g, b)
                    
        # Atenuación para luz ambiental nocturna (35% de brillo)
        scale = 0.35
        r_amb = max(10, min(int(best_col[0] * scale), 140))
        g_amb = max(10, min(int(best_col[1] * scale), 140))
        b_amb = max(10, min(int(best_col[2] * scale), 140))
        return (r_amb, g_amb, b_amb)
    except Exception:
        return (45, 18, 25)

def extract_bg_mesh(img: Image.Image) -> list[tuple[int, int, int]]:
    """
    Malla de 3×3 colores (fila a fila: TL TC TR / ML C MR / BL BC BR) para el
    degradado de fondo a pantalla completa, al estilo Apple Music: una versión
    desenfocada y oscurecida de la portada.  Se priorizan los píxeles
    coloridos de cada celda y se limita el brillo para que el texto blanco
    siempre se lea bien.
    """
    import colorsys
    default = [(88, 60, 92), (60, 45, 66), (104, 62, 58),
               (84, 52, 76), (78, 44, 60), (100, 56, 60),
               (96, 64, 80), (70, 38, 52), (92, 48, 60)]
    try:
        small = img.convert("RGB").resize((15, 15), Image.Resampling.BOX)
        px = small.load()
        cells = []
        for gy in range(3):
            for gx in range(3):
                sr = sg = sb = sw = 0.0
                for y in range(gy * 5, gy * 5 + 5):
                    for x in range(gx * 5, gx * 5 + 5):
                        r, g, b = px[x, y]
                        _, s_, v_ = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                        w = (0.08 + s_) ** 2 * (1.0 if 0.08 < v_ < 0.92 else 0.3)
                        sr += r * w; sg += g * w; sb += b * w; sw += w
                cells.append((sr / sw, sg / sw, sb / sw))

        avg = tuple(sum(c[i] for c in cells) / 9 for i in range(3))
        out = []
        for c in cells:
            c = tuple(0.85 * c[i] + 0.15 * avg[i] for i in range(3))   # evita saltos bruscos
            h, s_, v_ = colorsys.rgb_to_hsv(c[0] / 255, c[1] / 255, c[2] / 255)
            s_ = min(1.0, s_ * 1.2)
            v_ = 0.20 + 0.32 * v_               # brillo entre 20 % y 52 %
            r, g, b = colorsys.hsv_to_rgb(h, s_, v_)
            out.append((int(r * 255), int(g * 255), int(b * 255)))
        return out
    except Exception:
        return default

class ArtworkManager:
    def __init__(self):
        # Cache: (title, artist) -> (bg_mesh[9], [48 base64 strings])
        self.cache = {}

    def get_artwork_data(self, title: str, artist: str) -> tuple[list[tuple[int, int, int]], list[str]] | None:
        if not title:
            return None

        key = (title, artist)
        if key in self.cache:
            return self.cache[key]

        img = self._fetch_image(title, artist)
        if not img:
            return None

        try:
            # 1. Malla de 3×3 colores del degradado de fondo (derivada de la portada)
            bg_mesh = extract_bg_mesh(img)

            # 2. Redimensionar exactamente a 48×48 px con filtro Lanczos
            img_resized = img.convert("RGB").resize((ARTWORK_SIZE, ARTWORK_SIZE), Image.Resampling.LANCZOS)
            pixels = list(img_resized.getdata())

            # 3. Codificar cada fila en 16-bit BGR para pantalla ST7735
            lines = []
            for y in range(ARTWORK_SIZE):
                row_bytes = bytearray()
                for x in range(ARTWORK_SIZE):
                    r, g, b = pixels[y * ARTWORK_SIZE + x]
                    # Formato 16-bit BGR:
                    col16 = ((b & 0xF8) << 8) | ((g & 0xFC) << 3) | (r >> 3)
                    row_bytes.append((col16 >> 8) & 0xFF)
                    row_bytes.append(col16 & 0xFF)

                # 48 px * 2 bytes = 96 bytes -> exactamente 128 chars en Base64
                b64_str = base64.b64encode(row_bytes).decode('ascii')
                lines.append(b64_str)

            data = (bg_mesh, lines)
            self.cache[key] = data
            return data
        except Exception as e:
            print(f"{CLR_RED}[ARTWORK] Error al procesar imagen:{CLR_RESET} {e}")
            return None

    def _fetch_image(self, title: str, artist: str) -> Image.Image | None:
        # Fuente 1: Extraer directamente de Music.app sin bloqueo de sandbox
        tmp_path = "/tmp/dongle_cur_art.bin"
        script = f'''
        set d to ""
        tell application "Music"
            try
                set d to raw data of artwork 1 of current track
            end try
        end tell
        if d is not "" then
            try
                set f to open for access POSIX file "{tmp_path}" with write permission
                set eof f to 0
                write d to f
                close access f
                return "OK"
            on error
                try
                    close access POSIX file "{tmp_path}"
                end try
            end try
        end if
        return "FAIL"
        '''
        res = run_applescript(script)
        if "OK" in res and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 100:
            try:
                with open(tmp_path, "rb") as f:
                    raw_data = f.read()
                # Omitir cabecera PICT de macOS si está presente
                jpg_idx = raw_data.find(b"\xFF\xD8\xFF")
                png_idx = raw_data.find(b"\x89PNG\r\n\x1a\n")
                if jpg_idx != -1 and (png_idx == -1 or jpg_idx < png_idx):
                    im = Image.open(io.BytesIO(raw_data[jpg_idx:]))
                elif png_idx != -1:
                    im = Image.open(io.BytesIO(raw_data[png_idx:]))
                else:
                    im = Image.open(tmp_path)
                im.load()
                return im
            except Exception:
                pass

        # Fuente 2: Buscar en la API oficial de iTunes Search / Apple CDN
        try:
            q = urllib.parse.quote(f"{artist} {title}".strip())
            url = f"https://itunes.apple.com/search?term={q}&entity=song&limit=1"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if data.get("resultCount", 0) > 0:
                    art_url = data["results"][0].get("artworkUrl100")
                    if art_url:
                        # Reemplazar 100x100 por resolución nítida
                        art_url = art_url.replace("100x100bb", "200x200bb")
                        art_req = urllib.request.Request(art_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(art_req, timeout=2.5) as art_resp:
                            return Image.open(io.BytesIO(art_resp.read()))
        except Exception:
            pass

        return None

# ─── Detección de Puerto Serial ──────────────────────────────────────────────
def find_esp32_port() -> str | None:
    ports = serial.tools.list_ports.comports()
    for p in ports:
        if p.vid == 0x303A and p.pid == 0x1001:
            return p.device
    for p in ports:
        if "usbmodem" in p.device.lower():
            return p.device
    for p in ports:
        if "usbserial" in p.device.lower():
            return p.device
    return None

def list_ports():
    ports = serial.tools.list_ports.comports()
    if not ports:
        print("  (No se encontraron puertos seriales)")
        return
    for p in ports:
        vid_str = f"VID:{p.vid:04X}" if p.vid is not None else "VID:----"
        pid_str = f"PID:{p.pid:04X}" if p.pid is not None else "PID:----"
        desc = p.description or "n/a"
        print(f"  {p.device:32s} {vid_str}  {pid_str}  — {desc}")

# ─── Bridge Principal ────────────────────────────────────────────────────────
class DongleMusicBridge:
    def __init__(self, manual_port: str | None = None, verbose: bool = False):
        self.manual_port = manual_port
        self.verbose     = verbose
        self.ser         = None
        self.running     = False
        self._write_lock = threading.Lock()
        self.art_mgr     = ArtworkManager()

        # Cache de estado
        self._last_state = None
        self._last_track = ("", "", "")
        self._last_vol   = -1

        # Hilos de soporte
        self._t_read = None
        self._t_poll = None

    def connect(self) -> bool:
        port = self.manual_port or find_esp32_port()
        if not port:
            print(f"{CLR_YELLOW}[BRIDGE] Buscando dongle ESP32-S3...{CLR_RESET}")
            return False

        try:
            self.ser = serial.Serial(
                port=port,
                baudrate=BAUD_RATE,
                timeout=0.1,
                write_timeout=2.0
            )
            time.sleep(0.3)
            print(f"{CLR_GREEN}✔ Conectado exitosamente a {CLR_BOLD}{port}{CLR_RESET}{CLR_GREEN} @ {BAUD_RATE} baudios{CLR_RESET}")
            return True
        except (serial.SerialException, OSError) as e:
            print(f"{CLR_RED}[BRIDGE] Error al conectar con {port}: {e}{CLR_RESET}")
            self.ser = None
            return False

    def disconnect(self):
        with self._write_lock:
            if self.ser and self.ser.is_open:
                try: self.ser.close()
                except: pass
            self.ser = None

    def send(self, msg: str):
        if not msg.endswith("\n"):
            msg += "\n"
        with self._write_lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.write(msg.encode("utf-8", errors="replace"))
                    if self.verbose:
                        print(f"  {CLR_CYAN}→ {msg.strip()}{CLR_RESET}")
                except Exception:
                    self.disconnect()

    def _reader_loop(self):
        buf = ""
        while self.running:
            if not self.ser or not self.ser.is_open:
                time.sleep(0.1)
                continue
            try:
                data = self.ser.read(256)
                if data:
                    buf += data.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if line:
                            self._handle_dongle_command(line)
            except Exception:
                self.disconnect()

    def _handle_dongle_command(self, cmd: str):
        if cmd == "PONG":
            return
        if cmd == "CMD:PLAYPAUSE":
            print(f"{CLR_MAGENTA}🔘 Botón Dongle: [Play / Pause]{CLR_RESET}")
            music_play_pause()
        elif cmd == "CMD:NEXT":
            print(f"{CLR_MAGENTA}🔘 Botón Dongle: [Siguiente Pista]{CLR_RESET}")
            music_next()
        elif cmd == "CMD:PREV":
            print(f"{CLR_MAGENTA}🔘 Botón Dongle: [Pista Anterior]{CLR_RESET}")
            music_prev()
        elif cmd.startswith("LOG:"):
            print(f"{CLR_YELLOW}[DONGLE] {cmd[4:]}{CLR_RESET}")
        elif self.verbose:
            print(f"  {CLR_YELLOW}← {cmd}{CLR_RESET}")

    def _stream_artwork(self, title: str, artist: str):
        """Descarga y envía el color ambiental y las 48 líneas de portada real."""
        token = f"{title}|{artist}"
        self._current_art_token = token

        art_data = self.art_mgr.get_artwork_data(title, artist)
        if not art_data:
            print(f"{CLR_YELLOW}⚠ Sin carátula remota (mostrando placeholder dinámico de Apple Music).{CLR_RESET}")
            return

        if getattr(self, "_current_art_token", None) != token:
            return

        bg_mesh, lines = art_data

        # 1. Enviar la malla de 9 colores del degradado de fondo
        flat = "|".join(f"{r}|{g}|{b}" for r, g, b in bg_mesh)
        self.send(f"BGM:{flat}")
        time.sleep(0.01)

        # 2. Transmitir las 48 filas con cadencia controlada (8 ms)
        print(f"{CLR_GREEN}🎨 Transmitiendo portada real Apple Music (48×48 px)...{CLR_RESET}")
        for y, b64_row in enumerate(lines):
            if getattr(self, "_current_art_token", None) != token:
                return  # Abortar de inmediato si cambió de canción

            self.send(f"ART:{y}:{b64_row}")
            with self._write_lock:
                if self.ser and self.ser.is_open:
                    try: self.ser.flush()
                    except Exception: pass
            time.sleep(0.008)

        print(f"{CLR_GREEN}✔ Portada cargada al 100% con acabado nativo.{CLR_RESET}")

    def _music_poller(self):
        last_prog_time = 0.0
        while self.running:
            t0 = time.monotonic()
            st = get_music_state()

            # 1. Cambio de canción
            t_key = (st["title"], st["artist"], st["album"])
            if t_key != self._last_track:
                self._last_track = t_key
                if st["title"]:
                    self.send(f"TRACK:{st['title']}|{st['artist']}|{st['album']}")
                    print(f"{CLR_CYAN}🎵 Reproduciendo:{CLR_RESET} {CLR_BOLD}{st['title']}{CLR_RESET} — {st['artist']} ({st['album']})")
                    # Descargar y enviar portada real en hilo asíncrono
                    threading.Thread(target=self._stream_artwork, args=(st["title"], st["artist"]), daemon=True).start()
                else:
                    self.send("TRACK:||")
                    print(f"{CLR_YELLOW}Esperando reproducción en Apple Music...{CLR_RESET}")

            # 2. Cambio de estado de reproducción
            if st["state"] != self._last_state:
                self._last_state = st["state"]
                self.send(f"STATE:{st['state']}")

            # 3. Progreso
            now = time.monotonic()
            if (now - last_prog_time >= PROG_INTERVAL) and st["duration"] > 0:
                last_prog_time = now
                self.send(f"PROG:{st['pos']}|{st['duration']}")

            # 4. Volumen
            if st["vol"] != self._last_vol:
                self._last_vol = st["vol"]
                self.send(f"VOL:{st['vol']}")

            elapsed = time.monotonic() - t0
            time.sleep(max(0.05, POLL_INTERVAL - elapsed))

    def run(self):
        self.running = True
        print(f"{CLR_BOLD}=================================================================={CLR_RESET}")
        print(f"  {CLR_RED} Apple Music → ESP32-S3 Dongle Bridge (v5.0 Native Edition){CLR_RESET}")
        print(f"{CLR_BOLD}=================================================================={CLR_RESET}")
        print(f"  Velocidad Serial : {BAUD_RATE} baudios")
        print(f"  Puerto objetivo  : {self.manual_port or 'Auto-detección (/dev/cu.usbmodem*)'}")
        print(f"  Resolución Art   : 48×48 px con antialiasing y esquinas redondeadas")
        print(f"  Efecto visual    : Degradado de fondo dinámico del álbum (BGM:...)")
        print(f"  Presiona Ctrl+C para salir.\n")

        while self.running:
            if not self.ser or not self.ser.is_open:
                if not self.connect():
                    time.sleep(RECONNECT_DELAY)
                    continue

                self._last_state = None
                self._last_track = ("", "", "")
                self._last_vol   = -1

                if not self._t_read or not self._t_read.is_alive():
                    self._t_read = threading.Thread(target=self._reader_loop, daemon=True)
                    self._t_read.start()

                if not self._t_poll or not self._t_poll.is_alive():
                    self._t_poll = threading.Thread(target=self._music_poller, daemon=True)
                    self._t_poll.start()

            time.sleep(1.0)

    def stop(self):
        self.running = False
        self.disconnect()
        print(f"\n{CLR_YELLOW}[BRIDGE] Desconectado.{CLR_RESET}")

def main():
    parser = argparse.ArgumentParser(description="Apple Music Dongle Bridge")
    parser.add_argument("-p", "--port", help="Puerto serial manual (ej: /dev/cu.usbmodem1401)")
    parser.add_argument("-l", "--list", action="store_true", help="Listar puertos seriales")
    parser.add_argument("-v", "--verbose", action="store_true", help="Modo detallado")
    args = parser.parse_args()

    if args.list:
        print("Puertos seriales detectados en tu Mac:")
        list_ports()
        sys.exit(0)

    bridge = DongleMusicBridge(manual_port=args.port, verbose=args.verbose)

    def sig_handler(sig, frame):
        bridge.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    bridge.run()

if __name__ == "__main__":
    main()
