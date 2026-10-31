#!/usr/bin/env python3
"""
win_music_bridge.py — Apple Music (Windows) -> ESP32-S3 Dongle Bridge
=============================================================================
Lee en tiempo real el estado y la PORTADA REAL de Apple Music en Windows
(mediante Windows Media Control / SMTC) y la transmite al Pocket Dongle
ESP32-S3 a 921600 baudios con estética nativa de Apple.

Características en Windows:
 - Detección nativa de Apple Music (UWP / Win32) y reproductores compatibles por SMTC.
 - Extracción de carátula local de alta resolución (sin pérdida ni latencia).
 - Fallback automático a iTunes Search API / CDN de Apple.
 - Malla 3x3 de degradado ambiental (BGM:...) y portada BGR 48x48 px (ART:...).
 - Detección automática del puerto COM del ESP32-S3 (VID: 0x303A, PID: 0x1001).
 - Control bidireccional mediante el botón físico GPIO 0 (Play/Pause, Next, Prev).
 - Sincronización y control del volumen maestro de Windows.
"""

import sys
import os
import time
import threading
import signal
import argparse
import base64
import urllib.request
import urllib.parse
import json
import re
import io
import asyncio
from datetime import datetime, timezone

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("\033[91m[ERROR]\033[0m pyserial no está instalado.")
    print("Ejecuta:  pip install pyserial")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("\033[91m[ERROR]\033[0m Pillow no está instalado.")
    print("Ejecuta:  pip install Pillow")
    sys.exit(1)

try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SMTCManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus
    )
    from winsdk.windows.storage.streams import DataReader
except ImportError:
    print("\033[91m[ERROR]\033[0m winsdk no está instalado.")
    print("Ejecuta:  pip install winsdk")
    sys.exit(1)

# Pycaw para volumen del sistema
try:
    from ctypes import POINTER, cast
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    HAS_PYCAW = True
except Exception:
    HAS_PYCAW = False

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


def sanitize_str(s: str) -> str:
    if not s:
        return ""
    s = s.replace("|", "-").replace("\n", " ").replace("\r", "")
    s = re.sub(r"[\x00-\x1F\x7F]", "", s)
    return s.strip()


class MasterVolume:
    """Control de volumen maestro de Windows."""
    def __init__(self):
        self.ep = None
        self.enabled = HAS_PYCAW
        self._connect()

    def _connect(self):
        if not self.enabled:
            return
        try:
            dev = AudioUtilities.GetSpeakers()
            if hasattr(dev, "EndpointVolume"):
                self.ep = dev.EndpointVolume
            else:
                iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                self.ep = cast(iface, POINTER(IAudioEndpointVolume))
        except Exception:
            self.ep = None

    def get(self) -> int:
        if not self.ep:
            return 50
        try:
            val = float(self.ep.GetMasterVolumeLevelScalar())
            return int(round(val * 100))
        except Exception:
            self._connect()
            return 50

    def set(self, vol_percent: int):
        if not self.ep:
            return
        try:
            val = max(0.0, min(1.0, vol_percent / 100.0))
            self.ep.SetMasterVolumeLevelScalar(val, None)
        except Exception:
            self._connect()


def extract_bg_mesh(img: Image.Image) -> list[tuple[int, int, int]]:
    """
    Malla de 3×3 colores (fila a fila: TL TC TR / ML C MR / BL BC BR) para el
    degradado de fondo a pantalla completa, al estilo Apple Music.
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
            c = tuple(0.85 * c[i] + 0.15 * avg[i] for i in range(3))
            h, s_, v_ = colorsys.rgb_to_hsv(c[0] / 255, c[1] / 255, c[2] / 255)
            s_ = min(1.0, s_ * 1.2)
            v_ = 0.20 + 0.32 * v_
            r, g, b = colorsys.hsv_to_rgb(h, s_, v_)
            out.append((int(r * 255), int(g * 255), int(b * 255)))
        return out
    except Exception:
        return default


class ArtworkManager:
    def __init__(self):
        self.cache = {}

    def process_image(self, img: Image.Image, key: tuple) -> tuple[list[tuple[int, int, int]], list[str]] | None:
        try:
            bg_mesh = extract_bg_mesh(img)
            img_resized = img.convert("RGB").resize((ARTWORK_SIZE, ARTWORK_SIZE), Image.Resampling.LANCZOS)
            pixels = list(img_resized.getdata())

            lines = []
            for y in range(ARTWORK_SIZE):
                row_bytes = bytearray()
                for x in range(ARTWORK_SIZE):
                    r, g, b = pixels[y * ARTWORK_SIZE + x]
                    col16 = ((b & 0xF8) << 8) | ((g & 0xFC) << 3) | (r >> 3)
                    row_bytes.append((col16 >> 8) & 0xFF)
                    row_bytes.append(col16 & 0xFF)
                b64_str = base64.b64encode(row_bytes).decode('ascii')
                lines.append(b64_str)

            data = (bg_mesh, lines)
            self.cache[key] = data
            return data
        except Exception as e:
            print(f"{CLR_RED}[ARTWORK] Error al procesar imagen:{CLR_RESET} {e}")
            return None

    def fetch_itunes_artwork(self, title: str, artist: str) -> Image.Image | None:
        try:
            q = urllib.parse.quote(f"{artist} {title}".strip())
            url = f"https://itunes.apple.com/search?term={q}&entity=song&limit=1"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if data.get("resultCount", 0) > 0:
                    art_url = data["results"][0].get("artworkUrl100")
                    if art_url:
                        art_url = art_url.replace("100x100bb", "200x200bb")
                        art_req = urllib.request.Request(art_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(art_req, timeout=2.5) as art_resp:
                            return Image.open(io.BytesIO(art_resp.read()))
        except Exception:
            pass
        return None


def find_esp32_port() -> str | None:
    ports = serial.tools.list_ports.comports()
    # 1. ESP32-S3 nativo (Espressif VID 0x303A, PID 0x1001)
    for p in ports:
        if p.vid == 0x303A and p.pid == 0x1001:
            return p.device

    # 2. Cualquier chip USB Espressif
    for p in ports:
        if p.vid == 0x303A:
            return p.device

    # 3. Adaptadores comunes USB-Serial (CH340, CP210x, FTDI)
    for p in ports:
        desc = (p.description or "").lower()
        if "ch340" in desc or "cp210" in desc or "usb-serial" in desc or "espressif" in desc:
            return p.device

    # 4. Si solo hay un puerto COM disponible
    if len(ports) == 1:
        return ports[0].device

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
        print(f"  {p.device:12s} {vid_str}  {pid_str}  — {desc}")


# ─── Windows Media Manager (SMTC) ───────────────────────────────────────────
class WindowsMediaWatcher:
    """Observador del sistema de medios de Windows (SMTC)."""
    def __init__(self):
        self.mgr = None
        self.session = None

    async def init(self):
        self.mgr = await SMTCManager.request_async()

    def _pick_session(self):
        if not self.mgr:
            return None
        # Priorizar Apple Music si está abierto
        sessions = self.mgr.get_sessions()
        apple_session = None
        for s in sessions:
            src = (s.source_app_user_model_id or "").lower()
            if "applemusic" in src or "itunes" in src:
                apple_session = s
                break
        if apple_session:
            return apple_session
        return self.mgr.get_current_session()

    async def get_state(self) -> dict:
        if not self.mgr:
            await self.init()

        self.session = self._pick_session()
        if not self.session:
            return {
                "state": "stopped", "pos": 0, "duration": 0,
                "title": "", "artist": "", "album": "", "art_image": None
            }

        try:
            info = self.session.get_playback_info()
            status_enum = info.playback_status if info else PlaybackStatus.CLOSED
            if status_enum == PlaybackStatus.PLAYING:
                state_str = "playing"
            elif status_enum == PlaybackStatus.PAUSED:
                state_str = "paused"
            else:
                state_str = "stopped"

            props = await self.session.try_get_media_properties_async()
            if not props:
                return {
                    "state": state_str, "pos": 0, "duration": 0,
                    "title": "", "artist": "", "album": "", "art_image": None
                }

            title = sanitize_str(props.title or "")
            artist = sanitize_str(props.artist or "")
            album = sanitize_str(props.album_title or "")

            # Posición y duración
            pos, dur = 0, 0
            try:
                tl = self.session.get_timeline_properties()
                if tl:
                    pos = int(tl.position.total_seconds())
                    dur = int(tl.end_time.total_seconds())
            except Exception:
                pass

            # Carátula
            art_image = None
            if props.thumbnail:
                try:
                    stream = await props.thumbnail.open_read_async()
                    reader = DataReader(stream)
                    await reader.load_async(stream.size)
                    data = bytearray(stream.size)
                    reader.read_bytes(data)
                    art_image = Image.open(io.BytesIO(data))
                    art_image.load()
                except Exception:
                    art_image = None

            return {
                "state": state_str,
                "pos": pos,
                "duration": dur,
                "title": title,
                "artist": artist,
                "album": album,
                "art_image": art_image
            }
        except Exception:
            return {
                "state": "stopped", "pos": 0, "duration": 0,
                "title": "", "artist": "", "album": "", "art_image": None
            }

    async def play_pause(self):
        if self.session:
            try:
                await self.session.try_toggle_play_pause_async()
            except Exception:
                pass

    async def next_track(self):
        if self.session:
            try:
                await self.session.try_skip_next_async()
            except Exception:
                pass

    async def prev_track(self):
        if self.session:
            try:
                await self.session.try_skip_previous_async()
            except Exception:
                pass


# ─── Bridge Principal ────────────────────────────────────────────────────────
class DongleMusicWinBridge:
    def __init__(self, manual_port: str | None = None, baud: int = BAUD_RATE, verbose: bool = False):
        self.manual_port = manual_port
        self.baud        = baud
        self.verbose     = verbose
        self.ser         = None
        self.running     = False
        self._write_lock = threading.Lock()
        self.art_mgr     = ArtworkManager()
        self.media       = WindowsMediaWatcher()
        self.volume      = MasterVolume()

        # Cache de estado
        self._last_state = None
        self._last_track = ("", "", "")
        self._last_vol   = -1
        self._loop       = None

    def connect(self) -> bool:
        port = self.manual_port or find_esp32_port()
        if not port:
            print(f"{CLR_YELLOW}[BRIDGE] Buscando dongle ESP32-S3 en puertos COM...{CLR_RESET}")
            return False

        try:
            self.ser = serial.Serial()
            self.ser.port = port
            self.ser.baudrate = self.baud
            self.ser.timeout = 0.1
            self.ser.write_timeout = 2.0
            self.ser.dtr = False
            self.ser.rts = False
            self.ser.open()
            time.sleep(0.3)
            print(f"{CLR_GREEN}✔ Conectado exitosamente a {CLR_BOLD}{port}{CLR_RESET}{CLR_GREEN} @ {self.baud} baudios{CLR_RESET}")
            return True
        except (serial.SerialException, OSError) as e:
            print(f"{CLR_RED}[BRIDGE] Error al conectar con {port}: {e}{CLR_RESET}")
            self.ser = None
            return False

    def disconnect(self):
        with self._write_lock:
            if self.ser and self.ser.is_open:
                try: self.ser.close()
                except Exception: pass
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
            if self._loop:
                asyncio.run_coroutine_threadsafe(self.media.play_pause(), self._loop)
        elif cmd == "CMD:NEXT":
            print(f"{CLR_MAGENTA}🔘 Botón Dongle: [Siguiente Pista]{CLR_RESET}")
            if self._loop:
                asyncio.run_coroutine_threadsafe(self.media.next_track(), self._loop)
        elif cmd == "CMD:PREV":
            print(f"{CLR_MAGENTA}🔘 Botón Dongle: [Pista Anterior]{CLR_RESET}")
            if self._loop:
                asyncio.run_coroutine_threadsafe(self.media.prev_track(), self._loop)
        elif cmd.startswith("CMD:VOL:"):
            try:
                val = int(cmd.split(":")[2])
                self.volume.set(val)
                print(f"{CLR_MAGENTA}🔊 Volumen cambiado a {val}%{CLR_RESET}")
            except Exception:
                pass
        elif cmd.startswith("LOG:"):
            print(f"{CLR_YELLOW}[DONGLE] {cmd[4:]}{CLR_RESET}")
        elif self.verbose:
            print(f"  {CLR_YELLOW}← {cmd}{CLR_RESET}")

    def _stream_artwork(self, title: str, artist: str, art_image: Image.Image | None):
        """Descarga/procesa y transmite la malla de fondo y las 48 líneas de carátula."""
        token = f"{title}|{artist}"
        self._current_art_token = token
        key = (title, artist)

        art_data = self.art_mgr.cache.get(key)
        if not art_data:
            img = art_image or self.art_mgr.fetch_itunes_artwork(title, artist)
            if img:
                art_data = self.art_mgr.process_image(img, key)

        if not art_data:
            print(f"{CLR_YELLOW}⚠ Sin carátula (el dongle mostrará placeholder dinámico).{CLR_RESET}")
            return

        if getattr(self, "_current_art_token", None) != token:
            return

        bg_mesh, lines = art_data

        # 1. Enviar la malla de 9 colores (3x3) del fondo
        flat = "|".join(f"{r}|{g}|{b}" for r, g, b in bg_mesh)
        self.send(f"BGM:{flat}")
        time.sleep(0.01)

        # 2. Transmitir las 48 filas con cadencia de 8 ms
        print(f"{CLR_GREEN}🎨 Transmitiendo carátula (48×48 px)...{CLR_RESET}")
        for y, b64_row in enumerate(lines):
            if getattr(self, "_current_art_token", None) != token:
                return
            self.send(f"ART:{y}:{b64_row}")
            with self._write_lock:
                if self.ser and self.ser.is_open:
                    try: self.ser.flush()
                    except Exception: pass
            time.sleep(0.008)

        print(f"{CLR_GREEN}✔ Carátula cargada al 100%.{CLR_RESET}")

    async def _async_run(self):
        self._loop = asyncio.get_running_loop()
        await self.media.init()

        last_prog_time = 0.0
        last_ping_time = time.monotonic()

        while self.running:
            if not self.ser or not self.ser.is_open:
                if not self.connect():
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue

                self._last_state = None
                self._last_track = ("", "", "")
                self._last_vol   = -1

                t_read = threading.Thread(target=self._reader_loop, daemon=True)
                t_read.start()

            t0 = time.monotonic()
            st = await self.media.get_state()

            # 1. PING keepalive periódico
            if t0 - last_ping_time >= PING_INTERVAL:
                last_ping_time = t0
                self.send("PING")

            # 2. Cambio de canción
            t_key = (st["title"], st["artist"], st["album"])
            if t_key != self._last_track:
                self._last_track = t_key
                if st["title"]:
                    self.send(f"TRACK:{st['title']}|{st['artist']}|{st['album']}")
                    print(f"{CLR_CYAN}🎵 Reproduciendo:{CLR_RESET} {CLR_BOLD}{st['title']}{CLR_RESET} — {st['artist']} ({st['album']})")
                    threading.Thread(
                        target=self._stream_artwork,
                        args=(st["title"], st["artist"], st["art_image"]),
                        daemon=True
                    ).start()
                else:
                    self.send("TRACK:||")
                    print(f"{CLR_YELLOW}Esperando reproducción en Apple Music / Windows...{CLR_RESET}")

            # 3. Cambio de estado de reproducción
            if st["state"] != self._last_state:
                self._last_state = st["state"]
                self.send(f"STATE:{st['state']}")

            # 4. Progreso
            if (t0 - last_prog_time >= PROG_INTERVAL) and st["duration"] > 0:
                last_prog_time = t0
                self.send(f"PROG:{st['pos']}|{st['duration']}")

            # 5. Volumen del sistema
            cur_vol = self.volume.get()
            if cur_vol != self._last_vol:
                self._last_vol = cur_vol
                self.send(f"VOL:{cur_vol}")

            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.05, POLL_INTERVAL - elapsed))

    def run(self):
        self.running = True
        print(f"{CLR_BOLD}=================================================================={CLR_RESET}")
        print(f"  {CLR_RED} Apple Music → Pocket Dongle ESP32-S3 Bridge (Windows Edition){CLR_RESET}")
        print(f"{CLR_BOLD}=================================================================={CLR_RESET}")
        print(f"  Velocidad Serial : {self.baud} baudios")
        print(f"  Puerto COM       : {self.manual_port or 'Auto-detección (ESP32 / COMx)'}")
        print(f"  Resolución Art   : 48×48 px BGR 16-bit")
        print(f"  Fondo ambiental  : Malla dinámica 3×3 (BGM:...)")
        print(f"  Presiona Ctrl+C para salir.\n")

        try:
            asyncio.run(self._async_run())
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self.stop()

    def stop(self):
        self.running = False
        self.disconnect()
        print(f"\n{CLR_YELLOW}[BRIDGE] Desconectado.{CLR_RESET}")


def main():
    parser = argparse.ArgumentParser(description="Apple Music Windows Dongle Bridge")
    parser.add_argument("-p", "--port", help="Puerto serial manual (ej: COM4)")
    parser.add_argument("-b", "--baud", type=int, default=BAUD_RATE, help="Velocidad serial (default: 921600)")
    parser.add_argument("-l", "--list", action="store_true", help="Listar puertos seriales disponibles")
    parser.add_argument("-v", "--verbose", action="store_true", help="Modo detallado")
    args = parser.parse_args()

    if args.list:
        print("Puertos COM detectados en este equipo:")
        list_ports()
        sys.exit(0)

    bridge = DongleMusicWinBridge(manual_port=args.port, baud=args.baud, verbose=args.verbose)

    def sig_handler(sig, frame):
        bridge.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    bridge.run()


if __name__ == "__main__":
    main()
