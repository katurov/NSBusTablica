"""Route naming, stop -> line derivation and the two slow-changing reference caches for NStupido.

Sources (see docs/DATA_SOURCES.md for the why):
  nsmart getAllStations  -> every stop: name, coordinates, lines_for_station (display names, no direction)
  gspns.rs/mreza         -> network map: one button per (route, variant, direction) with a label such as
                            "1GLA" (route 1, variant GL, direction A) and the stops it serves; each map stop lists
                            all labels that serve it ("[3B],[8A],[18A]")
Naming model (Paul's, verified against the data):
  label = <route><variant><direction A|B>; the route itself may carry a letter (3A, 3B, 6A, 5N, 7A, 7B, 11A, 11B,
  18A, 18B are separate gspns timetables). A route is "distinct" iff it is a gspns timetable option. Everything
  else after the number is a variant (1GL, 14S, 9A, 54"A", 71IS ...) and is displayed as its base route.
Stdlib only.
"""
import json, math, os, re, threading, time, unicodedata
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
MAP_FILE = os.path.join(CACHE_DIR, "gspns", "map.json")
STATIONS_FILE = os.path.join(CACHE_DIR, "nsmart", "stations.json")
STATIONS_URL = "https://online.nsmart.rs/sr/AnnouncementForStation/getAllStations"
MAX_AGE_S = 14 * 86400          # reference caches older than this are refreshed ...
START_DELAY_S = 3600            # ... one hour after server start, and checked again daily at 05:00

# gspns routes whose letter is part of the route (separate timetables). Used before the gspns line lists are
# cached; the cached option lists (timetable index) extend it.
# Plain route numbers are bases too, except the loop routes that exist only lettered (7A/7B, 11A/11B, 18A/18B):
# a map label "7A" is then the loop 7A, "6A" is route 6 direction A, "6AA" route 6A direction A.
LOOP_NUMBERS = {"7", "11", "18"}
DISTINCT_FALLBACK = ({"3A", "3B", "5N", "6A", "7A", "7B", "11A", "11B", "18A", "18B"}
                     | {str(n) for n in range(1, 100) if str(n) not in LOOP_NUMBERS})


# ---------------- naming ----------------
_TRANS = str.maketrans({"Č": "C", "Ć": "C", "Ž": "Z", "Š": "S", "Đ": "DJ", "č": "c", "ć": "c", "ž": "z", "š": "s",
                        "đ": "dj"})


def norm(s):
    """Variant / mark code -> comparable ASCII ("ŽA"->"ZA", '"A"'->"A", "Đ"->"DJ")."""
    s = (s or "").translate(_TRANS).upper()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]", "", s)


def words(s):
    """Place name -> set of comparable words of >= 3 letters ("Z.STANICA" -> {"STANICA"})."""
    return {w for w in re.split(r"[^A-Z0-9]+", norm_keep_space(s)) if len(w) >= 3}


def norm_keep_space(s):
    s = (s or "").translate(_TRANS).upper()
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


class Naming:
    """Knows the distinct gspns routes; turns nsmart names and map labels into (base route, variant, direction)."""

    def __init__(self, bases=()):
        self.lock = threading.Lock()
        self.bases = set(DISTINCT_FALLBACK) | set(bases)

    def update(self, bases):
        with self.lock:
            self.bases |= {b for b in bases if b}

    def _prefix_base(self, s):
        """Longest known base that prefixes s with the rest not starting with a digit."""
        with self.lock:
            bases = list(self.bases)
        best = None
        for b in bases:
            if s.startswith(b) and not s[len(b):len(b) + 1].isdigit() and (best is None or len(b) > len(best)):
                best = b
        return best

    def base(self, line):
        """nsmart line name / gspns option -> base route: '14S'->'14', '3A'->'3A', '54"A"'->'54', '9A'->'9'."""
        line = (line or "").strip().rstrip(".*")
        b = self._prefix_base(line)
        if b:
            return b
        m = re.match(r"\d+", line)
        return m.group(0) if m else line

    def display(self, line):
        """What the board shows: the base route (letters only for distinct routes)."""
        return self.base(line)

    def is_distinct(self, base):
        with self.lock:
            return base in self.bases

    def parse_label(self, label):
        """Map button label -> (base, variant, letter): '1GLA'->('1','GL','A'), '3AA'->('3A','','A'),
        '3B'->('3','','B'), '9AA'->('9','A','A'), '7A'->('7A','','A'), '11ĐB'->('11B','Đ','B')."""
        label = (label or "").strip()
        if not label or label[-1] not in "AB":
            return self.base(label), label[len(self.base(label)):], None
        letter, rest = label[-1], label[:-1]
        b = self._prefix_base(rest)
        if b:
            return b, rest[len(b):], letter
        m = re.match(r"\d+", rest)
        num = m.group(0) if m else rest
        if self.is_distinct(num + letter):          # loop routes: 7A/7B, 11A/11B, 18A/18B
            return num + letter, rest[len(num):], letter
        return num, rest[len(num):], letter


def mark_variant(mark, known):
    """gspns departure mark -> map variant code (normalised) or None if it cannot be mapped.
    known: normalised variants of that route+direction on the whole map ("" = plain route).
    Tries the mark itself, without a low-floor prefix (NRG -> G, NRS -> S, NĐ -> DJ), a school-trip suffix
    (DDJ, MDJ -> DJ), then a unique prefix relation (G -> GL, ALI -> A)."""
    if mark is None:
        return "" if "" in known else None
    m = norm(mark)
    cands = [m]
    for p in ("NR", "N"):
        if m.startswith(p) and len(m) > len(p):
            cands.append(m[len(p):])
    if m.endswith("DJ") and len(m) > 2:
        cands.append("DJ")
    for c in cands:
        if c in known:
            return c
    nonempty = [k for k in known if k]
    for c in cands:
        hits = [k for k in nonempty if k.startswith(c) or c.startswith(k)]
        if len(hits) == 1:
            return hits[0]
    return None


def destination_words(text):
    """Header 'Смер B: DETELINARA - CENTAR - PETROVARADIN' / 'Polasci za FUTOG GRMEČKA' / map title
    'NOVI SAD (ŽELEZNIČKA STANICA) - FUTOG (GRMEČKA)' -> words of the destination."""
    t = re.sub(r"^[^:]*:\s*", "", text or "") if ":" in (text or "")[:12] else (text or "")
    t = re.sub(r"\([^)]*\)", " ", t)
    if re.match(r"(?i)\s*polasci za\s+", t):
        return words(re.sub(r"(?i)^\s*polasci za\s+", "", t))
    if re.match(r"(?i)\s*polasci iz\s+", t):
        return {"NOVI", "SAD"}
    parts = [p for p in re.split(r"\s+[-–]\s+|\s*–\s*", t) if p.strip()]
    return words(parts[-1]) if parts else set()


def header_letter(header):
    m = re.match(r"\s*(?:Smer|Смер)\s+([AB]|[АБ])\b", header or "")
    if not m:
        return None
    return {"А": "A", "Б": "B"}.get(m.group(1), m.group(1))


def choose_column(directions, letter=None, title=None):
    """Index of the timetable column that serves the stop, or None.
    1. the map button title's destination matches the column header's destination (robust: 3A's only column is
       labelled 'Smer B' although the map calls that trip 3AA);
    2. else the direction letter; 3. a single column without contrary evidence."""
    if not directions:
        return None
    want = destination_words(title) if title else set()
    if want:
        scores = [len(want & destination_words(d.get("header"))) for d in directions]
        best = max(scores)
        if best > 0 and scores.count(best) == 1:
            return scores.index(best)
        if len(directions) == 1:
            return None                       # the only column goes somewhere else (e.g. 3A towards Ž.stanica)
    if letter:
        for i, d in enumerate(directions):
            if header_letter(d.get("header")) == letter:
                return i
    return 0 if len(directions) == 1 else None


# ---------------- reference caches ----------------
def cache_due(fetched_ts, now, started_at, max_age_s=MAX_AGE_S, delay_s=START_DELAY_S):
    """14-day rule: refresh if the cache is missing or older than max_age_s, but never earlier than delay_s
    after server start (restart loops must not hammer upstream)."""
    if now - started_at < delay_s:
        return False
    return not fetched_ts or now - fetched_ts > max_age_s


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".%d.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


class Stations:
    """nsmart getAllStations, cached in cache/nsmart/stations.json as {"fetched", "stations": {uid: {...}}}."""

    def __init__(self, path=STATIONS_FILE, fetch=None):
        self.path, self._fetch = path, fetch
        self.lock = threading.RLock()
        self.fetched, self.stations, self.last_error = None, {}, None
        d = _load_json(path)
        if isinstance(d, dict) and isinstance(d.get("stations"), dict):
            self.fetched, self.stations = d.get("fetched"), d["stations"]

    @staticmethod
    def compact(raw):
        """getAllStations list -> {uid: {name, code, lat, lon, lines, types}}"""
        out = {}
        for s in raw or []:
            if not isinstance(s, dict) or s.get("id") is None:
                continue
            c = s.get("coordinates") or {}
            types = {}
            for a in s.get("lines_for_station_additional_data") or []:
                if isinstance(a, dict) and a.get("line_number_for_display"):
                    types[a["line_number_for_display"]] = a.get("line_type")
            out[str(s["id"])] = {"name": s.get("name"), "code": s.get("station_id"),
                                 "lat": _f(c.get("latitude")), "lon": _f(c.get("longitude")),
                                 "lines": list(s.get("lines_for_station") or []), "types": types}
        return out

    def refresh(self):
        """One request (caller paces it through the nsmart gate). Keeps the old cache on failure."""
        try:
            raw = self._fetch()
            st = self.compact(raw)
            if len(st) < 100:
                raise ValueError("only %d stations" % len(st))
        except Exception as e:
            self.last_error = repr(e)
            return False
        with self.lock:
            self.stations, self.fetched, self.last_error = st, time.time(), None
            _save_json(self.path, {"fetched": self.fetched, "source": STATIONS_URL, "stations": st})
        return True

    def get(self, uid):
        with self.lock:
            return self.stations.get(str(uid))

    def name(self, uid):
        s = self.get(uid)
        return s.get("name") if s else None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dist_m(lat1, lon1, lat2, lon2):
    k = math.pi / 180
    x = (lon2 - lon1) * k * math.cos((lat1 + lat2) / 2 * k)
    return math.hypot(x, (lat2 - lat1) * k) * 6371000


MAP_PAGE = "/mreza"
MAP_STOPS = "/mreza-get-stajalista-tacke"
BUTTON_RE = re.compile(r'<a id="(\d+)"[^>]*class="button-linija (\w+)[^"]*" title="([^"]*)"\s*>\s*([^<]+?)\s*</a>')


def parse_buttons(html_text):
    import html as _h
    return [{"id": i, "kind": k, "title": _h.unescape(t).strip(), "label": _h.unescape(l).strip()}
            for i, k, t, l in BUTTON_RE.findall(html_text or "")]


def parse_map_stops(text):
    """mreza-get-stajalista-tacke body (JSON list of 'labels|lon|lat|name|photo|zone') -> list of dicts."""
    i = (text or "").find("[")
    arr = json.loads(text[i:]) if i >= 0 else []
    out = []
    for s in arr:
        p = str(s).split("|")
        if len(p) < 4:
            continue
        out.append({"labels": re.findall(r"\[([^\]]+)\]", p[0]), "lon": _f(p[1]), "lat": _f(p[2]), "name": p[3].strip()})
    return out


class NetworkMap:
    """gspns.rs/mreza cached in cache/gspns/map.json: {"fetched", "buttons": [...], "stops": {button_id: [...]}}.
    Refresh is sequential through the gspns pacing (fetch(path, params) -> text), keeps old data on failure."""

    def __init__(self, path=MAP_FILE, fetch=None, min_buttons=50):
        self.path, self._fetch, self.min_buttons = path, fetch, min_buttons
        self.lock = threading.RLock()
        self.fetched, self.buttons, self.stops, self.last_error = None, [], {}, None
        self.partial_at = None
        d = _load_json(path)
        if isinstance(d, dict):
            self.fetched, self.buttons, self.stops = d.get("fetched"), d.get("buttons") or [], d.get("stops") or {}
            self.partial_at = d.get("partial_at")
        self._index = None

    def refresh(self):
        """-> True if the whole map was fetched. Partial failures keep the old stops of those buttons and the old
        "fetched" stamp (so the age rule tries again)."""
        try:
            buttons = parse_buttons(self._fetch(MAP_PAGE, None))
            if len(buttons) < self.min_buttons:
                raise ValueError("only %d map buttons" % len(buttons))
        except Exception as e:
            self.last_error = "map page: %r" % e
            return False
        with self.lock:
            old = dict(self.stops)
        stops, failed = {}, 0
        for b in buttons:
            try:
                stops[b["id"]] = parse_map_stops(self._fetch(MAP_STOPS, {"linija": b["id"]}))
            except Exception as e:
                failed += 1
                self.last_error = "map button %s: %r" % (b["label"], e)
                if b["id"] in old:
                    stops[b["id"]] = old[b["id"]]
        with self.lock:
            self.buttons, self.stops, self._index = buttons, stops, None
            if failed == 0:
                self.fetched, self.partial_at, self.last_error = time.time(), None, None
            else:
                self.partial_at = time.time()
            _save_json(self.path, {"fetched": self.fetched, "partial_at": self.partial_at, "buttons": buttons,
                                   "stops": stops})
        return failed == 0

    def index(self):
        """Physical map stops: [{lat, lon, name, labels(set)}] (same coordinates + same label set = one stop)."""
        with self.lock:
            if self._index is not None:
                return self._index
            seen = {}
            for bid, lst in self.stops.items():
                for s in lst:
                    if s.get("lat") is None or s.get("lon") is None:
                        continue
                    k = (round(s["lat"], 6), round(s["lon"], 6), ",".join(s["labels"]))
                    seen.setdefault(k, {"lat": s["lat"], "lon": s["lon"], "name": s["name"], "labels": set(s["labels"])})
            self._index = list(seen.values())
            return self._index

    def button_titles(self):
        with self.lock:
            return {b["label"]: b for b in self.buttons}

    def match(self, lat, lon, bases, naming, radius_m=120):
        """Map stop for an nsmart stop: most shared base routes within radius_m, then nearest.
        (6712 and 15889 have identical coordinates: the city map stop shares the city lines, the suburban one
        the suburban lines.)"""
        if lat is None or lon is None:
            return None
        best, best_key = None, None
        for s in self.index():
            d = _dist_m(lat, lon, s["lat"], s["lon"])
            if d > radius_m:
                continue
            sb = {naming.parse_label(l)[0] for l in s["labels"]}
            overlap = len(sb & set(bases)) if bases else 0
            if bases and overlap == 0:
                continue
            if not bases and d > 30:
                continue
            key = (overlap, -d)
            if best_key is None or key > best_key:
                best, best_key = dict(s, distance_m=round(d, 1), overlap=overlap), key
        return best


# ---------------- stop -> lines ----------------
class Resolver:
    """Which gspns (route, column, marks) pass a stop. Manual STOP_LINES (default stops) win; otherwise derived from
    nsmart lines_for_station + the map stop's variant-direction labels; routes seen live but unknown to both are
    learned from nsmart actual_line_number (trailing 'B' = direction B)."""

    def __init__(self, naming, stations, netmap, manual=None, rv_of=None):
        self.naming, self.stations, self.map = naming, stations, netmap
        self.manual = manual or {}
        self.rv_of = rv_of or (lambda base: None)
        self.lock = threading.RLock()
        self.learned = {}           # uid -> {base: {"letter", "seen": {normalised variant suffixes}, "line"}}
        self.cache = {}             # uid -> derived entries
        self.info = {}              # uid -> derivation notes (debug / docs)

    def invalidate(self, uid=None):
        with self.lock:
            if uid is None:
                self.cache.clear()
            else:
                self.cache.pop(uid, None)

    def __call__(self, uid, use_manual=True):
        uid = str(uid)
        if use_manual and uid in self.manual:
            extra = self._learned_entries(uid, {c["base"] for c in self.manual[uid]}, None)
            return list(self.manual[uid]) + extra
        with self.lock:
            if use_manual and uid in self.cache:
                return self.cache[uid]
        ent = self.derive(uid)
        if use_manual and self.stations.get(uid):          # unknown stop: try again once the station list is there
            with self.lock:
                self.cache[uid] = ent
        return ent

    def _network_variants(self):
        """(base, letter) -> {normalised variants on the whole map}; label -> button."""
        net = {}
        for label in self.map.button_titles():
            b, v, l = self.naming.parse_label(label)
            net.setdefault((b, l), set()).add(norm(v))
        return net

    def _rv(self, base, kind=None, ltype=None):
        rv = self.rv_of(base)
        if rv:
            return rv
        if kind:
            return "rvg" if kind == "grad" else "rvp"
        if ltype is not None:
            return "rvg" if str(ltype) == "1" else "rvp"
        m = re.match(r"\d+", base)
        return "rvg" if m and int(m.group(0)) < 21 else "rvp"

    def derive(self, uid):
        st = self.stations.get(uid) or {}
        lines = st.get("lines") or []
        bases = {self.naming.base(l) for l in lines}
        ms = self.map.match(st.get("lat"), st.get("lon"), bases, self.naming) if st else None
        titles = self.map.button_titles()
        net = self._network_variants()
        groups = {}                 # (base, letter) -> {"variants": set, "labels": [], "title", "kind"}
        for label in sorted(ms["labels"]) if ms else []:
            b, v, l = self.naming.parse_label(label)
            if bases and b not in bases:
                continue
            g = groups.setdefault((b, l), {"variants": set(), "labels": [], "title": None, "kind": None})
            g["variants"].add(norm(v))
            g["labels"].append(label)
            btn = titles.get(label) or {}
            if btn and (g["title"] is None or not v):
                g["title"], g["kind"] = btn.get("title"), btn.get("kind")
        out = []
        for (b, l), g in sorted(groups.items()):
            out.append(self._entry(uid, b, l, g["title"], g["variants"], net.get((b, l), set()), lines,
                                   self._rv(b, g["kind"], (st.get("types") or {}).get(b)), "map", g["labels"]))
        covered = {b for b, _ in groups}
        pending = sorted(bases - covered)
        out += self._learned_entries(uid, covered, ms, net, lines)
        with self.lock:
            self.info[uid] = {"name": st.get("name"), "nsmart_lines": lines,
                              "map_stop": ({k: ms[k] for k in ("name", "distance_m", "overlap")} if ms else None),
                              "map_labels": sorted(ms["labels"]) if ms else [], "pending_live": pending}
        return out

    def _entry(self, uid, base, letter, title, stop_vars, net_vars, lines, rv, source, labels=(), forced=()):
        forced = set(forced)

        def allow(mark):
            v = mark_variant(mark, net_vars)
            if v is None:
                return True                       # unknown mark: include (school/factory trips are shown)
            return v in stop_vars or v in forced

        cand_lines = [l for l in lines if self.naming.base(l) == base]

        def name_of(mark):
            if mark is None:
                return base
            m = norm(mark)
            tries = [m] + [m[len(p):] for p in ("NR", "N") if m.startswith(p) and len(m) > len(p)]
            for t in tries:
                for l in cand_lines:
                    if norm(l[len(base):]) == t:
                        return l
            return base

        return {"base": base, "rv": rv, "dir": None, "letter": letter, "title": title, "allow": allow,
                "name_of": name_of, "tt": {}, "source": source, "labels": list(labels),
                "stop_variants": sorted(stop_vars), "forced": sorted(forced)}

    def _learned_entries(self, uid, covered, ms, net=None, lines=None):
        with self.lock:
            lv = dict(self.learned.get(uid, {}))
        if not lv:
            return []
        net = net if net is not None else self._network_variants()
        if lines is None:
            lines = (self.stations.get(uid) or {}).get("lines") or []
        out = []
        for b, x in sorted(lv.items()):
            if b in covered:
                continue
            lines2 = list(lines) + [x["line"]]
            stop_vars = set()
            if ms is None:
                stop_vars = set(net.get((b, x["letter"]), set()))       # no map evidence -> everything counts
            out.append(self._entry(uid, b, x["letter"], None, stop_vars, net.get((b, x["letter"]), set()), lines2,
                                   self._rv(b), "live", (), x["seen"]))
        return out

    def observe(self, uid, line, actual):
        """A live bus of nsmart `line` (actual_line_number e.g. '542B') passes watched stop uid. Returns the base if
        that route was not known for the stop (caller fetches its timetable), else None."""
        if not line:
            return None
        uid = str(uid)
        b = self.naming.base(line)
        known = {c["base"] for c in self(uid)}
        suffix = norm(line[len(b):]) if line.startswith(b) else ""
        letter = "B" if str(actual or "").strip().upper().endswith("B") else "A"
        with self.lock:
            cur = self.learned.get(uid, {}).get(b)
            if b in known and cur is None:
                return None
            if cur is not None:
                if suffix in cur["seen"]:
                    return None
                cur["seen"].add(suffix)
                self.cache.pop(uid, None)
                return None
            self.learned.setdefault(uid, {})[b] = {"letter": letter, "seen": {suffix}, "line": line, "actual": actual}
            self.cache.pop(uid, None)
        return b

    def describe(self, uid, use_manual=True):
        """JSON-able view of the entries (debug, comparisons)."""
        out = []
        for c in self(uid, use_manual=use_manual):
            out.append({k: (sorted(v) if isinstance(v, set) else v) for k, v in c.items()
                        if k not in ("allow", "name_of") and not callable(v)})
        return out
