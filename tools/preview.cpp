// preview.cpp — Compila el MISMO motor de render del firmware en el PC y
// vuelca un frame a PPM, para ver el diseño sin flashear la placa.
//   g++ -O2 -std=c++17 -I../firmware/src preview.cpp -o preview
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include "ui_render.h"

int main(int argc, char** argv) {
    const char* out = "frame.ppm";
    const char* coverPath = nullptr;
    const char* corners = nullptr;
    TrackState m;
    strcpy(m.title, "Blinding Lights");
    strcpy(m.artist, "The Weeknd");
    m.state = 1; m.hasMusic = true;
    int toff = 0, aoff = 0;
    bool standby = false;

    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto nxt = [&]() { return i + 1 < argc ? argv[++i] : (char*)""; };
        if (a == "--out") out = nxt();
        else if (a == "--cover") coverPath = nxt();
        else if (a == "--mesh") corners = nxt();
        else if (a == "--title") strncpy(m.title, nxt(), sizeof(m.title) - 1);
        else if (a == "--artist") strncpy(m.artist, nxt(), sizeof(m.artist) - 1);
        else if (a == "--state") m.state = atoi(nxt());
        else if (a == "--toff") toff = atoi(nxt());
        else if (a == "--aoff") aoff = atoi(nxt());
        else if (a == "--standby") standby = true;
    }

    ui_init();
    if (standby) { m.hasMusic = false; m.title[0] = m.artist[0] = 0; ui_setStandbyBackground(); }
    else {
        ui_setDefaultBackground();
        if (corners) {
            UiRgb g[9]; int n = 0; char* e = (char*)corners;
            while (n < 27 && *e) { long v = strtol(e, &e, 10); ((uint8_t*)g)[n++] = (uint8_t)v; if (*e == '|') e++; }
            if (n == 27) ui_setGrid(g);
        }
        if (coverPath) {
            FILE* f = fopen(coverPath, "rb");
            if (f) { uint16_t buf[COVER_W * COVER_H]; if (fread(buf, 2, COVER_W * COVER_H, f) == COVER_W * COVER_H)
                         for (int y = 0; y < COVER_H; y++) ui_setCoverRow(y, &buf[y * COVER_W]);
                     fclose(f); }
        }
    }
    ui_tickersReset(m, 0);
    ui_tkTitle.off = toff; ui_tkArtist.off = aoff;
    const uint16_t* fb = ui_render(m);

    FILE* f = fopen(out, "wb");
    fprintf(f, "P6\n%d %d\n255\n", SCREEN_W, SCREEN_H);
    for (int i = 0; i < SCREEN_W * SCREEN_H; i++) {
        uint8_t r, g, b; unpx(fb[i], r, g, b);
        fputc(r, f); fputc(g, f); fputc(b, f);
    }
    fclose(f);
    fprintf(stderr, "title width=%d scrolls=%d  artist width=%d scrolls=%d\n",
            ui_tkTitle.textW, ui_tkTitle.scrolls, ui_tkArtist.textW, ui_tkArtist.scrolls);
    return 0;
}
