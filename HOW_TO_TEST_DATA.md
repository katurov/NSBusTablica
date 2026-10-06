# HOW_TO_TEST_DATA — Agent Playbook

Goal: quickly understand whether **live data is coming through** and **which source is broken**.
Run from project root (`/…/NStupido`).

## 1. Offline Unit Tests (No Network)

```bash
python3 -m unittest discover -s tests -v
```

or schedule/memory only:

```bash
python3 -m unittest tests.test_schedule -v
```

**Pass** = day type, midnight, `vaziod` change, trip matching, feeders — logic OK.
**Fail** = regression in code, not upstream. Fix code / fixtures in `tests/fixtures/`.

## 2. Live Connectivity (Network Required)

```bash
python3 -m unittest tests.test_live_sources -v
```

or together with all tests:

```bash
python3 -m unittest discover -s tests -v
```

| Test | What It Checks | Pass | Fail |
|---|---|---|---|
| `test_nsmart_arrivals_reachable` | `POST https://online.nsmart.rs/sr/najava-dolaska/` (station_uid=6539) | HTTP 200 + JSON-**list** (empty stop = `[{just_coordinates…}]` **OK**) | timeout / 5xx / HTML / not JSON / `[false,…]` |
| `test_nsmart_stations_list_reachable` | `POST …/AnnouncementForStation/getAllStations` | JSON list/dict with at least one station (`id`/`name`) | network / empty response |
| `test_gspns_gradski_page_reachable` | `GET http://gspns.rs/red-voznje/gradski` | 200 + markers `danas` / `vaziod` / `red vožnje` | site down / different HTML |
| `test_gspns_timetable_fetchable` | `ispis-polazaka` for line 3 (dan=R or today) | Departures HH:MM parsed | empty tables / network |
| `test_nstupido_cli_returns_json` | `nstupido.fetch("6539")` | dict with `buses` (list, may be `[]`) | traceback / `error` in response |

Fail message always contains URL/host — shows which upstream is dead.

## 3. Manual CLI Smoke Test

```bash
python3 nstupido.py 6539 --pretty
# without saving state:
python3 nstupido.py 6539 --pretty --no-state
# schedule only (without nsmart):
python3 nstupido.py 6539 --pretty --schedule-only
```

**Healthy JSON** (one stop):

- has `"stop": {"uid":"6539","name":…}` and `"buses":[…]`;
- `buses` may contain entries with `status`: `live` / `lost` / `scheduled`;
- **not** just `"error":{"type":"network",…}` without buses from memory;
- empty `"buses":[]` with working nsmart + daytime without scheduled trips in 40 min window — rare but happens; then check `--schedule-only` and another stop (`15889`, `6712`).

Symptoms:

| Symptom | Meaning |
|---|---|
| `error.type=network` / DNS / timeout | API/site unreachable from this machine |
| `error.type=http` / `api` / `bad_response` | nsmart responded badly (not login HTML page — we have no auth) |
| `buses:[]`, no errors, `--schedule-only` also empty | window without trips / `STOP_LINES` doesn't cover lines |
| Only `scheduled`, no `live`/`lost` | nsmart empty (often normal), schedule works |
| Has `live` with `source:"via 6540"` | Feeders work, stop itself empty |

## 4. HTTP Server Smoke Test

```bash
python3 nstupido.py --serve --port 8080 --host 127.0.0.1
# in another terminal:
curl -sS http://127.0.0.1:8080/buses | python3 -m json.tool | head -80
curl -sS "http://127.0.0.1:8080/buses?stop=6539" | python3 -m json.tool | head -60
curl -sS http://127.0.0.1:8080/debug | python3 -m json.tool | head -80
```

`/debug` shows `timetable` (vaziod, day_type, last_error), feeders, learn.
502 on `/buses` = all requested stops errored and nothing to show.

## 5. Direct Upstream Probes (If Tests Are Red)

```bash
# nsmart arrivals
python3 -c "
import urllib.request, urllib.parse, json
d=urllib.parse.urlencode({'station_uid':'6539','ibfm':'TS001831','direction':2,'company_info_id':216,'radius':''}).encode()
r=urllib.request.Request('https://online.nsmart.rs/sr/najava-dolaska/', data=d,
  headers={'User-Agent':'NStupido','X-Requested-With':'XMLHttpRequest',
           'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'})
print(json.load(urllib.request.urlopen(r, timeout=15)).keys())
"

# gspns index
curl -sS 'http://gspns.rs/red-voznje/gradski?selected_lang=lat' | head -c 2000

# gspns timetable (substitute current vaziod from index / cache/gspns/index.json)
curl -sS 'http://gspns.rs/red-voznje/ispis-polazaka?rv=rvg&vaziod=2026-10-01&dan=R&linija%5B%5D=3.' | head -c 2000
```

## 6. OLED / USB Bridge

1. Server: `python3 nstupido.py --serve --port 8080`
2. Firmware already uploaded (`pio run -t upload` in `firmware/`).
3. Bridge: `pip install -r requirements.txt` → `python3 oled_bridge.py --stop 6539`
4. **Don't open** the same serial port in `pio device monitor` / another process while bridge holds it — will get «veza izgubljena» / port failure.
5. Brightness: `OLED_CONTRAST` in `firmware/platformio.ini` (currently 80 out of 255).

## 7. Agent Success Checklist

- [ ] `python3 -c "import nstupido, timetable, rs_holidays"` without errors
- [ ] Offline: `unittest discover -s tests` — all `test_schedule` green
- [ ] Live: `tests.test_live_sources` — all 5 green (or clearly indicated that network from box/machine is unavailable)
- [ ] CLI `nstupido.py 6539 --pretty` returns JSON with `stop` + `buses` (or meaningful `error`)
- [ ] If needed: `/buses` and `/debug` respond 200
- [ ] If fixed upstream parsing — update fixtures in `tests/fixtures/`, don't mock live tests

No API keys. Sources are undocumented and may change without notice.
