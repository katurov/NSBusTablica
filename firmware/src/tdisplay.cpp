// NStupido v4.4 for the LilyGO TTGO T-Display (ESP32, 1.14" ST7789V IPS 135x240, used landscape 240x135).
// Same server and protocol as the S2 mini OLED board (src/main.cpp): GET <SERVER_URL>/frame?stop=<uid> every 5 s,
// parsed by include/frame.h. Built with Grok Bot.
//
// Layout (240x135):
//   top 34 px  light blue band (32 px, then 2 px black gap before the routes): stop number (black on a darker blue box; "*" warming, "!" nsmart offline;
//              inverted while switching stops), then the stop name (N line, server v4.4+) in one line, Roboto
//              Condensed SemiBold 24 px anti-aliased (TFT_eSPI smooth font, include/hdr_font.h made by
//              tools/make_vlw.py, has č ć š ž đ). A name wider than the space scrolls (marquee: 2 s pause, then
//              ~33 px/s, only the header band is pushed). Old server without N lines -> number only.
//   below      TD_ROWS route rows (default 4, helvB18; -DTD_ROWS=5 -> helvB14): route number bold, white,
//              right-aligned in a fixed 5-character column; then up to two waits "<min> min" coloured by source:
//              G live = green, P timetable = orange, E estimate (seen earlier, counted down) = yellow.
//              More pages -> dots at the right edge (current page white).
// Buttons (same debounce as main.cpp): GPIO0 (BTN_STOP) -> next stop of -DSTOPS, page 1;
//              GPIO35 (BTN_PAGE, input-only, external pull-up on the board) -> next page of routes. Pages do not
//              turn by themselves; 30 s after the last page press the board returns to page 1 (nearest buses).
// Serial console (115200): "?" identify, "stop" / "page" = the two buttons, "shot" = raw RGB565 screenshot.
// Errors: same texts and rules as main.cpp (full screen without data, bottom bar on stale data).
// Fonts: rows and messages use u8g2 BDF fonts via U8g2_for_TFT_eSPI (_te variants cover č ć š ž đ); the header
// uses the smooth font. Everything is drawn into one 240x135 sprite, pushed once per render (no flicker).
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <TFT_eSPI.h>
#include <U8g2_for_TFT_eSPI.h>
#include "frame.h"
#include "hdr_font.h"            // hdr24: Roboto Condensed SemiBold 24 px, anti-aliased, with č ć š ž đ

#ifndef WIFI_SSID
#error "WIFI_SSID not set: copy firmware/secrets.ini.example to firmware/secrets.ini and fill it in"
#endif
#ifndef WIFI_PASS
#define WIFI_PASS ""
#endif
#ifndef SERVER_URL
#define SERVER_URL "http://nstupido.teamof.top:8080"
#endif
#ifndef STOPS
#define STOPS "6539,15889,6712"
#endif
#ifndef BTN_STOP
#define BTN_STOP 0                // left button (BOOT), active low, internal pull-up
#endif
#ifndef BTN_PAGE
#define BTN_PAGE 35               // right button, active low, external pull-up (GPIO35 is input-only)
#endif
#ifndef TD_ROWS
#define TD_ROWS 4                 // 4 (helvB18) or 5 (helvB14) route rows under the header
#endif
#ifndef TFT_BRIGHTNESS
#define TFT_BRIGHTNESS 160        // backlight PWM 0..255
#endif

#define FETCH_MS        5000UL
#define RETRY_MS       60000UL
#define STALE_MS       90000UL
#define WIFI_WAIT_MS   20000UL
#define HTTP_TIMEOUT_MS 4000
#define BTN_FEEDBACK_MS 3000UL
#define PAGE_HOLD_MS   30000UL    // back to page 1 this long after the last page press

#define W 240
#define H 135
#define HDR 34                    // header area; route rows start below it
#define BAND (HDR - 2)            // blue band height: 2 px of black gap above the first route row

TFT_eSPI tft;
TFT_eSprite spr(&tft);
U8g2_for_TFT_eSPI u8f;
static bool useSprite = false;

static uint16_t C_BLUE, C_BLUE2, C_GREEN, C_ORANGE, C_YELLOW, C_SEP, C_ERR, C_DOT;

#if TD_ROWS >= 5
#define F_ROUTE u8g2_font_helvB14_tr
#define F_MIN   u8g2_font_helvB14_tr
#define F_UNIT  u8g2_font_helvR08_tr
#else
#define F_ROUTE u8g2_font_helvB18_tr
#define F_MIN   u8g2_font_helvB18_tr
#define F_UNIT  u8g2_font_helvR10_tr
#endif
#define F_MSG    u8g2_font_helvB14_te

// ---- stops ----
static char stops[8][8];
static char stopNames[8][NAME_BYTES];   // last name seen per stop: shown right away when switching
static int nStops = 0, stopIdx = 0;

static void parseStops() {
  const char *s = STOPS; int n = 0;
  while (*s && nStops < 8) {
    if (*s == ',') { if (n) { stops[nStops][n] = 0; nStops++; n = 0; } }
    else if (*s != ' ' && n < 7) stops[nStops][n++] = *s;
    s++;
  }
  if (n && nStops < 8) { stops[nStops][n] = 0; nStops++; }
  if (!nStops) { strcpy(stops[0], "6539"); nStops = 1; }
}

// ---- state ----
enum Phase { P_WIFI, P_SERVER, P_STOP, P_RUN };
static Phase phase = P_WIFI;
static uint32_t nextAt = 0, wifiSince = 0;
static bool wifiStarted = false;

static Frame frame;
static bool haveFrame = false;
static uint32_t lastGood = 0;
static int page = 0;
static uint32_t pagePressedAt = 0;

static char errMsg[28] = "";
static bool errFull = false;
static char msg1[40] = "NStupido", msg2[40] = "";
static bool showMsg = true;
static uint32_t btnFeedbackSince = 0;

// Debounced press detection, same rule as main.cpp: true once per press, on a LOW level stable for 30 ms.
struct Button {
  uint8_t pin; bool stable, lastRaw; uint32_t changedAt;
  explicit Button(uint8_t p) : pin(p), stable(HIGH), lastRaw(HIGH), changedAt(0) {}
  bool pressed() {
    bool raw = digitalRead(pin);
    if (raw != lastRaw) { lastRaw = raw; changedAt = millis(); }
    if (raw != stable && millis() - changedAt >= 30) { stable = raw; return stable == LOW; }
    return false;
  }
};
static Button btnStop(BTN_STOP), btnPage(BTN_PAGE);

// ---- drawing helpers (everything goes to the sprite, pushed once per render) ----
static TFT_eSPI &gfx() { return useSprite ? (TFT_eSPI &)spr : tft; }

static int textW(const char *s) { return u8f.getUTF8Width(s); }
static void text(int x, int y, const char *s, uint16_t fg) { u8f.setForegroundColor(fg); u8f.drawUTF8(x, y, s); }

static int pagesOf(const Frame &f) { return f.n > TD_ROWS ? (f.n + TD_ROWS - 1) / TD_ROWS : 1; }

// ---- header: stop number box + stop name (smooth font, scrolls when it does not fit) ----
#define MARQUEE_MS     30UL       // one pixel per tick, ~33 px/s
#define MARQUEE_PAUSE  2000UL     // standstill at the start of every pass
#define MARQUEE_GAP    48         // pixels between the end of the name and its next pass
static char shownName[NAME_BYTES] = "\x01";
static int nameOff = 0, nameTW = 0, nameX = 0, nameW = 0;
static uint32_t nameTick = 0, namePauseUntil = 0;

static const char *currentName() { return stopNames[stopIdx]; }   // "" until the server sent an N line

static void smoothAt(TFT_eSPI &g, int x, int y, const char *str) { g.setCursor(x, y); g.print(str); }

static void drawHeader() {
  TFT_eSPI &g = gfx();
  bool switching = btnFeedbackSince != 0;
  g.fillRect(0, 0, W, BAND, C_BLUE);
  const int top = (BAND - (g.gFont.maxAscent + g.gFont.maxDescent)) / 2;
  g.setTextWrap(false, false);
  // stop number: black on a darker blue box; inverted while switching stops (until the new frame arrives)
  char uid[12];
  const char *mark = (!switching && haveFrame) ? (strchr(frame.flags, 'O') ? "!" : (strchr(frame.flags, 'W') ? "*" : "")) : "";
  snprintf(uid, sizeof uid, "%s%s", stops[stopIdx], mark);
  int ub = g.textWidth(uid) + 12;
  uint16_t boxBg = switching ? TFT_BLACK : C_BLUE2, boxFg = switching ? C_BLUE : TFT_BLACK;
  g.fillRect(0, 0, ub, BAND, boxBg);
  g.setTextColor(boxFg, boxBg);
  smoothAt(g, 6, top, uid);
  // stop name
  const char *name = currentName();
  if (strcmp(name, shownName)) {                    // new name: restart the marquee
    strlcpy(shownName, name, sizeof shownName);
    nameOff = 0; namePauseUntil = millis() + MARQUEE_PAUSE;
  }
  nameX = ub + 6; nameW = W - nameX - 3;
  nameTW = *name ? g.textWidth(name) : 0;
  if (!nameTW) return;
  g.setTextColor(TFT_BLACK, C_BLUE);
  if (nameTW <= nameW) { smoothAt(g, nameX, top, name); nameOff = 0; return; }
  g.setViewport(nameX, 0, nameW, BAND);             // clip; coordinates relative to the viewport
  smoothAt(g, -nameOff, top, name);
  if (nameTW + MARQUEE_GAP - nameOff < nameW) smoothAt(g, nameTW + MARQUEE_GAP - nameOff, top, name);
  g.resetViewport();
}

static bool marqueeActive() { return !showMsg && useSprite && nameTW > nameW; }

// Called every loop: advances the scrolling name by one pixel per tick and pushes only the header band.
static void marqueeStep() {
  uint32_t now = millis();
  if (!marqueeActive() || now - nameTick < MARQUEE_MS) return;
  nameTick = now;
  if ((int32_t)(now - namePauseUntil) < 0) return;
  if (++nameOff >= nameTW + MARQUEE_GAP) { nameOff = 0; namePauseUntil = now + MARQUEE_PAUSE; }
  drawHeader();
  spr.pushSprite(0, 0, 0, 0, W, HDR);
}

// Page indicator: small dots at the right edge of the route area (only with more than one page).
static void drawPageDots(int pages) {
  if (pages < 2) return;
  TFT_eSPI &g = gfx();
  int y0 = HDR + (H - HDR - (pages - 1) * 9) / 2;
  for (int i = 0; i < pages; i++) g.fillCircle(W - 4, y0 + i * 9, 2, i == page ? TFT_WHITE : C_DOT);
}

static void drawCentered(int y, const char *s, uint16_t fg) {
  int x = (W - textW(s)) / 2; text(x > 0 ? x : 0, y, s, fg);
}

static void push() { if (useSprite) spr.pushSprite(0, 0); }

static void render() {
  TFT_eSPI &g = gfx();
  g.fillRect(0, 0, W, H, TFT_BLACK);
  if (showMsg) {
    u8f.setFont(u8g2_font_helvB18_tr); drawCentered(40, "NStupido", C_BLUE);
    u8f.setFont(F_MSG); drawCentered(78, msg1, TFT_WHITE);
    u8f.setFont(u8g2_font_helvB10_te); drawCentered(104, msg2, TFT_LIGHTGREY);
    push(); return;
  }
  int pages = haveFrame ? pagesOf(frame) : 1;
  if (page >= pages) page = 0;
  drawHeader();
  if (errFull || !haveFrame) {
    u8f.setFont(F_MSG);
    drawCentered(HDR + 56, errMsg[0] ? errMsg : "čekam podatke...", errMsg[0] ? C_ERR : TFT_WHITE);
    push(); return;
  }
  if (frame.n == 0) {
    u8f.setFont(F_MSG); drawCentered(HDR + 56, "nema autobusa", TFT_WHITE);
  } else {
    u8f.setFont(F_ROUTE);
    const int pitch = (H - HDR) / TD_ROWS;
    const int colW = 5 * textW("0");                       // fixed 5-character route column
    const int asc = u8f.getFontAscent(), desc = u8f.getFontDescent();   // descent is negative
    const int cellX = colW + 14, cellW = (W - cellX) / 2;
    for (int i = 0; i < TD_ROWS; i++) {
      int k = page * TD_ROWS + i;
      if (k >= frame.n) break;
      const Route &r = frame.r[k];
      int top = HDR + 1 + i * pitch;
      int base = top + (pitch + asc + desc) / 2 - 1;
      if (i) g.drawFastHLine(0, top - 1, W, C_SEP);
      u8f.setFont(F_ROUTE);
      text(2 + colW - textW(r.line), base, r.line, TFT_WHITE);
      for (int c = 0; c < r.n && c < 2; c++) {
        uint16_t col = r.c[c].kind == 'G' ? C_GREEN : (r.c[c].kind == 'E' ? C_YELLOW : C_ORANGE);
        char t[6]; snprintf(t, sizeof t, "%d", r.c[c].minutes > 99 ? 99 : r.c[c].minutes);
        int x = cellX + c * cellW;
        u8f.setFont(F_MIN); text(x, base, t, col);
        x += textW(t) + 2;
        u8f.setFont(F_UNIT); text(x, base, "min", col);
      }
    }
    drawPageDots(pages);
  }
  if (millis() - lastGood > STALE_MS) {                       // keep the last frame, warn at the bottom
    g.fillRect(0, H - 16, W, 16, C_ERR);
    u8f.setFont(u8g2_font_helvB10_te);
    drawCentered(H - 3, errMsg[0] ? errMsg : "veza izgubljena", TFT_WHITE);
  }
  push();
}

static void message(const char *a, const char *b = "") {
  strlcpy(msg1, a, sizeof msg1); strlcpy(msg2, b, sizeof msg2);
  showMsg = true; render();
  Serial.printf("[nstupido] %s %s\n", a, b);
}

// ---- network (same as main.cpp) ----
static int httpGet(const char *path, String &body) {
  HTTPClient http;
  String url = String(SERVER_URL) + path;
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(url)) return -1;
  int code = http.GET();
  body = code > 0 ? http.getString() : String();
  http.end();
  Serial.printf("[nstupido] GET %s -> %d\n", url.c_str(), code);
  return code;
}

static bool fail(int code) {
  errFull = false;
  if (WiFi.status() != WL_CONNECTED) strlcpy(errMsg, "nema WiFi", sizeof errMsg);
  else if (code <= 0) strlcpy(errMsg, "server ne odgovara", sizeof errMsg);
  else if (code == 429) { strlcpy(errMsg, "limit stanica", sizeof errMsg); errFull = true; }
  else if (code == 404) { strlcpy(errMsg, "nema stanice", sizeof errMsg); errFull = true; }
  else snprintf(errMsg, sizeof errMsg, "greška servera %d", code);
  if (!haveFrame || millis() - lastGood > STALE_MS) errFull = errFull || !haveFrame;
  return false;
}

static bool fetchFrame() {
  char path[40]; snprintf(path, sizeof path, "/frame?stop=%s", stops[stopIdx]);
  String body;
  int code = httpGet(path, body);
  if (code != 200) return fail(code);
  static Frame f; FrameParser p;                // static: Frame is ~400 bytes, keep it off the loop stack
  if (!p.parseBody(body.c_str(), f)) return fail(200);
  frame = f; haveFrame = true; lastGood = millis();
  if (frame.name[0]) strlcpy(stopNames[stopIdx], frame.name, NAME_BYTES);
  errMsg[0] = 0; errFull = false; btnFeedbackSince = 0;
  if (page >= pagesOf(frame)) page = 0;
  Serial.printf("[nstupido] stop %s \"%s\" routes %d page %d/%d\n", frame.stop, frame.name, frame.n, page + 1, pagesOf(frame));
  return true;
}

static void nextStop() {
  stopIdx = (stopIdx + 1) % nStops;
  page = 0; pagePressedAt = 0;
  haveFrame = false; errMsg[0] = 0; errFull = false;
  btnFeedbackSince = millis() | 1;
  render();
  if (phase == P_RUN) nextAt = 0;
}

static void nextPage() {
  if (!haveFrame || showMsg) return;
  int pages = pagesOf(frame);
  page = (page + 1) % pages;
  pagePressedAt = page ? (millis() | 1) : 0;
  render();
}

static void step() {
  uint32_t now = millis();
  switch (phase) {
    case P_WIFI:
      if (WiFi.status() == WL_CONNECTED) {
        message("WiFi OK", WiFi.localIP().toString().c_str());
        wifiStarted = false; phase = P_SERVER; nextAt = now + 1500;
      } else if (!wifiStarted) {
        message("WiFi...", WIFI_SSID);
        WiFi.mode(WIFI_STA); WiFi.setAutoReconnect(true); WiFi.begin(WIFI_SSID, WIFI_PASS);
        wifiStarted = true; wifiSince = now; nextAt = now + 500;
      } else if (now - wifiSince > WIFI_WAIT_MS) {
        message("nema WiFi", "ponovo za 60 s");
        WiFi.disconnect(); wifiStarted = false; nextAt = now + RETRY_MS;
      } else nextAt = now + 500;
      break;
    case P_SERVER: {
      message("server...", SERVER_URL + 7);
      String body; int code = httpGet("/", body);
      if (code == 200) { message("server OK", ""); phase = P_STOP; nextAt = now + 1000; }
      else {
        fail(code); message(errMsg, "ponovo za 60 s");
        if (WiFi.status() != WL_CONNECTED) phase = P_WIFI;
        nextAt = millis() + RETRY_MS;
      }
      break;
    }
    case P_STOP: {
      char a[24]; snprintf(a, sizeof a, "stanica %s...", stops[stopIdx]); message(a);
      if (fetchFrame()) { phase = P_RUN; nextAt = 0; showMsg = false; render(); }
      else { message(errMsg, "ponovo za 60 s"); nextAt = millis() + RETRY_MS; }
      break;
    }
    case P_RUN:
      showMsg = false;
      if (WiFi.status() != WL_CONNECTED) { fail(-1); render(); nextAt = now + RETRY_MS; break; }
      if (fetchFrame()) nextAt = millis() + FETCH_MS;
      else nextAt = millis() + RETRY_MS;
      render();
      break;
  }
}

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(50);
  parseStops();
  pinMode(BTN_STOP, INPUT_PULLUP);
  pinMode(BTN_PAGE, INPUT);                       // GPIO35 has no internal pull-up; the board has one
  tft.init();
  tft.setRotation(1);                             // landscape, USB on the right
  tft.fillScreen(TFT_BLACK);
  ledcSetup(0, 5000, 8); ledcAttachPin(TFT_BL, 0); ledcWrite(0, TFT_BRIGHTNESS);
  C_BLUE = tft.color565(135, 206, 250); C_GREEN = tft.color565(0, 230, 0); C_ORANGE = tft.color565(255, 140, 0);
  C_YELLOW = tft.color565(255, 225, 0); C_SEP = tft.color565(40, 40, 40); C_ERR = tft.color565(200, 30, 30);
  C_BLUE2 = tft.color565(80, 160, 230); C_DOT = tft.color565(90, 90, 90);
  spr.setColorDepth(16);
  useSprite = spr.createSprite(W, H) != nullptr;   // 63 KB, before WiFi takes its share of the heap
  gfx().loadFont(hdr24);                           // header font stays loaded; rows use u8g2 fonts
  u8f.begin(gfx());
  u8f.setFontMode(1);                              // transparent: we fill backgrounds ourselves
  u8f.setFontDirection(0);
  Serial.printf("[nstupido] T-Display v4.4, sprite %s, heap %u\n", useSprite ? "ok" : "FAILED (direct draw)", ESP.getFreeHeap());
  message("start", "v4.4 T-Display");
  nextAt = millis() + 800;
}

void loop() {
  if (Serial.available()) {                        // serial console (no USB-bridge frames on this board)
    String l = Serial.readStringUntil('\n'); l.trim();
    if (l == "?") Serial.println("NStupido T-Display v4.4");
    else if (l == "stop") nextStop();              // same as the left button
    else if (l == "page") nextPage();              // same as the right button
    else if (l == "shot" && useSprite) {           // screenshot: "SHOT 240 135\n" + raw RGB565 (sprite byte order)
      Serial.printf("SHOT %d %d\n", W, H);
      Serial.write((const uint8_t *)spr.getPointer(), W * H * 2);
      Serial.println();
    }
  }
  if (btnStop.pressed()) { Serial.println("[nstupido] button stop"); nextStop(); }
  if (btnPage.pressed()) { Serial.println("[nstupido] button page"); nextPage(); }
  if (btnFeedbackSince && millis() - btnFeedbackSince > BTN_FEEDBACK_MS) { btnFeedbackSince = 0; render(); }
  if (pagePressedAt && millis() - pagePressedAt > PAGE_HOLD_MS) { pagePressedAt = 0; page = 0; render(); }
  if ((int32_t)(millis() - nextAt) >= 0) step();
  marqueeStep();
  static uint32_t lastDraw = 0;
  if (millis() - lastDraw > 1000) { lastDraw = millis(); if (!showMsg) render(); }
  delay(5);
}
