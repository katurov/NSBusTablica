"""Offline tests for the v4 server: queue math, groups, capacity/429, watch TTL, backoff, log aggregation,
HTTP codes, timetable refresh schedule. No network: upstream fetches are fakes, time is a fake clock."""
import json, os, sys, threading, time, tempfile, shutil, unittest, urllib.request, urllib.error
from datetime import datetime, timedelta, date
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import nstupido as N           # noqa: E402
import timetable as T          # noqa: E402

TZ = N.local_tz("Europe/Belgrade")


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def time(self):
        return self.t

    def sleep(self, d):
        self.t += max(0.0, d)


def cfg(**polling):
    c = N.load_config(os.path.join(os.path.dirname(HERE), "config.toml"))
    c["polling"].update(polling)
    return c


class FakeUpstream:
    """fetch(uid) -> parsed-like result; records (time, uid)."""

    def __init__(self, clock, missing=(), failing=None):
        self.clock, self.missing, self.calls = clock, set(missing), []
        self.failing = failing or (lambda uid, n: False)

    def __call__(self, uid):
        self.calls.append((self.clock.time(), uid))
        if uid in self.missing:
            return {"error": {"type": "api", "code": None, "message": "unknown stop uid"}}
        if self.failing(uid, len(self.calls)):
            return {"error": {"type": "network", "message": "timed out"}}
        return {"name": "stop %s" % uid, "buses": []}


def make_service(feeders=None, defaults=("6539", "15889", "6712"), missing=(), failing=None, **polling):
    clock = FakeClock()
    c = cfg(**polling)
    c["stops"]["default"] = list(defaults)
    gate = N.Gate(c["polling"]["min_interval_s"], c["polling"]["backoff_min_s"], c["polling"]["backoff_max_s"],
                  clock=clock.time, sleep=clock.sleep)
    up = FakeUpstream(clock, missing, failing)
    svc = N.Service(c, N.Memory(None), fetch_fn=up, gate=gate, clock=clock.time, sleep=clock.sleep)
    feeders = feeders or {}
    svc.feeders_of = lambda uid: list(feeders.get(uid, []))
    return svc, clock, up


class QueueMathTest(unittest.TestCase):
    def test_spacing_and_capacity(self):
        self.assertEqual(N.capacity(40, 0.4), 100)
        self.assertEqual(N.spacing(8), 5.0)            # small queue polled gently
        self.assertEqual(N.spacing(100), 0.4)
        self.assertEqual(N.spacing(250), 0.4)          # never faster than min_interval
        self.assertEqual(N.spacing(0), 40)

    def test_groups_and_shared_feeders(self):
        fe = {"A": ["f1", "f2"], "B": ["f2", "f3"], "C": ["A"]}
        groups = N.plan_groups(["A", "B", "C"], lambda u: fe.get(u, []))
        self.assertEqual(groups, [("A", ["f1", "f2"]), ("B", ["f2", "f3"]), ("C", ["A"])])
        self.assertEqual(N.plan_sequence(groups), ["A", "f1", "f2", "B", "f3", "C"])   # f2 and A once

    def test_cycle_order_and_spacing(self):
        svc, clock, up = make_service({"6539": ["6540", "6750"], "6712": ["6586"]})
        t0 = clock.time()
        svc.run_cycle()
        uids = [u for _, u in up.calls]
        self.assertEqual(uids, ["6539", "6540", "6750", "15889", "6712", "6586"])   # stop, then its feeders
        gaps = [b[0] - a[0] for a, b in zip(up.calls, up.calls[1:])]
        for g in gaps:
            self.assertAlmostEqual(g, 40 / 6, places=6)
        self.assertGreaterEqual(clock.time() - t0, 40 - 1e-6)                       # one cycle = 40 s
        svc.run_cycle()
        self.assertAlmostEqual(up.calls[6][0] - up.calls[5][0], 40 / 6, places=6)  # even across cycles

    def test_full_queue_never_faster_than_min_interval(self):
        feeders = {str(s): [str(s + 1000), str(s + 2000), str(s + 3000)] for s in range(30)}
        svc, clock, up = make_service(feeders, defaults=[str(s) for s in range(30)])
        svc.run_cycle()
        self.assertEqual(len(up.calls), 120)
        gaps = [b[0] - a[0] for a, b in zip(up.calls, up.calls[1:])]
        self.assertGreaterEqual(min(gaps), 0.4 - 1e-9)


class AdmissionTest(unittest.TestCase):
    def test_new_stop_is_urgent_and_warming(self):
        svc, clock, up = make_service()
        self.assertEqual(svc.admit("6551"), "new")
        self.assertTrue(svc.warming("6551"))
        self.assertEqual(svc.answer("6551")["warming"], True)
        svc.run_cycle()
        self.assertEqual(up.calls[0][1], "6551")          # served before the regular queue
        self.assertFalse(svc.warming("6551"))
        self.assertEqual(svc.admit("6551"), "ok")

    def test_capacity_429_and_watched_always_served(self):
        svc, clock, up = make_service(defaults=["1"])
        # unknown stops cost 1 + 3 assumed feeders; 1 (default) + 24 * 4 = 97 <= 100, the 25th would be 101
        accepted = [svc.admit(str(100 + i)) for i in range(24)]
        self.assertEqual(set(accepted), {"new"})
        self.assertEqual(svc.admit("999"), "full")
        self.assertEqual(svc.admit("100"), "ok")          # already watched: always served
        self.assertEqual(svc.admit("1"), "ok")

    def test_known_feeders_shared_cost(self):
        svc, clock, up = make_service({"6539": ["6540"], "7000": ["6540"]}, defaults=["6539"])
        self.assertEqual(svc.group_cost("7000"), 1)       # 6540 already in the queue

    def test_missing_stop_404(self):
        svc, clock, up = make_service(missing={"4242"})
        self.assertEqual(svc.admit("4242"), "new")
        svc.run_cycle()
        self.assertEqual(svc.admit("4242"), "missing")
        self.assertNotIn("4242", svc.watch.stops())

    def test_ttl_expiry(self):
        svc, clock, up = make_service({"7000": ["7001"], "7001": []})
        svc.admit("7000")
        clock.sleep(3 * 3600)
        svc.admit("7000")                                 # request extends the timer to now + 4 h
        clock.sleep(3.9 * 3600)
        self.assertEqual(svc.watch.expire(), [])
        clock.sleep(0.2 * 3600)
        self.assertEqual(svc.watch.expire(), ["7000"])
        self.assertEqual(svc.watch.stops(), ["6539", "15889", "6712"])   # defaults never expire
        self.assertEqual(svc.queue_length(), 3)                          # its feeder left too


class BackoffTest(unittest.TestCase):
    def test_exponential_backoff_and_reset(self):
        fails = {"n": 7}
        svc, clock, up = make_service(defaults=["1"], failing=lambda uid, n: n <= fails["n"])
        g = svc.gate
        waits = []
        for _ in range(8):
            before = clock.time()
            svc.request("1")
            waits.append(round(clock.time() - before, 3))
        # first request immediate, then 5, 10, 20, 40, 80, 160, 180 (cap)
        self.assertEqual(waits, [0, 5, 10, 20, 40, 80, 160, 180])
        self.assertFalse(g.in_backoff())                  # 8th succeeded -> reset
        before = clock.time()
        svc.request("1")
        self.assertAlmostEqual(clock.time() - before, 0.4)

    def test_backoff_answers_schedule_only(self):
        svc, clock, up = make_service(defaults=["1"], failing=lambda uid, n: True)
        svc.request("1")
        a = svc.answer("1")
        self.assertFalse(a["live"])
        self.assertEqual(svc.errors.counts["timeout"], 1)


class LoggingTest(unittest.TestCase):
    def test_error_aggregation(self):
        clock = FakeClock()
        el = N.ErrorLog(1800, clock.time)
        for _ in range(30):
            el.record("timeout")
        for _ in range(7):
            el.record("http 503")
        self.assertIsNone(el.flush(180))                  # window not over yet
        clock.sleep(1800)
        self.assertEqual(el.flush(180), "nsmart errors: 37 in last 30 min (timeout 30, http 503 7), backoff 180 s")
        clock.sleep(1800)
        self.assertIsNone(el.flush(0))                    # no errors -> no line

    def test_unique_clients_window(self):
        clock = FakeClock()
        c = N.Clients(12 * 3600, clock.time)
        c.hit("10.0.0.1"); c.hit("10.0.0.2"); c.hit("10.0.0.1")
        self.assertEqual(c.count(), 2)
        clock.sleep(11 * 3600); c.hit("10.0.0.3")
        clock.sleep(2 * 3600)
        self.assertEqual(c.count(), 1)

    def test_stats_line(self):
        svc, clock, up = make_service()
        svc.clients.hit("1.2.3.4")
        self.assertIn("3 stops watched (3 default, 0 dynamic), queue 3 requests/cycle", svc.stats_line())
        self.assertIn("1 clients in last 12 h", svc.stats_line())

    def test_error_kind(self):
        self.assertEqual(N.error_kind({"type": "http", "status": 503}), "http 503")
        self.assertEqual(N.error_kind({"type": "network", "message": "timed out"}), "timeout")
        self.assertIsNone(N.error_kind({"type": "api", "code": 3, "message": "bad id"}))


class TimetableScheduleTest(unittest.TestCase):
    def dt(self, s):
        return datetime.fromisoformat(s).replace(tzinfo=TZ)

    def test_marks(self):
        self.assertEqual(N.last_mark(self.dt("2026-10-11 04:59")), self.dt("2026-10-10 05:00"))
        self.assertEqual(N.last_mark(self.dt("2026-10-11 05:00")), self.dt("2026-10-11 05:00"))
        self.assertEqual(N.next_mark(self.dt("2026-10-11 05:00")), self.dt("2026-10-12 05:00"))
        self.assertEqual(N.next_mark(self.dt("2026-10-11 23:00")), self.dt("2026-10-12 05:00"))

    def test_due(self):
        now = self.dt("2026-10-11 12:00")
        self.assertTrue(N.timetable_due(None, now))
        self.assertTrue(N.timetable_due(self.dt("2026-10-11 04:30").timestamp(), now))
        self.assertFalse(N.timetable_due(self.dt("2026-10-11 05:01").timestamp(), now))
        self.assertFalse(N.timetable_due(self.dt("2026-10-10 06:00").timestamp(), self.dt("2026-10-11 04:00")))

    def test_refresher_retries_then_waits_for_0500(self):
        class FakeTT:
            def __init__(self):
                self.ok, self.calls, self.at, self.last_result, self.last_error = False, 0, None, None, "x"
            def refreshed_at(self):
                return self.at
            def refresh(self, today=None, force=False, stops=None):
                self.calls += 1
                self.last_result = {"fetched": 0, "failed": 1}
                if self.ok:
                    self.at = now[0].timestamp()
                return self.ok
        now = [self.dt("2026-10-11 12:00")]
        svc, clock, up = make_service()
        tt = FakeTT()
        r = N.TimetableRefresher(svc, tt, cfg(), now_dt=lambda: now[0])
        self.assertEqual([r.step() for _ in range(3)], [900, 900, 900])     # 3 retries after 15 min
        self.assertTrue(svc.ready.is_set())                                  # gspns failure doesn't block
        self.assertEqual(r.step(), 17 * 3600)                                # then wait for tomorrow 05:00
        self.assertEqual(tt.calls, 4)
        tt.ok = True
        now[0] = self.dt("2026-10-12 05:00")
        self.assertEqual(r.step(), 24 * 3600)
        self.assertEqual(r.step(), 24 * 3600)                                # not due: no extra fetch
        self.assertEqual(tt.calls, 5)

    def test_gspns_pacing_and_partial_failure_keeps_old_copy(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        fx = os.path.join(HERE, "fixtures")
        pauses, reqs = [], []
        idx_html = '<p>danas voze po redu vožnje za RADNI DAN</p><option value="2026-10-01">'
        with open(os.path.join(fx, "ispis_rvg_R_14.html"), encoding="utf-8") as f:
            table = f.read()

        def fetch(path, params=None):
            reqs.append((path, dict(params or {})))
            if path.endswith("gradski"):
                return idx_html
            if path.endswith("lista-linija"):
                if params["dan"] == "S":
                    raise OSError("timeout")
                return '<option value="14">'
            return table
        tt = T.Timetables(cache_dir=tmp, fetch=fetch, pause=lambda: pauses.append(1))
        tt.data["2026-10-01"] = {"vaziod": "2026-10-01", "fetched": 0,
                                 "lines": {"rvg": {"S": {"14": {"title": "old", "directions": [], "legend": None}}}}}
        ok = tt.refresh(today=date(2026, 10, 12), force=True, stops={"6712"})
        self.assertFalse(ok)                                       # one (rv, day) failed
        self.assertEqual(len(pauses), len(reqs) - 1)               # a pause between every two requests
        self.assertEqual(tt.data["2026-10-01"]["lines"]["rvg"]["S"]["14"]["title"], "old")   # yesterday's copy kept
        self.assertIn("14", tt.data["2026-10-01"]["lines"]["rvg"]["R"])
        self.assertEqual(tt.last_result["failed"], 1)
        self.assertTrue(all(p[1].get("rv") in (None, "rvg") for p in reqs))   # only lines of the watched stop
        self.assertIsNone(tt.index.get("refreshed_at"))


class FrameTest(unittest.TestCase):
    def test_frame_text(self):
        view = {"stop": {"uid": "6539"}, "warming": True, "live": False, "buses": [
            {"line": "8", "seconds": 200, "status": "live"}, {"line": '54"A"', "seconds": 60, "status": "scheduled"},
            {"line": "8", "seconds": 1300, "status": "scheduled"}, {"line": "54ŽA", "seconds": 400, "status": "lost"}]}
        txt = N.frame_text(view, datetime(2026, 10, 11, 12, 34))
        self.assertEqual(txt, "H|6539|12:34|WO\nR|54A|P:1\nR|8|G:3|P:21\nR|54ZA|E:6\nE\n")


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock, self.up = make_service(missing={"4242"})
        handler = type("H", (N.Handler,), {"service": self.svc})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, dict(r.headers), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode()

    def test_codes(self):
        self.assertEqual(self.get("/buses?stop=6539")[0], 503)           # timetable not loaded yet
        code, h, body = self.get("/buses?stop=6539")
        self.assertEqual(h.get("Retry-After"), "10")
        self.svc.ready.set()
        self.assertEqual(self.get("/buses?stop=65a9")[0], 400)
        self.assertEqual(self.get("/buses?stop=12345678")[0], 400)
        code, h, body = self.get("/buses?stop=6551")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["warming"])
        code, h, body = self.get("/buses?stop=4242")                     # accepted first (warming) ...
        self.assertEqual(code, 200)
        self.svc._serve_urgent({})                                       # ... nsmart: no such stop
        self.assertEqual(self.get("/buses?stop=4242")[0], 404)
        self.assertEqual(self.get("/frame?stop=4242")[2], "ERR|404|stop 4242 does not exist in nsmart\n")
        self.svc.cap = self.svc.queue_length() + 2                       # almost full
        code, h, body = self.get("/buses?stop=7777")
        self.assertEqual(code, 429)
        self.assertEqual(h.get("Retry-After"), "600")
        self.assertEqual(json.loads(body), {"error": "stop limit reached", "limit": self.svc.cap})
        self.assertEqual(self.get("/buses?stop=6551")[0], 200)           # watched stops always served
        self.assertEqual(self.get("/nope")[0], 404)

    def test_root_and_frame(self):
        self.svc.ready.set()
        code, h, body = self.get("/")
        st = json.loads(body)
        self.assertEqual(st["default_stops"], ["6539", "15889", "6712"])
        self.assertEqual(st["queue"]["limit"], 100)
        self.assertIn("backoff", st)
        code, h, body = self.get("/frame?stop=6539")
        self.assertEqual(code, 200)
        self.assertTrue(h["Content-Type"].startswith("text/plain"))
        self.assertRegex(body, r"^H\|6539\|\d\d:\d\d\|W\nE\n$")


class ConfigTest(unittest.TestCase):
    def test_mini_toml_matches_defaults(self):
        with open(os.path.join(os.path.dirname(HERE), "config.toml"), encoding="utf-8") as f:
            txt = f.read()
        c = N._mini_toml(txt)
        self.assertEqual(c["stops"]["default"], ["6539", "15889", "6712"])
        self.assertEqual(c["polling"]["min_interval_s"], 0.4)
        self.assertEqual(c["timetable"]["pause_s"], [1, 2])
        self.assertEqual(N.load_config("/nonexistent.toml")["server"]["port"], 8080)


if __name__ == "__main__":
    unittest.main()
