// NStupido: Belgrade-style bus stop board on a 128x64 OLED.
// Host (oled_bridge.py) sends text frames over USB serial:
//   H|<stop>|<HH:MM>
//   R|<line>|<cell>|<cell>   0..2 cells
//      cell = G:<min>  live GPS ("Sveze")
//           = E:<min>  estimate after lose ("Videli-izgubili")
//           = P:<min>  timetable ("Po rasporedu")
//   E                    end of frame -> draw
#include <Arduino.h>
#include <Wire.h>
#include <U8g2lib.h>

#ifdef OLED_SH1106
U8G2_SH1106_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);
#else
U8G2_SSD1306_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);
#endif

#ifndef OLED_CONTRAST
#define OLED_CONTRAST 40   // 0..255; stock ~255 is too bright for a desk board
#endif

struct Cell { char kind; int minutes; };
struct Row  { char line[6]; int n; Cell c[2]; };

static char hdrStop[12] = "----";
static char hdrTime[8]  = "";
static Row rows[4], pending[4];
static int nRows = 0, nPending = 0;
static uint32_t lastFrame = 0;
static bool haveFrame = false;
static String buf;

// Line number column width; remaining width is split equally among cells.
static const int LINE_W = 28;
static const int ROW_H  = 12;
static const int BASE0  = 26;

static void drawCell(int cellLeft, int cellW, int baseline, const Cell &c) {
  char t[8];
  // minutes first, then tag: "4(G)" — centred in the cell
  snprintf(t, sizeof t, "%d(%c)", constrain(c.minutes, 0, 99), c.kind);
  oled.setFont(u8g2_font_7x13B_tr);
  int tw = oled.getStrWidth(t);
  int x = cellLeft + (cellW - tw) / 2;
  if (x < cellLeft) x = cellLeft;
  oled.drawStr(x, baseline, t);
}

static void render() {
  oled.clearBuffer();
  oled.setFont(u8g2_font_7x13B_tr);
  oled.drawStr(0, 11, hdrStop);
  oled.setFont(u8g2_font_6x12_tr);
  if (hdrTime[0]) oled.drawStr(128 - oled.getStrWidth(hdrTime), 10, hdrTime);
  oled.drawHLine(0, 13, 128);

  uint32_t age = haveFrame ? (millis() - lastFrame) : 0;
  // Keep last frame for a long time; only blank if we never got data.
  if (!haveFrame) {
    oled.setFont(u8g2_font_6x12_tr);
    oled.drawStr(0, 34, "NStupido");
    oled.drawStr(0, 48, "cekam podatke...");
  } else if (nRows == 0) {
    oled.setFont(u8g2_font_6x12_tr);
    oled.drawStr(0, 34, "nema autobusa");
  } else {
    for (int i = 0; i < nRows && i < 4; i++) {
      int base = BASE0 + i * ROW_H;
      oled.setFont(u8g2_font_7x13B_tr);
      oled.drawStr(0, base, rows[i].line);
      int n = rows[i].n > 0 ? rows[i].n : 1;
      int rem = 128 - LINE_W;
      int cellW = rem / n;
      for (int k = 0; k < rows[i].n && k < 2; k++) {
        drawCell(LINE_W + k * cellW, cellW, base, rows[i].c[k]);
      }
    }
  }
  // Soft warning if USB link is stale, without wiping the board.
  if (haveFrame && age > 90000UL) {
    oled.setFont(u8g2_font_5x7_tr);
    oled.setDrawColor(0); oled.drawBox(0, 57, 128, 7); oled.setDrawColor(1);
    oled.drawStr(0, 63, "veza izgubljena");
  }
  oled.sendBuffer();
}

static bool parseCell(const String &s, Cell &c) {
  if (s.length() < 3) return false;
  int colon = s.indexOf(':');
  if (colon < 1) return false;
  c.kind = s[0];
  c.minutes = s.substring(colon + 1).toInt();
  return c.kind == 'G' || c.kind == 'E' || c.kind == 'P';
}

static void handleLine(String line) {
  line.trim();
  if (line.isEmpty()) return;
  String parts[4]; int np = 0, start = 0;
  for (int i = 0; i <= (int)line.length() && np < 4; i++) {
    if (i == (int)line.length() || line[i] == '|') { parts[np++] = line.substring(start, i); start = i + 1; }
  }
  char type = parts[0][0];
  if (type == 'H' && np >= 2) {
    strlcpy(hdrStop, parts[1].c_str(), sizeof hdrStop);
    strlcpy(hdrTime, np >= 3 ? parts[2].c_str() : "", sizeof hdrTime);
    nPending = 0;
  } else if (type == 'R' && np >= 2 && nPending < 4) {
    Row &r = pending[nPending];
    strlcpy(r.line, parts[1].c_str(), sizeof r.line);
    r.n = 0;
    for (int k = 2; k < np && r.n < 2; k++) if (parseCell(parts[k], r.c[r.n])) r.n++;
    nPending++;
  } else if (type == 'E') {
    memcpy(rows, pending, sizeof rows); nRows = nPending;
    haveFrame = true; lastFrame = millis();
    render();
    Serial.println("OK");
  } else if (type == '?') {
    Serial.println("NStupido OLED v2");
  }
}

void setup() {
  Serial.begin(115200);
  Wire.begin(OLED_SDA, OLED_SCL);
  delay(300);
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) Serial.printf("I2C device at 0x%02X\n", a);
  }
  oled.setBusClock(400000);
  oled.begin();
  oled.setContrast(OLED_CONTRAST);
  render();
}

void loop() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n') { handleLine(buf); buf = ""; }
    else if (buf.length() < 120) buf += ch;
  }
  static uint32_t lastDraw = 0;
  if (millis() - lastDraw > 5000) { lastDraw = millis(); render(); }
}
