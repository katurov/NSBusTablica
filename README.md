# NStupido

Home display for upcoming buses in GSP Novi Sad: live nsmart data + memory between snapshots + official gspns.rs schedule → compact JSON, HTTP API, and USB-connected OLED 128×64.

## Purpose

The script polls the undocumented nsmart endpoint for one or more stops, remembers buses seen (so empty snapshots don't "reset" the display), blends in ETAs from previous stops on the route, and adds "scheduled" departures from gspns.rs. Output:

- CLI → JSON to stdout;
- HTTP server (`--serve`) → `GET /buses`, `GET /debug`;
- ESP32 firmware + `oled_bridge.py` → strings like `4(G)` / `12(E)` / `8(P)` on OLED.

No keys or authentication required. Python 3 standard library only for server; USB bridge requires `pyserial` (see `requirements.txt`).

## Project Structure

| Path | Role |
|---|---|
| `nstupido.py` | Core: fetch nsmart, memory, feeders, merge with schedule, CLI and HTTP server |
| `timetable.py` | Load/cache gspns.rs, day type, `STOP_LINES`, output scheduled departures |
| `rs_holidays.py` | Republic of Serbia holidays (incl. Orthodox Easter) for fallback day type |
| `oled_bridge.py` | Reads `/buses` from local server and sends frames to ESP32 via serial; BOOT button cycles stops |
| `firmware/` | PlatformIO + `src/main.cpp` — OLED board (Wemos/LOLIN S2 mini) |
| `tests/` | Offline unit tests + live connectivity (`test_live_sources.py`) |
| `tests/fixtures/` | gspns HTML snapshots for offline parsers |
| `samples/` | Sample raw nsmart responses |
| `HOW_TO_TEST_DATA.md` | Agent playbook: how to verify data is coming through |
| `requirements.txt` | `pyserial` only for bridge; core uses stdlib |
| `state.json` | **runtime**, not in git: bus memory + learned travel times |
| `cache/gspns/` | **runtime**: schedule cache and index (vaziod, day type) |
| `stations_cache.json` | **runtime**: full stop names from getAllStations |

## Data Sources

| Source | How | Auth |
|---|---|---|
| **nsmart** live | `POST https://online.nsmart.rs/sr/najava-dolaska/` (`station_uid`, …) | none |
| **nsmart** names | `POST …/AnnouncementForStation/getAllStations` | none |
| **gspns** day type / vaziod | `GET http://gspns.rs/red-voznje/gradski` | none |
| **gspns** departures | `GET …/ispis-polazaka?rv=&vaziod=&dan=&linija[]=` | none |
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

For a new stop: pass uid in CLI/`?stop=`; for your own schedule — add `STOP_LINES` in `timetable.py` and optionally `SEED_FEEDERS` / `DEFAULT_STOPS` in `nstupido.py`.

## Parameters

### CLI / server (`nstupido.py`)

| Flag | Default | Meaning |
|---|---|---|
| `stops…` | 6539 15889 6712 | Stop uids |
| `--merge` | off | One combined bus list, sorted by time |
| `--serve` | off | HTTP server + background polling |
| `--port` | 8080 | Server port |
| `--host` | `0.0.0.0` | Bind address |
| `--interval` | 15 | Poll period, seconds |
| `--state FILE` | `state.json` | Memory file |
| `--no-state` | — | Don't load/save memory |
| `--no-schedule` | — | Without gspns |
| `--schedule-only` | — | Schedule only, no nsmart (debug) |
| `--no-feeders` | — | Don't poll previous stops |
| `--pretty` | — | Indented JSON (CLI) |
| `--log-snapshots FILE` | — | Each upstream snapshot → JSONL |

On server: `GET /buses[?stop=6539[,6551]][&merge=1][&schedule=0\|only]`, `GET /debug`.

### USB bridge (`oled_bridge.py`)

| Flag | Default | Meaning |
|---|---|---|
| `--stop` | 6539 | Stop shown at start |
| `--stops` | server's `default_stops` | Comma-separated stops the BOOT button cycles through |
| `--port` | auto (`/dev/cu.usbmodem*` / `ttyACM*`) | Serial port |
| `--server-port` | 8080 | Local nstupido port |
| `--every` | 5 | Frame refresh period, seconds |
| `--once` | — | One frame and exit |

### Firmware (`firmware/platformio.ini` → `build_flags`)

| Define | Default | Meaning |
|---|---|---|
| `OLED_SDA` | 18 | I2C SDA (LOLIN S2 mini) |
| `OLED_SCL` | 16 | I2C SCL |
| `OLED_CONTRAST` | 40 | Brightness 0…255 (stock ~255 too bright) |
| `OLED_SH1106` | off | Uncomment for SH1106 1.3″ panel |
| `BTN_PIN` | 0 | Stop-switch button (BOOT on LOLIN S2 mini, active low) |

### BOOT button: cycle stops

Pressing the board's **BOOT** button (GPIO0) switches the display to the next stop, wrapping around — handy for demos.

1. Firmware debounces GPIO0 (30 ms, `INPUT_PULLUP`), sends `BTN` over USB serial and immediately shows the header inverted with `>>` (instant feedback; reverts after 3 s if the host doesn't answer).
2. `oled_bridge.py` reads the serial input non-blocking between refreshes, advances to the next stop and pushes that stop's frame right away (no wait for the 5 s cycle).
3. The header shows the current stop and position, e.g. `15889 2/3`.

Stop list: `--stops` if given, else `default_stops` from the server (`GET /` → 6539, 15889, 6712); the bridge always starts at `--stop` (6539). Any stop works since the server answers `/buses?stop=<uid>`. The choice isn't persisted: restarting the bridge returns to `--stop`.

Serial protocol, board → host: `OK` (frame drawn), `BTN` (button), `I2C device at 0x..` (boot scan); unknown lines are ignored. `?` returns the firmware version (`NStupido OLED v3`).

## Quick Start

```bash
# Core — Python 3 only
python3 nstupido.py 6539 --pretty
python3 nstupido.py --serve --port 8080

# OLED bridge
pip install -r requirements.txt          # pyserial
python3 nstupido.py --serve --port 8080  # if not already running
python3 oled_bridge.py --stop 6539      # BOOT button cycles 6539 → 15889 → 6712

# Bridge as a background daemon that survives closing the terminal
nohup python3 oled_bridge.py --stop 6539 >> oled_bridge.log 2>&1 &

# Firmware (PlatformIO required)
cd firmware && pio run -t upload && cd ..
# Brightness: -DOLED_CONTRAST=40 in platformio.ini
```

Don't open serial monitor while bridge holds the port.

## Tests

```bash
python3 -m unittest discover -s tests -v          # offline + live
python3 -m unittest tests.test_schedule -v        # offline only
python3 -m unittest tests.test_live_sources -v    # upstream only
python3 -m unittest tests.test_bridge -v          # bridge: stop cycling, serial input
```

Offline: day type, midnight, schedule change, matching, feeders (`tests/fixtures/`).
Live: nsmart and gspns availability — see **[HOW_TO_TEST_DATA.md](HOW_TO_TEST_DATA.md)** (agent checklist, curl, differences between "empty nsmart" vs "API down").

## Memory (Brief)

nsmart snapshots refresh ~15–20 s and are often nearly empty. NStupido remembers each bus by key (stop, garage, line) and counts down ETA itself until a fresh value arrives. Removal: 0 s already 30 s without appearing in snapshot; not seen longer than `last value + 120 s` (≤20 min); same trip already past our stop; new trip of same bus on line. `state.json` beside script; server accumulates memory even without clients.

## Previous Stops (Feeders)

The stop itself often returns empty, while the previous one on the route shows the same buses earlier. ETA = ETA to previous + `second_left_by_route` difference. Marked `"source":"via 6540"`. Seeds: 6539 ← 6540/6750/6679; 6712 ← 6586/6728; 15889 — none (terminals before it with fake `P1`…). Polled every other cycle (~30 s). `--no-feeders` disables.

## Schedule (Brief)

Once daily (and at startup) the day type is read from gspns + `vaziod`, R/S/N/P for lines from `STOP_LINES` are downloaded → `cache/gspns/`. Day type fallback: holiday → P, Sat → S, Sun → N, otherwise R. Trips after midnight belong to previous service day. Window ~40 min before ETA; 3 min after due without bus — removed. Matching with live by `entered_departure_time` ±1 min or nearest ±6 min. `--no-schedule` / `--schedule-only`; on server `?schedule=0` / `?schedule=only`.

## JSON Format

Per-stop (default):

```json
{"timestamp":"…","stops":[
  {"stop":{"uid":"6539","name":"…"},"timestamp":"…","buses":[
    {"line":"8","direction":"Liman 1","minutes":3,"seconds":236,"stops_between":2,
     "garage_no":"1119","lat":…,"lng":…,"current_stop":"…",
     "live":true,"last_seen":1,"status":"live","label":"Sveže",
     "scheduled_departure":"17:18","expected":"17:37","source":"via 6750","trip_id":"…"}
  ]}
]}
```

`--merge` / `?merge=1`: one `buses[]` with `stop_uid` / `stop_name`; stop errors — in `errors`.

| Field | Value |
|---|---|
| `line` / `direction` | Line and destination |
| `minutes` / `seconds` | Until arrival |
| `stops_between` | Stops until you (`null` for scheduled) |
| `garage_no`, `lat`, `lng`, `current_stop` | Position (`null` for scheduled) |
| `status` / `label` / `live` | live / lost / scheduled |
| `last_seen` | Seconds since last snapshot (`null` for scheduled) |
| `scheduled_departure` / `expected` | HH:MM |
| `source` | `direct` or `via <uid>` |
| `variant` / `trip_id` | gspns variant and trip id |

For stop: `updated` — seconds since last successful upstream. On network error there's `error`, but memory is still returned. Sorted by `seconds`. CLI exit code 1 if all requests errored and nothing to show. HTTP: 200 / 400 / 404 / 502.

## Notes

- nsmart often has short horizon → memory, feeders, and schedule needed.
- Line list for 6712/15889 partially presumed; 56 not included (per mreza doesn't pass here). Announcements on `gspns.rs/aktuelno` not read by script.
- `radius` doesn't affect arrival prediction (only nearest stop search).
- Names in response truncated to 50 characters — restored from dictionary / `stations_cache.json`.
- Find uid: `POST …/getAllStations` (fields `id`, `name`).
