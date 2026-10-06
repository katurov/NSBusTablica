#!/usr/bin/env python3
"""NStupido - upcoming buses for Novi Sad (JGSP / nsmart.rs) stops as compact JSON:
live data + memory + official timetable (gspns.rs).

CLI:     python3 nstupido.py [STOP_UID ...] [--merge] [--pretty] [--state FILE | --no-state]
                             [--no-schedule | --schedule-only] [--no-feeders]
         (default stops: 6539 15889 6712)
Server:  python3 nstupido.py --serve [--port 8080] [--host 0.0.0.0] [--interval 15]
         GET /buses                 -> default stops
         GET /buses?stop=6551       -> that stop (comma-separated list allowed: ?stop=6539,6551)
         GET /buses?merge=1         -> all buses of the requested stops in one list sorted by time
         GET /debug                 -> timetable status, feeders, learned travel times, recent arrivals

Every board entry has a status:
  "live"      (Sveže)          - in a fresh upstream snapshot (of the stop itself or of a preceding stop)
  "lost"      (Videli-izgubili) - seen earlier, now estimated from memory (countdown)
  "scheduled" (Po rasporedu)    - from the official timetable: departure + travel time to the stop, not seen yet
A scheduled trip is matched to a live bus by line + trip departure time (+-1 min), else the nearest one of the
same line within +-6 min, and is never shown twice.
Stdlib only. Data: https://online.nsmart.rs/sr/najava-dolaska/ (undocumented, no auth), http://gspns.rs/red-voznje
"""
import json, os, sys, time, argparse, threading, signal, statistics
from concurrent.futures import ThreadPoolExecutor
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import timetable                                   # noqa: E402  (vendored, same directory)

API_URL = "https://online.nsmart.rs/sr/najava-dolaska/"
DEFAULT_STOPS = [
    "6539",    # 0220B   Bulevar kralja Petra prvog-Mašinska škola
    "15889",   # 0509-1A Bulevar Oslobodjenja  Bulevar Kralja Petra prvog prigrad (suburban lines)
    "6712",    # 0509A   Bulevar Oslobođenja - Bulevar Kralja Petra prvog (city lines)
]
# preceding stops whose arrivals responses also show buses heading to our stop (seed; more are learned from
# all_stations order). 6539: Sajam 6540 (lines 3, 8), 6750 (8), 6679 (3). 6712: Železnička stanica 6586, 6728.
# 15889: its predecessors are the suburban terminals, which only list planned departures -> no feeders.
SEED_FEEDERS = {"6539": ["6540", "6750", "6679"], "6712": ["6586", "6728"], "15889": []}
FEEDERS_PER_STOP = 4
STATIONS_URL = "https://online.nsmart.rs/sr/AnnouncementForStation/getAllStations"
STATIONS_CACHE = os.path.join(HERE, "stations_cache.json")
STATE_FILE = os.path.join(HERE, "state.json")
STATIONS_MAX_AGE = 7 * 24 * 3600
TRUNCATED_LEN = 50             # upstream cuts station_name in arrival responses to 50 chars
KNOWN_NAMES = {                # full names (from getAllStations) for the default stops
    "6539": "Bulevar kralja Petra prvog-Mašinska škola",
    "15889": "Bulevar Oslobodjenja  Bulevar Kralja Petra prvog prigrad",
    "6712": "Bulevar Oslobođenja - Bulevar Kralja Petra prvog",
}
TIMEOUT = 15

# --- memory tuning ---
POLL_INTERVAL = 15             # server background poll, seconds (upstream snapshot cadence ~15-20 s)
FEEDER_EVERY = 2               # poll preceding stops every N-th round
ZERO_HOLD = 30                 # drop an estimated bus that has been at 0 s for this long
HOLD_EXTRA = 120               # keep an unseen bus at most last_seconds_left + HOLD_EXTRA ...
HOLD_MAX = 20 * 60             # ... but never longer than this
WATCH_TTL = 10 * 60            # server: stop polling a non-default stop not requested for this long
VEHICLE_TTL = 30 * 60          # forget vehicle positions older than this
LIVE_MAX_AGE = 40              # "live" = seen (directly or via a preceding stop) within this many seconds

# --- schedule tuning ---
SCHED_AHEAD = 40 * 60          # show scheduled trips expected within the next 40 min
SCHED_GRACE = 180              # drop an unseen scheduled trip 3 min after it was due
MATCH_EXACT = 60               # live bus <-> scheduled trip: same departure +-1 min ...
MATCH_NEAR = 6 * 60            # ... else nearest expected arrival within +-6 min
MATCH_PLAUSIBLE = 15 * 60      # departure-label match only if the bus ETA is within 15 min of that trip's expected time
DELAY_MIN_SAMPLES = 3          # use learned travel times once we have this many arrivals
DELAY_CLIP = 300               # clamp learned correction vs scheduled tt to +-5 min
SAMPLES_KEEP = 20
ARRIVAL_SECONDS = 20           # a live bus at <= 20 s and 0 stops between counts as arriving (learning sample)
NEAR_SECONDS = 150             # ... or, when it leaves memory, its closest live ETA was <= 150 s (arrival = obs + ETA)
# measured 6 Oct 2026 (departure -> arrival, s), seed for travel time learning
SEED_TRAVEL = {"6539|8": [1254, 942, 1302, 954, 1153], "6539|3": [498, 648, 684, 524], "6712|4": [162, 174, 403, 72],
               "6712|7A": [852, 1194, 1044, 1123], "6712|14": [1584, 1366], "6712|14S": [1188, 1074],
               "6712|10MAL": [1236, 774], "15889|55": [151, 168], "15889|71": [203], "15889|64": [51],
               "15889|74": [88], "15889|62": [146], "15889|52": [214], "15889|76": [177], "15889|54": [273],
               "15889|72": [114], "15889|86": [210]}

LABELS = {"live": "Sveže", "lost": "Videli-izgubili", "scheduled": "Po rasporedu"}


def _iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).astimezone().isoformat(timespec="seconds")

def _hm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

def dep_datetime(dep, now):
    """Trip departure 'HH:MM:SS' (no date) -> the datetime nearest to now, within [now-20h, now+4h]."""
    try:
        hh, mm, ss = (int(x) for x in (dep.split(":") + ["0", "0"])[:3])
    except (AttributeError, ValueError):
        return None
    base = datetime.fromtimestamp(now).replace(hour=hh, minute=mm, second=ss, microsecond=0)
    cands = [base + timedelta(days=k) for k in (-1, 0, 1)]
    cands = [c for c in cands if -20 * 3600 <= c.timestamp() - now <= 4 * 3600] or cands
    return min(cands, key=lambda c: abs(c.timestamp() - now))


# ---------------- upstream ----------------
def fetch_raw(stop_uid):
    data = urllib.parse.urlencode({"station_uid": stop_uid, "ibfm": "TS001831", "direction": 2,
                                   "company_info_id": 216, "radius": ""}).encode()
    req = urllib.request.Request(API_URL, data=data, headers={
        "User-Agent": "Mozilla/5.0 (NStupido home board)",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))

_names, _names_lock = None, threading.Lock()

def full_name(uid, name):
    """Upstream truncates station_name to 50 chars; restore the full name from the stop list."""
    global _names
    if name is None or len(name) < TRUNCATED_LEN:
        return name
    if uid in KNOWN_NAMES:
        return KNOWN_NAMES[uid]
    with _names_lock:
        if _names is None:
            _names = {}
            try:
                if os.path.exists(STATIONS_CACHE) and time.time() - os.path.getmtime(STATIONS_CACHE) < STATIONS_MAX_AGE:
                    with open(STATIONS_CACHE, encoding="utf-8") as f:
                        _names = json.load(f)
                else:
                    req = urllib.request.Request(STATIONS_URL, data=b"", headers={
                        "User-Agent": "Mozilla/5.0 (NStupido home board)", "X-Requested-With": "XMLHttpRequest"})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        _names = {str(x["id"]): x["name"] for x in json.loads(r.read().decode("utf-8"))}
                    try:
                        with open(STATIONS_CACHE, "w", encoding="utf-8") as f:
                            json.dump(_names, f, ensure_ascii=False)
                    except OSError:
                        pass
            except Exception:
                _names = {}
    full = _names.get(uid)
    return full if full and full.startswith(name) else name

def fetch(stop_uid):
    """Fetch one stop: parsed result ({"name","buses"} or {"error"}), buses carry their route ("_ids","_slr","_tt")."""
    stop_uid = str(stop_uid).strip()
    if not stop_uid.isdigit():
        return {"error": {"type": "bad_request", "message": "stop uid must be numeric"}}
    try:
        d = fetch_raw(stop_uid)
    except urllib.error.HTTPError as e:
        return {"error": {"type": "http", "status": e.code, "message": str(e.reason)}}
    except urllib.error.URLError as e:
        return {"error": {"type": "network", "message": str(e.reason)}}
    except (TimeoutError, OSError) as e:
        return {"error": {"type": "network", "message": str(e)}}
    except (ValueError, UnicodeDecodeError) as e:
        return {"error": {"type": "bad_response", "message": "upstream did not return JSON: %s" % e}}
    return parse(stop_uid, d)

def snapshot(stop_uid):
    """Backwards compatible: one stop, display fields only."""
    r = fetch(stop_uid)
    for b in r.get("buses", []):
        for k in ("_ids", "_slr", "_tt", "planned"):
            b.pop(k, None)
    return r

def parse(stop_uid, d):
    """Turn one raw upstream response into {"name", "buses"} or {"error"}."""
    if not isinstance(d, list) or not d:
        return {"error": {"type": "bad_response", "message": "unexpected response shape"}}
    if d[0] is False:   # site convention: [false, code]; 1=no agency, 2=no api key, 3=bad id, 4=bad url
        code = d[1] if len(d) > 1 else None
        msg = {1: "no agency", 2: "no api key", 3: "bad id", 4: "bad url"}.get(code, "api error")
        return {"error": {"type": "api", "code": code, "message": msg}}
    if not isinstance(d[0], dict):
        return {"error": {"type": "bad_response", "message": "unexpected response shape"}}
    if d[0].get("station_name") is None and d[0].get("gpsx") is None:
        return {"error": {"type": "api", "code": None, "message": "unknown stop uid"}}
    out = {"name": full_name(stop_uid, d[0].get("station_name")), "buses": []}
    if d[0].get("just_coordinates") == "1":
        return out
    for e in d:
        if not isinstance(e, dict) or "seconds_left" not in e:
            continue
        veh = (e.get("vehicles") or [{}])[0] or {}
        st = [s for s in (e.get("all_stations") or []) if isinstance(s, dict)]
        ids = [str(s.get("id")) for s in st]
        out["buses"].append({
            "line": e.get("line_number"),
            "direction": e.get("to_price"),
            "seconds": _int(e.get("seconds_left")) or 0,
            "stops_between": _int(e.get("stations_between")),
            "garage_no": str(e.get("garage_no")),
            "lat": _num(veh.get("lat")),
            "lng": _num(veh.get("lng")),
            "current_stop": veh.get("station_name"),
            # internal
            "dep": e.get("entered_departure_time"),           # scheduled trip departure from the terminal
            "next_station": _int(e.get("next_station")),      # 1-based index of bus's next stop on route
            "pos": ids.index(stop_uid) + 1 if stop_uid in ids else None,   # our stop's index on route
            "first": ids[0] if ids else None,
            # terminal stops also list planned departures with garage "P1", "P2"... (not real vehicles;
            # their departure time is off by +1 h) - kept out of memory, the gspns timetable covers them
            "planned": not str(e.get("garage_no")).isdigit(),
            "_ids": ids,
            "_slr": [_int(s.get("second_left_by_route")) or 0 for s in st],
            "_tt": [_int(s.get("tt_time")) for s in st],
        })
    return out

def project(bus, feeder, target):
    """A bus listed for stop `feeder` -> observation for a later stop `target` on its route, or None."""
    ids = bus.get("_ids") or []
    if feeder not in ids or target not in ids:
        return None
    i, j = ids.index(feeder), ids.index(target)
    if j <= i:
        return None
    ns = bus.get("next_station")
    if ns is not None and ns > j + 1:          # already past the target
        return None
    slr, tt = bus["_slr"], bus["_tt"]
    if slr[j] > 0 and slr[j] >= slr[i]:
        sec = bus["seconds"] + (slr[j] - slr[i])          # feeder ETA + route time between the two stops
    elif tt[i] is not None and tt[j] is not None:
        sec = bus["seconds"] + (tt[j] - tt[i]) * 60
    else:
        return None
    ob = {k: v for k, v in bus.items() if not k.startswith("_")}
    ob.update(seconds=max(0, int(sec)), pos=j + 1,
              stops_between=max(0, (j + 1) - ns) if ns is not None else None, source=feeder)
    return ob


# ---------------- memory ----------------
class Memory:
    """Remembers buses per stop, estimates their countdown between snapshots, learns routes / travel times and
    merges the official timetable into the board. Thread-safe."""

    def __init__(self, tt=None):
        self.lock = threading.RLock()
        self.tt = tt                 # timetable.Timetables or None
        self.stops = {}              # uid -> {"name", "fetched_at", "error", "buses": {key: entry}}
        self.vehicles = {}           # garage_no -> {"dep", "next_station", "line", "lat", "lng", "current_stop", "t"}
        self.learn = {"tt": {}, "travel": {k: list(v) for k, v in SEED_TRAVEL.items()}, "recorded": {},
                      "routes": {}, "names": {}, "to": {}}
        self.matched = {}            # trip_id -> {"key", "stop", "t"}  (scheduled trip already shown as a real bus)
        self.arrivals = []           # recent arrival observations (debug / accuracy)

    # -- persistence --
    def load(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            return
        with self.lock:
            self.stops = st.get("stops", {})
            self.vehicles = st.get("vehicles", {})
            for k, v in (st.get("learn") or {}).items():
                if isinstance(v, dict):
                    self.learn.setdefault(k, {}).update(v)
            self.matched = st.get("matched", {})
            self.arrivals = st.get("arrivals", [])[-200:]

    def save(self, path):
        with self.lock:
            data = json.dumps({"saved_at": time.time(), "stops": self.stops, "vehicles": self.vehicles,
                               "learn": self.learn, "matched": self.matched, "arrivals": self.arrivals[-200:]},
                              ensure_ascii=False, separators=(",", ":"))
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError:
            pass

    # -- learning --
    def observe_routes(self, buses, now):
        """Learn route order, scheduled tt to our stops, destinations from any bus of any response."""
        with self.lock:
            for b in buses:
                ids = b.get("_ids") or []
                if not ids or not b.get("line"):
                    continue
                self.learn["routes"]["%s|%s" % (b["line"], ids[0])] = {"ids": ids, "t": now}
                if b.get("direction"):
                    self.learn["to"][b["line"]] = b["direction"]
                for uid in self.stops.keys() | set(DEFAULT_STOPS):
                    if uid in ids and b["_tt"][ids.index(uid)] is not None:
                        self.learn["tt"]["%s|%s|%s" % (uid, b["line"], ids[0])] = {"tt": b["_tt"][ids.index(uid)], "t": now}
            for k in [k for k, v in self.learn["routes"].items() if now - v["t"] > 30 * 86400]:
                del self.learn["routes"][k]

    def feeders(self, uid):
        score = {}
        for f in SEED_FEEDERS.get(uid, []):
            score[f] = score.get(f, 0) + 3
        with self.lock:
            routes = [r["ids"] for r in self.learn["routes"].values()]
        for ids in routes:
            if uid in ids:
                i = ids.index(uid)
                for back, w in ((1, 2), (2, 1)):
                    # a route's first stop is useless as a feeder: its response lists only planned departures
                    # ("P1", "P2") - real buses appear there only as they leave (verified 6 Oct: 0 extra buses)
                    if i - back >= 1:
                        f = ids[i - back]
                        score[f] = score.get(f, 0) + w
        ours = set(DEFAULT_STOPS) | {uid}
        best = sorted((f for f in score if f not in ours), key=lambda f: -score[f])
        return best[:FEEDERS_PER_STOP]

    def _record_arrival(self, uid, e, now, arr=None, quality="seen"):
        """Remember dep->arrival travel time (learning) and the arrival (accuracy). Called when a live bus is right
        at the stop (<= ARRIVAL_SECONDS, 0 stops between; quality "seen") or when a bus leaves memory and its
        closest live observation was <= NEAR_SECONDS away (quality "near": arrival = that observation + its ETA)."""
        if arr is None and (e["seconds"] > ARRIVAL_SECONDS or (e.get("stops_between") or 0) != 0):
            return
        if not e.get("dep"):
            return
        tk = "%s|%s|%s|%s" % (uid, e["line"], e["garage_no"], e["dep"])
        if tk in self.learn["recorded"]:
            return
        arr = now + e["seconds"] if arr is None else arr
        self.learn["recorded"][tk] = now
        d = dep_datetime(e["dep"], now)
        if d is not None:
            travel = arr - d.timestamp()
            exp = self.travel_seconds(uid, e["line"], None)
            # a stale departure label (bus already on its next trip, still labelled with the old departure) would
            # give a bogus sample: ignore samples more than 15 min off the scheduled travel time
            if 0 < travel < 3 * 3600 and (exp is None or abs(travel - exp) <= 900):
                s = self.learn["travel"].setdefault("%s|%s" % (uid, e["line"]), [])
                s.append(int(travel))
                del s[:-SAMPLES_KEEP]
        self.arrivals.append({"stop": uid, "line": e["line"], "garage_no": e["garage_no"], "dep": e["dep"],
                              "arrival": int(arr), "arrival_hm": datetime.fromtimestamp(arr).strftime("%H:%M:%S"),
                              "trip_id": e.get("trip_id"), "source": e.get("source"), "quality": quality})
        del self.arrivals[:-200]
        for k in [k for k, t in self.learn["recorded"].items() if now - t > 12 * 3600]:
            del self.learn["recorded"][k]

    def travel_seconds(self, uid, line, cfg_tt):
        """Expected seconds from scheduled departure to arrival at uid for an nsmart line name."""
        with self.lock:
            tts = [v["tt"] for k, v in self.learn["tt"].items() if k.startswith("%s|%s|" % (uid, line))]
            samples = list(self.learn["travel"].get("%s|%s" % (uid, line), []))
        tt = statistics.median(tts) if tts else cfg_tt
        if tt is None:
            if len(samples) >= DELAY_MIN_SAMPLES:
                return statistics.median(samples)
            return None
        corr = 0
        if len(samples) >= DELAY_MIN_SAMPLES:
            corr = max(-DELAY_CLIP, min(DELAY_CLIP, statistics.median(samples) - tt * 60))
        return tt * 60 + corr

    # -- updates --
    def update(self, uid, snap, now=None):
        now = now if now is not None else time.time()
        with self.lock:
            st = self.stops.setdefault(uid, {"name": None, "fetched_at": None, "error": None, "buses": {}})
            if "error" in snap:
                st["error"] = snap["error"]
                st["error_at"] = now
                if not snap.get("buses"):
                    return
            else:
                st["error"] = None
            st["name"] = snap.get("name") or st["name"]
            st["fetched_at"] = now
            seen = set()
            for b in snap.get("buses", []):
                key = "%s|%s" % (b["garage_no"], b["line"])
                if key in seen:
                    continue
                seen.add(key)
                old = st["buses"].get(key)
                same_value = (old is not None and old["dep"] == b["dep"] and old["raw_seconds"] == b["seconds"]
                              and old["lat"] == b["lat"] and old["lng"] == b["lng"])
                entry = {k: v for k, v in b.items() if not k.startswith("_")}
                entry.setdefault("source", "direct")
                entry["raw_seconds"] = b["seconds"]
                # upstream repeats the same snapshot for ~15-20 s: keep the time we FIRST saw this value,
                # so the countdown keeps running instead of resetting on every identical fetch
                entry["value_at"] = old["value_at"] if same_value else now
                entry["seen_at"] = now
                entry["first_seen"] = old.get("first_seen", now) if old and old["dep"] == b["dep"] else now
                if old and old["dep"] == b["dep"] and old.get("trip_id"):
                    entry["trip_id"] = old["trip_id"]
                entry["live"] = True
                best = old.get("best") if old and old["dep"] == b["dep"] else None
                entry["best"] = [now, b["seconds"]] if best is None or b["seconds"] <= best[1] else best
                st["buses"][key] = entry
                self._record_arrival(uid, entry, now)
                v = self.vehicles.get(b["garage_no"])
                if v is None or v["t"] <= now:
                    self.vehicles[b["garage_no"]] = {"dep": b["dep"], "next_station": b["next_station"],
                                                     "line": b["line"], "lat": b["lat"], "lng": b["lng"],
                                                     "current_stop": b["current_stop"], "t": now}
            for key, e in st["buses"].items():
                if key not in seen:
                    e["live"] = False
            self._prune(now)

    def _estimate(self, e, now):
        return max(0, int(round(e["raw_seconds"] - (now - e["value_at"]))))

    def _prune(self, now):
        for uid, st in self.stops.items():
            for key in list(st["buses"]):
                e = st["buses"][key]
                zero_at = e["value_at"] + e["raw_seconds"]     # moment the countdown hits 0
                v = self.vehicles.get(e["garage_no"])
                reason = None
                if v and v["t"] > e["seen_at"]:
                    if v["dep"] != e["dep"] and v["line"] == e["line"]:
                        reason = "new trip"
                    elif (v["dep"] == e["dep"] and e.get("pos") and v.get("next_station")
                          and v["next_station"] > e["pos"]):
                        reason = "passed"
                if not reason and now - e["seen_at"] > LIVE_MAX_AGE:
                    if now - zero_at >= ZERO_HOLD:
                        reason = "arrived"
                    elif now - e["seen_at"] > min(e["raw_seconds"] + HOLD_EXTRA, HOLD_MAX):
                        reason = "expired"
                if reason:
                    best = e.get("best")
                    if reason != "new trip" and best and best[1] <= NEAR_SECONDS:
                        self._record_arrival(uid, e, now, arr=best[0] + best[1], quality="near")
                    del st["buses"][key]
        for g in list(self.vehicles):
            if now - self.vehicles[g]["t"] > VEHICLE_TTL:
                del self.vehicles[g]
        for t in [t for t, m in self.matched.items() if now - m["t"] > 6 * 3600]:
            del self.matched[t]

    # -- schedule --
    def scheduled(self, uid, now):
        """Scheduled trips around now with expected arrival (seconds since epoch)."""
        if self.tt is None:
            return []
        out = []
        for t in self.tt.window(uid, datetime.fromtimestamp(now)):
            with self.lock:
                line = self.learn["names"].get("%s|%d|%s" % (t["base"], t["dir"], t["mark"]), t["line"])
            tr = self.travel_seconds(uid, line, t["tt"] if t["tt"] is not None else
                                     (timetable.DEFAULT_TT_MIN if t["base"] != "18A" else None))
            if tr is None:
                continue
            t = dict(t, line=line, dep_ts=t["dep"].timestamp())
            t["expected"] = t["dep_ts"] + tr
            if now - 30 * 60 <= t["expected"] <= now + SCHED_AHEAD + 20 * 60:
                out.append(t)
        return out

    def _match(self, uid, entries, sched, now):
        """Assign scheduled trips to remembered buses; returns set of taken trip_ids."""
        by_id = {t["trip_id"]: t for t in sched}
        taken = set()
        pending = []
        for e in entries:
            tid = e.get("trip_id")
            if tid and tid in by_id and tid not in taken:
                taken.add(tid)
                e["_trip"] = by_id[tid]
            else:
                e["trip_id"] = None
                pending.append(e)
        # exact: same base line and departure within +-1 min
        for e in pending:
            base = timetable.base_of(e["line"], uid)
            d = dep_datetime(e.get("dep"), now)
            if not base or d is None:
                continue
            exp = now + e["_est"]
            # nsmart sometimes keeps a stale departure label (e.g. a 7A labelled 16:57 still 16 min away at 17:44):
            # trust the label only if the bus's ETA is plausible for that trip, else use the nearest-ETA fallback
            c = [t for t in sched if t["base"] == base and t["trip_id"] not in taken
                 and abs(t["dep_ts"] - d.timestamp()) <= MATCH_EXACT and abs(t["expected"] - exp) <= MATCH_PLAUSIBLE]
            if c:
                t = min(c, key=lambda t: abs(t["dep_ts"] - d.timestamp()))
                taken.add(t["trip_id"])
                e["trip_id"], e["_trip"] = t["trip_id"], t
                self.learn["names"]["%s|%d|%s" % (t["base"], t["dir"], t["mark"])] = e["line"]
        # fallback: nearest expected arrival within +-6 min
        for e in pending:
            if e.get("trip_id"):
                continue
            base = timetable.base_of(e["line"], uid)
            if not base:
                continue
            exp = now + e["_est"]
            c = [t for t in sched if t["base"] == base and t["trip_id"] not in taken
                 and t["trip_id"] not in self.matched and abs(t["expected"] - exp) <= MATCH_NEAR]
            if c:
                t = min(c, key=lambda t: abs(t["expected"] - exp))
                taken.add(t["trip_id"])
                e["trip_id"], e["_trip"] = t["trip_id"], t
        for e in entries:
            if e.get("trip_id"):
                self.matched[e["trip_id"]] = {"key": "%s|%s" % (e["garage_no"], e["line"]), "stop": uid, "t": now}
                for a in self.arrivals[-30:]:     # arrival seen before the trip was matched: fill in the trip
                    if (a["trip_id"] is None and a["stop"] == uid and a["garage_no"] == e["garage_no"]
                            and a["line"] == e["line"] and a["dep"] == e.get("dep")):
                        a["trip_id"] = e["trip_id"]
        return taken

    # -- output --
    def view(self, uid, now=None, schedule=True, live=True):
        now = now if now is not None else time.time()
        with self.lock:
            self._prune(now)
            st = self.stops.get(uid) or {"name": KNOWN_NAMES.get(uid), "fetched_at": None, "error": None, "buses": {}}
            entries = []
            if live:
                for e in st["buses"].values():
                    e["_est"] = self._estimate(e, now)
                    entries.append(e)
            sched = self.scheduled(uid, now) if schedule else []
            taken = self._match(uid, entries, sched, now) if sched else set()
            buses = []
            for e in entries:
                est = e.pop("_est")
                trip = e.pop("_trip", None)
                lat, lng, cur, sb = e["lat"], e["lng"], e["current_stop"], e["stops_between"]
                v = self.vehicles.get(e["garage_no"])
                if now - e["seen_at"] > LIVE_MAX_AGE and v and v["t"] > e["seen_at"] and v["dep"] == e["dep"]:
                    # fresher position of the same trip seen at another stop
                    lat, lng, cur = v["lat"], v["lng"], v["current_stop"]
                    if e.get("pos") and v.get("next_station"):
                        sb = max(0, e["pos"] - v["next_station"])
                status = "live" if now - e["seen_at"] <= LIVE_MAX_AGE else "lost"
                buses.append({"line": e["line"], "direction": e["direction"], "minutes": est // 60,
                              "seconds": est, "stops_between": sb, "garage_no": e["garage_no"],
                              "lat": lat, "lng": lng, "current_stop": cur,
                              "live": status == "live", "last_seen": int(now - e["seen_at"]),
                              "status": status, "label": LABELS[status],
                              "scheduled_departure": (e.get("dep") or "")[:5] or None,
                              "expected": _hm(now + est),
                              "source": "direct" if e.get("source", "direct") == "direct" else "via " + e["source"],
                              "trip_id": e.get("trip_id")})
            for t in sched:
                if t["trip_id"] in taken or t["trip_id"] in self.matched:
                    continue
                if not (now - SCHED_GRACE <= t["expected"] <= now + SCHED_AHEAD):
                    continue
                est = max(0, int(round(t["expected"] - now)))
                buses.append({"line": t["line"], "direction": self.learn["to"].get(t["line"]) or timetable.destination(t["header"]),
                              "minutes": est // 60, "seconds": est, "stops_between": None, "garage_no": None,
                              "lat": None, "lng": None, "current_stop": None, "live": False, "last_seen": None,
                              "status": "scheduled", "label": LABELS["scheduled"],
                              "scheduled_departure": t["dep"].strftime("%H:%M"), "expected": _hm(t["expected"]),
                              "variant": t["mark"], "trip_id": t["trip_id"]})
            order = {"live": 0, "lost": 1, "scheduled": 2}
            buses.sort(key=lambda b: (b["seconds"], order[b["status"]]))
            out = {"stop": {"uid": uid, "name": st["name"]}, "timestamp": _iso(now),
                   "updated": int(now - st["fetched_at"]) if st.get("fetched_at") else None,
                   "buses": buses}
            if st.get("error"):
                out["error"] = st["error"]
            return out


def _parallel(fn, items):
    if not items:
        return []
    if len(items) == 1:
        return [fn(items[0])]
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as ex:
        return list(ex.map(fn, items))

def combine(results, merge=False):
    if merge:
        buses, stops, errors = [], [], []
        for r in results:
            stops.append(r["stop"])
            if "error" in r:
                errors.append({"stop": r["stop"], "error": r["error"]})
            for b in r.get("buses", []):
                buses.append({"stop_uid": r["stop"]["uid"], "stop_name": r["stop"]["name"], **b})
        order = {"live": 0, "lost": 1, "scheduled": 2}
        buses.sort(key=lambda b: (b["seconds"], order.get(b.get("status"), 0)))
        out = {"timestamp": _iso(), "stops": stops, "buses": buses}
        if errors:
            out["errors"] = errors
        return out
    if len(results) == 1:
        return results[0]
    return {"timestamp": _iso(), "stops": results}

def all_failed(results):
    return all("error" in r and not r.get("buses") for r in results)

def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------- polling ----------------
TT = timetable.Timetables()
MEM = Memory(TT)
_watch, _watch_lock = {}, threading.Lock()   # uid -> last requested (non-default stops)
_state_path = None
_snaplog = None                               # optional JSONL log of every upstream snapshot (debug)
_use_feeders = True

def poll_once(stops, feeders=True):
    """Fetch our stops (+ their preceding stops), project feeder buses onto our stops, update memory."""
    fmap = {uid: (MEM.feeders(uid) if feeders and _use_feeders else []) for uid in stops}
    extra = [f for f in dict.fromkeys(x for fs in fmap.values() for x in fs) if f not in stops]
    allq = list(stops) + extra
    res = dict(zip(allq, _parallel(fetch, allq)))
    now = time.time()
    MEM.observe_routes([b for r in res.values() for b in r.get("buses", [])], now)
    round_obs = {}
    for uid in stops:
        direct = res[uid]
        obs = {}
        if "error" not in direct:
            for b in direct["buses"]:
                if b.get("planned"):
                    continue
                obs["%s|%s" % (b["garage_no"], b["line"])] = dict(b, source="direct")
        for f in fmap[uid]:
            for b in res.get(f, {}).get("buses", []):
                key = "%s|%s" % (b["garage_no"], b["line"])
                if key in obs or b.get("planned"):
                    continue                      # direct observation wins
                ob = project(b, f, uid)
                if ob:
                    obs[key] = ob
        snap = dict(direct)
        snap["buses"] = list(obs.values())
        round_obs[uid] = [[b["garage_no"], b["line"], b.get("source", "direct"), b["seconds"], b.get("dep")] for b in obs.values()]
        if "error" in direct and obs:
            snap["name"] = None
        MEM.update(uid, snap, now)
    if _snaplog:
        try:
            with open(_snaplog, "a", encoding="utf-8") as f:
                f.write(dumps({"t": now, "feeders": fmap, "obs": round_obs, "snaps": {
                    k: {kk: ([{x: y for x, y in b.items() if not x.startswith("_")} for b in vv] if kk == "buses" else vv)
                        for kk, vv in v.items()} for k, v in res.items()}}) + "\n")
        except OSError:
            pass
    if _state_path:
        MEM.save(_state_path)
    return res

def poller(interval):
    n = 0
    while True:
        t0 = time.time()
        with _watch_lock:
            for uid in [u for u, t in _watch.items() if t0 - t > WATCH_TTL]:
                del _watch[uid]
            stops = list(dict.fromkeys(DEFAULT_STOPS + list(_watch)))
        try:
            poll_once(stops, feeders=(n % FEEDER_EVERY == 0))
        except Exception as e:      # never let the poller die
            sys.stderr.write("poller error: %r\n" % e)
        n += 1
        time.sleep(max(1.0, interval - (time.time() - t0)))

def tt_refresher():
    try:
        TT.refresh()
    except Exception as e:
        sys.stderr.write("timetable refresh error: %r\n" % e)
    while True:
        time.sleep(600)
        try:
            TT.maybe_refresh(min_interval=1800)
        except Exception as e:
            sys.stderr.write("timetable refresh error: %r\n" % e)

class Handler(BaseHTTPRequestHandler):
    server_version = "NStupido/3.0"

    def _send(self, status, obj):
        body = dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path.rstrip("/")
        q = urllib.parse.parse_qs(u.query)
        flag = lambda name: (q.get(name) or ["0"])[-1].lower() in ("1", "true", "yes", "on")
        if p == "":
            return self._send(200, {"usage": "GET /buses[?stop=6539[,6551]][&merge=1][&schedule=0|only]", "default_stops": DEFAULT_STOPS})
        if p == "/debug":
            with MEM.lock:
                learn = {"travel": MEM.learn["travel"], "tt": MEM.learn["tt"], "names": MEM.learn["names"]}
                arr = MEM.arrivals[-50:]
            return self._send(200, {"timestamp": _iso(), "timetable": TT.status(),
                                    "feeders": {s: MEM.feeders(s) for s in DEFAULT_STOPS},
                                    "learn": learn, "arrivals": arr})
        if p != "/buses":
            return self._send(404, {"error": {"type": "not_found", "message": "use GET /buses?stop=<uid>"}})
        stops = [s.strip() for v in q.get("stop", []) for s in v.split(",") if s.strip()] or list(DEFAULT_STOPS)
        if not all(s.isdigit() for s in stops):
            return self._send(400, {"timestamp": _iso(), "error": {"type": "bad_request", "message": "stop must be numeric"}})
        merge = flag("merge")
        sched_q = (q.get("schedule") or ["1"])[-1].lower()
        schedule, live = sched_q not in ("0", "false", "no", "off"), sched_q != "only"
        now = time.time()
        with _watch_lock:
            for s in stops:
                if s not in DEFAULT_STOPS:
                    _watch[s] = now
        with MEM.lock:
            missing = [s for s in stops if s not in MEM.stops]
        if missing and live:             # first request for a stop: fetch synchronously
            poll_once(missing)
        results = [MEM.view(s, schedule=schedule, live=live) for s in stops]
        self._send(502 if live and all_failed(results) else 200, combine(results, merge))

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    global _state_path, _snaplog, _use_feeders
    ap = argparse.ArgumentParser(description="Upcoming Novi Sad buses as JSON (live + memory + timetable)")
    ap.add_argument("stops", nargs="*", help="stop uid(s), default %s" % " ".join(DEFAULT_STOPS))
    ap.add_argument("--merge", action="store_true", help="one list of buses from all stops, sorted by time")
    ap.add_argument("--serve", action="store_true", help="run HTTP server with background poller")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--interval", type=float, default=POLL_INTERVAL, help="server poll interval, s")
    ap.add_argument("--state", default=STATE_FILE, help="memory state file (default: %(default)s)")
    ap.add_argument("--no-state", action="store_true", help="do not load/save memory")
    ap.add_argument("--no-schedule", action="store_true", help="do not use the gspns.rs timetable")
    ap.add_argument("--schedule-only", action="store_true", help="timetable entries only, no live fetch (debug)")
    ap.add_argument("--no-feeders", action="store_true", help="do not poll preceding stops")
    ap.add_argument("--pretty", action="store_true", help="indented JSON (CLI)")
    ap.add_argument("--log-snapshots", metavar="FILE", help="append every upstream snapshot to FILE (JSONL, debug)")
    a = ap.parse_args()
    _snaplog = a.log_snapshots
    _state_path = None if a.no_state else a.state
    _use_feeders = not a.no_feeders
    if a.no_schedule:
        MEM.tt = None
    if _state_path:
        MEM.load(_state_path)
    if a.serve:
        def _term(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _term)     # kill PID -> save state and exit cleanly
        if MEM.tt is not None:
            threading.Thread(target=tt_refresher, daemon=True).start()
        threading.Thread(target=poller, args=(a.interval,), daemon=True).start()
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
        sys.stderr.write("NStupido serving on http://%s:%d/buses (poll every %gs, state %s)\n"
                         % (a.host, a.port, a.interval, _state_path))
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
            if _state_path:
                MEM.save(_state_path)
            sys.stderr.write("NStupido stopped, state saved\n")
        return 0
    stops = [s for s in a.stops if s] or list(DEFAULT_STOPS)
    bad = [s for s in stops if not s.isdigit()]
    if bad:
        res = combine([{"stop": {"uid": s, "name": None}, "timestamp": _iso(),
                        "error": {"type": "bad_request", "message": "stop uid must be numeric"}} for s in stops], a.merge)
        print(dumps(res))
        return 1
    if MEM.tt is not None:
        try:
            MEM.tt.maybe_refresh(min_interval=600)
        except Exception:
            pass
    if not a.schedule_only:
        poll_once(stops)
    results = [MEM.view(s, schedule=MEM.tt is not None, live=not a.schedule_only) for s in stops]
    res = combine(results, a.merge)
    print(json.dumps(res, ensure_ascii=False, indent=1) if a.pretty else dumps(res))
    return 1 if not a.schedule_only and all_failed(results) else 0

if __name__ == "__main__":
    sys.exit(main())
