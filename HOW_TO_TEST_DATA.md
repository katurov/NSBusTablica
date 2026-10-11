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

or route naming / new stops only:

```bash
python3 -m unittest tests.test_routes -v
```

**Pass** = day type, midnight, `vaziod` change, trip matching, feeders, route names (`14S` → `14`, `3A` stays), map matching, mark rules, 14-day cache rule, new-stop flow — logic OK.
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
| `buses:[]`, no errors, `--schedule-only` also empty | window without trips / no lines derived for that stop (check `/debug` → `lines.<uid>.derivation`: no `map_stop`? empty `nsmart_lines`?) |
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

`/debug` shows `timetable` (vaziod, day_type, last_error), `lines` per watched stop (entries, derivation, `manual`), `pending_timetables`, `reference` cache stamps, feeders, learn.
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

## 6. OLED Board (WiFi, v4)

1. Server running: `./nstupido-server.sh status` (start: `./nstupido-server.sh start`; on the Mac `PYTHON=/opt/homebrew/bin/python3.10`).
2. Same URL the board uses: `curl -s http://nstupido.teamof.top:8080/` (200, `"ready": true`) and `curl -s "http://nstupido.teamof.top:8080/frame?stop=6539"` (lines `H|…`, `R|…`, `E`).
3. Board log over USB (nothing else may hold the port): `pio device monitor` in `firmware/` → `[nstupido] GET http://…/frame?stop=6539 -> 200` every 5 s. `-1` = server unreachable (DNS / firewall / server down), `503` = server starting.
4. Server side: `tail nstupido.log` (`client 192.168.42.91 first request: /frame?stop=6539 -> 200`, `http: /frame 200 x360` every 30 min); `GET /` shows queue, backoff, watch list, `http` counts and the last request per client.
5. Legacy USB bridge: `python3 oled_bridge.py --stop 6539` (requires `pyserial`); don't open the serial monitor at the same time.
6. Brightness: `OLED_CONTRAST` in `firmware/platformio.ini` (currently 40 out of 255).

## 7. New Stop / Route Derivation (v4.1)

1. Ask for a stop that is not a default (busy city examples: 6660 Bulevar Mihajla Pupina-Pothodnik, 6678 Rumenačka-Kornelija Stankovića, 6685 Bulevar Cara Lazara-Urbis):

   ```bash
   for i in $(seq 12); do curl -s "http://127.0.0.1:8080/frame?stop=6660" | head -1; sleep 5; done
   ```

   Expect `H|6660|HH:MM|W` for ~15 s (live poll + paced timetable fetch), then no `W` and one `R|` row per base route.
2. `tail nstupido.log` → `timetables for stop 6660: ok (rvg 1,12,6)` (only lines never cached are fetched).
3. `curl -s http://127.0.0.1:8080/debug | python3 -c 'import json,sys; d=json.load(sys.stdin)["lines"]["6660"]; print(d["derivation"]); [print(e["base"], e["letter"], e["labels"], e["source"]) for e in d["entries"]]'`
   → `map_stop` within a few metres, labels like `1A 1GLA 1JA 3B 4B 6B 6DB 8A 12A 18A`.
4. `/buses?stop=6660`: `line` is the base route, `nsmart_line` the variant (`1GL`), scheduled entries have `entry: "map"`.
5. Offline comparison of the automatic derivation with the hand-made `STOP_LINES` (no requests): `python3 tools/compare_lines.py [--date 2026-10-12] [6539 6712 15889]`. Expected today: identical for 6539 and 6712; 15889 differs by one `55 KN` weekday trip (see docs/DATA_SOURCES.md).
6. Reference caches: `GET /debug` → `reference.stations_fetched` / `map_fetched` (epoch s); start log line `reference caches: station list 5.1 days old, network map 5.1 days old …`. They refresh 1 h after start only if older than 14 days.

## 8. Agent Success Checklist

- [ ] `python3 -c "import nstupido, timetable, routes, rs_holidays"` without errors
- [ ] Offline: `unittest discover -s tests` — `test_server`, `test_schedule`, `test_routes`, `test_bridge`, `test_firmware_frame` green
- [ ] A non-default stop warms up and then shows P rows for all its routes (§7)
- [ ] Live: `tests.test_live_sources` — all 5 green (or clearly indicated that network from box/machine is unavailable)
- [ ] CLI `nstupido.py 6539 --pretty` returns JSON with `stop` + `buses` (or meaningful `error`)
- [ ] If needed: `/`, `/buses`, `/frame` and `/debug` respond 200
- [ ] If fixed upstream parsing — update fixtures in `tests/fixtures/`, don't mock live tests

No API keys. Sources are undocumented and may change without notice.
