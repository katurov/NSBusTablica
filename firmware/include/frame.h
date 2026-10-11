// NStupido frame model, parser and pager. Plain C++ (no Arduino) so it can be unit-tested on the host:
//   c++ -std=c++11 -Ifirmware/include tests/firmware/test_frame.cpp
//
// Text frame (server GET /frame?stop=<uid>, or the legacy USB bridge):
//   H|<stop>|<HH:MM>[|<flags>]   flags: W = warming (stop just added), O = offline (nsmart backoff, timetable only)
//   R|<line>|<cell>|<cell>        0..2 cells, cell = G:<min> live, E:<min> estimate, P:<min> timetable
//   E                             end of frame
#pragma once
#include <string.h>
#include <stdlib.h>

#ifndef MAX_ROUTES
#define MAX_ROUTES 24
#endif
#define PER_PAGE 4

struct Cell { char kind; int minutes; };
struct Route { char line[6]; int n; Cell c[2]; };
struct Frame {
  char stop[12]; char time[8]; char flags[4];
  Route r[MAX_ROUTES]; int n;
};

static inline void frameClear(Frame &f) { memset(&f, 0, sizeof f); }

// Copy at most dstSize-1 chars of [src, src+len) into dst, always terminated.
static inline void copyField(char *dst, size_t dstSize, const char *src, size_t len) {
  if (len >= dstSize) len = dstSize - 1;
  memcpy(dst, src, len); dst[len] = 0;
}

static inline bool parseCell(const char *s, size_t len, Cell &c) {
  if (len < 3 || s[1] != ':') return false;
  if (s[0] != 'G' && s[0] != 'E' && s[0] != 'P') return false;
  char num[8]; copyField(num, sizeof num, s + 2, len - 2);
  c.kind = s[0]; c.minutes = atoi(num);
  if (c.minutes < 0) c.minutes = 0;
  return true;
}

// Feed one line (without '\n'). Returns true when an 'E' line completes a frame; `out` then holds it.
struct FrameParser {
  Frame pending;
  bool inFrame;
  FrameParser() : inFrame(false) { frameClear(pending); }

  bool feed(const char *line, Frame &out) {
    size_t L = strlen(line);
    while (L && (line[L - 1] == '\r' || line[L - 1] == ' ')) L--;
    if (!L) return false;
    const char *p[5]; size_t pl[5]; int np = 0; size_t start = 0;
    for (size_t i = 0; i <= L && np < 5; i++) {
      if (i == L || line[i] == '|') { p[np] = line + start; pl[np] = i - start; np++; start = i + 1; }
    }
    char type = line[0];
    if (type == 'H' && np >= 2) {
      frameClear(pending); inFrame = true;
      copyField(pending.stop, sizeof pending.stop, p[1], pl[1]);
      if (np >= 3) copyField(pending.time, sizeof pending.time, p[2], pl[2]);
      if (np >= 4) copyField(pending.flags, sizeof pending.flags, p[3], pl[3]);
    } else if (type == 'R' && np >= 2 && inFrame && pending.n < MAX_ROUTES) {
      Route &r = pending.r[pending.n];
      copyField(r.line, sizeof r.line, p[1], pl[1]);
      r.n = 0;
      for (int k = 2; k < np && r.n < 2; k++) if (parseCell(p[k], pl[k], r.c[r.n])) r.n++;
      pending.n++;
    } else if (type == 'E' && L == 1 && inFrame) {
      out = pending; inFrame = false;
      return true;
    }
    return false;
  }

  // Parse a whole HTTP body; true if it contained a complete frame.
  bool parseBody(const char *body, Frame &out) {
    bool ok = false; char line[128]; size_t n = 0;
    for (const char *c = body;; c++) {
      if (*c == '\n' || *c == 0) {
        line[n] = 0;
        if (feed(line, out)) ok = true;
        n = 0;
        if (*c == 0) break;
      } else if (n < sizeof line - 1) line[n++] = *c;
    }
    return ok;
  }
};

// Pages of PER_PAGE routes, one page per push, wrapping around. Order is taken (nearest first, as sent) at
// the start of a cycle and kept for its remaining pages: a route never jumps pages mid-cycle. Same logic as
// oled_bridge.Pager.
struct Pager {
  char order[MAX_ROUTES][6]; int nOrder; int page;
  Pager() { reset(); }
  void reset() { nOrder = 0; page = 0; }

  static int find(const Frame &f, const char *name) {
    for (int i = 0; i < f.n; i++) if (!strcmp(f.r[i].line, name)) return i;
    return -1;
  }
  void takeAll(const Frame &f) {
    nOrder = 0;
    for (int i = 0; i < f.n; i++) { strcpy(order[nOrder++], f.r[i].line); }
  }
  // Fills idx[] with indexes into f.r for the page to show; returns how many. Sets shown/pages.
  int next(const Frame &f, int idx[PER_PAGE], int &shown, int &pages) {
    if (page == 0 || nOrder == 0) takeAll(f);
    else {
      char keep[MAX_ROUTES][6]; int nk = 0;
      for (int i = 0; i < nOrder; i++) if (find(f, order[i]) >= 0) strcpy(keep[nk++], order[i]);
      for (int i = 0; i < f.n && nk < MAX_ROUTES; i++) {
        bool have = false;
        for (int j = 0; j < nk; j++) if (!strcmp(keep[j], f.r[i].line)) { have = true; break; }
        if (!have) strcpy(keep[nk++], f.r[i].line);
      }
      memcpy(order, keep, sizeof keep); nOrder = nk;
    }
    pages = nOrder ? (nOrder + PER_PAGE - 1) / PER_PAGE : 1;
    if (page >= pages) { page = 0; takeAll(f); pages = nOrder ? (nOrder + PER_PAGE - 1) / PER_PAGE : 1; }
    shown = page;
    int cnt = 0;
    for (int k = shown * PER_PAGE; k < nOrder && cnt < PER_PAGE; k++) idx[cnt++] = find(f, order[k]);
    page = (shown + 1) % pages;
    return cnt;
  }
};
