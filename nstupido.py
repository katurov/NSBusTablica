#!/usr/bin/env python3
"""NStupido v4.3 - upcoming buses for Novi Sad (JGSP / nsmart.rs) stops: live data + memory + official timetable
(gspns.rs), served as JSON and as text frames for the WiFi OLED board. Any stop works: its lines come from the
nsmart station list, their direction from the gspns network map (routes.py), missing timetables are fetched on the
first request. Display: base route only (14S -> 14, 1GL -> 1), letters only for distinct routes (3A, 7B, 18A).

CLI:     python3 nstupido.py [STOP_UID ...] [--merge] [--pretty] [--no-schedule | --schedule-only] [--no-feeders]
Server:  python3 nstupido.py --serve [--config config.toml] [--host 0.0.0.0] [--port 8080]
         GET /                      -> usage, default stops, watch list with expiry, queue, backoff
         GET /buses?stop=6551       -> JSON for that stop (comma-separated list allowed, &merge=1)
         GET /frame?stop=6551       -> text frame for the board (H|stop|HH:MM|flags, R|line|G:3|P:20, E)
         GET /debug                 -> timetable status, feeders, learned travel times, recent arrivals
         Codes: 200 (maybe "warming": true / "live": false), 400 bad uid, 401 outside the trusted networks without
                a valid token (auth.py), 404 no such stop in nsmart, 429 queue full (Retry-After), 503 starting.

Everything except the reference caches (cache/gspns/ timetables + network map, cache/nsmart/ station list) lives in
memory. nsmart is polled by one queue:
each watched stop followed by its feeder stops, spacing max(0.4 s, 40 s / N), at most 100 requests per 40 s
cycle, global exponential backoff 5..180 s. Config: config.toml. Logs: syslog (daemon, ident nstupido) + stderr.

Every board entry has a status:
  "live"      (Sveže)          - in a fresh upstream snapshot (of the stop itself or of a preceding stop)
  "lost"      (Videli-izgubili) - seen earlier, now estimated from memory (countdown)
  "scheduled" (Po rasporedu)    - from the official timetable: departure + travel time to the stop, not seen yet
Stdlib only (Python >= 3.9; config.toml read with tomllib / tomli / a built-in mini parser).
"""
import json, os, re, sys, time, errno, socket, traceback, argparse, threading, signal, statistics, random, logging, logging.handlers, collections
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import timetable                                   # noqa: E402  (vendored, same directory)
import routes                                      # noqa: E402
import auth                                        # noqa: E402

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
STATIONS_URL = routes.STATIONS_URL
TRUNCATED_LEN = 50             # upstream cuts station_name in arrival responses to 50 chars
KNOWN_NAMES = {                # full names (from getAllStations) for the default stops
    "6539": "Bulevar kralja Petra prvog-Mašinska škola",
    "15889": "Bulevar Oslobodjenja  Bulevar Kralja Petra prvog prigrad",
    "6712": "Bulevar Oslobođenja - Bulevar Kralja Petra prvog",
}
TIMEOUT = 15

# --- memory tuning ---
ZERO_HOLD = 30                 # drop an estimated bus that has been at 0 s for this long
HOLD_EXTRA = 120               # keep an unseen bus at most last_seconds_left + HOLD_EXTRA ...
HOLD_MAX = 20 * 60             # ... but never longer than this
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

def fetch_stations():
    """nsmart getAllStations (one ~1.5 MB request; every stop with coordinates and lines_for_station)."""
    req = urllib.request.Request(STATIONS_URL, data=b"", headers={
        "User-Agent": "Mozilla/5.0 (NStupido home board)", "X-Requested-With": "XMLHttpRequest"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


NAMING = routes.Naming()
STATIONS = routes.Stations(fetch=fetch_stations)
_stations_lock = threading.Lock()


def ensure_stations(stations=None, gate=None):
    """The station list is needed right away (names, lines of a new stop): fetch it once if there is no cache at
    all (an old cache waits for the 14-day rule)."""
    stations = stations or STATIONS
    with _stations_lock:
        if stations.fetched is None and not stations.stations and not getattr(stations, "_tried", False):
            stations._tried = True
            (gate or GATE).wait()
            ok = stations.refresh()
            LOG.info("station list: %s", "fetched (%d stops)" % len(stations.stations) if ok else
                     "FAILED (%s)" % stations.last_error)
    return stations


def full_name(uid, name):
    """Upstream truncates station_name to 50 chars; restore the full name from the stop list."""
    if name is None or len(name) < TRUNCATED_LEN:
        return name
    if uid in KNOWN_NAMES:
        return KNOWN_NAMES[uid]
    try:
        full = ensure_stations().name(uid)
    except Exception:
        full = None
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
            "actual": e.get("actual_line_number"),             # e.g. "542B": route 54, variant 2, direction B
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

    def __init__(self, tt=None, naming=None):
        self.lock = threading.RLock()
        self.tt = tt                 # timetable.Timetables or None
        self.naming = naming or NAMING
        self.stops = {}              # uid -> {"name", "fetched_at", "error", "buses": {key: entry}}
        self.vehicles = {}           # garage_no -> {"dep", "next_station", "line", "lat", "lng", "current_stop", "t"}
        self.learn = {"tt": {}, "travel": {k: list(v) for k, v in SEED_TRAVEL.items()}, "recorded": {},
                      "routes": {}, "names": {}, "to": {}}
        self.matched = {}            # trip_id -> {"key", "stop", "t"}  (scheduled trip already shown as a real bus)
        self.arrivals = []           # recent arrival observations (debug / accuracy)

    # -- learning --
    def observe_routes(self, buses, now):
        """Learn route order, scheduled tt to our stops, destinations from any bus of any response."""
        with self.lock:
            for b in buses:
                ids = b.get("_ids") or []
                if not ids or not b.get("line"):
                    continue
                self.learn["routes"]["%s|%s" % (b["line"], ids[0])] = {"ids": ids, "tt": list(b.get("_tt") or []), "t": now}
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
            if not tts:
                # any bus of this line seen anywhere (feeders, other stops) whose route passes uid: its scheduled
                # minutes terminal -> uid (nsmart all_stations[].tt_time)
                for k, r in self.learn["routes"].items():
                    if k.startswith(line + "|") and uid in r["ids"]:
                        i = r["ids"].index(uid)
                        if i < len(r.get("tt") or []) and r["tt"][i] is not None:
                            tts.append(r["tt"][i])
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

    def _base_of(self, line, uid):
        return self.tt.base_of(line, uid) if hasattr(self.tt, "base_of") else timetable.base_of(line, uid)

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
            base = self._base_of(e["line"], uid)
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
            base = self._base_of(e["line"], uid)
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
                buses.append({"line": self.naming.display(e["line"]), "nsmart_line": e["line"],
                              "direction": e["direction"], "minutes": est // 60,
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
                buses.append({"line": self.naming.display(t["line"]), "nsmart_line": t["line"],
                              "direction": self.learn["to"].get(t["line"]) or timetable.destination(t["header"]),
                              "minutes": est // 60, "seconds": est, "stops_between": None, "garage_no": None,
                              "lat": None, "lng": None, "current_stop": None, "live": False, "last_seen": None,
                              "status": "scheduled", "label": LABELS["scheduled"],
                              "scheduled_departure": t["dep"].strftime("%H:%M"), "expected": _hm(t["expected"]),
                              "variant": t["mark"], "trip_id": t["trip_id"], "entry": t.get("source", "manual")})
            order = {"live": 0, "lost": 1, "scheduled": 2}
            buses.sort(key=lambda b: (b["seconds"], order[b["status"]]))
            out = {"stop": {"uid": uid, "name": st["name"]}, "timestamp": _iso(now),
                   "updated": int(now - st["fetched_at"]) if st.get("fetched_at") else None,
                   "buses": buses}
            if st.get("error"):
                out["error"] = st["error"]
            return out


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


# ---------------- config ----------------
CONFIG_FILE = os.path.join(HERE, "config.toml")
DEFAULT_CONFIG = {
    "server": {"host": "0.0.0.0", "port": 8080, "timezone": "Europe/Belgrade"},
    "stops": {"default": list(DEFAULT_STOPS)},
    "polling": {"cycle_s": 40, "min_interval_s": 0.4, "watch_ttl_h": 4, "backoff_min_s": 5, "backoff_max_s": 180,
                "assumed_feeders": 3},
    "timetable": {"refresh_at": "05:00", "pause_s": [1, 2], "retry_min": 15, "retries": 3},
    "reference": {"max_age_days": 14, "start_delay_min": 60},
    "logging": {"stats_every_h": 12, "errors_every_min": 30},
    "auth": json.loads(json.dumps(auth.DEFAULT_AUTH)),
}


def _mini_toml(text):
    """Tiny TOML subset (tables, key = string / number / bool / flat array) for Pythons without tomllib/tomli."""
    out, cur = {}, None
    for raw in text.splitlines():
        line = re.sub(r"""#(?=(?:[^"']|"[^"]*"|'[^']*')*$).*""", "", raw).strip()
        if not line:
            continue
        m = re.fullmatch(r"\[\s*([A-Za-z0-9_.-]+)\s*\]", line)
        if m:
            cur = out
            for part in m.group(1).split("."):
                cur = cur.setdefault(part, {})
            continue
        k, sep, v = line.partition("=")
        if not sep:
            raise ValueError("bad config line: %r" % raw)
        v = v.strip()
        if v.startswith("{"):                            # inline table of strings: { name = "token", ... }
            if not v.endswith("}"):
                raise ValueError("bad inline table: %r" % raw)
            val = {}
            for kk, vv in re.findall(r"""([A-Za-z0-9_-]+|"[^"]*")\s*=\s*("[^"]*"|'[^']*')""", v[1:-1]):
                val[kk.strip('"')] = vv[1:-1]
        else:
            val = json.loads(re.sub(r"'([^']*)'", lambda mm: json.dumps(mm.group(1)), v))
        (cur if cur is not None else out)[k.strip().strip('"')] = val
    return out


def parse_toml(text):
    try:
        import tomllib                                   # Python 3.11+
        return tomllib.loads(text)
    except ImportError:
        pass
    try:
        import tomli                                     # pip install tomli (optional)
        return tomli.loads(text)
    except ImportError:
        return _mini_toml(text)


def load_config(path=None):
    """DEFAULT_CONFIG overlaid with the file (missing file = defaults)."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = path or CONFIG_FILE
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = parse_toml(f.read())
        for sec, vals in data.items():
            if isinstance(vals, dict):
                cfg.setdefault(sec, {}).update(vals)
    merge_auth_file(cfg, os.path.dirname(os.path.abspath(path)))
    cfg["stops"]["default"] = [str(x) for x in cfg["stops"]["default"]]
    return cfg


def merge_auth_file(cfg, base_dir):
    """Overlay the untracked auth file (default auth.toml next to config.toml): its [auth] keys win, tokens from
    [auth.tokens], `[auth] tokens = {...}` or a top-level [tokens] table are added. Missing file = nothing."""
    a = cfg.setdefault("auth", {})
    a["tokens"] = dict(a.get("tokens") or {})
    fn = a.get("file") or "auth.toml"
    path = fn if os.path.isabs(fn) else os.path.join(base_dir, fn)
    a["file_path"], a["file_loaded"] = path, False
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        data = parse_toml(f.read())
    extra = dict(data.get("auth") or {})
    tokens = dict(extra.pop("tokens", None) or {})
    tokens.update(data.get("tokens") or {})
    a.update(extra)
    a["tokens"].update({str(k): v for k, v in tokens.items()})
    a["file_loaded"] = True


# ---------------- logging (syslog: facility daemon, ident nstupido) ----------------
LOG = logging.getLogger("nstupido")


def setup_logging(to_stderr=True):
    LOG.setLevel(logging.INFO)
    LOG.handlers[:] = []
    LOG.propagate = False
    for addr in ("/var/run/syslog", "/dev/log"):          # macOS, Linux
        if os.path.exists(addr):
            try:
                h = logging.handlers.SysLogHandler(address=addr, facility=logging.handlers.SysLogHandler.LOG_DAEMON)
                h.ident = "nstupido: "
                h.setFormatter(logging.Formatter("%(message)s"))
                LOG.addHandler(h)
                break
            except OSError:
                pass
    if to_stderr or not LOG.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s nstupido: %(message)s", "%Y-%m-%d %H:%M:%S"))
        LOG.addHandler(h)


# ---------------- rate limit / backoff ----------------
class Gate:
    """Global nsmart pacing: at least `min_interval` s between two requests, plus exponential backoff after a
    failure (5, 10, 20, ... s, capped at backoff_max); the first success resets it. Global on purpose: a ban hits
    the whole server, not one stop."""

    def __init__(self, min_interval=0.4, backoff_min=5, backoff_max=180, clock=time.monotonic, sleep=time.sleep):
        self.min_interval, self.backoff_min, self.backoff_max = min_interval, backoff_min, backoff_max
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.next_at = None
        self.failures = 0
        self.requests = 0

    def wait(self):
        with self.lock:
            if self.next_at is not None:
                d = self.next_at - self.clock()
                if d > 0:
                    self.sleep(d)
            self.next_at = self.clock() + self.min_interval
            self.requests += 1

    def backoff_s(self):
        return 0 if not self.failures else min(self.backoff_max, self.backoff_min * 2 ** (self.failures - 1))

    def in_backoff(self):
        return self.failures > 0

    def failure(self):
        self.failures += 1
        self.next_at = max(self.next_at or 0, self.clock() + self.backoff_s())

    def success(self):
        self.failures = 0


GATE = Gate()


def error_kind(err):
    """Upstream error dict -> aggregation key ("timeout", "http 503", ...) or None if nsmart said the stop does
    not exist (a valid answer, not a failure)."""
    t = err.get("type")
    if t == "api" and (err.get("code") == 3 or err.get("message") == "unknown stop uid"):
        return None
    if t == "http":
        return "http %s" % err.get("status")
    if t == "network":
        return "timeout" if "timed out" in str(err.get("message", "")).lower() else "network"
    if t == "bad_response":
        return "bad response"
    return "api %s" % err.get("code")


class ErrorLog:
    """Counts nsmart errors; flush() returns at most one summary line per window, None if there were none."""

    def __init__(self, every_s=1800, clock=time.time):
        self.every_s, self.clock = every_s, clock
        self.lock = threading.Lock()
        self.counts = collections.Counter()
        self.window_start = clock()

    def record(self, kind):
        with self.lock:
            self.counts[kind] += 1

    def flush(self, backoff_s=0, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            if now - self.window_start < self.every_s:
                return None
            total = sum(self.counts.values())
            line = None
            if total:
                parts = ", ".join("%s %d" % (k, n) for k, n in sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0])))
                line = "nsmart errors: %d in last %d min (%s), backoff %d s" % (
                    total, round((now - self.window_start) / 60), parts, backoff_s)
            self.counts.clear()
            self.window_start = now
            return line


class ConnLog:
    """Scanner noise: connections dropped mid-request (reset, broken pipe, timeout) and malformed requests
    (e.g. TLS sent to plain HTTP -> 400). Counted per address, summarised at most once per window."""

    def __init__(self, every_s=1800, clock=time.time):
        self.every_s, self.clock = every_s, clock
        self.lock = threading.Lock()
        self.dropped, self.bad = collections.Counter(), collections.Counter()
        self.window_start = clock()

    def drop(self, ip):
        with self.lock:
            self.dropped[str(ip)] += 1

    def bad_request(self, ip):
        with self.lock:
            self.bad[str(ip)] += 1

    def flush(self, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            if now - self.window_start < self.every_s:
                return []
            mins = round((now - self.window_start) / 60)
            out = []
            for what, c in (("dropped", self.dropped), ("bad requests", self.bad)):
                n = sum(c.values())
                if n:
                    out.append("%s: %d %s in last %d min from %d address(es)" % (
                        what, n, "connections" if what == "dropped" else "", mins, len(c)))
                c.clear()
            self.window_start = now
        return [re.sub(r"  +", " ", l) for l in out]


CONN_ERRNOS = {errno.ECONNRESET, errno.EPIPE, errno.ETIMEDOUT, errno.ECONNABORTED, errno.ENOTCONN}


def is_connection_error(exc):
    """Errors caused by the peer going away / stalling, not by our code."""
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout)):
        return True
    try:
        import ssl
        if isinstance(exc, ssl.SSLError):
            return True
    except ImportError:
        pass
    return isinstance(exc, OSError) and exc.errno in CONN_ERRNOS


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that counts connection-level errors instead of printing tracebacks to stderr."""
    daemon_threads = True
    service = None

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        svc = self.service or SERVICE
        ip = client_address[0] if client_address else "?"
        if is_connection_error(exc):
            if svc is not None:
                svc.conn.drop(ip)
            return
        LOG.error("error while handling request from %s:\n%s", ip, traceback.format_exc().rstrip())


class Clients:
    """Unique client IPs seen in the last `window_s` seconds."""

    def __init__(self, window_s=12 * 3600, clock=time.time):
        self.window_s, self.clock = window_s, clock
        self.lock = threading.Lock()
        self.seen = {}

    def hit(self, ip):
        with self.lock:
            self.seen[ip] = self.clock()

    def count(self):
        now = self.clock()
        with self.lock:
            for ip in [i for i, t in self.seen.items() if now - t > self.window_s]:
                del self.seen[ip]
            return len(self.seen)


# ---------------- watch list + polling plan ----------------
class Watch:
    """Default stops (never expire) + dynamic stops, each with expiry = last request + ttl."""

    def __init__(self, defaults, ttl_s=4 * 3600, clock=time.time):
        self.defaults = list(dict.fromkeys(defaults))
        self.ttl_s, self.clock = ttl_s, clock
        self.dynamic = {}                        # uid -> expiry (insertion ordered)
        self.lock = threading.RLock()

    def is_watched(self, uid):
        with self.lock:
            return uid in self.defaults or uid in self.dynamic

    def touch(self, uid):
        """Extend a dynamic stop's timer; True if the stop is watched."""
        with self.lock:
            if uid in self.defaults:
                return True
            if uid in self.dynamic:
                self.dynamic[uid] = self.clock() + self.ttl_s
                return True
            return False

    def add(self, uid):
        with self.lock:
            if uid not in self.defaults:
                self.dynamic[uid] = self.clock() + self.ttl_s

    def remove(self, uid):
        with self.lock:
            self.dynamic.pop(uid, None)

    def expire(self):
        now = self.clock()
        with self.lock:
            gone = [u for u, t in self.dynamic.items() if t <= now]
            for u in gone:
                del self.dynamic[u]
            return gone

    def stops(self):
        with self.lock:
            return self.defaults + [u for u in self.dynamic if u not in self.defaults]

    def snapshot(self):
        with self.lock:
            return {**{u: None for u in self.defaults}, **dict(self.dynamic)}


def plan_groups(stops, feeders_of):
    """[(stop, [feeders])]: each watched stop followed by its preceding stops."""
    return [(s, [f for f in feeders_of(s) if f != s]) for s in stops]


def plan_sequence(groups):
    """Unique request order for one cycle; a feeder shared by several stops (or itself watched) is requested once."""
    seq = []
    for s, fs in groups:
        for u in [s] + list(fs):
            if u not in seq:
                seq.append(u)
    return seq


def spacing(n, cycle_s=40, min_interval_s=0.4):
    """Seconds between two requests with n requests per cycle: a small queue is polled gently."""
    return cycle_s if n <= 0 else max(min_interval_s, cycle_s / n)


def capacity(cycle_s=40, min_interval_s=0.4):
    return int(round(cycle_s / min_interval_s))


# ---------------- timetable refresh schedule ----------------
def _hhmm(s):
    h, m = (int(x) for x in s.split(":"))
    return h, m


def last_mark(now_dt, at="05:00"):
    """Most recent `at` (local time of now_dt) at or before now_dt."""
    h, m = _hhmm(at)
    t = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t <= now_dt else t - timedelta(days=1)


def next_mark(now_dt, at="05:00"):
    """Next `at` strictly after now_dt."""
    return last_mark(now_dt, at) + timedelta(days=1)


def timetable_due(refreshed_ts, now_dt, at="05:00"):
    """Refresh needed if never refreshed, or the last refresh is older than today's (or, before `at`, yesterday's) mark."""
    if not refreshed_ts:
        return True
    return datetime.fromtimestamp(refreshed_ts, now_dt.tzinfo) < last_mark(now_dt, at)


def local_tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return datetime.now().astimezone().tzinfo


class TimetableRefresher:
    """Startup + daily refresh at `refresh_at` local; failed refresh -> retry after retry_min, up to `retries` times."""

    def __init__(self, service, tt, cfg, now_dt=None):
        tc = cfg["timetable"]
        self.svc, self.tt = service, tt
        self.at, self.retry_s, self.max_retries = tc["refresh_at"], tc["retry_min"] * 60, tc["retries"]
        tz = local_tz(cfg["server"]["timezone"])
        self.now_dt = now_dt or (lambda: datetime.now(tz))
        self.retries = 0

    def step(self):
        """Run one check (maybe a refresh); returns seconds to sleep until the next check."""
        now = self.now_dt()
        if timetable_due(self.tt.refreshed_at(), now, self.at):
            ok = self.tt.refresh(today=now.date(), force=True, stops=set(self.svc.watch.stops()))
            r = self.tt.last_result or {}
            if hasattr(self.tt, "known_bases"):
                NAMING.update(self.tt.known_bases())
            LOG.info("timetable refresh: %s, %d fetched, %d failed%s", "ok" if ok else "FAILED", r.get("fetched", 0),
                     r.get("failed", 0), "" if ok else " (%s)" % self.tt.last_error)
            self.svc.ready.set()
            if not ok and self.retries < self.max_retries:
                self.retries += 1
                return self.retry_s
            self.retries = 0
        self.svc.ready.set()
        return max(1.0, (next_mark(self.now_dt(), self.at) - self.now_dt()).total_seconds())


class ReferenceRefresher:
    """nsmart station list + gspns network map (slow-changing). 14-day rule: a cache older than max_age (or
    missing) is refreshed one hour after server start; long-running servers re-check the age daily at
    `refresh_at` (05:00, right after the timetables). A failed refresh keeps the old cache."""

    def __init__(self, service, cfg, clock=time.time, now_dt=None):
        rc = cfg.get("reference") or DEFAULT_CONFIG["reference"]
        self.svc, self.clock = service, clock
        self.max_age_s = float(rc["max_age_days"]) * 86400
        self.delay_s = float(rc["start_delay_min"]) * 60
        self.at = cfg["timetable"]["refresh_at"]
        tz = local_tz(cfg["server"]["timezone"])
        self.now_dt = now_dt or (lambda: datetime.now(tz))
        self.started = clock()
        self.log = []                                # [(what, ok)] for tests / debug

    def due(self, fetched):
        return routes.cache_due(fetched, self.clock(), self.started, self.max_age_s, self.delay_s)

    def step(self):
        """Refresh what is due; -> seconds until the next check (start + 1 h, then every `refresh_at`)."""
        if self.clock() - self.started < self.delay_s:
            return self.started + self.delay_s - self.clock()
        svc, changed = self.svc, False
        st = svc.stations
        if st is not None and self.due(st.fetched):
            svc.gate.wait()
            ok = st.refresh()
            self.log.append(("stations", ok))
            LOG.info("station list refresh: %s", "ok (%d stops)" % len(st.stations) if ok else "FAILED, keeping old (%s)" % st.last_error)
            changed = changed or ok
        mp = svc.netmap
        if mp is not None and mp._fetch is not None and self.due(mp.fetched):
            ok = mp.refresh()
            self.log.append(("map", ok))
            LOG.info("network map refresh: %s", "ok (%d buttons)" % len(mp.buttons) if ok else "FAILED, keeping old (%s)" % mp.last_error)
            changed = changed or ok
        if changed and svc.resolver is not None:
            svc.resolver.invalidate()
            for uid in svc.watch.stops():
                svc.want_timetables(uid)
        nxt = next_mark(self.now_dt(), self.at) + timedelta(minutes=10)     # after the 05:00 timetable refresh
        return max(1.0, (nxt - self.now_dt()).total_seconds())


# ---------------- service: queue, polling, answers ----------------
class Service:
    def __init__(self, cfg, mem, fetch_fn=None, gate=None, clock=time.time, sleep=time.sleep, use_feeders=True,
                 resolver=None, stations=None, netmap=None):
        pol = cfg["polling"]
        self.cfg, self.mem = cfg, mem
        self.resolver, self.stations, self.netmap = resolver, stations, netmap
        self.tt_pending = set()                   # stops waiting for missing gspns timetables (-> warming)
        self.tt_retry = {}                        # uid -> time of the next attempt after a failure
        self.tt_later = set()
        self.tt_cond = threading.Condition()
        self.fetch = fetch_fn or fetch
        self.gate = gate or GATE
        self.clock, self.sleep = clock, sleep
        self.cycle_s, self.min_interval_s = float(pol["cycle_s"]), float(pol["min_interval_s"])
        self.cap = capacity(self.cycle_s, self.min_interval_s)
        self.assumed_feeders = int(pol.get("assumed_feeders", 3))
        self.use_feeders = use_feeders
        self.watch = Watch(cfg["stops"]["default"], float(pol["watch_ttl_h"]) * 3600, clock)
        self.missing = {}                         # uid -> when nsmart said "no such stop"
        self.urgent = collections.deque()
        self.errors = ErrorLog(cfg["logging"]["errors_every_min"] * 60, clock)
        self.clients = Clients(cfg["logging"]["stats_every_h"] * 3600, clock)
        self.auth = auth.Auth(cfg.get("auth"), clock)
        self.conn = ConnLog(cfg["logging"]["errors_every_min"] * 60, clock)
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.lock = threading.RLock()

    # -- plan --
    def feeders_of(self, uid):
        return self.mem.feeders(uid) if self.use_feeders else []

    def groups(self):
        return plan_groups(self.watch.stops(), self.feeders_of)

    def queue_length(self):
        return len(plan_sequence(self.groups()))

    def interval(self):
        return spacing(self.queue_length(), self.cycle_s, self.min_interval_s)

    def group_cost(self, uid, seq=None):
        """New requests per cycle that watching `uid` would add (unknown stop: itself + assumed feeders)."""
        seq = plan_sequence(self.groups()) if seq is None else seq
        with self.mem.lock:
            known = uid in self.mem.stops
        fs = self.feeders_of(uid)
        if not known and not fs and self.use_feeders:
            return 1 + self.assumed_feeders
        return len([u for u in [uid] + fs if u not in seq])

    def planned_load(self, seq=None):
        """Requests per cycle, counting assumed feeders for accepted stops not polled yet."""
        seq = plan_sequence(self.groups()) if seq is None else seq
        pending = 0
        if self.use_feeders:
            with self.mem.lock:
                pending = sum(1 for u in self.watch.stops() if u not in self.mem.stops and not self.feeders_of(u))
        return len(seq) + pending * self.assumed_feeders

    # -- client requests --
    def admit(self, uid):
        """-> "ok" (watched, timer extended), "new" (accepted, warming), "missing" (404) or "full" (429)."""
        with self.lock:
            if uid in self.missing:
                return "missing"
            if self.watch.touch(uid):
                return "ok"
            seq = plan_sequence(self.groups())
            if self.planned_load(seq) + self.group_cost(uid, seq) > self.cap:
                return "full"
            self.watch.add(uid)
            self.urgent.append(uid)
        self.want_timetables(uid)
        return "new"

    def warming(self, uid):
        with self.tt_cond:
            if uid in self.tt_pending:
                return True
        with self.mem.lock:
            st = self.mem.stops.get(uid)
            return not (st and st.get("fetched_at"))

    # -- gspns timetables for new stops / new routes --
    def want_timetables(self, uid):
        """Queue `uid` for the timetable worker if some of its lines were never fetched."""
        tt = self.mem.tt
        if tt is None or not hasattr(tt, "missing"):
            return False
        if self.stations is not None and uid not in self.watch.defaults:
            ensure_stations(self.stations, self.gate)
        if not tt.missing([uid]):
            return False
        with self.tt_cond:
            self.tt_pending.add(uid)
            self.tt_cond.notify_all()
        return True

    def timetable_step(self):
        """Fetch missing timetables for one pending stop (sequential, gspns pacing). -> True if it did work."""
        tt = self.mem.tt
        now = self.clock()
        with self.tt_cond:
            todo = [u for u in self.tt_pending if self.tt_retry.get(u, 0) <= now]
        if not todo:
            return False
        uid = todo[0]
        if not self.watch.is_watched(uid):
            with self.tt_cond:
                self.tt_pending.discard(uid)
            return True
        miss = tt.missing([uid])
        ok = bad = 0
        if miss:
            ok, bad = tt.ensure([uid])
            NAMING.update(tt.known_bases())
            LOG.info("timetables for stop %s: %s (%s)", uid, "ok" if not bad else "FAILED, retry in %d min" %
                     (self.cfg["timetable"]["retry_min"]), ", ".join("%s %s" % (rv, ",".join(sorted(b))) for rv, b in miss.items()))
        with self.tt_cond:
            self.tt_pending.discard(uid)          # answer without the missing timetable meanwhile (not warming forever)
            if bad:
                self.tt_retry[uid] = now + self.cfg["timetable"]["retry_min"] * 60
                self.tt_later.add(uid)
            else:
                self.tt_retry.pop(uid, None)
        return True

    def timetable_forever(self):
        while not self.stopping.is_set():
            try:
                for uid in list(self.tt_later):
                    if self.tt_retry.get(uid, 0) <= self.clock():
                        self.tt_later.discard(uid)
                        if self.watch.is_watched(uid):
                            self.want_timetables(uid)
                if not self.timetable_step():
                    with self.tt_cond:
                        self.tt_cond.wait(5)
            except Exception as e:
                LOG.info("timetable worker error: %r", e)
                self.stopping.wait(30)

    def observe_live(self, buses):
        """Routes seen live but unknown for a watched stop on their route: learn the direction from nsmart's
        actual_line_number (trailing 'B' = direction B) and fetch the timetable."""
        if self.resolver is None:
            return
        watched = set(self.watch.stops())
        for b in buses:
            if b.get("planned") or not b.get("line"):
                continue
            for w in watched.intersection(b.get("_ids") or []):
                base = self.resolver.observe(w, b["line"], b.get("actual"))
                if base:
                    LOG.info("stop %s: route %s seen live (%s, direction %s), not in map/timetables - learning", w,
                             base, b.get("actual"), "B" if str(b.get("actual") or "").endswith("B") else "A")
                    self.want_timetables(w)

    def answer(self, uid):
        live = not self.gate.in_backoff()
        v = self.mem.view(uid, schedule=self.mem.tt is not None, live=live)
        v["warming"] = self.warming(uid)
        v["live"] = live
        return v

    # -- upstream --
    def request(self, uid):
        self.gate.wait()
        r = self.fetch(uid)
        err = r.get("error")
        if err:
            kind = error_kind(err)
            if kind is None:
                self.gate.success()
                self._mark_missing(uid)
            else:
                self.gate.failure()
                self.errors.record(kind)
        else:
            self.gate.success()
        self.mem.observe_routes(r.get("buses", []), self.clock())
        self.observe_live(r.get("buses", []))
        return r

    def _mark_missing(self, uid):
        with self.lock:
            if uid in self.watch.defaults or not self.watch.is_watched(uid):
                return                             # a default stop is never dropped; a feeder is just useless
            self.missing[uid] = self.clock()
            self.watch.remove(uid)
        with self.mem.lock:
            self.mem.stops.pop(uid, None)
        LOG.info("stop %s does not exist in nsmart, dropped", uid)

    def process(self, uid, direct, feeder_results):
        """Merge a stop's own snapshot with buses projected from its feeders into memory."""
        obs = {}
        if "error" not in direct:
            for b in direct["buses"]:
                if not b.get("planned"):
                    obs["%s|%s" % (b["garage_no"], b["line"])] = dict(b, source="direct")
        for f, res in feeder_results.items():
            for b in res.get("buses", []):
                key = "%s|%s" % (b["garage_no"], b["line"])
                if key in obs or b.get("planned"):
                    continue                       # direct observation wins
                ob = project(b, f, uid)
                if ob:
                    obs[key] = ob
        snap = dict(direct)
        snap["buses"] = list(obs.values())
        if "error" in direct and obs:
            snap["name"] = None
        self.mem.update(uid, snap, self.clock())

    def poll_group(self, uid, fetched, pace):
        """Stop first (reveals routes -> feeders), then its feeders back to back; reuse what this cycle fetched."""
        if uid not in fetched:
            pace()
            fetched[uid] = self.request(uid)
        if uid in self.missing:
            return
        fs = self.feeders_of(uid)
        for f in fs:
            if f not in fetched:
                pace()
                fetched[f] = self.request(f)
        self.process(uid, fetched[uid], {f: fetched[f] for f in fs})

    def _serve_urgent(self, fetched):
        while self.urgent:
            uid = self.urgent.popleft()
            if self.watch.is_watched(uid):
                self.poll_group(uid, fetched, lambda: None)      # the gate still enforces min_interval

    def _wait_until(self, t, fetched):
        while not self.stopping.is_set():
            self._serve_urgent(fetched)
            d = t - self.clock()
            if d <= 0:
                return
            self.sleep(min(d, 0.2))

    def run_cycle(self):
        for uid in self.watch.expire():
            with self.mem.lock:
                self.mem.stops.pop(uid, None)
        t0 = self.clock()
        groups = self.groups()
        iv = spacing(len(plan_sequence(groups)), self.cycle_s, self.min_interval_s)
        fetched, last = {}, [None]

        def pace():
            if last[0] is not None:
                self._wait_until(last[0] + iv, fetched)
            last[0] = self.clock()

        for uid, _ in groups:
            if self.stopping.is_set():
                break
            self._serve_urgent(fetched)
            if self.watch.is_watched(uid):
                self.poll_group(uid, fetched, pace)
        if last[0] is not None:
            self._wait_until(max(t0 + self.cycle_s, last[0] + iv), fetched)
        return fetched

    def poll_forever(self):
        while not self.stopping.is_set():
            try:
                self.run_cycle()
            except Exception as e:                # never let the poller die
                LOG.info("poller error: %r", e)
                self.sleep(5)

    # -- logging --
    def record_http(self, ip, path, status):
        """No access log (the board asks every 5 s): count (path, status), log a client's first request once."""
        p = urllib.parse.urlparse(auth.redact(path))
        key = "%s %d" % (p.path or "/", status)
        with self.lock:
            if not hasattr(self, "http"):
                self.http, self.http_clients = collections.Counter(), {}
            self.http[key] += 1
            first = ip not in self.http_clients
            self.http_clients[ip] = {"last": "%s?%s -> %d" % (p.path, p.query, status) if p.query else
                                     "%s -> %d" % (p.path, status), "at": _iso(self.clock())}
        if first:
            LOG.info("client %s first request: %s", ip, self.http_clients[ip]["last"])

    def http_line(self):
        with self.lock:
            c = getattr(self, "http", None)
            if not c:
                return None
            line = "http: %s" % ", ".join("%s x%d" % (k, n) for k, n in sorted(c.items(), key=lambda kv: -kv[1]))
            c.clear()
        return line

    def stats_line(self):
        st = self.watch.stops()
        nd = len(self.watch.defaults)
        return "stats: %d stops watched (%d default, %d dynamic), queue %d requests/cycle (interval %.1f s, limit %d), " \
               "%d clients in last %g h, %s" % (len(st), nd, len(st) - nd, self.queue_length(), self.interval(), self.cap,
                                                self.clients.count(), self.cfg["logging"]["stats_every_h"],
                                                self.auth.usage_line())

    def housekeeping_forever(self):
        every = self.cfg["logging"]["stats_every_h"] * 3600
        last_stats = self.clock()
        while not self.stopping.wait(30):
            line = self.errors.flush(self.gate.backoff_s())
            if line:
                LOG.info(line)
            for line in self.conn.flush():
                LOG.info(line)
            line = self.auth.flush_denied(self.cfg["logging"]["errors_every_min"] * 60)
            if line:
                LOG.info(line)
            if self.clock() - getattr(self, "_http_at", 0) >= self.cfg["logging"]["errors_every_min"] * 60:
                if getattr(self, "_http_at", 0):
                    line = self.http_line()
                    if line:
                        LOG.info(line)
                self._http_at = self.clock()
            if self.clock() - last_stats >= every:
                last_stats = self.clock()
                LOG.info(self.stats_line())

    def status(self, trusted=True):
        """Service overview; the per-client list (addresses) only for trusted networks."""
        out = self._status()
        if not trusted:
            out.pop("clients", None)
        return out

    def _status(self):
        tz = local_tz(self.cfg["server"]["timezone"])
        return {"usage": {"GET /buses?stop=<uid>": "JSON arrivals (G/E/P), stops comma-separated, &merge=1",
                          "GET /frame?stop=<uid>": "text frame for the OLED board (H/R/E lines)",
                          "GET /debug": "timetable + learning internals"},
                "default_stops": list(self.watch.defaults),
                "watch": {u: (datetime.fromtimestamp(t, tz).isoformat(timespec="seconds") if t else None)
                          for u, t in self.watch.snapshot().items()},
                "queue": {"length": self.queue_length(), "limit": self.cap, "interval_s": round(self.interval(), 2),
                          "cycle_s": self.cycle_s},
                "backoff": {"active": self.gate.in_backoff(), "failures": self.gate.failures,
                            "seconds": self.gate.backoff_s()},
                "ready": self.ready.is_set(),
                "http": dict(getattr(self, "http", {})), "clients": dict(getattr(self, "http_clients", {}))}


# ---------------- board frame (text, same protocol as the USB bridge) ----------------
_ASCII = str.maketrans({"Č": "C", "Ć": "C", "Ž": "Z", "Š": "S", "Đ": "D", "č": "c", "ć": "c", "ž": "z", "š": "s",
                        "đ": "d", '"': None, "|": None})
FRAME_MAX_ROUTES = 24


def ascii_line(line):
    return (line or "?").translate(_ASCII).encode("ascii", "replace").decode()[:5] or "?"


def frame_rows(buses, max_routes=FRAME_MAX_ROUTES):
    """Routes nearest-first, up to 2 cells each: ["R|8|G:3|P:20", ...]."""
    key = lambda b: b.get("seconds") if b.get("seconds") is not None else 10 ** 9
    by_line = {}
    for b in sorted(buses, key=key):
        by_line.setdefault(ascii_line(b.get("line")), []).append(b)
    rows = []
    for line, bl in list(by_line.items())[:max_routes]:
        cells = []
        for b in bl[:2]:
            mins = max(0, int(key(b) // 60)) if b.get("seconds") is not None else 0
            tag = {"live": "G", "lost": "E"}.get(b.get("status"), "P")
            cells.append("%s:%d" % (tag, mins))
        rows.append("|".join(["R", line] + cells))
    return rows


def frame_text(view, now_dt):
    flags = ("W" if view.get("warming") else "") + ("" if view.get("live", True) else "O")
    head = "H|%s|%s|%s" % (view["stop"]["uid"], now_dt.strftime("%H:%M"), flags)
    return "\n".join([head] + frame_rows(view.get("buses") or []) + ["E"]) + "\n"


# ---------------- HTTP ----------------
UID_RE = re.compile(r"^\d{1,7}$")
SERVICE = None                                  # set by serve()


class Handler(BaseHTTPRequestHandler):
    server_version = "NStupido/4.3"
    timeout = 15                                # idle / slowloris connections are closed (counted as dropped)
    service = None                              # tests may set a class attribute; default: SERVICE

    @property
    def svc(self):
        return self.service or SERVICE

    def _send(self, status, obj, headers=None, text=None):
        try:
            if status != 401:                       # rejected requests are only counted (Auth.flush_denied)
                self.svc.record_http(str(self._client_ip()), self.path, status)
        except Exception:
            pass
        body = (text if text is not None else dumps(obj)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8" if text is not None else "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
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

    def _client_ip(self):
        """Peer address (IPv4-mapped IPv6 unwrapped); X-Forwarded-For only via [auth] trusted_proxies."""
        ip = self.svc.auth.client_ip(self.client_address[0], self.headers.get("X-Forwarded-For"))
        return ip if ip is not None else self.client_address[0]

    def _authorize(self, q):
        """-> True if the request may proceed (trusted network or valid token); else a bare 401 was sent."""
        ip = self._client_ip()
        kind, name = self.svc.auth.check(ip if not isinstance(ip, str) else auth.parse_ip(ip), self.headers, q)
        self._trusted = kind == "trusted"
        if kind == "denied":
            body = b"unauthorized\n"
            self.send_response(401)
            self.send_header("WWW-Authenticate", "Bearer")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return False
        if kind == "token-first":
            LOG.info("token %s first request from %s: %s", name, ip, auth.redact(self.path))
        return True

    def resolve(self, q):
        """-> (status, obj, headers, views) for /buses and /frame."""
        svc = self.svc
        stops = [s.strip() for v in q.get("stop", []) for s in v.split(",") if s.strip()] or list(svc.watch.defaults)
        bad = [s for s in stops if not UID_RE.match(s)]
        if bad:
            return 400, {"error": {"type": "bad_request", "message": "stop uid must be 1-7 digits: %s" % bad[0]}}, {}, None
        if not svc.ready.is_set():
            return 503, {"error": {"type": "starting", "message": "server is starting (timetable)"}}, {"Retry-After": 10}, None
        for s in stops:
            r = svc.admit(s)
            if r == "missing":
                return 404, {"error": {"type": "not_found", "message": "stop %s does not exist in nsmart" % s}}, {}, None
            if r == "full":
                return 429, {"error": "stop limit reached", "limit": svc.cap}, {"Retry-After": 600}, None
        return 200, None, {}, [svc.answer(s) for s in stops]

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path.rstrip("/")
        q = urllib.parse.parse_qs(u.query)
        svc = self.svc
        if not self._authorize(q):
            return
        svc.clients.hit(str(self._client_ip()))
        if p == "":
            return self._send(200, svc.status(trusted=self._trusted))
        if p == "/debug":
            mem = svc.mem
            with mem.lock:
                learn = {"travel": mem.learn["travel"], "tt": mem.learn["tt"], "names": mem.learn["names"]}
                arr = mem.arrivals[-50:]
            lines = {}
            if svc.resolver is not None:
                for s_ in svc.watch.stops():
                    lines[s_] = {"entries": svc.resolver.describe(s_), "derivation": svc.resolver.info.get(s_),
                                 "manual": s_ in svc.resolver.manual}
            return self._send(200, {"timestamp": _iso(), "timetable": mem.tt.status() if mem.tt else None,
                                    "lines": lines, "pending_timetables": sorted(svc.tt_pending),
                                    "reference": {"stations_fetched": svc.stations.fetched if svc.stations else None,
                                                  "map_fetched": svc.netmap.fetched if svc.netmap else None},
                                    "feeders": {s: svc.feeders_of(s) for s in svc.watch.stops()},
                                    "missing": list(svc.missing), "learn": learn, "arrivals": arr})
        if p not in ("/buses", "/frame"):
            return self._send(404, {"error": {"type": "not_found", "message": "use GET /buses?stop=<uid>"}})
        status, obj, headers, views = self.resolve(q)
        if p == "/frame":
            if views is None:
                msg = obj["error"] if isinstance(obj["error"], str) else obj["error"]["message"]
                return self._send(status, None, headers, text="ERR|%d|%s\n" % (status, msg))
            now_dt = datetime.now(local_tz(svc.cfg["server"]["timezone"]))
            return self._send(200, None, headers, text=frame_text(views[0], now_dt))
        if views is None:
            return self._send(status, obj, headers)
        flag = (q.get("merge") or ["0"])[-1].lower() in ("1", "true", "yes", "on")
        self._send(200, combine(views, flag))

    do_HEAD = do_GET

    def log_error(self, fmt, *args):
        """send_error (400 bad request line, 414, 501…) and 'Request timed out': counted, not printed."""
        try:
            svc = self.svc
            if svc is not None:
                ip = self.client_address[0]
                if fmt.startswith("Request timed out"):
                    svc.conn.drop(ip)
                else:
                    svc.conn.bad_request(ip)
        except Exception:
            pass

    def log_message(self, fmt, *args):
        pass                                      # the board asks every 5 s; no access log


# ---------------- main ----------------
def _age(ts):
    return "missing" if not ts else "%.1f days old" % ((time.time() - ts) / 86400)


def build_service(cfg, tt, use_feeders=True, stations=None, netmap=None, fetch_fn=None, gate=None,
                  clock=time.time, sleep=time.sleep):
    """Service + the stop -> lines resolver (manual STOP_LINES for the default stops, derived otherwise)."""
    stations = stations or STATIONS
    netmap = netmap or routes.NetworkMap(fetch=(tt.fetch if tt is not None else None))
    resolver = None
    if tt is not None:
        NAMING.update(tt.known_bases())
        resolver = routes.Resolver(NAMING, stations, netmap, manual=timetable.STOP_LINES,
                                   rv_of=lambda b: tt.known_bases().get(b))
        tt.resolver = resolver
    return Service(cfg, Memory(tt), fetch_fn=fetch_fn, gate=gate, clock=clock, sleep=sleep, use_feeders=use_feeders,
                   resolver=resolver, stations=stations, netmap=netmap)


def serve(cfg, host, port, use_schedule=True, use_feeders=True):
    global SERVICE
    tc = cfg["timetable"]
    pause = tuple(tc["pause_s"])
    tt = timetable.Timetables(pause=lambda: time.sleep(random.uniform(*pause))) if use_schedule else None
    GATE.min_interval = float(cfg["polling"]["min_interval_s"])
    GATE.backoff_min, GATE.backoff_max = float(cfg["polling"]["backoff_min_s"]), float(cfg["polling"]["backoff_max_s"])
    svc = SERVICE = build_service(cfg, tt, use_feeders=use_feeders)
    srv = Server((host, port), Handler)
    srv.service = svc
    srv.daemon_threads = True

    def _term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    LOG.info("start: listening on %s:%d, default stops %s, cycle %gs, min interval %gs, limit %d requests/cycle",
             host, port, ",".join(svc.watch.defaults), svc.cycle_s, svc.min_interval_s, svc.cap)
    a = cfg.get("auth") or {}
    LOG.info("auth file %s: %s", a.get("file_path"), "loaded" if a.get("file_loaded") else "not found")
    for line in svc.auth.startup_lines():
        LOG.info(line)
    if tt is not None:
        if tt.data:
            svc.ready.set()                      # a cached (maybe stale) timetable is good enough to answer
        ref = TimetableRefresher(svc, tt, cfg)

        def tt_loop():
            while not svc.stopping.is_set():
                try:
                    d = ref.step()
                except Exception as e:
                    LOG.info("timetable refresh error: %r", e)
                    svc.ready.set()
                    d = 900
                svc.stopping.wait(d)
        threading.Thread(target=tt_loop, daemon=True, name="timetable").start()
        threading.Thread(target=svc.timetable_forever, daemon=True, name="timetables-new").start()
        refs = ReferenceRefresher(svc, cfg)

        def ref_loop():
            while not svc.stopping.is_set():
                try:
                    d = refs.step()
                except Exception as e:
                    LOG.info("reference refresh error: %r", e)
                    d = 3600
                svc.stopping.wait(d)
        threading.Thread(target=ref_loop, daemon=True, name="reference").start()
        LOG.info("reference caches: station list %s, network map %s (refresh if older than %g days, %g min after start "
                 "and daily at %s)", _age(STATIONS.fetched), _age(svc.netmap.fetched),
                 refs.max_age_s / 86400, refs.delay_s / 60, cfg["timetable"]["refresh_at"])
    else:
        svc.ready.set()
    threading.Thread(target=svc.poll_forever, daemon=True, name="poller").start()
    threading.Thread(target=svc.housekeeping_forever, daemon=True, name="housekeeping").start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        svc.stopping.set()
        srv.server_close()
        LOG.info("stop: %s", svc.stats_line())
    return 0


def main():
    ap = argparse.ArgumentParser(description="Upcoming Novi Sad buses as JSON (live + memory + timetable)")
    ap.add_argument("stops", nargs="*", help="stop uid(s), default: [stops] default in config.toml")
    ap.add_argument("--config", default=CONFIG_FILE, help="config file (default: %(default)s)")
    ap.add_argument("--merge", action="store_true", help="one list of buses from all stops, sorted by time")
    ap.add_argument("--serve", action="store_true", help="run HTTP server with background poller")
    ap.add_argument("--port", type=int, default=None, help="override [server] port")
    ap.add_argument("--host", default=None, help="override [server] host")
    ap.add_argument("--no-schedule", action="store_true", help="do not use the gspns.rs timetable")
    ap.add_argument("--schedule-only", action="store_true", help="timetable entries only, no live fetch (debug)")
    ap.add_argument("--no-feeders", action="store_true", help="do not poll preceding stops")
    ap.add_argument("--pretty", action="store_true", help="indented JSON (CLI)")
    a = ap.parse_args()
    cfg = load_config(a.config)
    DEFAULT_STOPS[:] = cfg["stops"]["default"]
    if a.serve:
        setup_logging()
        return serve(cfg, a.host or cfg["server"]["host"], a.port or int(cfg["server"]["port"]),
                     use_schedule=not a.no_schedule, use_feeders=not a.no_feeders)
    stops = [s for s in a.stops if s] or list(DEFAULT_STOPS)
    bad = [s for s in stops if not UID_RE.match(s)]
    if bad:
        res = combine([{"stop": {"uid": s, "name": None}, "timestamp": _iso(),
                        "error": {"type": "bad_request", "message": "stop uid must be numeric"}} for s in stops], a.merge)
        print(dumps(res))
        return 1
    tt = None if a.no_schedule else timetable.Timetables()
    if tt is not None:
        try:
            tt.maybe_refresh(min_interval=600)
        except Exception:
            pass
    cfg["stops"]["default"] = list(DEFAULT_STOPS)
    svc = build_service(cfg, tt, use_feeders=not a.no_feeders)
    for s in stops:
        if s not in svc.watch.defaults:
            svc.watch.add(s)
            if tt is not None and svc.want_timetables(s):
                while svc.timetable_step():
                    pass
    if not a.schedule_only:
        fetched = {}
        for s in stops:                          # sequential, paced by GATE (>= 0.4 s apart)
            svc.poll_group(s, fetched, lambda: None)
    results = [svc.mem.view(s, schedule=tt is not None, live=not a.schedule_only) for s in stops]
    res = combine(results, a.merge)
    print(json.dumps(res, ensure_ascii=False, indent=1) if a.pretty else dumps(res))
    return 1 if not a.schedule_only and all_failed(results) else 0


if __name__ == "__main__":
    sys.exit(main())
