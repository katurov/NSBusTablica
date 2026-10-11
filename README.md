# NStupido

Home display for upcoming buses in GSP Novi Sad: live nsmart data + memory between snapshots + official gspns.rs schedule → compact JSON, HTTP API, and a **WiFi** OLED 128×64 board (Wemos S2 mini). A research prototype built for fun, together with Grok Bot.

## v4 in short

- **Server** (`nstupido.py --serve`, config in `config.toml`) polls nsmart politely through one queue (≤ 1 request / 0.4 s, at most 100 per 40 s cycle), keeps everything in memory except the timetable cache, and serves JSON (`/buses`) plus ready-made text frames for the board (`/frame`).
- **Board** connects to WiFi and fetches `GET <SERVER_URL>/frame?stop=<uid>` every 5 s; BOOT button cycles the compiled stop list; more than 4 routes → pages.
- **USB bridge** (`oled_bridge.py`) is legacy/optional: the v4 firmware still draws frames arriving over USB serial.
- **v4.1 – any stop:** ask for any nsmart stop and, after ~15 s of `"warming"`, get the full list: live buses (G/E) plus the official timetable (P) of *every* line through that stop. Lines come from the nsmart station list, their direction from the gspns network map, missing timetables are downloaded on the spot (paced). The board shows the **base route** only (`14S` → `14`, `1GL` → `1`); letters stay only where they name a different route (`3A`, `7B`, `18A`).

Design spec and status: [docs/WIFI_PLAN.md](docs/WIFI_PLAN.md). Data sources, the route naming model and *why* things are done this way: **[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)**.

## Project Structure

| Path | Role |
|---|---|
| `nstupido.py` | Core: fetch nsmart, memory, feeders, schedule merge, polling queue, HTTP server, CLI |
| `config.toml` | Server config: default stops, host/port, polling limits, timetable refresh, logging |
| `nstupido-server.sh` | Start/stop the server detached (`start`/`stop`/`restart`/`status`) |
| `timetable.py` | Load/cache gspns.rs, day type, hand-made `STOP_LINES` (default stops), scheduled departures, on-demand fetch of missing lines |
| `routes.py` | Route naming (base / variant / direction), nsmart station list + gspns network map caches, stop → lines derivation, live learning |
| `tools/compare_lines.py` | Offline check: automatic stop → lines derivation vs hand-made `STOP_LINES`, departure by departure |
| `rs_holidays.py` | Republic of Serbia holidays (incl. Orthodox Easter) for fallback day type |
| `firmware/` | PlatformIO project for the WiFi OLED board; `include/frame.h` = frame parser + pager (host-testable) |
| `firmware/secrets.ini.example` | Template for the untracked `firmware/secrets.ini` (WiFi SSID / password) |
| `oled_bridge.py` | Legacy USB bridge: reads `/buses`, sends frames over serial |
| `tests/` | Offline unit tests (server, schedule, bridge, firmware frame) + live connectivity (`test_live_sources.py`) |
| `docs/WIFI_PLAN.md` | v4 design and implementation notes |
| `docs/DATA_SOURCES.md` | Sources, route naming model, how they are combined, caching / rate-limit / display decisions and why |
| `HOW_TO_TEST_DATA.md` | Agent playbook: how to verify data is coming through |
| `requirements.txt` | Optional deps (`pyserial` for the bridge, `tomli` on Python < 3.11 optional) |
| `cache/gspns/` | **runtime**: timetables + index (`timetable_<vaziod>.json`, `index.json`), network map (`map.json`) |
| `cache/nsmart/stations.json` | **runtime**: nsmart station list (names, coordinates, lines per stop) |
| `nstupido.log`, `nstupido.pid` | **runtime**: server log / PID when started with `nstupido-server.sh` |

## Data Sources

| Source | How | Auth |
|---|---|---|
| **nsmart** live | `POST https://online.nsmart.rs/sr/najava-dolaska/` (`station_uid`, …) | none |
| **nsmart** station list | `POST …/AnnouncementForStation/getAllStations` (names, coordinates, `lines_for_station`) | none |
| **gspns** day type / vaziod | `GET http://gspns.rs/red-voznje/gradski` | none |
| **gspns** line list | `GET …/red-voznje/lista-linija?rv=&vaziod=&dan=` | none |
| **gspns** departures | `GET …/ispis-polazaka?rv=&vaziod=&dan=&linija[]=` | none |
| **gspns** network map | `GET http://gspns.rs/mreza` (buttons) + `GET /mreza-get-stajalista-tacke?linija=<id>` (stops + labels) | none |
| **holidays** | `rs_holidays.py` (RS law) | — |

Endpoints are undocumented and may change without notice.

## Display Status Types

| `status` | `label` | OLED | Meaning |
|---|---|---|---|
| `live` | Sveže | `G` | Bus in fresh snapshot (this stop or previous one) |
| `lost` | Videli-izgubili | `E` | Seen earlier, now estimating from memory |
| `scheduled` | Po rasporedu | `P` | Departure from schedule, not yet seen |

## Default Stops

| uid | Code | Name | Lines |
|---|---|---|---|
| 6539 | 0220B | Bulevar kralja Petra prvog-Mašinska škola | 18A, 3, 8 |
| 15889 | 0509-1A | Bulevar Oslobodjenja … prigrad (suburban) | 52–56, 60–64, 68, 69, 71–74, 76–81, 84, 86 (+IS) |
| 6712 | 0509A | Bulevar Oslobođenja - Bulevar Kralja Petra prvog (city) | 4, 7A, 10/10MAL, 14/14S/14GS, 15, 18A, 19, 3A, 5N |

15889 and 6712 are ~3 m apart, ~385 m from 6539. Other useful stops: 6551 (opposite 6539), 6709 (other side of boulevard).

Any other stop: just ask for it (`/buses?stop=<uid>`, `/frame?stop=<uid>`, or add it to the board's `STOPS`); the server starts polling it (live + feeders) on the first request and derives its lines automatically (station list + network map), fetching missing timetables right away. The three default stops keep their hand-made `STOP_LINES` in `timetable.py` as an override (the automatic derivation reproduces them, see [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md#automatic-vs-hand-made-lines)); default stops: `[stops] default` in `config.toml`.

## Server

### Run it (on the host behind `nstupido.teamof.top`)

`nstupido.teamof.top` currently resolves (LAN DNS) to `192.168.42.56`, Paul's Mac, so the server runs there on port 8080. Any Linux/macOS host with Python ≥ 3.9 works the same way:

```bash
git clone https://github.com/katurov/NSBusTablica.git NStupido && cd NStupido
# edit config.toml if needed (default stops, port)
./nstupido-server.sh start            # detached; PYTHON=/opt/homebrew/bin/python3.10 ./nstupido-server.sh start on the Mac
./nstupido-server.sh status
curl -s http://nstupido.teamof.top:8080/ ; curl -s "http://nstupido.teamof.top:8080/frame?stop=6539"
./nstupido-server.sh stop
```

Foreground (debug): `python3 nstupido.py --serve` (`--port`, `--host`, `--config` override `config.toml`). The macOS firewall must allow incoming connections for the Python binary. For port 80 put a reverse proxy in front (or set `port = 80` where the OS allows it) and change the board's `SERVER_URL`.

### Behaviour

- **Watch list:** default stops (config) never expire. Any other stop is added on its first request and dropped 4 h after its last request (its feeders leave too, unless shared).
- **Unknown stop:** answered immediately with what exists and `"warming": true`; it jumps the queue for its first live request, and its lines are derived (nsmart `lines_for_station` → gspns map labels of the matching map stop → route, direction, variants). Timetables of lines never fetched before are downloaded at once (one request every 1–2 s, about 8 requests per kind of line) and `"warming"` stays on until they are in. If nsmart says the stop doesn't exist → `404` afterwards.
- **Route seen live but unknown:** if a live bus passing a watched stop belongs to a route that the map/timetables don't place there, its direction is learned from nsmart `actual_line_number` (trailing `B` = direction B) and its timetable is fetched — without waiting for a map refresh.
- **Reference caches:** the nsmart station list (`cache/nsmart/stations.json`) and the gspns network map (`cache/gspns/map.json`) change rarely. A cache older than 14 days (or missing) is refreshed 1 hour after server start, and the age is re-checked daily at 05:10 for long-running servers. A failed refresh keeps the old cache. Exception: with no station list at all, it is fetched once immediately (one request), because nothing works without it.
- **Queue:** each stop is a group = the stop followed immediately by its feeder stops; shared feeders are requested once per cycle. With `N` requests per cycle the spacing is `max(0.4 s, 40 s / N)` (8 requests → one every 5 s). Capacity `40 / 0.4 = 100` requests per cycle; a new stop is accepted only if it fits (a never-seen stop is counted as 1 + 3 assumed feeders), else `429`. Watched stops are always served.
- **Backoff:** global; after an nsmart failure the next request waits 5, 10, 20, 40, 80, 160 s, capped at 180 s; the first success resets it. Meanwhile clients get timetable-only data with `"live": false`.
- **Rate guarantee:** every nsmart request (arrivals and the station list) passes one gate with ≥ 0.4 s spacing; every gspns request (timetables, line lists, map) passes one lock with a random 1–2 s pause. HTTP clients never trigger upstream requests directly (they only queue work).
- **Logging:** syslog, facility `daemon`, ident `nstupido` (macOS `/var/run/syslog`, Linux `/dev/log`, fallback stderr) and a copy on stderr (`nstupido.log`). Lines: start / stop, reference cache ages at start, timetable refresh result, `timetables for stop 6660: ok (rvg 1,12,6)`, station list / network map refresh results, `stop … route … seen live … learning`, `client <ip> first request: /frame?stop=6539 -> 200` (once per client; there is no access log), `http: /frame 200 x360, …` every 30 min, `stats:` every 12 h (watched stops, queue length, unique client IPs in 12 h), `nsmart errors: 37 in last 30 min (timeout 30, http 503 7), backoff 180 s` at most every 30 min and only if there were errors. macOS: `log show --last 1h --predicate 'eventMessage CONTAINS "nstupido"'`; Linux: `journalctl -t nstupido`.

### API

| Request | Answer |
|---|---|
| `GET /` | usage, default stops, watch list with expiry, queue length/limit/interval, backoff state, ready, HTTP status counts, last request per client |
| `GET /buses?stop=<uid>[,<uid>…][&merge=1]` | JSON (format below) |
| `GET /frame?stop=<uid>` | text frame for the board (below) |
| `GET /debug` | timetable status, per watched stop: line entries + derivation (nsmart lines, matched map stop, labels, lines pending live), reference cache ages, feeders, learned travel times, recent arrivals |

| Code | When |
|---|---|
| `200` | data (maybe `"warming": true` or `"live": false`) |
| `400` | malformed uid (must be 1–7 digits) |
| `404` | stop does not exist in nsmart |
| `429` | queue full: `Retry-After: 600`, `{"error": "stop limit reached", "limit": 100}` |
| `503` | starting, no timetable yet: `Retry-After: 10` (only on a first start without cache) |

**Board frame** (`text/plain`, same line protocol as the USB bridge; all routes, nearest arrival first, max 24; one row per base route — variants such as 1 / 1GL / 1J share a row — ASCII-folded to 5 chars):

```
H|6539|12:33|W        stop | server time | flags: W = warming, O = offline (nsmart backoff)
R|8|G:1|P:31          route | up to 2 cells: G live, E estimate, P timetable (minutes)
R|3|E:6|P:33
E
```

Errors on `/frame` come as `ERR|<code>|<message>` with the same HTTP codes.

### `config.toml`

| Key | Default | Meaning |
|---|---|---|
| `server.host` / `server.port` | `0.0.0.0` / `8080` | listen address |
| `server.timezone` | `Europe/Belgrade` | 05:00 refresh, frame clock |
| `stops.default` | `["6539", "15889", "6712"]` | always polled, never expire |
| `polling.cycle_s` | 40 | every watched stop refreshed once per cycle |
| `polling.min_interval_s` | 0.4 | minimum spacing between nsmart requests |
| `polling.watch_ttl_h` | 4 | dynamic stop expiry after its last request |
| `polling.backoff_min_s` / `backoff_max_s` | 5 / 180 | exponential backoff |
| `polling.assumed_feeders` | 3 | capacity estimate for a never-seen stop |
| `timetable.refresh_at` | `"05:00"` | daily gspns refresh (local) |
| `timetable.pause_s` | `[1, 2]` | random pause between gspns requests |
| `timetable.retry_min` / `retries` | 15 / 3 | retry after a failed refresh (also: failed timetable fetch for a new stop) |
| `reference.max_age_days` | 14 | station list / network map older than this are refreshed … |
| `reference.start_delay_min` | 60 | … this long after start, then checked daily at `refresh_at` + 10 min |
| `logging.stats_every_h` / `errors_every_min` | 12 / 30 | syslog cadence |

Python 3.11+ reads it with `tomllib`; older Pythons use `tomli` if installed, else a built-in parser for this simple subset.

### CLI

`python3 nstupido.py [uid …] [--pretty] [--merge] [--no-schedule | --schedule-only] [--no-feeders]` — one-shot JSON (requests paced ≥ 0.4 s). v4 removed `--state`, `--no-state`, `--interval`, `--log-snapshots` and `?schedule=` on the server.

## Firmware (WiFi board)

Wemos/LOLIN S2 mini + 128×64 I2C OLED (SDA 18, SCL 16), PlatformIO env `lolin_s2_mini`.

### Secrets and build flags

WiFi credentials are never committed: `firmware/platformio.ini` includes `firmware/secrets.ini` (git-ignored) via `extra_configs`.

```bash
cd firmware
cp secrets.ini.example secrets.ini     # fill in WIFI_SSID / WIFI_PASS
pio run -t upload --upload-port /dev/cu.usbmodem01
pio device monitor                     # optional: "[nstupido] GET … -> 200" log lines
```

| Define | Default | Where | Meaning |
|---|---|---|---|
| `WIFI_SSID`, `WIFI_PASS` | — | `secrets.ini` | WiFi network (build fails without `WIFI_SSID`) |
| `SERVER_URL` | `"http://nstupido.teamof.top:8080"` | `platformio.ini` (override in `secrets.ini`) | server base URL, plain HTTP |
| `STOPS` | `"6539,15889,6712"` | `platformio.ini` | stops for the BOOT button, first shown at start (max 8) |
| `OLED_SDA` / `OLED_SCL` | 18 / 16 | `platformio.ini` | I2C pins |
| `OLED_CONTRAST` | 40 | `platformio.ini` | brightness 0…255 |
| `OLED_SH1106` | off | `platformio.ini` | SH1106 1.3″ panel |
| `BTN_PIN` | 0 | — | stop button (BOOT, active low) |

### What it shows

1. Startup: `WiFi...` → `WiFi OK <ip>` → `server...` → `server OK` (`GET /`) → `stop 6539...` → `stop 6539 OK` → the board.
2. Board (same layout as v3): header `<stop> <i>/<n>` and the clock (from the server), up to 4 routes with cells `4(G)` / `12(E)` / `8(P)`. More than 4 routes → pages of 4, one page per 5 s fetch, header `<stop> p<page>/<pages>` (e.g. `15889 p2/4`); order is nearest-first, frozen for one cycle so a route never jumps pages. Header marks: `*` = stop warming on the server, `!` = server has no live nsmart data (timetable only).
3. BOOT button: next stop, page 1; header inverts with `>>` until the new frame arrives (max 3 s).
4. Errors (next try after 60 s): `nema WiFi`, `server ne odgovara` (unreachable / timeout), `greska servera <code>` (other code or a bad frame), `limit stanica` (429), `nema stanice` (404). The last good frame stays on screen while it is younger than 90 s; after that it stays with a bottom bar showing the error (or `veza izgubljena`). Without any frame the error is shown full screen.

### Legacy USB bridge

`oled_bridge.py` (needs `pyserial`) still works with the v4 firmware: frames arriving over USB serial are drawn as sent (bridge does its own paging/header), WiFi fetching pauses while they keep coming (30 s), and BOOT then sends `BTN` to the bridge. Board → host lines: `OK`, `BTN`, `[nstupido] …` log lines (ignored by the bridge); `?` → `NStupido OLED v4`. Bridge flags: `--stop` (6539), `--stops` (server default stops), `--port` (auto), `--server-port` (8080), `--every` (5 s), `--once`. Don't run the bridge and a serial monitor at the same time.

## Tests

```bash
python3 -m unittest discover -s tests -v          # offline + live
python3 -m unittest tests.test_server -v          # v4 server: queue math, groups, capacity/429, TTL, backoff,
                                                  # log aggregation, HTTP codes, timetable refresh schedule
python3 -m unittest tests.test_schedule -v        # schedule, matching, feeders
python3 -m unittest tests.test_routes -v          # v4.1: route names, map matching, mark rules, cache age rules,
                                                  # new-stop flow, unknown live route, HTTP counters
python3 -m unittest tests.test_bridge -v          # legacy bridge: stop cycling, serial input, paging
python3 -m unittest tests.test_firmware_frame -v  # firmware frame.h parser + pager, compiled with c++ (skipped without a compiler)
python3 -m unittest tests.test_live_sources -v    # upstream reachability (network)
```

Offline tests use fake upstreams and a fake clock; they never contact nsmart or gspns. Live checks: **[HOW_TO_TEST_DATA.md](HOW_TO_TEST_DATA.md)**.

## Memory (Brief)

nsmart snapshots refresh ~15–20 s and are often nearly empty. NStupido remembers each bus by key (stop, garage, line) and counts down ETA itself until a fresh value arrives. Removal: 0 s already 30 s without appearing in snapshot; not seen longer than `last value + 120 s` (≤20 min); same trip already past our stop; new trip of same bus on line. Memory lives in RAM only (v4: no `state.json`); after a restart it is rebuilt within a few cycles, learned travel times fall back to the timetable offsets until relearned.

## Previous Stops (Feeders)

The stop itself often returns empty, while the previous one on the route shows the same buses earlier. ETA = ETA to previous + `second_left_by_route` difference. Marked `"source":"via 6540"`. Seeds: 6539 ← 6540/6750/6679; 6712 ← 6586/6728; 15889 — none (terminals before it with fake `P1`…). Polled in the same queue group right after their stop (once per 40 s cycle, shared feeders once). `--no-feeders` disables.

## Schedule (Brief)

At startup (if the cache is older than today's 05:00) and daily at 05:00 Europe/Belgrade the day type is read from gspns + `vaziod`, and R/S/N/P timetables for the lines of **all watched stops** (default + dynamic; hand-made `STOP_LINES` or derived) are downloaded with a random 1–2 s pause between requests → `cache/gspns/` (atomic writes; the only data on disk). A failed part keeps the previous copy; a failed refresh is retried after 15 min, up to 3 times. Day type fallback: holiday → P, Sat → S, Sun → N, otherwise R. Trips after midnight belong to previous service day. Window ~40 min before ETA; 3 min after due without bus — removed. Matching with live by `entered_departure_time` ±1 min or nearest ±6 min. `--no-schedule` / `--schedule-only` (CLI).

## JSON Format

Per-stop (default). v4 adds `"warming": true` (stop just added: no live answer or timetables yet) and `"live": false` (nsmart in backoff: timetable entries only). v4.1: `line` is the displayed base route, `nsmart_line` the original variant name, `entry` (scheduled only) tells whether the line came from the hand-made table (`manual`), the network map (`map`) or live learning (`live`):

```json
{"timestamp":"…","stops":[
  {"stop":{"uid":"6539","name":"…"},"timestamp":"…","buses":[
    {"line":"8","nsmart_line":"8","direction":"Liman 1","minutes":3,"seconds":236,"stops_between":2,
     "garage_no":"1119","lat":…,"lng":…,"current_stop":"…",
     "live":true,"last_seen":1,"status":"live","label":"Sveže",
     "scheduled_departure":"17:18","expected":"17:37","source":"via 6750","trip_id":"…"}
  ]}
]}
```

`--merge` / `?merge=1`: one `buses[]` with `stop_uid` / `stop_name`; stop errors — in `errors`.

| Field | Value |
|---|---|
| `line` / `nsmart_line` / `direction` | Displayed route (base), nsmart variant name, destination |
| `minutes` / `seconds` | Until arrival |
| `stops_between` | Stops until you (`null` for scheduled) |
| `garage_no`, `lat`, `lng`, `current_stop` | Position (`null` for scheduled) |
| `status` / `label` / `live` | live / lost / scheduled |
| `last_seen` | Seconds since last snapshot (`null` for scheduled) |
| `scheduled_departure` / `expected` | HH:MM |
| `source` | `direct` or `via <uid>` |
| `variant` / `trip_id` | gspns variant and trip id |

For stop: `updated` — seconds since last successful upstream. On network error there's `error`, but memory is still returned. Sorted by `seconds`. CLI exit code 1 if all requests errored and nothing to show. HTTP: see the server API table above.

## Notes

- nsmart often has short horizon → memory, feeders, and schedule needed.
- nsmart lists 54"A", 54ŽA, 56, 61, 62, 76 at 6712 and 56 at 15889, but the network map does not: they are not scheduled there until a live bus proves it (then learned). Announcements on `gspns.rs/aktuelno` not read by script.
- `radius` doesn't affect arrival prediction (only nearest stop search).
- Names in response truncated to 50 characters — restored from the station list (`cache/nsmart/stations.json`; the old `stations_cache.json` is no longer used and can be deleted).
- Find uid: `POST …/getAllStations` (fields `id`, `name`).
- No mDNS (`nstupido.local`) in v4: the board uses `SERVER_URL` (DNS name or IP).
