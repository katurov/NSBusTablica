"""Offline unit tests: python3 -m unittest discover -s tests -v   (from the project directory)"""
import os, sys, json, time, shutil, tempfile, unittest
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import timetable as T          # noqa: E402
import rs_holidays             # noqa: E402
import nstupido as N           # noqa: E402

def deps(*ts, mark=None):
    return [{"t": t, "mark": mark, "lowfloor": False} for t in ts]

def table(*dirs):
    return {"title": "x", "legend": None, "directions": [{"header": h, "departures": d} for h, d in dirs]}

def fake_lines(tag=""):
    """Minimal timetable: line 8 (6539, dir 0, tt 22), 18A night loop, 3 dir 1."""
    r = {"8": table(("Smer A: NOVO NASELJE - CENTAR - LIMAN I", deps("17:03", "17:18", "23:40", "00:00")),
                    ("Smer B: LIMAN I - CENTAR - NOVO NASELJE", deps("17:00"))),
         "18A": table(("Smer A: N.NASELJE - ... - N.NASELJE", deps("00:30", "01:30", "02:30", "03:30"))),
         "3": table(("Smer A: PETROVARADIN - CENTAR - DETELINARA", deps("17:00")),
                    ("Smer B: DETELINARA - CENTAR - PETROVARADIN", deps("17:05", "17:21")))}
    p = {"8": table(("Smer A: NOVO NASELJE - CENTAR - LIMAN I", deps("10:00" if not tag else "10:05")), ("B", []))}
    return {"rvg": {"R": r, "S": r, "N": r, "P": p}, "rvp": {d: {} for d in "RSNP"}}

def make_tt(tmp, versions=("2026-10-01",)):
    tt = T.Timetables(cache_dir=tmp, fetch=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    for v in versions:
        tt.data[v] = {"vaziod": v, "fetched": time.time(), "lines": fake_lines(v if v != versions[0] else "")}
    return tt


class DayType(unittest.TestCase):
    def test_holidays_law(self):
        h26, h27 = rs_holidays.rs_holidays(2026), rs_holidays.rs_holidays(2027)
        self.assertIn(date(2026, 4, 10), h26)            # Good Friday (Orthodox Easter 12 Apr 2026)
        self.assertIn(date(2026, 4, 13), h26)            # Easter Monday
        self.assertIn(date(2026, 2, 17), h26)            # 15 Feb on Sunday -> Tuesday off (čl. 3a)
        self.assertIn(date(2027, 5, 4), h27)             # 2 May = Easter Sunday -> 4 May off
        self.assertEqual(rs_holidays.orthodox_easter(2027), date(2027, 5, 2))

    def test_fallback(self):
        self.assertEqual(T.fallback_day_type(date(2026, 11, 11)), "P")   # Dan primirja (Wednesday)
        self.assertEqual(T.fallback_day_type(date(2026, 10, 11)), "N")   # Sunday
        self.assertEqual(T.fallback_day_type(date(2026, 10, 10)), "S")   # Saturday
        self.assertEqual(T.fallback_day_type(date(2026, 10, 6)), "R")    # Tuesday
        self.assertEqual(T.fallback_day_type(date(2026, 5, 2)), "P")     # holiday on Saturday -> P

    def test_site_indicator_wins_for_its_day(self):
        tmp = tempfile.mkdtemp()
        try:
            tt = make_tt(tmp)
            tt.index["day_types"]["2026-11-11"] = "N"
            self.assertEqual(tt.day_type(date(2026, 11, 11)), "N")
            self.assertEqual(tt.day_type(date(2026, 11, 12)), "R")      # no indicator -> fallback
        finally:
            shutil.rmtree(tmp)

    def test_parse_index_praznik(self):
        with open(os.path.join(HERE, "fixtures", "index_praznik_20260413.html"), encoding="utf-8", errors="replace") as f:
            idx = T.parse_index(f.read())
        self.assertEqual(idx["day_type"], "P")
        self.assertEqual(idx["vaziod"], ["2026-04-01"])


class Departures(unittest.TestCase):
    def test_wrap_after_midnight(self):
        e = T.expand_departures(deps("22:50", "23:40", "00:15", "03:10"))
        self.assertEqual([x[0] for x in e], [0, 0, 1, 1])

    def test_early_start_and_late_end(self):           # e.g. 56 from Begeč: 03:50 ... 23:40, 03:00
        e = T.expand_departures(deps("03:50", "12:00", "23:40", "03:00"))
        self.assertEqual([x[0] for x in e], [0, 0, 0, 1])

    def test_night_line(self):                          # 18A: 00:30-03:30, all after midnight
        e = T.expand_departures(deps("00:30", "01:30", "02:30", "03:30"))
        self.assertEqual([x[0] for x in e], [1, 1, 1, 1])

    def test_parse_real_table(self):
        with open(os.path.join(HERE, "fixtures", "ispis_rvg_R_14.html"), encoding="utf-8") as f:
            t = T.parse_timetables(f.read())["14"]
        self.assertEqual(len(t["directions"]), 2)
        self.assertIn("VETERNIK - SAJLOVO - CENTAR", t["directions"][1]["header"])
        marks = {x["mark"] for x in t["directions"][1]["departures"]}
        self.assertTrue({"S", "NRS"} <= marks)

    def test_after_midnight_trip_belongs_to_previous_service_day(self):
        tmp = tempfile.mkdtemp()
        try:
            tt = make_tt(tmp)
            now = datetime(2026, 10, 7, 0, 10)          # Wednesday 00:10
            w = {(t["line"], t["dep"]) : t for t in tt.window("6539", now, back=timedelta(hours=1), ahead=timedelta(hours=1))}
            # 00:00 departure of line 8 comes from Tuesday's list (after 23:40) -> dated 7 Oct 00:00
            self.assertIn(("8", datetime(2026, 10, 7, 0, 0)), w)
            self.assertEqual(w[("8", datetime(2026, 10, 7, 0, 0))]["trip_id"], "6539|8|0|2026-10-07T00:00")
            # 18A 00:30 of service day 6 Oct runs on 7 Oct 00:30 (and 7 Oct's own 18A trips are on 8 Oct)
            self.assertIn(("18A", datetime(2026, 10, 7, 0, 30)), w)
            self.assertNotIn(("18A", datetime(2026, 10, 8, 0, 30)), w)
        finally:
            shutil.rmtree(tmp)

    def test_holiday_uses_P_table(self):
        tmp = tempfile.mkdtemp()
        try:
            tt = make_tt(tmp)
            trips = tt.trips("6539", date(2026, 11, 11))
            self.assertEqual({t["day_type"] for t in trips}, {"P"})
            self.assertEqual([t["dep"].strftime("%H:%M") for t in trips if t["line"] == "8"], ["10:00"])
            self.assertEqual({t["day_type"] for t in tt.trips("6539", date(2026, 10, 11))}, {"N"})
        finally:
            shutil.rmtree(tmp)


class Switch(unittest.TestCase):
    def test_validity_switch(self):
        tmp = tempfile.mkdtemp()
        try:
            tt = make_tt(tmp, versions=("2026-10-01", "2026-11-01"))
            self.assertEqual(tt.validity_for(date(2026, 10, 31)), "2026-10-01")
            self.assertEqual(tt.validity_for(date(2026, 11, 1)), "2026-11-01")
            self.assertEqual(tt.validity_for(date(2026, 9, 15)), "2026-10-01")   # nothing older: oldest copy
            p11 = [t["dep"].strftime("%H:%M") for t in tt.trips("6539", date(2026, 11, 11)) if t["line"] == "8"]
            self.assertEqual(p11, ["10:05"])          # holiday table of the new validity
        finally:
            shutil.rmtree(tmp)

    def test_refresh_new_vaziod_and_site_down(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(HERE, "fixtures", "ispis_rvg_R_14.html"), encoding="utf-8") as f:
                table_html = f.read()
            calls = []
            def fetch(path, params=None):
                calls.append((path, dict(params or {})))
                if path == "/red-voznje/gradski":
                    return ('<p>Autobusi danas voze po redu vožnje za\n RADNI DAN </p>'
                            '<option value="2026-10-01"  >x</option><option value="2026-11-01"  >y</option>')
                if path == "/red-voznje/lista-linija":
                    return '<option value="14">14 CENTAR</option><option value="99">99</option>'
                return table_html
            tt = T.Timetables(cache_dir=tmp, fetch=fetch)
            self.assertTrue(tt.refresh(today=date(2026, 10, 20)))
            self.assertEqual(sorted(tt.data), ["2026-10-01", "2026-11-01"])
            self.assertTrue(os.path.exists(os.path.join(tmp, "timetable_2026-11-01.json")))
            self.assertEqual(tt.validity_for(date(2026, 10, 20)), "2026-10-01")
            self.assertEqual(tt.validity_for(date(2026, 11, 2)), "2026-11-01")
            asked = [c[1].get("linija[]") for c in calls if c[0] == "/red-voznje/ispis-polazaka"]
            self.assertTrue(all(a == ["14"] for a in asked))           # only our lines are requested
            # site down: refresh fails, cached copies survive (also after reload from disk)
            tt.fetch = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
            self.assertFalse(tt.refresh(today=date(2026, 10, 21)))
            tt2 = T.Timetables(cache_dir=tmp, fetch=tt.fetch)
            self.assertEqual(sorted(tt2.data), ["2026-10-01", "2026-11-01"])
            self.assertEqual(tt2.day_type(date(2026, 10, 20)), "R")
        finally:
            shutil.rmtree(tmp)


class Matching(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mem = N.Memory(make_tt(self.tmp))
        self.mem.learn["travel"] = {}
        self.now = datetime(2026, 10, 6, 17, 10).timestamp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def bus(self, g, line, dep, sec, **kw):
        b = {"line": line, "direction": "Liman 1", "seconds": sec, "stops_between": 3, "garage_no": g,
             "lat": 45.0, "lng": 19.0, "current_stop": "x", "dep": dep, "next_station": 9, "pos": 12, "first": "13219"}
        b.update(kw)
        return b

    def board(self, t):
        return [(b["line"], b["status"], b["scheduled_departure"], b["garage_no"]) for b in self.mem.view("6539", t)["buses"]]

    def test_scheduled_turns_live_then_lost_never_twice(self):
        b0 = self.board(self.now)
        self.assertIn(("8", "scheduled", "17:18", None), b0)          # dep 17:18 + 22 min = 17:40
        self.mem.update("6539", {"name": "s", "buses": [self.bus("790", "8", "17:18:00", 1700)]}, self.now)
        b1 = self.board(self.now + 1)
        self.assertIn(("8", "live", "17:18", "790"), b1)
        self.assertNotIn(("8", "scheduled", "17:18", None), b1)       # the trip is not shown twice
        b2 = self.board(self.now + 120)                               # not seen for 2 min -> lost
        self.assertIn(("8", "lost", "17:18", "790"), b2)
        self.assertEqual(sum(1 for x in b2 if x[2] == "17:18"), 1)

    def test_fallback_nearest_within_6_min(self):
        # bus labelled with a wrong departure (17:13) but expected 17:38 -> matches scheduled 17:18 trip (17:40)
        self.mem.update("6539", {"name": "s", "buses": [self.bus("791", "8", "17:13:00", 28 * 60)]}, self.now)
        v = self.mem.view("6539", self.now + 1)["buses"]
        self.assertEqual([b["trip_id"] for b in v if b["garage_no"] == "791"], ["6539|8|0|2026-10-06T17:18"])
        self.assertFalse(any(b["status"] == "scheduled" and b["scheduled_departure"] == "17:18" for b in v))

    def test_stale_departure_label_not_trusted(self):
        # bus labelled 17:03 (expected 17:25) but due 17:45 -> must not consume the 17:03 trip; nearest is 17:18 (17:40)
        self.mem.update("6539", {"name": "s", "buses": [self.bus("792", "8", "17:03:00", 35 * 60)]}, self.now)
        v = self.mem.view("6539", self.now + 1)["buses"]
        self.assertEqual([b["trip_id"] for b in v if b["garage_no"] == "792"], ["6539|8|0|2026-10-06T17:18"])
        self.assertIn("6539|8|0|2026-10-06T17:03", [b["trip_id"] for b in v if b["status"] == "scheduled"])

    def test_arrived_trip_stays_suppressed(self):
        self.mem.update("6539", {"name": "s", "buses": [self.bus("790", "8", "17:03:00", 10, stops_between=0)]}, self.now)
        self.mem.view("6539", self.now + 1)
        later = self.now + 200                                         # bus gone (arrived); 17:25 is within grace
        v = self.mem.view("6539", later)["buses"]
        self.assertFalse(any(b["scheduled_departure"] == "17:03" for b in v))
        self.assertEqual(self.mem.arrivals[-1]["dep"], "17:03:00")          # arrival recorded for accuracy/learning
        self.assertEqual(self.mem.learn["travel"]["6539|8"], [int(self.now + 10 - datetime(2026, 10, 6, 17, 3).timestamp())])

    def test_unseen_scheduled_dropped_3_min_after_due(self):
        due = datetime(2026, 10, 6, 17, 25).timestamp()               # line 8 dep 17:03 + 22
        self.assertIn(("8", "scheduled", "17:03", None), self.board(due + 170))
        self.assertNotIn(("8", "scheduled", "17:03", None), self.board(due + 190))

    def test_project_feeder(self):
        bus = {"line": "8", "seconds": 503, "garage_no": "790", "next_station": 2, "dep": "17:03:00",
               "_ids": ["a", "6540", "6539"], "_slr": [0, 498, 615], "_tt": [0, 20, 22]}
        ob = N.project(bus, "6540", "6539")
        self.assertEqual(ob["seconds"], 503 + 117)
        self.assertEqual(ob["pos"], 3)
        self.assertEqual(ob["source"], "6540")
        self.assertIsNone(N.project(bus, "6539", "6540"))               # target before feeder
        self.assertIsNone(N.project(dict(bus, next_station=4), "6540", "6539"))   # bus already past target


class Learning(unittest.TestCase):
    def test_stale_label_sample_ignored(self):
        m = N.Memory(None)
        m.learn["travel"] = {}
        m.learn["tt"] = {"6712|7A|13219": {"tt": 23, "t": 0}}
        now = datetime(2026, 10, 6, 18, 15).timestamp()
        b = {"line": "7A", "direction": "x", "seconds": 5, "stops_between": 0, "garage_no": "971", "lat": 1, "lng": 1,
             "current_stop": "x", "dep": "17:09:00", "next_station": 13, "pos": 13}
        m.update("6712", {"name": "s", "buses": [b]}, now)          # 66 min after "departure" -> not a sample
        self.assertEqual(m.learn["travel"].get("6712|7A", []), [])
        self.assertEqual(m.arrivals[-1]["garage_no"], "971")         # still logged as an arrival


class Feeders(unittest.TestCase):
    def test_predecessors_without_terminals(self):
        m = N.Memory(None)
        m.learn["routes"] = {"55|6822": {"ids": ["6822", "15889", "x"], "t": time.time()},
                             "14|16558": {"ids": ["a", "b", "6728", "6586", "6712"], "t": time.time()}}
        self.assertEqual(m.feeders("15889"), [])                     # only predecessor is the terminal
        self.assertEqual(m.feeders("6712"), ["6586", "6728"])

    def test_near_arrival_recorded_on_drop(self):
        m = N.Memory(None)
        m.learn["travel"] = {}
        t0 = datetime(2026, 10, 6, 17, 30).timestamp()
        b = {"line": "8", "direction": "L", "seconds": 90, "stops_between": 0, "garage_no": "7", "lat": 1, "lng": 1,
             "current_stop": "x", "dep": "17:08:00", "next_station": 12, "pos": 12, "source": "6540"}
        m.update("6539", {"name": "s", "buses": [b]}, t0)
        m.view("6539", t0 + 90 + 31 + N.LIVE_MAX_AGE)                  # countdown hit 0 >30 s ago, unseen -> dropped
        self.assertEqual(m.arrivals[-1]["quality"], "near")
        self.assertEqual(m.arrivals[-1]["arrival"], int(t0 + 90))


if __name__ == "__main__":
    unittest.main()
