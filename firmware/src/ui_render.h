/*
 * ui_render.h — Motor de dibujo del reproductor (estilo Apple Music)
 * ====================================================================
 * Sin dependencias de Arduino: dibuja TODO en un framebuffer RGB565 de
 * 160×80 y devuelve un puntero que main.cpp envía a la pantalla de una
 * sola vez.  Ventajas frente a dibujar directamente sobre el panel:
 *
 *   · Sin parpadeo ni recuadros negros detrás del texto (cada frame se
 *     compone desde cero sobre el degradado).
 *   · Texto e iconos con anti-aliasing mezclados contra el fondo real.
 *   · El scroll de títulos largos es un simple desplazamiento + recorte.
 *
 * Formato de pixel: el mismo que ya usaba el proyecto (panel BGR):
 *     word = (B5 << 11) | (G6 << 5) | R5      ← = color565(b, g, r)
 * Así la portada que envía mac_music_bridge.py se copia tal cual.
 */
#pragma once

#include <stdint.h>
#include <string.h>
#include <math.h>
#include "ui_assets.h"

// ═══════════════════════════════════════════════════════════════
//  OPCIONES
// ═══════════════════════════════════════════════════════════════
#ifndef SHOW_PROGRESS
#define SHOW_PROGRESS 0     // 1 = barra fina de progreso en el borde inferior
#endif

// ═══════════════════════════════════════════════════════════════
//  GEOMETRÍA (160 × 80) — medida sobre el diseño objetivo
// ═══════════════════════════════════════════════════════════════
#define SCREEN_W        160
#define SCREEN_H         80

#define COVER_X           6
#define COVER_Y           4
#define COVER_W          48
#define COVER_H          48
#define COVER_R           5          // radio de esquina

#define TEXT_X           60
#define TEXT_W           94          // zona visible del texto (hasta x=154)
#define APPLE_X          59
#define APPLE_Y           7
#define BASE_HDR         15          // línea base "Apple Music"
#define BASE_TITLE       30          // línea base del título
#define BASE_ARTIST      43          // línea base del artista

#define CTRL_CY          64          // centro vertical de los controles
#define CTRL_PREV_CX     30
#define CTRL_PLAY_CX     80
#define CTRL_NEXT_CX    130

#define FADE_PX           7          // desvanecido en los bordes al hacer scroll
#define TICKER_HOLD_START_MS  1600
#define TICKER_HOLD_END_MS    1200
#define TICKER_STEP_MS          34

// ═══════════════════════════════════════════════════════════════
//  ESTADO DE REPRODUCCIÓN
// ═══════════════════════════════════════════════════════════════
struct TrackState {
    char    title[192]  = "";
    char    artist[144] = "";
    char    album[144]  = "";
    int32_t pos         = 0;
    int32_t dur         = 0;
    int32_t vol         = 50;
    uint8_t state       = 0;     // 0 = stopped, 1 = playing, 2 = paused
    bool    hasMusic    = false;
    bool    hasRealArt  = false;
};

// ═══════════════════════════════════════════════════════════════
//  BUFFERS
// ═══════════════════════════════════════════════════════════════
struct UiRgb { uint8_t r, g, b; };
static inline uint8_t uiCh(const UiRgb& c, int k) { return k == 0 ? c.r : (k == 1 ? c.g : c.b); }

static uint16_t ui_fb[SCREEN_W * SCREEN_H];      // frame final
static uint16_t ui_bg[SCREEN_W * SCREEN_H];      // degradado de fondo
static uint16_t ui_cover[COVER_W * COVER_H];     // portada (formato panel)
static uint8_t  ui_cornerA[COVER_R][COVER_R];    // alfa de las esquinas
static UiRgb    ui_corner[4];                    // TL, TR, BL, BR del fondo

// ═══════════════════════════════════════════════════════════════
//  COLOR
// ═══════════════════════════════════════════════════════════════
static inline uint16_t px565(uint8_t r, uint8_t g, uint8_t b) {
    return (uint16_t)(((b & 0xF8) << 8) | ((g & 0xFC) << 3) | (r >> 3));
}

static inline void unpx(uint16_t w, uint8_t &r, uint8_t &g, uint8_t &b) {
    uint8_t b5 = (w >> 11) & 0x1F, g6 = (w >> 5) & 0x3F, r5 = w & 0x1F;
    r = (uint8_t)((r5 << 3) | (r5 >> 2));
    g = (uint8_t)((g6 << 2) | (g6 >> 4));
    b = (uint8_t)((b5 << 3) | (b5 >> 2));
}

static inline uint32_t div255(uint32_t x) { return ((x + 128) * 257) >> 16; }

static inline uint16_t blendPx(uint16_t dst, uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
    if (a == 0)   return dst;
    if (a == 255) return px565(r, g, b);
    uint8_t dr, dg, db;
    unpx(dst, dr, dg, db);
    uint32_t ia = 255 - a;
    return px565((uint8_t)div255(dr * ia + r * a),
                 (uint8_t)div255(dg * ia + g * a),
                 (uint8_t)div255(db * ia + b * a));
}

static inline uint8_t clamp8(float v) { return v < 0 ? 0 : (v > 255 ? 255 : (uint8_t)(v + 0.5f)); }

// ═══════════════════════════════════════════════════════════════
//  FONDO: malla de 3×3 colores interpolada con Catmull-Rom
//  (se parece a la portada desenfocada de Apple Music) + dithering
// ═══════════════════════════════════════════════════════════════
#define BG_GX 3
#define BG_GY 3
static UiRgb ui_grid[BG_GX * BG_GY];             // fila a fila: TL TC TR / ML C MR / BL BC BR

static void ui_catmull(float t, float w[4]) {
    float t2 = t * t, t3 = t2 * t;
    w[0] = 0.5f * (-t3 + 2.0f * t2 - t);
    w[1] = 0.5f * (3.0f * t3 - 5.0f * t2 + 2.0f);
    w[2] = 0.5f * (-3.0f * t3 + 4.0f * t2 + t);
    w[3] = 0.5f * (t3 - t2);
}

static inline int ui_clampi(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }

static void ui_buildBackground() {
    static const uint8_t bayer[4][4] = {
        { 0,  8,  2, 10}, {12,  4, 14,  6}, { 3, 11,  1,  9}, {15,  7, 13,  5}
    };
    static float  wx[SCREEN_W][4], wy[SCREEN_H][4];
    static int8_t bx[SCREEN_W],    by[SCREEN_H];

    for (int x = 0; x < SCREEN_W; x++) {
        float s = (float)x / (SCREEN_W - 1) * (BG_GX - 1);
        int i = ui_clampi((int)s, 0, BG_GX - 2);
        ui_catmull(s - i, wx[x]);
        bx[x] = (int8_t)i;
    }
    for (int y = 0; y < SCREEN_H; y++) {
        float s = (float)y / (SCREEN_H - 1) * (BG_GY - 1);
        int j = ui_clampi((int)s, 0, BG_GY - 2);
        ui_catmull(s - j, wy[y]);
        by[y] = (int8_t)j;
    }

    for (int y = 0; y < SCREEN_H; y++) {
        for (int x = 0; x < SCREEN_W; x++) {
            float c0 = 0, c1 = 0, c2 = 0;
            for (int j = 0; j < 4; j++) {
                int gy = ui_clampi(by[y] - 1 + j, 0, BG_GY - 1);
                for (int i = 0; i < 4; i++) {
                    int gx = ui_clampi(bx[x] - 1 + i, 0, BG_GX - 1);
                    float w = wy[y][j] * wx[x][i];
                    const UiRgb& g = ui_grid[gy * BG_GX + gx];
                    c0 += g.r * w; c1 += g.g * w; c2 += g.b * w;
                }
            }
            // dithering ordenado: R,B tienen 5 bits (paso 8), G tiene 6 (paso 4)
            float t = (bayer[y & 3][x & 3] + 0.5f) / 16.0f;
            ui_bg[y * SCREEN_W + x] = px565(clamp8(c0 + t * 8.0f - 4.0f),
                                            clamp8(c1 + t * 4.0f - 2.0f),
                                            clamp8(c2 + t * 8.0f - 4.0f));
        }
    }
}

// Malla completa (9 colores, fila a fila).  El cálculo pesado se aplaza hasta
// ui_render(): así el parser serie vuelve enseguida y no se pierden filas de
// portada mientras el Mac sigue transmitiendo.
static bool ui_bgDirty = true;
static void ui_setGrid(const UiRgb g[BG_GX * BG_GY]) {
    for (int i = 0; i < BG_GX * BG_GY; i++) ui_grid[i] = g[i];
    ui_bgDirty = true;
}

static UiRgb ui_mix(UiRgb a, UiRgb b) {
    return UiRgb{(uint8_t)((a.r + b.r) / 2), (uint8_t)((a.g + b.g) / 2), (uint8_t)((a.b + b.b) / 2)};
}

// 4 esquinas (TL, TR, BL, BR) → malla de 3×3
static void ui_setCorners(const UiRgb c[4]) {
    UiRgb g[9];
    g[0] = c[0];            g[2] = c[1];
    g[6] = c[2];            g[8] = c[3];
    g[1] = ui_mix(c[0], c[1]);
    g[3] = ui_mix(c[0], c[2]);
    g[5] = ui_mix(c[1], c[3]);
    g[7] = ui_mix(c[2], c[3]);
    g[4] = ui_mix(g[1], g[7]);
    ui_setGrid(g);
}

// Fondo mientras no llega el color de la portada (ciruela / cobre)
static void ui_setDefaultBackground() {
    const UiRgb g[9] = { {88, 60, 92}, {60, 45, 66}, {104, 62, 58},
                         {84, 52, 76}, {78, 44, 60}, {100, 56, 60},
                         {96, 64, 80}, {70, 38, 52}, {92, 48, 60} };
    ui_setGrid(g);
}

// Fondo de la pantalla de espera (casi negro con un matiz rosado)
static void ui_setStandbyBackground() {
    const UiRgb c[4] = { {34, 22, 36}, {44, 24, 32}, {16, 12, 20}, {32, 16, 26} };
    ui_setCorners(c);
}

// Compatibilidad con el mensaje antiguo BG:r|g|b (un solo color)
static void ui_setBackgroundFromSingle(uint8_t r, uint8_t g, uint8_t b) {
    uint8_t mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
    float k = mx > 0 ? 100.0f / mx : 1.0f;
    if (k > 3.0f) k = 3.0f;
    if (k < 0.5f) k = 0.5f;
    const float f[4] = {0.85f, 1.10f, 0.95f, 0.75f};
    UiRgb c[4];
    for (int i = 0; i < 4; i++) {
        c[i].r = clamp8(r * k * f[i]);
        c[i].g = clamp8(g * k * f[i]);
        c[i].b = clamp8(b * k * f[i]);
    }
    ui_setCorners(c);
}

// ═══════════════════════════════════════════════════════════════
//  PRIMITIVAS
// ═══════════════════════════════════════════════════════════════
static void ui_blitMask(uint16_t* dst, int dw, int dh, int x, int y, int w, int h,
                        const uint8_t* mask, uint8_t r, uint8_t g, uint8_t b, uint8_t aMul) {
    for (int j = 0; j < h; j++) {
        int yy = y + j;
        if (yy < 0 || yy >= dh) continue;
        for (int i = 0; i < w; i++) {
            int xx = x + i;
            if (xx < 0 || xx >= dw) continue;
            uint8_t a = mask[j * w + i];
            if (!a) continue;
            if (aMul != 255) a = (uint8_t)div255((uint32_t)a * aMul);
            dst[yy * dw + xx] = blendPx(dst[yy * dw + xx], r, g, b, a);
        }
    }
}

// Igual, pero con degradado vertical de color (arriba → abajo)
static void ui_blitMaskGradV(uint16_t* dst, int dw, int dh, int x, int y, int w, int h,
                             const uint8_t* mask, UiRgb top, UiRgb bot) {
    for (int j = 0; j < h; j++) {
        int yy = y + j;
        if (yy < 0 || yy >= dh) continue;
        float t = h > 1 ? (float)j / (h - 1) : 0.f;
        uint8_t r = clamp8(top.r + (bot.r - top.r) * t);
        uint8_t g = clamp8(top.g + (bot.g - top.g) * t);
        uint8_t b = clamp8(top.b + (bot.b - top.b) * t);
        for (int i = 0; i < w; i++) {
            int xx = x + i;
            if (xx < 0 || xx >= dw) continue;
            uint8_t a = mask[j * w + i];
            if (!a) continue;
            dst[yy * dw + xx] = blendPx(dst[yy * dw + xx], r, g, b, a);
        }
    }
}

// ═══════════════════════════════════════════════════════════════
//  TEXTO (UTF-8 → glifos con anti-aliasing)
// ═══════════════════════════════════════════════════════════════
static const uint8_t* ui_utf8(const uint8_t* s, uint32_t* cp) {
    uint8_t c = *s++;
    if (c < 0x80) { *cp = c; return s; }
    if ((c & 0xE0) == 0xC0 && (s[0] & 0xC0) == 0x80) {
        *cp = ((uint32_t)(c & 0x1F) << 6) | (s[0] & 0x3F);
        return s + 1;
    }
    if ((c & 0xF0) == 0xE0 && (s[0] & 0xC0) == 0x80 && (s[1] & 0xC0) == 0x80) {
        *cp = ((uint32_t)(c & 0x0F) << 12) | ((uint32_t)(s[0] & 0x3F) << 6) | (s[1] & 0x3F);
        return s + 2;
    }
    if ((c & 0xF8) == 0xF0 && (s[0] & 0xC0) == 0x80 && (s[1] & 0xC0) == 0x80 && (s[2] & 0xC0) == 0x80) {
        *cp = 0xFFFD;                       // emoji y similares → '?'
        return s + 3;
    }
    *cp = '?';                              // byte suelto / secuencia cortada
    return s;
}

static int ui_glyphIndex(uint32_t cp) {
    for (int i = 0; i < FONT_NRANGES; i++) {
        uint32_t st = FONT_RANGE_START[i];
        if (cp >= st && cp < st + FONT_RANGE_COUNT[i]) return FONT_RANGE_BASE[i] + (int)(cp - st);
    }
    return FONT_QMARK_INDEX;
}

// Ancho en 1/16 de píxel (los avances son lineales, sin redondear)
static int ui_textWidth16(const FontDef& f, const char* text) {
    int w = 0;
    const uint8_t* p = (const uint8_t*)text;
    while (*p) {
        uint32_t cp;
        p = ui_utf8(p, &cp);
        w += f.adv16[ui_glyphIndex(cp)];
    }
    return w;
}

static int ui_textWidth(const FontDef& f, const char* text) {
    return (ui_textWidth16(f, text) + 8) >> 4;
}

// Dibuja `text` con origen x y línea base `base`; recorta a [clipL, clipR) y
// desvanece fadeL/fadeR píxeles junto a cada borde (0 = sin desvanecido).
// El lápiz avanza en 1/16 px y cada glifo se elige entre 4 fases subpíxel.
static void ui_drawText(const FontDef& f, const char* text, int x, int base,
                        uint8_t r, uint8_t g, uint8_t b, uint8_t aMul,
                        int clipL, int clipR, int fadeL, int fadeR) {
    static_assert(FONT_PHASES == 4, "ui_drawText asume 4 fases subpíxel");
    int pen = x * 16;
    const uint8_t* p = (const uint8_t*)text;
    while (*p) {
        uint32_t cp;
        p = ui_utf8(p, &cp);
        int gidx = ui_glyphIndex(cp);
        const GlyphInfo& gi = f.glyphs[gidx * FONT_PHASES + ((pen & 15) >> 2)];
        int gx = (pen >> 4) + gi.xo;
        if (gi.w && gx < clipR && gx + gi.w > clipL) {
            const uint8_t* bm = f.bmp + gi.off;
            int gy = base + gi.yo;
            for (int j = 0; j < gi.h; j++) {
                int yy = gy + j;
                if (yy < 0 || yy >= SCREEN_H) continue;
                for (int i = 0; i < gi.w; i++) {
                    int xx = gx + i;
                    if (xx < clipL || xx >= clipR || xx < 0 || xx >= SCREEN_W) continue;
                    uint32_t a = bm[j * gi.w + i];
                    if (!a) continue;
                    if (fadeL > 0 && xx < clipL + fadeL) a = a * (xx - clipL + 1) / (fadeL + 1);
                    if (fadeR > 0 && xx >= clipR - fadeR) a = a * (clipR - xx) / (fadeR + 1);
                    if (aMul != 255) a = div255(a * aMul);
                    ui_fb[yy * SCREEN_W + xx] = blendPx(ui_fb[yy * SCREEN_W + xx], r, g, b, (uint8_t)a);
                }
            }
        }
        pen += f.adv16[gidx];
        if ((pen >> 4) >= clipR + 40) break;    // ya fuera de la zona visible
    }
}

// ═══════════════════════════════════════════════════════════════
//  TICKER (scroll de textos largos)
// ═══════════════════════════════════════════════════════════════
struct UiTicker {
    int16_t  textW   = 0;
    int16_t  off     = 0;       // desplazamiento actual en px
    int16_t  maxOff  = 0;
    uint8_t  phase   = 0;       // 0 = pausa inicial, 1 = avanzando, 2 = pausa final
    bool     scrolls = false;
    uint32_t t0      = 0;
    uint32_t lastStep= 0;
};
static UiTicker ui_tkTitle, ui_tkArtist;

static void ui_tickerInit(UiTicker& tk, const FontDef& f, const char* text, uint32_t now) {
    tk.textW   = (int16_t)ui_textWidth(f, text);
    tk.off     = 0;
    tk.scrolls = tk.textW > TEXT_W;
    tk.maxOff  = tk.scrolls ? (int16_t)(tk.textW - TEXT_W + 6) : 0;
    tk.phase   = 0;
    tk.t0      = now;
    tk.lastStep= now;
}

static bool ui_tickerStep(UiTicker& tk, uint32_t now) {
    if (!tk.scrolls) return false;
    switch (tk.phase) {
        case 0:
            if (now - tk.t0 >= TICKER_HOLD_START_MS) { tk.phase = 1; tk.lastStep = now; }
            return false;
        case 1:
            if (now - tk.lastStep >= TICKER_STEP_MS) {
                tk.lastStep = now;
                tk.off++;
                if (tk.off >= tk.maxOff) { tk.off = tk.maxOff; tk.phase = 2; tk.t0 = now; }
                return true;
            }
            return false;
        default:
            if (now - tk.t0 >= TICKER_HOLD_END_MS) { tk.off = 0; tk.phase = 0; tk.t0 = now; return true; }
            return false;
    }
}

// Llamar cuando cambie la canción
static void ui_tickersReset(const TrackState& m, uint32_t now) {
    ui_tickerInit(ui_tkTitle,  FONT_TITLE,  m.title,  now);
    ui_tickerInit(ui_tkArtist, FONT_ARTIST, m.artist, now);
}

// Llamar en cada vuelta del loop; devuelve true si hay que redibujar
static bool ui_tick(uint32_t now) {
    bool a = ui_tickerStep(ui_tkTitle,  now);
    bool b = ui_tickerStep(ui_tkArtist, now);
    return a || b;
}

// ═══════════════════════════════════════════════════════════════
//  PORTADA
// ═══════════════════════════════════════════════════════════════
static void ui_initCorners() {
    for (int y = 0; y < COVER_R; y++)
        for (int x = 0; x < COVER_R; x++) {
            float dx = COVER_R - (x + 0.5f), dy = COVER_R - (y + 0.5f);
            float a  = COVER_R - sqrtf(dx * dx + dy * dy) + 0.5f;
            ui_cornerA[y][x] = clamp8((a < 0 ? 0 : (a > 1 ? 1 : a)) * 255.0f);
        }
}

// Portada genérica: degradado rosa → púrpura con nota musical
static void ui_resetCover() {
    for (int y = 0; y < COVER_H; y++) {
        float t = (float)y / (COVER_H - 1);
        uint16_t c = px565(clamp8(255 + (70 - 255) * t), clamp8(45 + (15 - 45) * t), clamp8(85 + (60 - 85) * t));
        for (int x = 0; x < COVER_W; x++) ui_cover[y * COVER_W + x] = c;
    }
    ui_blitMask(ui_cover, COVER_W, COVER_H, (COVER_W - ICON_NOTE_L_W) / 2, (COVER_H - ICON_NOTE_L_H) / 2,
                ICON_NOTE_L_W, ICON_NOTE_L_H, ICON_NOTE_L, 255, 255, 255, 255);
}

static void ui_setCoverRow(int y, const uint16_t* row48) {
    if (y < 0 || y >= COVER_H) return;
    memcpy(&ui_cover[y * COVER_W], row48, COVER_W * sizeof(uint16_t));
}

static void ui_drawCover() {
    for (int y = 0; y < COVER_H; y++) {
        uint16_t*       dst = &ui_fb[(COVER_Y + y) * SCREEN_W + COVER_X];
        const uint16_t* src = &ui_cover[y * COVER_W];
        int ay = (y < COVER_R) ? y : (y >= COVER_H - COVER_R ? COVER_H - 1 - y : COVER_R);
        if (ay >= COVER_R) { memcpy(dst, src, COVER_W * sizeof(uint16_t)); continue; }
        for (int x = 0; x < COVER_W; x++) {
            int ax = (x < COVER_R) ? x : (x >= COVER_W - COVER_R ? COVER_W - 1 - x : COVER_R);
            uint8_t a = (ax >= COVER_R) ? 255 : ui_cornerA[ay][ax];
            if (a == 255) dst[x] = src[x];
            else if (a) {
                uint8_t r, g, b;
                unpx(src[x], r, g, b);
                dst[x] = blendPx(dst[x], r, g, b, a);
            }
        }
    }
}

// ═══════════════════════════════════════════════════════════════
//  PANTALLAS
// ═══════════════════════════════════════════════════════════════
static void ui_drawStandby() {
    const int lx = (SCREEN_W - ICON_SQUIRCLE_W) / 2, ly = 7;
    ui_blitMaskGradV(ui_fb, SCREEN_W, SCREEN_H, lx, ly, ICON_SQUIRCLE_W, ICON_SQUIRCLE_H,
                     ICON_SQUIRCLE, UiRgb{251, 92, 116}, UiRgb{250, 35, 59});
    ui_blitMask(ui_fb, SCREEN_W, SCREEN_H, lx + (ICON_SQUIRCLE_W - ICON_NOTE_S_W) / 2 + 0,
                ly + (ICON_SQUIRCLE_H - ICON_NOTE_S_H) / 2, ICON_NOTE_S_W, ICON_NOTE_S_H,
                ICON_NOTE_S, 255, 255, 255, 255);

    const char* t1 = "Apple Music";
    const char* t2 = "Esperando m" "\xC3\xBA" "sica" "\xE2\x80\xA6";
    ui_drawText(FONT_TITLE,  t1, (SCREEN_W - ui_textWidth(FONT_TITLE,  t1)) / 2, 53, 255, 255, 255, 255, 0, SCREEN_W, 0, 0);
    ui_drawText(FONT_ARTIST, t2, (SCREEN_W - ui_textWidth(FONT_ARTIST, t2)) / 2, 68, 190, 175, 185, 255, 0, SCREEN_W, 0, 0);
}

static void ui_drawControls(uint8_t state) {
    ui_blitMask(ui_fb, SCREEN_W, SCREEN_H, CTRL_PREV_CX - ICON_REWIND_W / 2, CTRL_CY - ICON_REWIND_H / 2,
                ICON_REWIND_W, ICON_REWIND_H, ICON_REWIND, 255, 255, 255, 255);
    if (state == 1) {
        ui_blitMask(ui_fb, SCREEN_W, SCREEN_H, CTRL_PLAY_CX - ICON_PAUSE_W / 2, CTRL_CY - ICON_PAUSE_H / 2,
                    ICON_PAUSE_W, ICON_PAUSE_H, ICON_PAUSE, 255, 255, 255, 255);
    } else {
        // el triángulo de play se ve ópticamente descentrado: lo movemos 1 px a la derecha
        ui_blitMask(ui_fb, SCREEN_W, SCREEN_H, CTRL_PLAY_CX - ICON_PLAY_W / 2 + 1, CTRL_CY - ICON_PLAY_H / 2,
                    ICON_PLAY_W, ICON_PLAY_H, ICON_PLAY, 255, 255, 255, 255);
    }
    ui_blitMask(ui_fb, SCREEN_W, SCREEN_H, CTRL_NEXT_CX - ICON_FORWARD_W / 2, CTRL_CY - ICON_FORWARD_H / 2,
                ICON_FORWARD_W, ICON_FORWARD_H, ICON_FORWARD, 255, 255, 255, 255);
}

#if SHOW_PROGRESS
static void ui_drawProgress(const TrackState& m) {
    const int x0 = 8, x1 = 152, y0 = 77;
    int filled = 0;
    if (m.dur > 0) {
        filled = (int)((int64_t)m.pos * (x1 - x0) / m.dur);
        if (filled < 0) filled = 0;
        if (filled > x1 - x0) filled = x1 - x0;
    }
    for (int x = x0; x < x1; x++)
        for (int y = y0; y < y0 + 2; y++)
            ui_fb[y * SCREEN_W + x] = blendPx(ui_fb[y * SCREEN_W + x], 255, 255, 255, x < x0 + filled ? 255 : 70);
}
#endif

// ═══════════════════════════════════════════════════════════════
//  RENDER PRINCIPAL — devuelve el framebuffer listo para enviar
// ═══════════════════════════════════════════════════════════════
static const uint16_t* ui_render(const TrackState& m) {
    if (ui_bgDirty) { ui_buildBackground(); ui_bgDirty = false; }
    memcpy(ui_fb, ui_bg, sizeof(ui_fb));

    if (!m.hasMusic || (m.title[0] == '\0' && m.artist[0] == '\0')) {
        ui_drawStandby();
        return ui_fb;
    }

    ui_drawCover();

    // Cabecera:  Apple Music
    ui_blitMask(ui_fb, SCREEN_W, SCREEN_H, APPLE_X, APPLE_Y, ICON_APPLE_W, ICON_APPLE_H,
                ICON_APPLE, 255, 255, 255, 235);
    ui_drawText(FONT_HDR, "Apple Music", TEXT_X + 9, BASE_HDR, 255, 255, 255, 235, 0, SCREEN_W, 0, 0);

    // Título y artista (con scroll + desvanecido si no caben)
    ui_drawText(FONT_TITLE, m.title, TEXT_X - ui_tkTitle.off, BASE_TITLE, 255, 255, 255, 255,
                TEXT_X, TEXT_X + TEXT_W,
                ui_tkTitle.off > 0 ? FADE_PX : 0,
                (ui_tkTitle.scrolls && ui_tkTitle.off < ui_tkTitle.maxOff) ? FADE_PX : 0);
    ui_drawText(FONT_ARTIST, m.artist, TEXT_X - ui_tkArtist.off, BASE_ARTIST, 255, 255, 255, 225,
                TEXT_X, TEXT_X + TEXT_W,
                ui_tkArtist.off > 0 ? FADE_PX : 0,
                (ui_tkArtist.scrolls && ui_tkArtist.off < ui_tkArtist.maxOff) ? FADE_PX : 0);

    ui_drawControls(m.state);
#if SHOW_PROGRESS
    ui_drawProgress(m);
#endif
    return ui_fb;
}

static void ui_init() {
    ui_initCorners();
    ui_resetCover();
    ui_setStandbyBackground();
}
