"""Official JGSP Novi Sad timetables (gspns.rs) for NStupido: fetch, cache, day type, scheduled trips.

gspns.rs endpoints (plain GET, HTML fragments, no auth):
  /red-voznje/gradski?selected_lang=lat         -> "danas voze po redu vožnje za RADNI DAN|SUBOTA|NEDELJA|PRAZNIK"
                                                   and <select id=vaziod> = timetable validity dates
  /red-voznje/lista-linija?rv=&vaziod=&dan=     -> <option value="3."> line options (suffixes . and * are part of the value)
  /red-voznje/ispis-polazaka?rv=&vaziod=&dan=&linija[]=..  -> one table per line: departures from the terminal per direction
rv: rvg (gradski / urban) | rvp (prigradski / suburban); dan: R weekday, S Saturday, N Sunday, P holiday.
Stdlib only.
"""
import os, re, json, time, html, threading, urllib.request, urllib.parse
from datetime import date, datetime, timedelta
from rs_holidays import rs_holidays

HERE = os.path.dirname(os.path.abspath(__file__))
GSPNS = "http://gspns.rs"
CACHE_DIR = os.path.join(HERE, "cache", "gspns")
TIMEOUT = 20
DAY_TYPES = ("R", "S", "N", "P")
INDICATOR = {"RADNI DAN": "R", "SUBOTA": "S", "NEDELJA": "N", "PRAZNIK": "P"}
NIGHT_MAX_HOUR = 5          # a direction whose departures are all before 05:00 is a night line (runs after midnight)

# ---- which line directions pass our stops ----
# base: gspns line (option value without '.'/'*'); dir: index of the direction column in the gspns table;
# names: gspns departure mark -> nsmart line name (None = unmarked departure; marks not listed are skipped,
#        i.e. variants that do not pass the stop); tt: scheduled minutes terminal -> stop per nsmart line
#        (from nsmart all_stations[].tt_time; values marked guess are refined from live data).
# Evidence: nsmart all_stations order/tt_time (6 Oct 2026), gspns.rs/mreza stop->line-direction lists.
STOP_LINES = {
    "6539": [   # Bulevar kralja Petra prvog - Mašinska škola (mreza: 3B, 8A, 18A)
        {"base": "3", "rv": "rvg", "dir": 1, "names": {None: "3"}, "tt": {"3": 9}},              # Detelinara -> Petrovaradin
        {"base": "8", "rv": "rvg", "dir": 0, "names": {None: "8"}, "tt": {"8": 22}},             # Novo naselje -> Liman 1
        {"base": "18A", "rv": "rvg", "dir": 0, "names": {None: "18A"}, "tt": {}},                # night loop, tt unknown (learned)
    ],
    "6712": [   # Bulevar oslobođenja - Bulevar kralja Petra prvog, city side (all pass Železnička stanica 6586 ~2 min before)
        {"base": "4", "rv": "rvg", "dir": 1, "names": {None: "4"}, "tt": {"4": 2}},              # Ž.stanica -> Liman IV
        {"base": "7A", "rv": "rvg", "dir": 0, "names": {None: "7A"}, "tt": {"7A": 23}},          # loop from Novo naselje
        {"base": "14", "rv": "rvg", "dir": 1, "names": {None: "14", "NGS": "14GS", "S": "14S", "NRS": "14S"},
         "tt": {"14": 32, "14S": 23, "14GS": 32}},                                               # Veternik/Sajlovo -> Centar (14GS guess)
        {"base": "10", "rv": "rvg", "dir": 1, "names": {None: "10", "AL": "10", "ALM": "10MAL", "MR": "10MAL"},
         "tt": {"10MAL": 16, "10": 16}},                                                         # Ind. zona jug -> Centar (10 guess)
        {"base": "15", "rv": "rvg", "dir": 1, "names": {"KN": "15NK", "NP": "15"}, "tt": {"15NK": 15, "15": 15}},  # guess
        {"base": "19", "rv": "rvg", "dir": 0, "names": {None: "19"}, "tt": {"19": 2}},           # Ž.stanica -> Mišeluk (guess)
        {"base": "3A", "rv": "rvg", "dir": 0, "names": {None: "3A"}, "tt": {"3A": 2}},           # Ž.stanica -> Pobeda (guess)
        {"base": "5N", "rv": "rvg", "dir": 0, "names": {None: "5N"}, "tt": {"5N": 2}},           # Sundays (guess)
        {"base": "18A", "rv": "rvg", "dir": 0, "names": {None: "18A"}, "tt": {}},
    ],
    "15889": [  # same corner, suburban side: direction A ("Polasci za ...") of every suburban line leaving Novi Sad
    ],
}
_SUB_TT = {"52": 4, "53": 2, "55": 1, "62": 2, "64": 1, "69": 2, "71": 6, "72": 6, "74": 2, "76": 1, "79": 2, "81": 2}
_SUB_NAMES = {"68": {"IS": "68IS"}, "71": {"IS": "71IS"}, "72": {"IS": "72IS"}, "73": {"IS": "73IS"}, "74": {"IS": "74IS"},
              "54": {'"A"': '54"A"', "ZA": "54ŽA"}, "53": {"CG": "53CG"}, "55": {"CG": "55CG", "KN": "55KN"}}
for _l in ["52", "53", "54", "55", "60", "61", "62", "63", "64", "68", "69", "71", "72", "73", "74", "76", "77", "78",
           "79", "80", "81", "84", "86"]:
    _names = {None: _l}
    for _m, _n in _SUB_NAMES.get(_l, {}).items():
        _names[_m] = _n
    STOP_LINES["15889"].append({"base": _l, "rv": "rvp", "dir": 0, "names": _names, "all_marks": True,
                                "tt": {_l: _SUB_TT.get(_l, 2), **{n: (4 if n == "72IS" else _SUB_TT.get(_l, 2)) for n in _names.values()}}})
DEFAULT_TT_MIN = 2

def needed_lines():
    out = {}
    for cfgs in STOP_LINES.values():
        for c in cfgs:
            out.setdefault(c["rv"], set()).add(c["base"])
    return out

def base_of(line, stop):
    """nsmart line name -> configured gspns base line for this stop (longest prefix), or None."""
    best = None
    for c in STOP_LINES.get(stop, []):
        b = c["base"]
        if line == b or (line.startswith(b) and not line[len(b):len(b) + 1].isdigit()):
            if best is None or len(b) > len(best):
                best = b
    return best

# ---------------- service day / day type ----------------
def fallback_day_type(d):
    if d in rs_holidays(d.year):
        return "P"
    return {5: "S", 6: "N"}.get(d.weekday(), "R")

def expand_departures(deps):
    """[{"t":"HH:MM","mark"}] in timetable order -> [(day_offset, hh, mm, mark)].
    Lists run in service order and wrap past midnight (…23:40, 00:15, 03:10) - entries after the wrap belong to
    the next calendar day. A direction with only small-hours departures (night line, e.g. 18A 00:30-03:30) is
    entirely after midnight of the service day."""
    out, prev, off = [], None, 0
    hours = [int(x["t"][:2]) for x in deps]
    night = bool(hours) and max(hours) < NIGHT_MAX_HOUR
    for x, h in zip(deps, hours):
        if prev is not None and h < prev:
            off = 1
        prev = h
        out.append((1 if night else off, h, int(x["t"][3:5]), x.get("mark")))
    return out

# ---------------- parsing ----------------
def _txt(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()

def parse_index(h):
    m = re.search(r"(?:danas voze po redu vo[žz]nje za|данас возе по реду вожње за)\s*(.*?)</p>", h, re.S | re.I)
    ind = _txt(m.group(1)).upper() if m else None
    return {"day_type": INDICATOR.get(ind), "indicator": ind,
            "vaziod": sorted(set(re.findall(r'<option value="(\d{4}-\d\d-\d\d)"', h)))}

def parse_line_options(h):
    return [html.unescape(v) for v in re.findall(r'<option value="([^"]*)">', h)]

def parse_timetables(h):
    """ispis-polazaka HTML (one or more tables) -> {base_line: {"title","directions":[{"header","departures"}],"legend"}}"""
    out = {}
    for chunk in h.split("table-title")[1:]:
        title = _txt(chunk.split(">", 1)[1].split("</div>", 1)[0])
        title = re.sub(r"^\S+\s*:\s*", "", title)          # "Linija : 8 NOVO NASELJE..."
        base = title.split(" ", 1)[0]
        ths = [_txt(t) for t in re.findall(r"<th>(.*?)</th>", chunk, re.S)]
        tds = re.findall(r"<td\s+valign='top'[^>]*>(.*?)</td>", chunk, re.S)
        dirs = []
        for th, td in zip(ths, tds):
            deps, hour = [], None
            for m in re.finditer(r"<br/><b>(\d\d)</b>|<span class='([^']*)'>(\d\d)<b>([^<]*)</b></span>", td):
                if m.group(1):
                    hour = int(m.group(1))
                    continue
                if hour is None:
                    continue
                mark = html.unescape(html.unescape(m.group(4))).strip() or None
                deps.append({"t": "%02d:%s" % (hour, m.group(3)), "mark": mark, "lowfloor": "niskopodni" in m.group(2)})
            dirs.append({"header": th, "departures": deps})
        foot = re.search(r"tabelapolascifooter[^>]*>(.*?)</td>", chunk, re.S)
        out[base] = {"title": title, "directions": dirs, "legend": _txt(foot.group(1)) if foot else None}
    return out

def destination(header):
    """'Smer B: DETELINARA - CENTAR - PETROVARADIN' -> 'Petrovaradin'; 'Smer A: Polasci za VETERNIK' -> 'Veternik'."""
    h = re.sub(r"^.*?:\s*", "", header or "")
    h = re.sub(r"(?i)^polasci za\s*", "", h)
    last = re.split(r"\s+-\s+|\s*–\s*", h)[-1].strip()
    if not last:
        return None
    return " ".join(w.upper() if re.fullmatch(r"(?i)[ivx]+", w) else w.capitalize() for w in last.split())

# ---------------- fetching + cache ----------------
def _get(path, params=None, opener=None):
    url = GSPNS + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (NStupido home board)"})
    with (opener or urllib.request.urlopen)(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")

class Timetables:
    """Thread-safe holder of the gspns index (day type per date, validity dates) and cached timetables."""

    def __init__(self, cache_dir=CACHE_DIR, fetch=None):
        self.cache_dir = cache_dir
        self.fetch = fetch or _get            # injectable for tests: fetch(path, params) -> html
        self.lock = threading.RLock()
        self.index = {"day_types": {}, "vaziod": [], "checked": None, "ok_at": None}
        self.data = {}                         # vaziod -> {"fetched", "lines": {rv: {dan: {base: table}}}}
        self.last_error = None
        self._load()

    # -- cache files --
    def _path(self, name):
        return os.path.join(self.cache_dir, name)

    def _load(self):
        try:
            with open(self._path("index.json"), encoding="utf-8") as f:
                self.index.update(json.load(f))
        except (OSError, ValueError):
            pass
        try:
            for fn in os.listdir(self.cache_dir):
                m = re.match(r"timetable_(\d{4}-\d\d-\d\d)\.json$", fn)
                if m:
                    with open(self._path(fn), encoding="utf-8") as f:
                        self.data[m.group(1)] = json.load(f)
        except (OSError, ValueError):
            pass

    def _save(self, name, obj):
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp = self._path(name + ".%d.tmp" % os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, self._path(name))

    # -- refresh --
    def refresh(self, today=None, force=False, max_age_days=7):
        """Read today's day type + validity dates, fetch timetables for validity dates not cached (or older than
        max_age_days). On any network error keep whatever is cached. Returns True if the index fetch worked."""
        today = today or date.today()
        with self.lock:
            self.index["checked"] = time.time()
        try:
            idx = parse_index(self.fetch("/red-voznje/gradski", {"selected_lang": "lat"}))
        except Exception as e:
            self.last_error = "index: %r" % e
            return False
        with self.lock:
            if idx["day_type"]:
                self.index["day_types"][today.isoformat()] = idx["day_type"]
                # keep ~60 days of history
                for k in sorted(self.index["day_types"])[:-60]:
                    del self.index["day_types"][k]
            if idx["vaziod"]:
                self.index["vaziod"] = idx["vaziod"]
            self.index["ok_at"] = time.time()
            self.index["indicator"] = idx["indicator"]
            self._save("index.json", self.index)
            todo = [v for v in self.index["vaziod"]
                    if force or v not in self.data or time.time() - self.data[v].get("fetched", 0) > max_age_days * 86400]
        for v in todo:
            try:
                lines = self._fetch_timetable(v)
            except Exception as e:
                self.last_error = "timetable %s: %r" % (v, e)
                continue
            if not any(lines.get(rv, {}).get(d) for rv in lines for d in DAY_TYPES):
                self.last_error = "timetable %s: empty" % v
                continue
            obj = {"vaziod": v, "fetched": time.time(), "lines": lines}
            with self.lock:
                self.data[v] = obj
                self._save("timetable_%s.json" % v, obj)
        return True

    def _fetch_timetable(self, vaziod):
        lines = {}
        for rv, bases in needed_lines().items():
            for dan in DAY_TYPES:
                opts = parse_line_options(self.fetch("/red-voznje/lista-linija", {"rv": rv, "vaziod": vaziod, "dan": dan}))
                want = [o for o in opts if o.rstrip(".*") in bases]
                tables = parse_timetables(self.fetch("/red-voznje/ispis-polazaka",
                                                     {"rv": rv, "vaziod": vaziod, "dan": dan, "linija[]": want})) if want else {}
                lines.setdefault(rv, {})[dan] = tables
        return lines

    def maybe_refresh(self, now=None, min_interval=3600):
        """Refresh if the day changed since the last successful index fetch, or every min_interval s if it failed."""
        now = now or time.time()
        today = date.fromtimestamp(now)
        with self.lock:
            ok_day = date.fromtimestamp(self.index["ok_at"]).isoformat() if self.index.get("ok_at") else None
            checked = self.index.get("checked") or 0
            stale = ok_day != today.isoformat() or any(v not in self.data for v in self.index["vaziod"])
        if stale and now - checked >= min_interval:
            return self.refresh(today)
        return None

    # -- queries --
    def day_type(self, d):
        with self.lock:
            return self.index["day_types"].get(d.isoformat()) or fallback_day_type(d)

    def validity_for(self, d):
        """Timetable validity date in effect on service day d: the latest cached vaziod <= d (auto-switch when a new
        timetable takes effect); if none is <= d, the oldest cached one."""
        with self.lock:
            have = sorted(self.data)
        if not have:
            return None
        past = [v for v in have if v <= d.isoformat()]
        return past[-1] if past else have[0]

    def trips(self, stop, service_day):
        """Scheduled trips passing `stop` on one service day: list of dicts with dep (datetime), base, dir, mark,
        line (nsmart name), header, day_type, vaziod, tt (configured minutes) and trip_id."""
        v = self.validity_for(service_day)
        if not v:
            return []
        dt_ = self.day_type(service_day)
        with self.lock:
            lines = self.data[v]["lines"]
        out = []
        for c in STOP_LINES.get(stop, []):
            t = lines.get(c["rv"], {}).get(dt_, {}).get(c["base"])
            if not t or c["dir"] >= len(t["directions"]):
                continue
            d = t["directions"][c["dir"]]
            for off, hh, mm, mark in expand_departures(d["departures"]):
                if mark in c["names"]:
                    name = c["names"][mark]
                elif c.get("all_marks"):
                    name = c["names"][None]
                else:
                    continue
                dep = datetime.combine(service_day + timedelta(days=off), datetime.min.time()).replace(hour=hh, minute=mm)
                out.append({"stop": stop, "base": c["base"], "dir": c["dir"], "mark": mark, "line": name,
                            "dep": dep, "header": d["header"], "day_type": dt_, "vaziod": v,
                            "tt": c["tt"].get(name, c["tt"].get(c["base"])),
                            "trip_id": "%s|%s|%d|%s" % (stop, c["base"], c["dir"], dep.strftime("%Y-%m-%dT%H:%M"))})
        return out

    def window(self, stop, now_dt, back=timedelta(hours=3), ahead=timedelta(hours=2)):
        """Trips of the previous, current and next service day whose departure is within [now-back, now+ahead]."""
        out = []
        for k in (-1, 0, 1):
            for t in self.trips(stop, now_dt.date() + timedelta(days=k)):
                if now_dt - back <= t["dep"] <= now_dt + ahead:
                    out.append(t)
        return out

    def status(self):
        with self.lock:
            return {"validity": sorted(self.data), "vaziod_online": self.index.get("vaziod"),
                    "today_day_type": self.day_type(date.today()),
                    "indicator": self.index.get("indicator"),
                    "index_ok_at": self.index.get("ok_at"), "last_error": self.last_error}
