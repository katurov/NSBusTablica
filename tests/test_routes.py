"""Offline tests for route naming, the network map / station list caches, stop -> line derivation, the new-stop
flow and routes first seen live. No network: gspns and nsmart are fakes, time is a fake clock."""
import json, os, sys, tempfile, shutil, unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import nstupido as N           # noqa: E402
import timetable as T          # noqa: E402
import routes as R             # noqa: E402

TZ = N.local_tz("Europe/Belgrade")


# ---------------- fake gspns ----------------
TABLES = {   # rv -> base -> [(header, [(HH:MM, mark)])]
    "rvg": {
        "1": ("1 KLISA-CENTAR-LIMAN I", [("Смер A: KLISA - CENTAR - LIMAN I", [("07:05", None), ("07:10", "GL"), ("07:20", "NRJ"), ("07:30", "J")]),
                                         ("Смер B: LIMAN I - CENTAR - KLISA", [("07:07", None)])]),
        "6": ("6 PODBARA-CENTAR-ADICE", [("Смер A: PODBARA - CENTAR - ADICE", [("07:00", None)]),
                                         ("Смер B: ADICE - CENTAR - PODBARA", [("07:02", None), ("07:12", "D"), ("07:22", "NĐ")])]),
        "14": ("14 CENTAR - SAJLOVO - VETERNIK", [("Смер A: CENTAR - SAJLOVO - VETERNIK", [("07:01", None)]),
                                                  ("Смер B: VETERNIK - SAJLOVO - CENTAR", [("07:03", None), ("07:13", "S"), ("07:23", "NGS"), ("07:33", "DDJ")])]),
        "3A": ("3A ZEL.STANICA - POBEDA", [("Смер B: ZEL.STANICA - POBEDA", [("07:09", None)])]),
    },
    "rvp": {
        "54": ("54 FUTOG GRMEČKA", [("Смер A: Polasci za FUTOG GRMEČKA", [("07:04", None), ("07:14", '"A"')]),
                                    ("Смер B: Polasci iz FUTOG GRMEČKA", [("07:06", None), ("07:16", '"A"'), ("07:26", "ZA")])]),
    },
}
OPTIONS = {"rvg": ["1*", "3A.", "6", "14"], "rvp": ["54"]}


def ispis_html(rv, bases):
    out = []
    for b in bases:
        title, dirs = TABLES[rv][b.rstrip(".*")]
        ths = "".join("<th>%s</th>" % h for h, _ in dirs)
        tds = ""
        for _, deps in dirs:
            cell = ""
            for hm, mark in deps:
                cell += "<br/><b>%s</b><sup><span class=' '>%s<b>%s</b></span></sup>" % (
                    hm[:2], hm[3:], (mark or "").replace('"', "&quot;"))
            tds += "<td valign='top'>%s</td>" % cell
        out.append("<div class='table-title'>Linija : %s</div><table><tr>%s</tr><tr>%s</tr></table>" % (title, ths, tds))
    return "".join(out)


class FakeGspns:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if self.fail:
            raise OSError("offline")
        if path == "/red-voznje/gradski":
            return '<p>danas voze po redu vožnje za RADNI DAN</p><option value="2026-10-01">'
        if path == "/red-voznje/lista-linija":
            return "".join('<option value="%s">%s</option>' % (o, o) for o in OPTIONS[params["rv"]])
        if path == "/red-voznje/ispis-polazaka":
            return ispis_html(params["rv"], params["linija[]"])
        if path == R.MAP_PAGE:
            return "".join('<li><a id="%s" data-toggle="tooltip" class="button-linija %s ablin%s" title="%s"  >   %s</a></li>'
                           % (b["id"], b["kind"], b["id"], b["title"], b["label"]) for b in BUTTONS)
        if path == R.MAP_STOPS:
            return json.dumps(["%s|%s|%s|%s|x|I" % (",".join("[%s]" % l for l in s["labels"]), s["lon"], s["lat"], s["name"])
                               for s in MAP_STOPS if any(l == BTN_BY_ID[params["linija"]] for l in s["labels"])])
        raise AssertionError(path)


# ---------------- fake map + station list ----------------
def _btn(i, label, title, kind="grad"):
    return {"id": str(i), "kind": kind, "title": title, "label": label}


BUTTONS = [_btn(1, "1A", "KLISA - CENTAR - LIMAN 1"), _btn(2, "1B", "LIMAN 1 - CENTAR - KLISA"),
           _btn(3, "1GLA", "KLISA (GORNJE LIVADE) - CENTAR - LIMAN 1"), _btn(4, "1JA", "KLISA (JEGRIČKA) - CENTAR - LIMAN I"),
           _btn(5, "6A", "PODBARA - CENTAR - ADICE"), _btn(6, "6B", "ADICE - CENTAR - PODBARA"),
           _btn(7, "6DB", "ADICE - CENTAR - PODBARA (DANUBIUS)"), _btn(8, "6ĐB", "ADICE - DO NOVITETA (ĐAČKI)"),
           _btn(9, "14A", "CENTAR - SAJLOVO - VETERNIK"), _btn(10, "14B", "VETERNIK - SAJLOVO - CENTAR"),
           _btn(11, "14SB", "VETERNIK - SAJLOVO - CENTAR (OD SAJLOVA)"), _btn(12, "14ĐB", "SAJLOVO - DETELINARA (ĐAČKI)"),
           _btn(13, "3AA", "ŽELEZNIČKA STANICA - POBEDA"), _btn(14, "3AB", "POBEDA - ŽELEZNIČKA STANICA"),
           _btn(20, "54A", "NOVI SAD (ŽELEZNIČKA STANICA) - FUTOG (GRMEČKA)", "prigrad"),
           _btn(21, "54B", "FUTOG (GRMEČKA) - NOVI SAD (ŽELEZNIČKA STANICA)", "prigrad"),
           _btn(22, "54AA", "NOVI SAD (ŽELEZNIČKA STANICA) - FUTOG (GRMEČKA) (AROMA)", "prigrad"),
           _btn(23, "54AB", "FUTOG (GRMEČKA) - NOVI SAD (ŽELEZNIČKA STANICA) (AROMA)", "prigrad"),
           _btn(24, "54ŽAA", "NOVI SAD (ŽELEZNIČKA STANICA) - FUTOG (GRMEČKA) (I ŽELEZNIČKOM DO AROME)", "prigrad")]
BTN_BY_ID = {b["id"]: b["label"] for b in BUTTONS}
MAP_STOPS = [   # CITY and SUB share coordinates (like 6712 / 15889); OPP is the other side, ~78 m away
    {"name": "CENTER", "lat": 45.25, "lon": 19.84, "labels": ["1A", "1GLA", "6B", "6DB", "14B", "14SB", "3AA"]},
    {"name": "CENTER", "lat": 45.25, "lon": 19.84, "labels": ["54A", "54AA", "54ŽAA"]},
    {"name": "CENTER OPP", "lat": 45.2507, "lon": 19.84, "labels": ["1B", "6A", "14A", "3AB"]},
    {"name": "ELSEWHERE", "lat": 45.30, "lon": 19.80, "labels": ["1JA", "6ĐB", "14ĐB", "54B", "54AB"]},
]
STATIONS = {
    "9001": {"name": "Center city", "lat": 45.25, "lon": 19.84, "lines": ["1", "1GL", "3A", "6", "14", "14S", "14GS"], "types": {}},
    "9002": {"name": "Center suburban", "lat": 45.25, "lon": 19.84, "lines": ["54", '54"A"'], "types": {}},
    "9003": {"name": "Center opposite", "lat": 45.2507, "lon": 19.84, "lines": ["1", "3A", "6", "14"], "types": {}},
    "9004": {"name": "Far away", "lat": 45.40, "lon": 19.60, "lines": ["54"], "types": {}},
}


def raw_stations(n=120):
    out = [{"id": int(u), "name": s["name"], "station_id": "X", "coordinates": {"latitude": str(s["lat"]), "longitude": str(s["lon"])},
            "lines_for_station": s["lines"], "lines_for_station_additional_data": []} for u, s in STATIONS.items()]
    out += [{"id": 100000 + i, "name": "filler %d" % i, "coordinates": {"latitude": "45", "longitude": "19"},
             "lines_for_station": []} for i in range(n)]
    return out


class Env:
    """Temp cache dir with a station list + map + timetable index (no timetables yet)."""

    def __init__(self, map_fetched=None, stations_fetched=None):
        self.dir = tempfile.mkdtemp()
        self.gspns = FakeGspns()
        R._save_json(os.path.join(self.dir, "stations.json"),
                     {"fetched": stations_fetched or 1.0, "stations": R.Stations.compact(raw_stations())})
        stops = {b["id"]: [dict(s) for s in MAP_STOPS if b["label"] in s["labels"]] for b in BUTTONS}
        R._save_json(os.path.join(self.dir, "map.json"), {"fetched": map_fetched or 1.0, "buttons": BUTTONS, "stops": stops})
        os.makedirs(os.path.join(self.dir, "tt"))
        R._save_json(os.path.join(self.dir, "tt", "index.json"),
                     {"day_types": {"2026-10-09": "R"}, "vaziod": ["2026-10-01"], "checked": 1, "ok_at": 1})
        self.stations = R.Stations(os.path.join(self.dir, "stations.json"), fetch=raw_stations)
        self.tt = T.Timetables(cache_dir=os.path.join(self.dir, "tt"), fetch=self.gspns)
        self.map = R.NetworkMap(os.path.join(self.dir, "map.json"), fetch=self.tt.fetch, min_buttons=5)
        self.naming = R.Naming()
        self.resolver = R.Resolver(self.naming, self.stations, self.map, manual={},
                                   rv_of=lambda b: self.tt.known_bases().get(b))
        self.tt.resolver = self.resolver

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def trips(self, uid, day=datetime(2026, 10, 9).date()):
        return sorted((t["base"], t["dep"].strftime("%H:%M"), t["mark"]) for t in self.tt.trips(uid, day))


# ---------------- tests ----------------
class NamingTest(unittest.TestCase):
    def setUp(self):
        self.n = R.Naming({"1", "3", "3A", "3B", "6", "6A", "9", "10", "14", "15", "54", "71", "5N"})

    def test_display_merges_variants_keeps_distinct_routes(self):
        cases = {"1GL": "1", "1J": "1", "14S": "14", "14GS": "14", "10MAL": "10", "10APT": "10", "9A": "9",
                 '54"A"': "54", "54ŽA": "54", "54KF": "54", "71IS": "71", "15NK": "15", "3": "3", "3A": "3A", "3B": "3B",
                 "6A": "6A", "5N": "5N", "7A": "7A", "7B": "7B", "11A": "11A", "11B": "11B", "18A": "18A", "18B": "18B",
                 "1*": "1", "3A.": "3A", "20": "20"}
        for line, want in cases.items():
            self.assertEqual(self.n.display(line), want, line)

    def test_parse_map_labels(self):
        cases = {"1GLA": ("1", "GL", "A"), "1A": ("1", "", "A"), "3B": ("3", "", "B"), "3AA": ("3A", "", "A"),
                 "3AB": ("3A", "", "B"), "3BA": ("3B", "", "A"), "9AA": ("9", "A", "A"), "6AA": ("6A", "", "A"),
                 "6A": ("6", "", "A"), "6DB": ("6", "D", "B"), "7A": ("7A", "", "A"), "7B": ("7B", "", "B"),
                 "11ĐB": ("11B", "Đ", "B"), "18A": ("18A", "", "A"), "54AŽGB": ("54", "AŽG", "B"),
                 "10ALMB": ("10", "ALM", "B"), "71VMČA": ("71", "VMČ", "A"), "5NA": ("5N", "", "A"), "13UĐA": ("13", "UĐ", "A")}
        for label, want in cases.items():
            self.assertEqual(self.n.parse_label(label), want, label)

    def test_mark_to_variant(self):
        one = {"", "GL", "J"}
        self.assertEqual(R.mark_variant(None, one), "")
        self.assertEqual(R.mark_variant("GL", one), "GL")
        self.assertEqual(R.mark_variant("NRJ", one), "J")              # low-floor prefix
        self.assertEqual(R.mark_variant("NRG", one), "GL")             # NR + unique prefix
        fourteen = {"", "S", "DJ"}
        self.assertEqual(R.mark_variant("NRS", fourteen), "S")
        self.assertIsNone(R.mark_variant("NGS", fourteen))            # 14GS is not on the map -> unknown
        self.assertEqual(R.mark_variant("DDJ", fourteen), "DJ")        # school trip
        self.assertEqual(R.mark_variant('"A"', {"", "A", "ZA"}), "A")
        self.assertEqual(R.mark_variant("ZA", {"", "A", "ZA"}), "ZA")  # map "ŽA"
        self.assertEqual(R.mark_variant("ALI", {"", "A"}), "A")        # 9 Alibegovac: three names for one variant
        self.assertIsNone(R.mark_variant("MR", {"AL", "ALM", "APT"}))
        self.assertIsNone(R.mark_variant(None, {"AL", "ALM"}))         # route 10 has no plain variant on the map

    def test_choose_column(self):
        two = [{"header": "Смер A: PETROVARADIN - CENTAR - DETELINARA"}, {"header": "Смер B: DETELINARA - CENTAR - PETROVARADIN"}]
        self.assertEqual(R.choose_column(two, "B", "DETELINARA - CENTAR - PETROVARADIN"), 1)
        self.assertEqual(R.choose_column(two, "A", None), 0)
        one_b = [{"header": "Смер B: ZEL.STANICA - POBEDA"}]
        self.assertEqual(R.choose_column(one_b, "A", "ŽELEZNIČKA STANICA - POBEDA"), 0)   # map 3AA = gspns "Smer B"
        self.assertIsNone(R.choose_column(one_b, "B", "POBEDA - ŽELEZNIČKA STANICA"))   # the other side: not served
        sub = [{"header": "Смер A: Polasci za FUTOG GRMEČKA"}, {"header": "Смер B: Polasci iz FUTOG GRMEČKA"}]
        self.assertEqual(R.choose_column(sub, "A", "NOVI SAD (ŽELEZNIČKA STANICA) - FUTOG (GRMEČKA)"), 0)
        self.assertEqual(R.choose_column(sub, "B", "FUTOG (GRMEČKA) - NOVI SAD (ŽELEZNIČKA STANICA)"), 1)
        sunday = [{"header": "Смер B: Polasci iz STARI RAKOVAC"}]
        self.assertIsNone(R.choose_column(sunday, "A", "NOVI SAD (AUTOBUSKA STANICA) - STARI RAKOVAC"))


class MapMatchTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_same_coordinates_tie_broken_by_line_set(self):
        n, m = self.env.naming, self.env.map
        city = m.match(45.25, 19.84, {"1", "6", "14"}, n)
        sub = m.match(45.25, 19.84, {"54"}, n)
        self.assertIn("1GLA", city["labels"])
        self.assertIn("54AA", sub["labels"])
        opp = m.match(45.2507, 19.84, {"1", "6", "14"}, n)
        self.assertIn("1B", opp["labels"])
        self.assertIsNone(m.match(45.40, 19.60, {"54"}, n))           # nothing within radius

    def test_derived_entries_and_mark_rules(self):
        e = self.env
        self.assertEqual({(c["base"], c["letter"]) for c in e.resolver("9001")},
                         {("1", "A"), ("3A", "A"), ("6", "B"), ("14", "B")})
        e.tt.ensure(["9001", "9002", "9003"])
        got = e.trips("9001")
        # 1: plain + GL pass; J / NRJ belong to 1JA which the map puts elsewhere
        self.assertIn(("1", "07:05", None), got)
        self.assertIn(("1", "07:10", "GL"), got)
        self.assertNotIn(("1", "07:20", "NRJ"), got)
        self.assertNotIn(("1", "07:30", "J"), got)
        # 6: D (6DB) passes, NĐ (6ĐB, school trip elsewhere) does not
        self.assertIn(("6", "07:12", "D"), got)
        self.assertNotIn(("6", "07:22", "NĐ"), got)
        # 14: S passes, NGS unknown to the map -> included, DDJ = 14ĐB elsewhere -> excluded
        self.assertIn(("14", "07:13", "S"), got)
        self.assertIn(("14", "07:23", "NGS"), got)
        self.assertNotIn(("14", "07:33", "DDJ"), got)
        # 3A: map 3AA, gspns' only column is labelled "Smer B" -> matched by destination
        self.assertIn(("3A", "07:09", None), got)
        self.assertNotIn("3A", {b for b, _, _ in e.trips("9003")})   # 3AB side: that column does not serve it
        self.assertEqual(e.trips("9002"), [("54", "07:04", None), ("54", "07:14", '"A"')])
        self.assertEqual({(b, hm) for b, hm, _ in e.trips("9003")}, {("1", "07:07"), ("6", "07:00"), ("14", "07:01")})

    def test_entry_names_follow_nsmart(self):
        c = {x["base"]: x for x in self.env.resolver("9001")}
        self.assertEqual(c["1"]["name_of"]("GL"), "1GL")
        self.assertEqual(c["14"]["name_of"]("NRS"), "14S")
        self.assertEqual(c["14"]["name_of"]("NGS"), "14GS")
        self.assertEqual(c["6"]["name_of"]("D"), "6")


class CacheAgeTest(unittest.TestCase):
    def test_rule(self):
        day = 86400
        start = 1_000_000
        self.assertFalse(R.cache_due(None, start + 3599, start))       # missing, but not before +1 h
        self.assertTrue(R.cache_due(None, start + 3600, start))
        self.assertFalse(R.cache_due(start - 13 * day, start + 3600, start))
        self.assertTrue(R.cache_due(start - 15 * day, start + 3600, start))

    def test_refresher_one_hour_after_start_then_daily(self):
        env = Env(map_fetched=1.0, stations_fetched=1.0)          # both ancient
        try:
            clock = [datetime(2026, 10, 11, 14, 0, tzinfo=TZ).timestamp()]
            svc = N.Service(N.load_config(os.path.join(os.path.dirname(HERE), "config.toml")), N.Memory(env.tt),
                            fetch_fn=lambda u: {"name": u, "buses": []},
                            gate=N.Gate(0, 5, 180, clock=lambda: clock[0], sleep=lambda d: None),
                            clock=lambda: clock[0], sleep=lambda d: None,
                            resolver=env.resolver, stations=env.stations, netmap=env.map)
            ref = N.ReferenceRefresher(svc, svc.cfg, clock=lambda: clock[0],
                                       now_dt=lambda: datetime.fromtimestamp(clock[0], TZ))
            self.assertAlmostEqual(ref.step(), 3600)                   # nothing before +1 h
            self.assertEqual(ref.log, [])
            clock[0] += 3600
            wait = ref.step()                                          # 15:00: both refreshed
            self.assertEqual(ref.log, [("stations", True), ("map", True)])
            self.assertAlmostEqual(wait, (datetime(2026, 10, 12, 5, 10, tzinfo=TZ).timestamp() - clock[0]))
            pages = [c for c in env.gspns.calls if c[0] == R.MAP_PAGE]
            self.assertEqual(len(pages), 1)
            clock[0] += wait                                           # next day 05:10: fresh, nothing to do
            ref.step()
            self.assertEqual(len(ref.log), 2)
            clock[0] += 15 * 86400                                      # long-running server, 15 days later
            ref.step()
            self.assertEqual(ref.log[2:], [("stations", True), ("map", True)])
        finally:
            env.close()

    def test_map_failure_keeps_old_cache(self):
        env = Env(map_fetched=5.0)
        try:
            env.gspns.fail = True
            self.assertFalse(env.map.refresh())
            self.assertEqual(env.map.fetched, 5.0)
            self.assertEqual(len(env.map.buttons), len(BUTTONS))
            again = R.NetworkMap(env.map.path)
            self.assertEqual(again.fetched, 5.0)
        finally:
            env.close()

    def test_missing_station_list_fetched_once_through_gate(self):
        d = tempfile.mkdtemp()
        try:
            calls, waits = [], []
            st = R.Stations(os.path.join(d, "s.json"), fetch=lambda: calls.append(1) or raw_stations())
            gate = type("G", (), {"wait": lambda self: waits.append(1)})()
            N.ensure_stations(st, gate)
            N.ensure_stations(st, gate)
            self.assertEqual((len(calls), len(waits)), (1, 1))
            self.assertEqual(st.get("9001")["lines"][:2], ["1", "1GL"])
            self.assertTrue(os.path.exists(os.path.join(d, "s.json")))
        finally:
            shutil.rmtree(d)


def bus(line, actual, ids, tt, seconds=300, garage="1001"):
    return {"line": line, "actual": actual, "direction": "x", "seconds": seconds, "stops_between": 2,
            "garage_no": garage, "lat": None, "lng": None, "current_stop": None, "dep": "07:00:00",
            "next_station": 1, "pos": ids.index(ids[-1]) + 1, "first": ids[0], "planned": False,
            "_ids": ids, "_slr": [0] * len(ids), "_tt": tt}


class NewStopFlowTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.clock = [datetime(2026, 10, 9, 7, 0, tzinfo=TZ).timestamp()]
        self.live = {}
        c = N.load_config(os.path.join(os.path.dirname(HERE), "config.toml"))
        gate = N.Gate(0, 5, 180, clock=lambda: self.clock[0], sleep=lambda d: None)
        self.svc = N.Service(c, N.Memory(self.env.tt, naming=self.env.naming),
                             fetch_fn=lambda u: {"name": u, "buses": list(self.live.get(u, []))}, gate=gate,
                             clock=lambda: self.clock[0], sleep=lambda d: None, use_feeders=False,
                             resolver=self.env.resolver, stations=self.env.stations, netmap=self.env.map)

    def tearDown(self):
        self.env.close()

    def test_new_stop_warming_then_full_list(self):
        svc, e = self.svc, self.env
        self.live["9001"] = [bus("1GL", "11", ["7000", "9001"], [0, 7], seconds=240)]
        self.assertEqual(svc.admit("9001"), "new")
        self.assertTrue(svc.warming("9001"))
        self.assertIn("9001", svc.tt_pending)
        self.assertEqual(e.tt.missing(["9001"]), {"rvg": {"1", "3A", "6", "14"}})
        self.assertTrue(svc.timetable_step())                    # fetch the 4 routes, 4 day types
        req = [c for c in e.gspns.calls if c[0] == "/red-voznje/ispis-polazaka"]
        self.assertEqual(len(req), 4)
        self.assertEqual(sorted(req[0][1]["linija[]"]), ["1*", "14", "3A.", "6"])
        self.assertEqual(e.tt.missing(["9001"]), {})
        self.assertTrue(svc.warming("9001"))                     # nsmart not polled yet
        svc._serve_urgent({})
        self.assertFalse(svc.warming("9001"))
        # learned from the live bus's all_stations: 1GL reaches 9001 7 min after its terminal departure
        self.assertEqual(svc.mem.travel_seconds("9001", "1GL", None), 420)
        v = svc.mem.view("9001", now=self.clock[0])
        lines = {b["line"] for b in v["buses"]}
        self.assertTrue(lines <= {"1", "3A", "6", "14"}, lines)    # variants shown as their base route
        self.assertIn("1GL", {b["nsmart_line"] for b in v["buses"]})
        rows = N.frame_rows(v["buses"])
        self.assertEqual(len([r for r in rows if r.startswith("R|1|")]), 1)   # 1 and 1GL share one row
        self.assertTrue(any(r.startswith("R|1|G:") for r in rows))

    def test_gspns_failure_retries_later_without_warming_forever(self):
        svc, e = self.svc, self.env
        e.gspns.fail = True
        svc.admit("9001")
        svc.timetable_step()
        self.assertNotIn("9001", svc.tt_pending)
        self.assertIn("9001", svc.tt_later)
        svc._serve_urgent({})
        self.assertFalse(svc.warming("9001"))

    def test_unknown_live_route_learns_direction_and_fetches_timetable(self):
        svc, e = self.svc, self.env
        svc.watch.add("9004")                                    # far away: no map stop, no derived lines
        e.tt.ensure(["9004"])
        self.assertEqual(e.resolver("9004"), [])
        self.live["9004"] = [bus("54ŽA", "543B", ["6000", "9004"], [0, 12])]
        svc.request("9004")
        self.assertEqual(e.resolver.learned["9004"]["54"]["letter"], "B")
        self.assertIn("9004", svc.tt_pending)
        svc.timetable_step()
        got = e.trips("9004")
        self.assertEqual(got, [("54", "07:06", None), ("54", "07:16", '"A"'), ("54", "07:26", "ZA")])
        # a second sighting of the same route does not trigger another fetch
        n = len(e.gspns.calls)
        svc.request("9004")
        svc.timetable_step()
        self.assertEqual(len(e.gspns.calls), n)

    def test_http_counts_and_first_request_log(self):
        svc = self.svc
        with self.assertLogs("nstupido", "INFO") as lg:
            N.LOG.propagate = True
            try:
                for _ in range(3):
                    svc.record_http("192.168.1.9", "/frame?stop=6539", 200)
                svc.record_http("192.168.1.9", "/buses?stop=x", 400)
            finally:
                N.LOG.propagate = False
        self.assertEqual(len([m for m in lg.output if "first request" in m]), 1)
        self.assertEqual(svc.status()["http"], {"/frame 200": 3, "/buses 400": 1})
        self.assertEqual(svc.http_line(), "http: /frame 200 x3, /buses 400 x1")
        self.assertIsNone(svc.http_line())

    def test_manual_entries_win_for_default_stops(self):
        e = self.env
        e.resolver.manual = {"9001": [{"base": "14", "rv": "rvg", "dir": 1, "names": {None: "14"}, "tt": {"14": 5}}]}
        e.resolver.invalidate()
        self.assertEqual([c["base"] for c in e.resolver("9001")], ["14"])
        self.assertEqual(len(e.resolver("9001", use_manual=False)), 4)


class ManualLetterGuardTest(unittest.TestCase):
    def test_sunday_table_without_direction_a(self):
        d = tempfile.mkdtemp()
        try:
            tt = T.Timetables(cache_dir=d, fetch=lambda *a: (_ for _ in ()).throw(OSError()),
                              resolver=lambda s: [{"base": "77", "rv": "rvp", "dir": 0, "letter": "A",
                                                   "names": {None: "77"}, "all_marks": True, "tt": {}}])
            tt.data["2026-10-01"] = {"lines": {"rvp": {"N": {"77": {"title": "77", "legend": None, "directions": [
                {"header": "Смер B: Polasci iz STARI RAKOVAC", "departures": [{"t": "07:00", "mark": None}]}]}}}}}
            tt.index["day_types"]["2026-10-11"] = "N"
            self.assertEqual(tt.trips("15889", datetime(2026, 10, 11).date()), [])
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
