// NStupido v4: Belgrade-style bus stop board on a 128x64 OLED, fed over WiFi.
// The board asks the NStupido server for one stop every 5 s:  GET <SERVER_URL>/frame?stop=<uid>
// (text frame, see include/frame.h) and draws it: header with stop, page and clock, up to 4 routes with
// minute cells "4(G)" live, "12(E)" estimate, "8(P)" timetable; more than 4 routes -> pages every 5 s.
// BOOT button (GPIO0) -> next stop of the compiled list (-DSTOPS="6539,15889,6712"), page 1.
//
// Legacy: frames arriving over USB serial (oled_bridge.py) are still drawn; while they keep coming (30 s)
// WiFi fetching pauses and the button sends "BTN" to the bridge instead.
//
// Build flags (platformio.ini + untracked secrets.ini): WIFI_SSID, WIFI_PASS, SERVER_URL, STOPS,
// OLED_SDA, OLED_SCL, OLED_CONTRAST, OLED_SH1106, BTN_PIN.
#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <U8g2lib.h>
#include "frame.h"

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
#ifndef BTN_PIN
#define BTN_PIN 0                 // BOOT button on LOLIN S2 mini, active low
#endif
#ifndef OLED_CONTRAST
#define OLED_CONTRAST 40          // 0..255; stock ~255 is too bright for a desk board
#endif

#define FETCH_MS        5000UL    // data + page cadence
#define RETRY_MS       60000UL    // after an error
#define STALE_MS       90000UL    // keep the last frame this long, then "veza izgubljena"
#define WIFI_WAIT_MS   20000UL
#define HTTP_TIMEOUT_MS 4000
#define SERIAL_HOLD_MS 30000UL    // legacy USB frames pause WiFi fetching this long
#define BTN_FEEDBACK_MS 3000UL

#ifdef OLED_SH1106
U8G2_SH1106_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);
#else
U8G2_SSD1306_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);
#endif

// ---- stops ----
static char stops[8][8];
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

static Frame frame;                    // last good frame of the current stop
static bool haveFrame = false;
static uint32_t lastGood = 0;
static Pager pager;
static int pageIdx[PER_PAGE], pageCnt = 0, pageShown = 0, pageTotal = 1;

static char errMsg[28] = "";           // current error ("" = none)
static bool errFull = false;           // full-screen error (no frame / 404 / 429)
static char msg1[24] = "NStupido", msg2[24] = "";   // startup screen lines
static bool showMsg = true;

static bool serialFrame = false;       // what is on screen came from USB
static uint32_t serialAt = 0;
static FrameParser serialParser;
static char serialBuf[128]; static size_t serialLen = 0;

static uint32_t btnFeedbackSince = 0;

// Debounced press detection (polled every loop, ~10 ms; HTTP to the LAN server blocks only ~0.1 s):
// true once per press, on a LOW level stable for 30 ms.
static bool buttonPressed() {
  static bool stable = HIGH, lastRaw = HIGH;
  static uint32_t changedAt = 0;
  bool raw = digitalRead(BTN_PIN);
  if (raw != lastRaw) { lastRaw = raw; changedAt = millis(); }
  if (raw != stable && millis() - changedAt >= 30) {
    stable = raw;
    return stable == LOW;
  }
  return false;
}

// ---- drawing ----
static void drawCell(int cellLeft, int cellW, int baseline, const Cell &c) {
  char t[8];
  snprintf(t, sizeof t, "%d(%c)", c.minutes > 99 ? 99 : c.minutes, c.kind);
  oled.setFont(u8g2_font_7x13B_tr);
  int tw = oled.getStrWidth(t);
  int x = cellLeft + (cellW - tw) / 2;
  if (x < cellLeft) x = cellLeft;
  oled.drawStr(x, baseline, t);
}

static void headerLabel(char *out, size_t n) {
  if (serialFrame) { snprintf(out, n, "%s", frame.stop); return; }
  if (pageTotal > 1) snprintf(out, n, "%s p%d/%d", stops[stopIdx], pageShown + 1, pageTotal);
  else if (nStops > 1) snprintf(out, n, "%s %d/%d", stops[stopIdx], stopIdx + 1, nStops);
  else snprintf(out, n, "%s", stops[stopIdx]);
}

static void drawHeader() {
  bool switching = btnFeedbackSince != 0;
  char label[16]; headerLabel(label, sizeof label);
  if (switching) { oled.drawBox(0, 0, 128, 13); oled.setDrawColor(0); }
  oled.setFont(u8g2_font_7x13B_tr);
  oled.drawStr(0, 11, label);
  oled.setFont(u8g2_font_6x12_tr);
  if (switching) oled.drawStr(128 - oled.getStrWidth(">>"), 10, ">>");
  else if (haveFrame && frame.time[0]) {
    int tx = 128 - oled.getStrWidth(frame.time);
    oled.drawStr(tx, 10, frame.time);
    // W = stop just added on the server (warming), O = nsmart offline (timetable only)
    const char *mark = strchr(frame.flags, 'O') ? "!" : (strchr(frame.flags, 'W') ? "*" : "");
    if (*mark) oled.drawStr(tx - 7, 10, mark);
  }
  oled.setDrawColor(1);
  oled.drawHLine(0, 13, 128);
}

static void drawCentered(int y, const char *s) {
  oled.drawStr((128 - oled.getStrWidth(s)) / 2 > 0 ? (128 - oled.getStrWidth(s)) / 2 : 0, y, s);
}

static void render() {
  oled.clearBuffer();
  if (showMsg) {                                   // startup screens
    oled.setFont(u8g2_font_7x13B_tr);
    drawCentered(22, "NStupido");
    oled.setFont(u8g2_font_6x12_tr);
    drawCentered(42, msg1);
    drawCentered(56, msg2);
    oled.sendBuffer();
    return;
  }
  drawHeader();
  if (errFull || !haveFrame) {
    oled.setFont(u8g2_font_7x13B_tr);
    drawCentered(40, errMsg[0] ? errMsg : "cekam podatke...");
    oled.sendBuffer();
    return;
  }
  if (frame.n == 0) {
    oled.setFont(u8g2_font_6x12_tr);
    oled.drawStr(0, 34, "nema autobusa");
  } else {
    const int LINE_W = 28, ROW_H = 12, BASE0 = 26;
    int idx[PER_PAGE], cnt;
    if (serialFrame) { cnt = frame.n < PER_PAGE ? frame.n : PER_PAGE; for (int i = 0; i < cnt; i++) idx[i] = i; }
    else { cnt = pageCnt; memcpy(idx, pageIdx, sizeof idx); }
    for (int i = 0; i < cnt; i++) {
      if (idx[i] < 0) continue;
      const Route &r = frame.r[idx[i]];
      int base = BASE0 + i * ROW_H;
      oled.setFont(u8g2_font_7x13B_tr);
      oled.drawStr(0, base, r.line);
      int n = r.n > 0 ? r.n : 1;
      int cellW = (128 - LINE_W) / n;
      for (int k = 0; k < r.n && k < 2; k++) drawCell(LINE_W + k * cellW, cellW, base, r.c[k]);
    }
  }
  uint32_t age = millis() - (serialFrame ? serialAt : lastGood);
  if (age > STALE_MS) {                            // keep the last frame, warn at the bottom
    oled.setFont(u8g2_font_5x7_tr);
    oled.setDrawColor(0); oled.drawBox(0, 56, 128, 8); oled.setDrawColor(1);
    oled.drawStr(0, 63, errMsg[0] ? errMsg : "veza izgubljena");
  }
  oled.sendBuffer();
}

static void message(const char *a, const char *b = "") {
  strlcpy(msg1, a, sizeof msg1); strlcpy(msg2, b, sizeof msg2);
  showMsg = true; render();
  Serial.printf("[nstupido] %s %s\n", a, b);
}

// ---- network ----
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

// Sets errMsg/errFull for a failed request; returns false.
static bool fail(int code) {
  errFull = false;
  if (WiFi.status() != WL_CONNECTED) strlcpy(errMsg, "nema WiFi", sizeof errMsg);
  else if (code <= 0) strlcpy(errMsg, "server ne odgovara", sizeof errMsg);
  else if (code == 429) { strlcpy(errMsg, "limit stanica", sizeof errMsg); errFull = true; }
  else if (code == 404) { strlcpy(errMsg, "nema stanice", sizeof errMsg); errFull = true; }
  else snprintf(errMsg, sizeof errMsg, "greska servera %d", code);
  if (!haveFrame || millis() - lastGood > STALE_MS) errFull = errFull || !haveFrame;
  return false;
}

static bool fetchFrame() {
  char path[40]; snprintf(path, sizeof path, "/frame?stop=%s", stops[stopIdx]);
  String body;
  int code = httpGet(path, body);
  if (code != 200) return fail(code);
  Frame f; FrameParser p;
  if (!p.parseBody(body.c_str(), f)) return fail(200);    // 200 but not a frame -> "greska servera 200"
  frame = f; haveFrame = true; lastGood = millis(); serialFrame = false;
  errMsg[0] = 0; errFull = false; btnFeedbackSince = 0;
  pageCnt = pager.next(frame, pageIdx, pageShown, pageTotal);
  return true;
}

static void nextStop() {
  stopIdx = (stopIdx + 1) % nStops;
  pager.reset(); pageTotal = 1; pageShown = 0;
  haveFrame = false; errMsg[0] = 0; errFull = false;
  btnFeedbackSince = millis() | 1;
  render();
  if (phase == P_RUN) nextAt = 0;                 // fetch the new stop right away
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
      message("server...", SERVER_URL + 7);         // skip "http://"
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
      char a[24]; snprintf(a, sizeof a, "stop %s...", stops[stopIdx]); message(a);
      if (fetchFrame()) {
        snprintf(a, sizeof a, "stop %s OK", stops[stopIdx]); message(a);
        phase = P_RUN; nextAt = millis() + 1000;
      } else { message(errMsg, "ponovo za 60 s"); nextAt = millis() + RETRY_MS; }
      break;
    }
    case P_RUN:
      showMsg = false;
      if (WiFi.status() != WL_CONNECTED) {
        fail(-1); render();
        nextAt = now + RETRY_MS;                    // auto-reconnect runs meanwhile
        break;
      }
      if (fetchFrame()) nextAt = millis() + FETCH_MS;
      else nextAt = millis() + RETRY_MS;
      render();
      break;
  }
}

// ---- legacy USB serial frames ----
static void pollSerial() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n') {
      serialBuf[serialLen] = 0; serialLen = 0;
      if (!strcmp(serialBuf, "?")) { Serial.println("NStupido OLED v4"); continue; }
      Frame f;
      if (serialParser.feed(serialBuf, f)) {
        frame = f; haveFrame = true; serialFrame = true; serialAt = millis();
        showMsg = false; errFull = false; errMsg[0] = 0; btnFeedbackSince = 0;
        render();
        Serial.println("OK");
      }
    } else if (serialLen < sizeof serialBuf - 1) serialBuf[serialLen++] = ch;
  }
}

void setup() {
  Serial.begin(115200);
  parseStops();
  pinMode(BTN_PIN, INPUT_PULLUP);
  Wire.begin(OLED_SDA, OLED_SCL);
  oled.setBusClock(400000);
  oled.begin();
  oled.setContrast(OLED_CONTRAST);
  oled.setFontMode(1);
  message("start", "v4 WiFi");
  nextAt = millis() + 800;
}

void loop() {
  pollSerial();
  bool serialMode = serialFrame && millis() - serialAt < SERIAL_HOLD_MS;
  if (buttonPressed()) {
    if (serialMode) { Serial.println("BTN"); btnFeedbackSince = millis() | 1; render(); }
    else { Serial.println("[nstupido] button"); nextStop(); }
  }
  if (btnFeedbackSince && millis() - btnFeedbackSince > BTN_FEEDBACK_MS) { btnFeedbackSince = 0; render(); }
  if (!serialMode && (int32_t)(millis() - nextAt) >= 0) step();
  static uint32_t lastDraw = 0;
  if (millis() - lastDraw > 1000) { lastDraw = millis(); if (!showMsg) render(); }
  delay(10);
}
