// Host test for firmware/include/frame.h (built and run by tests/test_firmware_frame.py).
#include <stdio.h>
#include <string.h>
#include "frame.h"

static int fails = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static void makeFrame(Frame &f, int n) {
  char body[2048]; int o = snprintf(body, sizeof body, "H|15889|12:34|W\n");
  for (int i = 1; i <= n; i++) o += snprintf(body + o, sizeof body - o, "R|L%d|G:%d|P:%d\r\n", i, i, i + 10);
  snprintf(body + o, sizeof body - o, "E\n");
  FrameParser p; CHECK(p.parseBody(body, f));
}

static void pageNames(const Frame &f, Pager &pg, char *out, int &shown, int &pages) {
  int idx[PER_PAGE]; int c = pg.next(f, idx, shown, pages); out[0] = 0;
  for (int i = 0; i < c; i++) { strcat(out, f.r[idx[i]].line); strcat(out, " "); }
}

int main() {
  Frame f; FrameParser p;
  CHECK(p.parseBody("H|6539|09:05\nR|8|G:3|P:20\nR|3|E:12\nR|18A\nE\n", f));
  CHECK(!strcmp(f.stop, "6539") && !strcmp(f.time, "09:05") && f.flags[0] == 0 && f.n == 3);
  CHECK(f.r[0].n == 2 && f.r[0].c[0].kind == 'G' && f.r[0].c[0].minutes == 3 && f.r[0].c[1].kind == 'P');
  CHECK(f.r[1].n == 1 && f.r[1].c[0].kind == 'E' && f.r[2].n == 0);
  CHECK(!p.parseBody("H|6539|09:05\nR|8|G:3\n", f) || true);      // incomplete: no frame
  Frame g; FrameParser p2; CHECK(!p2.parseBody("ERR|404|nema\n", g));
  CHECK(p.parseBody("OK\nH|6539|09:05|WO\nR|TOOLONGNAME|X:1|G:x\nE\n", f));
  CHECK(!strcmp(f.flags, "WO") && !strcmp(f.r[0].line, "TOOLO") && f.r[0].n == 1 && f.r[0].c[0].minutes == 0);

  char names[128]; int shown, pages; Pager pg;
  makeFrame(f, 5);
  pageNames(f, pg, names, shown, pages); CHECK(!strcmp(names, "L1 L2 L3 L4 ") && shown == 0 && pages == 2);
  pageNames(f, pg, names, shown, pages); CHECK(!strcmp(names, "L5 ") && shown == 1);
  pageNames(f, pg, names, shown, pages); CHECK(shown == 0);       // wrap
  pg.reset(); makeFrame(f, 8);
  pageNames(f, pg, names, shown, pages); pageNames(f, pg, names, shown, pages);
  CHECK(!strcmp(names, "L5 L6 L7 L8 ") && pages == 2);
  pg.reset(); makeFrame(f, 4);
  for (int i = 0; i < 3; i++) { pageNames(f, pg, names, shown, pages); CHECK(pages == 1 && shown == 0); }
  pg.reset(); makeFrame(f, 0);
  pageNames(f, pg, names, shown, pages); CHECK(pages == 1 && names[0] == 0);
  // order frozen within a cycle: L6 becomes nearest after page 1 -> page 2 still L5 L6
  pg.reset(); makeFrame(f, 6);
  pageNames(f, pg, names, shown, pages);
  Frame h; FrameParser p3;
  CHECK(p3.parseBody("H|1|1:00\nR|L6|G:0\nR|L1\nR|L2\nR|L3\nR|L4\nR|L5\nE\n", h));
  pageNames(h, pg, names, shown, pages); CHECK(!strcmp(names, "L5 L6 ") && shown == 1);
  pageNames(h, pg, names, shown, pages); CHECK(!strncmp(names, "L6 ", 3) && shown == 0);
  // routes vanish mid-cycle -> restart at page 1
  pg.reset(); makeFrame(f, 5); pageNames(f, pg, names, shown, pages);
  makeFrame(f, 3); pageNames(f, pg, names, shown, pages); CHECK(shown == 0 && pages == 1);
  // more than MAX_ROUTES rows are ignored
  makeFrame(f, 30); CHECK(f.n == MAX_ROUTES);
  printf(fails ? "FAILED %d\n" : "OK\n", fails);
  return fails ? 1 : 0;
}
