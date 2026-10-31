# Mini Display & Controlador de Apple Music
## ESP32-S3 + ST7735 80×160 (macOS & Windows 10/11)

---

## Estructura del proyecto

```
dongle_music/
├── win_music_bridge.py         ← Script Python para Windows 10 / 11 (SMTC nativo)
├── iniciar_windows.bat         ← Lanzador rápido para Windows (doble clic)
├── mac_music_bridge.py         ← Script Python para macOS (AppleScript)
├── requirements.txt            ← Dependencias de Python para Windows y macOS
├── platformio.ini
├── firmware/src/
│   ├── main.cpp                ← Hardware, serie, botón (PlatformIO)
│   ├── ui_render.h             ← Motor de dibujo (framebuffer, fondo, texto, iconos)
│   └── ui_assets.h             ← Fuentes e iconos GENERADOS (no editar a mano)
├── dongle_music/               ← Mismo firmware para Arduino IDE
│   ├── dongle_music.ino        (copia de main.cpp)
│   ├── ui_render.h
│   └── ui_assets.h
└── tools/
    ├── gen_assets.py           ← Regenera ui_assets.h (fuentes Roboto + iconos)
    ├── preview.py / preview.cpp← Vista previa del diseño en el PC
    └── fonts/                  ← Roboto (Apache 2.0)
```

> Si cambias `main.cpp` o `ui_render.h`, copia también los cambios a
> `dongle_music/` (o viceversa) para mantener ambas carpetas iguales.

---

## Hardware requerido

| Componente | Detalle |
|---|---|
| Microcontrolador | ESP32-S3 (Flash 16 MB, USB CDC On Boot) |
| Pantalla | ST7735 · Greentab / `INITR_MINI160x80_PLUGIN` |
| Resolución | 80 × 160 px (landscape: 160 ancho × 80 alto) |
| Modo color | BGR + `invertDisplay(true)` |
| Velocidad SPI | 40 MHz |

### Pines

| Señal | GPIO |
|---|---|
| MOSI | 11 |
| SCLK | 10 |
| CS | 12 |
| DC | 13 |
| RST | 14 |
| BL (backlight) | 38 → HIGH = encendido |
| Botón usuario | GPIO 0 (BOOT) |

---

## Configuración Arduino IDE

### 1. Placa
- **Board Manager URL**: `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
- **Board**: `ESP32S3 Dev Module`
- **USB Mode**: `USB CDC and JTAG`
- **USB CDC On Boot**: `Enabled`
- **Flash Size**: `16MB (128Mb)`
- **Partition Scheme**: `16M Flash (3MB APP/9.9MB FATFS)` (o cualquiera con ≥3 MB app)
- **Upload Speed**: `921600`

### 2. Librerías (Gestor de librerías)
```
Adafruit ST7735 and ST7789 Library   ← buscar "ST7735"
Adafruit GFX Library                 ← dependencia automática
Adafruit BusIO                       ← dependencia automática
```

---

## Instalación y ejecución del script Python

### En Windows 10 / 11

1. Instala las dependencias:
```powershell
pip install -r requirements.txt
```

2. Ejecuta el puente con el dongle conectado por USB:
```powershell
# Detección automática del puerto COM
python win_music_bridge.py

# O especificando puerto manualmente
python win_music_bridge.py --port COM4

# Listar puertos COM disponibles
python win_music_bridge.py --list
```
*(También puedes hacer doble clic en `iniciar_windows.bat`).*

> **Cómo funciona en Windows**: Utiliza las APIs nativas de Windows Runtime (SMTC) para comunicarse directamente con Apple Music para Windows (`AppleMusic.exe`), extrayendo portada a resolución completa, estados de reproducción, tiempos de pista y control del botón físico.

---

### En macOS

1. Instala las dependencias:
```bash
pip3 install -r requirements.txt
```

2. Ejecuta el script:
```bash
# Detección automática del puerto
python3 mac_music_bridge.py

# Especificar puerto manualmente
python3 mac_music_bridge.py --port /dev/cu.usbmodem1234

# Modo verbose (ver todos los mensajes)
python3 mac_music_bridge.py --verbose

# Listar puertos disponibles
python3 mac_music_bridge.py --list-ports
```

> **Nota de permisos en macOS**: La primera ejecución puede requerir
> autorizar el acceso a _Automatización_ para que AppleScript controle
> `Music.app`. Acepta el diálogo del sistema.

---

## Protocolo de comunicación (USB Serial · 921600 baud)

### PC / Mac → ESP32

| Mensaje | Descripción |
|---|---|
| `TRACK:<título>\|<artista>\|<álbum>\n` | Metadatos de la pista activa |
| `PROG:<pos_s>\|<dur_s>\n` | Posición y duración en segundos |
| `STATE:<playing\|paused\|stopped>\n` | Estado del reproductor |
| `VOL:<0-100>\n` | Volumen del sistema (no se dibuja) |
| `BGM:<r\|g\|b>×9\n` | Malla 3×3 de colores del fondo (TL TC TR / ML C MR / BL BC BR) |
| `ART:<y>:<base64>\n` | Fila `y` (0-47) de la portada, 48 px RGB565 / BGR |
| `BG:<r>\|<g>\|<b>\n` | (antiguo) un solo color de fondo, aún aceptado |
| `PING\n` | Keepalive (cada 4-5 s) |

### ESP32 → PC / Mac

| Mensaje | Descripción |
|---|---|
| `PONG\n` | Respuesta al PING |
| `CMD:PLAYPAUSE\n` | Solicita play/pause (botón corto) |
| `CMD:NEXT\n` | Solicita siguiente pista (botón largo) |
| `CMD:PREV\n` | Solicita pista anterior |
| `CMD:VOL:<0-100>\n` | Solicita cambio de volumen |
| `LOG:<texto>\n` | Mensaje de depuración del firmware |

---

## UI de la pantalla (160 × 80)

```
┌────────────────────────────────────────────────┐
│ ┌──────────┐   Apple Music                     │  ← baseline y=15
│ │ portada  │   Título (Bold 12 px, scroll)     │  ← baseline y=30
│ │  48×48   │   Artista (Medium 12 px)          │  ← baseline y=43
│ └──────────┘                                   │
│   ◀◀              ❚❚  / ▶              ▶▶     │  ← centro y=64
└────────────────────────────────────────────────┘
        fondo: degradado 3×3 con los colores de la portada
```

- Todo se compone en un framebuffer RGB565 y se envía de una vez (sin parpadeo).
- Texto UTF-8: ASCII, Latin-1 y Latin Extendido-A (acentos, ñ, ç, ł…). Otros
  alfabetos (CJK, cirílico) y emojis se muestran como `?`.
- Títulos largos: pausa, scroll suave y desvanecido en los bordes.
- **Barra de progreso opcional**: compila con `-DSHOW_PROGRESS=1` (o cambia el
  `#define` en `ui_render.h`) para añadir una barra fina en el borde inferior.
- **Vista previa sin flashear**: `python tools/preview.py portada.jpg`
- **Cambiar tipografía/tamaños**: edita `FACES` en `tools/gen_assets.py` y ejecuta
  `pip install pillow numpy freetype-py && python tools/gen_assets.py`.

### Comportamiento del botón (GPIO 0)

| Acción | Resultado |
|---|---|
| Pulsación corta (<800 ms) | Play / Pause |
| Pulsación larga (≥800 ms) | Siguiente pista |

---

## Solución de problemas

| Síntoma | Causa probable | Solución |
|---|---|---|
| Pantalla en blanco | Backlight apagado | Verificar `TFT_BL=38` en `HIGH` |
| Colores invertidos | Falta `invertDisplay(true)` | Comprobar que está en `setup()` |
| Pantalla rotada | Rotación incorrecta | Probar `setRotation(1)` en vez de `3` |
| Puerto no detectado en Windows | Driver o cable solo de carga | Instalar driver CP210x o usar cable con datos |
| Puerto no detectado en ESP32-S3 | USB CDC no habilitado | Activar *USB CDC On Boot* en Arduino IDE o PlatformIO |
| Caracteres corruptos | Baudios incorrectos | Confirmar 921600 en ambos lados |
| AppleScript denegado (Mac) | Sin permisos de automatización | Sistema > Privacidad > Automatización |
