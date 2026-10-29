/*
 * dongle_music.ino  — v6.0  "Apple Music Fluid Edition"
 * ====================================================
 * Mini Display & Controlador estilo Apple Music para ESP32-S3 Dongle.
 *
 *  - Fondo degradado a pantalla completa (malla 3×3 con los colores de la
 *    portada, como el fondo "fluido" de Apple Music).
 *  - Portada real 48×48 con esquinas redondeadas anti-alias.
 *  - Tipografía Roboto suave (anti-aliasing + espaciado subpíxel).
 *  - Controles grandes con esquinas redondeadas: ◀◀  ❚❚ / ▶  ▶▶.
 *  - Todo se compone en un framebuffer de 160×80 y se envía de una vez:
 *    sin parpadeo ni recuadros detrás del texto.
 *  - Botón físico (GPIO 0): Tap, Doble-Tap, Pulsación larga.
 *
 *  El dibujo vive en ui_render.h; las fuentes e iconos en ui_assets.h
 *  (generado con tools/gen_assets.py).
 */

#include <Arduino.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7735.h>
#include <SPI.h>

#include "ui_render.h"

// ═══════════════════════════════════════════════════════════════
//  PINES DE HARDWARE
// ═══════════════════════════════════════════════════════════════
#define TFT_MOSI  11
#define TFT_SCLK  10
#define TFT_CS    12
#define TFT_DC    13
#define TFT_RST   14
#define TFT_BL    38
#define BTN_PIN    0   // GPIO 0 / BOOT

// ═══════════════════════════════════════════════════════════════
//  DRIVER ST7735 CON CALIBRACIÓN EXACTA DE BORDES
// ═══════════════════════════════════════════════════════════════
class DongleDisplay : public Adafruit_ST7735 {
public:
    DongleDisplay(SPIClass *spiClass, int8_t cs, int8_t dc, int8_t rst)
        : Adafruit_ST7735(spiClass, cs, dc, rst) {}

    void setCalibrationOffsets(uint8_t xs, uint8_t ys) {
        _xstart = xs;
        _ystart = ys;
    }

    void clearAllControllerRam() {
        startWrite();
        writeCommand(ST77XX_CASET);
        spiWrite(0x00); spiWrite(0x00);
        spiWrite(0x00); spiWrite(161);
        writeCommand(ST77XX_RASET);
        spiWrite(0x00); spiWrite(0x00);
        spiWrite(0x00); spiWrite(131);
        writeCommand(ST77XX_RAMWR);
        for (uint32_t i = 0; i < 162UL * 132UL; i++) {
            spiWrite(0x00);
            spiWrite(0x00);
        }
        endWrite();
    }
};

SPIClass *spi = new SPIClass(FSPI);
DongleDisplay tft = DongleDisplay(spi, TFT_CS, TFT_DC, TFT_RST);

// ═══════════════════════════════════════════════════════════════
//  ESTADO
// ═══════════════════════════════════════════════════════════════
static TrackState music;                 // definido en ui_render.h
static bool       needRender  = true;    // hay algo nuevo que dibujar
static uint32_t   lastFrameMs = 0;
static uint32_t   lastSecond  = 0;

#define FRAME_MIN_MS 30                  // máx. ~33 fps

// ═══════════════════════════════════════════════════════════════
//  BOTÓN DE USUARIO (GPIO 0)
// ═══════════════════════════════════════════════════════════════
enum ButtonAction { BTN_ACT_NONE, BTN_ACT_TAP, BTN_ACT_DOUBLE, BTN_ACT_HOLD };

struct ButtonHandler {
    bool     raw          = HIGH;
    bool     stable       = HIGH;
    uint32_t lastBounce   = 0;
    bool     isPressed    = false;
    uint32_t pressStart   = 0;
    bool     holdTriggered= false;
    uint8_t  tapCount     = 0;
    bool     waitSecondTap= false;
    uint32_t tapWaitStart = 0;
};
static ButtonHandler btn;

// ═══════════════════════════════════════════════════════════════
//  BUFFER SERIAL
// ═══════════════════════════════════════════════════════════════
#define SBUF_SIZE 640
static char     sBuf[SBUF_SIZE];
static uint16_t sBufLen = 0;

// ═══════════════════════════════════════════════════════════════
//  DECODIFICADOR BASE64 LIGERO
// ═══════════════════════════════════════════════════════════════
static int b64_val(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

static int decode_base64(const char* in, uint8_t* out, int maxOut) {
    int len = 0;
    int val = 0, valb = -8;
    for (const char* p = in; *p && *p != '='; p++) {
        int d = b64_val(*p);
        if (d < 0) continue;
        val = (val << 6) | d;
        valb += 6;
        if (valb >= 0) {
            if (len < maxOut) out[len++] = (uint8_t)((val >> valb) & 0xFF);
            valb -= 8;
        }
    }
    return len;
}

// ═══════════════════════════════════════════════════════════════
//  ENVÍO DEL FRAME A LA PANTALLA
// ═══════════════════════════════════════════════════════════════
static void presentFrame() {
    const uint16_t* fb = ui_render(music);
    tft.drawRGBBitmap(0, 0, (uint16_t*)fb, SCREEN_W, SCREEN_H);
}

// Copia segura de texto UTF-8 sin cortar una secuencia multibyte a la mitad
static void copyUtf8(char* dst, size_t cap, const char* src) {
    size_t n = strlen(src);
    if (n >= cap) {
        n = cap - 1;
        while (n > 0 && ((uint8_t)src[n] & 0xC0) == 0x80) n--;   // retrocede al inicio del carácter
    }
    memcpy(dst, src, n);
    dst[n] = '\0';
}

// ═══════════════════════════════════════════════════════════════
//  PARSER SERIAL
// ═══════════════════════════════════════════════════════════════
static void parseCommand(const char* line) {
    // 1. Streaming de líneas de portada (ART:<y>:<b64>)
    if (strncmp(line, "ART:", 4) == 0) {
        const char* p = line + 4;
        int y = atoi(p);
        const char* colon = strchr(p, ':');
        if (colon && y >= 0 && y < COVER_H) {
            uint8_t raw[96];
            int decLen = decode_base64(colon + 1, raw, 96);
            if (decLen == 96) {
                uint16_t rowPix[COVER_W];
                for (int i = 0; i < COVER_W; i++) {
                    rowPix[i] = ((uint16_t)raw[i * 2] << 8) | raw[i * 2 + 1];
                }
                ui_setCoverRow(y, rowPix);
                music.hasRealArt = true;
                needRender = true;
            }
        }
        return;
    }

    // 2. Malla de color del fondo (BGM:<r|g|b> × 9)
    if (strncmp(line, "BGM:", 4) == 0) {
        UiRgb g[BG_GX * BG_GY];
        const char* p = line + 4;
        int n = 0;
        while (n < 3 * BG_GX * BG_GY && *p) {
            char* end;
            long v = strtol(p, &end, 10);
            if (end == p) break;
            ((uint8_t*)g)[n++] = (uint8_t)constrain(v, 0, 255);
            p = (*end == '|') ? end + 1 : end;
        }
        if (n == 3 * BG_GX * BG_GY) {
            ui_setGrid(g);
            needRender = true;
        }
        return;
    }

    // 2b. Color único (formato antiguo BG:<r>|<g>|<b>)
    if (strncmp(line, "BG:", 3) == 0) {
        int r = 0, g = 0, b = 0;
        if (sscanf(line + 3, "%d|%d|%d", &r, &g, &b) == 3) {
            ui_setBackgroundFromSingle((uint8_t)r, (uint8_t)g, (uint8_t)b);
            needRender = true;
        }
        return;
    }

    // 3. Metadatos de la pista (TRACK:<t>|<a>|<al>)
    if (strncmp(line, "TRACK:", 6) == 0) {
        char buf[SBUF_SIZE];
        strncpy(buf, line + 6, sizeof(buf) - 1);
        buf[sizeof(buf) - 1] = '\0';

        char* s1 = strchr(buf, '|');
        char* s2 = s1 ? strchr(s1 + 1, '|') : nullptr;
        const char *nTitle = buf, *nArtist = "", *nAlbum = "";
        if (s1) {
            *s1 = '\0';
            nArtist = s1 + 1;
            if (s2) { *s2 = '\0'; nAlbum = s2 + 1; }
        }

        bool trackChanged = (strcmp(music.title, nTitle) != 0 ||
                             strcmp(music.artist, nArtist) != 0 ||
                             strcmp(music.album, nAlbum) != 0);

        if (trackChanged) {
            copyUtf8(music.title,  sizeof(music.title),  nTitle);
            copyUtf8(music.artist, sizeof(music.artist), nArtist);
            copyUtf8(music.album,  sizeof(music.album),  nAlbum);
            music.pos        = 0;
            music.hasMusic   = (music.title[0] != '\0');
            music.hasRealArt = false;

            if (music.hasMusic) {
                ui_resetCover();              // portada genérica hasta que llegue la real
                ui_setDefaultBackground();    // fondo por defecto hasta recibir BGM:
            } else {
                ui_setStandbyBackground();
            }
            ui_tickersReset(music, millis());
            needRender = true;
        }

    // 4. Estado de reproducción (STATE:<st>)
    } else if (strncmp(line, "STATE:", 6) == 0) {
        const char* st = line + 6;
        uint8_t oldState = music.state;

        if (strcmp(st, "playing") == 0)      { music.state = 1; music.hasMusic = true; }
        else if (strcmp(st, "paused") == 0)  { music.state = 2; music.hasMusic = true; }
        else                                 { music.state = 0; }

        if (oldState != music.state) needRender = true;

    // 5. Progreso de reproducción (PROG:<pos>|<dur>)
    } else if (strncmp(line, "PROG:", 5) == 0) {
        int p = 0, d = 0;
        sscanf(line + 5, "%d|%d", &p, &d);
        music.pos = (int32_t)p;
        music.dur = (int32_t)d;
#if SHOW_PROGRESS
        needRender = true;
#endif

    // 6. Volumen (no se muestra, se guarda por si se quiere usar)
    } else if (strncmp(line, "VOL:", 4) == 0) {
        music.vol = atoi(line + 4);

    // 7. Keepalive
    } else if (strcmp(line, "PING") == 0) {
        Serial.println("PONG");
    }
}

static void readSerialMessages() {
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\n' || c == '\r') {
            if (sBufLen > 0) {
                sBuf[sBufLen] = '\0';
                parseCommand(sBuf);
                sBufLen = 0;
            }
        } else if (sBufLen < SBUF_SIZE - 1) {
            sBuf[sBufLen++] = c;
        }
    }
}

// ═══════════════════════════════════════════════════════════════
//  MANEJO DEL BOTÓN DE USUARIO (GPIO 0)
// ═══════════════════════════════════════════════════════════════
static ButtonAction checkButton() {
    uint32_t now = millis();
    bool sample = digitalRead(BTN_PIN);
    ButtonAction act = BTN_ACT_NONE;

    if (sample != btn.raw) {
        btn.raw = sample;
        btn.lastBounce = now;
    }

    if ((now - btn.lastBounce) >= 40) {
        if (sample != btn.stable) {
            btn.stable = sample;

            if (btn.stable == LOW) {
                btn.isPressed = true;
                btn.pressStart = now;
                btn.holdTriggered = false;
            } else {
                btn.isPressed = false;
                if (!btn.holdTriggered) {
                    btn.tapCount++;
                    if (btn.tapCount >= 2) {
                        btn.tapCount = 0;
                        btn.waitSecondTap = false;
                        act = BTN_ACT_DOUBLE;
                    } else {
                        btn.waitSecondTap = true;
                        btn.tapWaitStart  = now;
                    }
                } else {
                    btn.tapCount = 0;
                }
            }
        }
    }

    if (btn.isPressed && !btn.holdTriggered && (now - btn.pressStart) >= 700) {
        btn.holdTriggered = true;
        btn.tapCount = 0;
        btn.waitSecondTap = false;
        act = BTN_ACT_HOLD;
    }

    if (btn.waitSecondTap && !btn.isPressed && (now - btn.tapWaitStart) >= 320) {
        btn.waitSecondTap = false;
        btn.tapCount = 0;
        act = BTN_ACT_TAP;
    }

    return act;
}

// ═══════════════════════════════════════════════════════════════
//  SETUP
// ═══════════════════════════════════════════════════════════════
void setup() {
    // Buffer de recepción grande: la portada llega como 48 líneas de ~133 bytes
    // y el dibujado de un frame tarda más que el intervalo entre líneas.
    Serial.setRxBufferSize(8192);     // debe ir ANTES de begin()
    Serial.begin(921600);

    // 1. Retroiluminación
    pinMode(TFT_BL, OUTPUT);
    digitalWrite(TFT_BL, HIGH);

    // 2. Botón de usuario
    pinMode(BTN_PIN, INPUT_PULLUP);

    // 3. Bus SPI
    spi->begin(TFT_SCLK, -1, TFT_MOSI, TFT_CS);

    // 4. Inicializar pantalla ST7735 80x160 Greentab
    tft.initR(INITR_MINI160x80);
    tft.setRotation(1);
    tft.invertDisplay(true);
    tft.setSPISpeed(40000000UL);

    // 5. Calibración exacta de bordes y limpieza de VRAM
    tft.clearAllControllerRam();
    tft.setCalibrationOffsets(1, 26);

    // 6. Motor de dibujo y pantalla inicial de espera
    ui_init();
    presentFrame();
    lastFrameMs = millis();

    delay(200);
    Serial.println("LOG:dongle_music v6.0 Apple Music Fluid listo");
}

// ═══════════════════════════════════════════════════════════════
//  LOOP
// ═══════════════════════════════════════════════════════════════
void loop() {
    uint32_t now = millis();

    // 1. Procesar datos del Mac
    readSerialMessages();

    // 2. Botón físico (GPIO 0)
    switch (checkButton()) {
        case BTN_ACT_TAP:    Serial.println("CMD:PLAYPAUSE"); break;
        case BTN_ACT_DOUBLE: Serial.println("CMD:PREV");      break;
        case BTN_ACT_HOLD:   Serial.println("CMD:NEXT");      break;
        default: break;
    }

    // 3. Animación del scroll de textos largos
    if (music.hasMusic && ui_tick(now)) needRender = true;

#if SHOW_PROGRESS
    if (music.hasMusic && music.state == 1 && now - lastSecond >= 1000) {
        lastSecond = now;
        if (music.dur > 0) music.pos++;       // avance local entre mensajes PROG
        needRender = true;
    }
#endif

    // 4. Enviar frame (máx. ~33 fps).  Si aún hay datos pendientes en el puerto
    //    serie se espera (hasta 150 ms) para vaciar primero el buffer.
    bool serialBusy = Serial.available() > 0;
    if (needRender && (now - lastFrameMs) >= FRAME_MIN_MS &&
        (!serialBusy || (now - lastFrameMs) >= 150)) {
        needRender = false;
        lastFrameMs = now;
        presentFrame();
    }
}
