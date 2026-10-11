# NStupido v4 plan: WiFi board, on-demand stops, polite polling

Status: **implemented** (v4, 11 Oct 2026; see *Implementation notes* at the end for deviations). This is a research prototype built for fun, together with Grok Bot.

Goal: remove the USB bridge. The Wemos S2 mini fetches its frame from the NStupido server over WiFi. The server tracks the stops that clients actually ask for, keeps everything except the timetable cache in memory, and never polls nsmart.rs faster than a fixed rate.

## 1. Components

| Part | Role |
|---|---|
| `nstupido.py` server | Polls nsmart (live) and gspns.rs (timetables), merges them into G/E/P arrivals, serves HTTP JSON. |
| `config.toml` | Default stops and server settings. The only hand-edited file. |
| `cache/timetables.json` | On-disk timetable cache (the only persisted data). |
| Firmware (ESP32-S2, U8g2) | Connects to WiFi, asks the server for one stop, draws it, cycles stops with the BOOT button. |
| `oled_bridge.py` | Kept for USB-only setups, no longer required. |

## 2. Server lifecycle

### 2.1 Start
1. Read `config.toml`: default stops, port/host, limits (section 7).
2. Load `cache/timetables.json` if present. If it is missing, or older than today's 05:00 local time (Europe/Belgrade), refresh it (section 2.3).
3. For each default stop: one live nsmart request to learn which routes pass through it and its preceding ("feeder") stops, then fetch the GSPNS timetable for any routes not yet cached.
4. Start the polling queue (section 3). Default stops never expire.

### 2.2 In memory only (lost on restart, rebuilt in minutes)
- Live bus memory (first-seen countdown, last seen).
- Learned travel times from feeder stops to the target stop. Until relearned, (E) estimates fall back to timetable offsets.
- Watch list with per-stop expiry timers.
- Client statistics.

### 2.3 Daily timetable refresh at 05:00 local
- Take the union of routes of all currently watched stops.
- Fetch the GSPNS timetable route by route, with a random 1–2 s pause between requests.
- Day type (weekday / Saturday / Sunday / holiday) comes from the Serbian holiday calendar.
- If a fetch fails, keep yesterday's copy for that route and retry after 15 min (up to 3 times).
- Write `cache/timetables.json` atomically (temp file + rename).

### 2.4 Watch list and expiry
- Every `GET /buses?stop=<uid>` extends that stop's timer to now + 4 h.
- When a timer expires, the stop and its feeders leave the queue (unless a feeder also serves another watched stop).
- Default stops from the config have no timer.

### 2.5 Unknown stop requested
1. Validate the uid format, otherwise return `400`.
2. If adding it would exceed queue capacity (section 3), return `429`.
3. Otherwise answer immediately with whatever exists (timetable entries if its routes are already cached, otherwise an empty list) and `"warming": true`.
4. Add it to the queue: first a live request (which reveals routes and feeders), then a timetable fetch for new routes, then normal polling.
5. If nsmart says the stop does not exist, drop it and return `404` to later requests.

## 3. Polling queue (rate limit to nsmart)

- **Target cycle:** every watched stop is refreshed once per `cycle = 40 s`.
- **Minimum spacing:** never more than one nsmart request per `min_interval = 0.4 s`, regardless of how often clients call us.
- **Groups:** each watched stop forms a group: the stop itself followed immediately by its feeder stops. Groups are polled back to back, so feeders are always fresh relative to their stop.
- **Spacing:** with `N` requests in the queue (sum of group sizes), spacing is `interval = max(min_interval, cycle / N)`. A small queue is polled gently (e.g. 8 requests means one every 5 s), a full one at 0.4 s.
- **Capacity:** `N_max = cycle / min_interval = 100` requests per cycle. With about 3 feeders per stop that is roughly 25 stops. A new stop is accepted only if `N + its group size <= N_max`, otherwise `429`.
- Feeders shared by several stops are requested once per cycle.

## 4. Error handling and backoff (nsmart)

- Backoff is global, because a ban hits the whole server, not one stop: after a failure the next request waits 5, 10, 20, 40, 80 s, capped at 180 s. The first success resets it to the normal interval.
- During backoff, clients get timetable-based (P) data with `"live": false`.
- GSPNS failures are handled separately (section 2.3) and never block live polling.

## 5. Logging (syslog)

Facility `daemon`, ident `nstupido`.
- **Every 12 h:** one line with the number of watched stops (default + dynamic), queue length, and number of clients (unique IPs in the last 12 h).
- **nsmart errors:** aggregated. At most one line per 30 min, e.g. `nsmart errors: 37 in last 30 min (timeout 30, http 503 7), backoff 180 s`. No line if there were no errors.
- Timetable refresh: one line with the result (routes fetched, failed).
- Startup and shutdown: one line each.

## 6. HTTP API

`GET /buses?stop=<uid>`

| Code | When |
|---|---|
| `200` | Data (possibly `"warming": true` or `"live": false`). |
| `400` | Malformed stop uid. |
| `404` | Stop does not exist in nsmart. |
| `429` | Queue full, new stop not accepted. Includes `Retry-After` and `{"error": "stop limit reached", "limit": N}`. Stops already being watched are always served. |
| `503` | Server still starting (no timetable yet). Includes `Retry-After`. |

`GET /` returns usage, default stops, the watch list with expiry times, queue length and backoff state.

The server listens on `0.0.0.0` (LAN) and advertises `nstupido.local` via mDNS.

## 7. Configuration (`config.toml`)

```toml
[server]
host = "0.0.0.0"
port = 8080
timezone = "Europe/Belgrade"

[stops]
default = ["6539", "15889", "6712"]

[polling]
cycle_s = 40
min_interval_s = 0.4
watch_ttl_h = 4
backoff_max_s = 180

[timetable]
refresh_at = "05:00"
pause_s = [1, 2]

[logging]
stats_every_h = 12
errors_every_min = 30
```

## 8. Firmware (Wemos S2 mini, WiFi)

### 8.1 Startup screens
1. `WiFi...` then `WiFi OK <ip>` (or the error, section 8.3).
2. `server...` then `server OK` (`GET /`).
3. `stop 6539...` then `stop 6539 OK` (first `GET /buses?stop=6539` accepted, `200`).
4. The board.

### 8.2 Normal operation
- Stop list compiled in (`-DSTOPS="6539,15889,6712"`), first one shown on start.
- Fetch `GET /buses?stop=<current>` every 5 s, parse with ArduinoJson, render the same layout as v3: header with stop, page and clock, up to 4 rows, route plus minute cells with G/E/P, paging every 5 s when there are more than 4 routes.
- `"warming": true` shows a small marker in the header.
- BOOT button (GPIO0) switches to the next stop at page 1, with instant feedback.

### 8.3 Errors (retry after 60 s)
| Situation | Message |
|---|---|
| No WiFi / lost | `nema WiFi` |
| Server unreachable / timeout | `server ne odgovara` |
| Bad JSON / unexpected code | `greska servera <code>` |
| `429` | `limit stanica` |
| `404` | `nema stanice` |
| Data older than 90 s | keep last frame + `veza izgubljena` |

The last good frame stays on screen while the data is less than 90 s old.

### 8.4 WiFi credentials
Compile-time flags (`-DWIFI_SSID`, `-DWIFI_PASS`, `-DSERVER_HOST`) kept in an untracked `secrets.ini`, to be decided against a WiFiManager setup portal.

## 9. Implementation steps

1. Server: config loader, in-memory state, drop `state.json`.
2. Server: polling queue with groups, adaptive spacing, capacity and `429`.
3. Server: watch timers, dynamic stops, `warming`, `404`.
4. Server: timetable cache, 05:00 refresh, polite pacing.
5. Server: global backoff, syslog stats and aggregated errors.
6. Firmware: WiFi, HTTP client, JSON parsing, frame rendering moved from the bridge.
7. Firmware: startup screens, error screens, 60 s retry, button.
8. Tests: queue spacing and capacity math, expiry, backoff, log aggregation, HTTP codes (offline, with fake upstreams); firmware frame building tested on the host where possible.
9. Docs: README, HOW_TO_TEST_DATA, this plan updated to "implemented".

## 10. Implementation notes (v4, as built)

Implemented as specified unless listed here. Tests: `tests/test_server.py` (queue math, groups, capacity/429, TTL, backoff, log aggregation, HTTP codes, refresh schedule), `tests/test_firmware_frame.py` (firmware `frame.h` parser + pager compiled on the host).

Deviations / decisions:

- **Board protocol:** the firmware does not parse `/buses` JSON with ArduinoJson. The server has a compact endpoint `GET /frame?stop=<uid>` that returns the v3 text frame (`H|stop|HH:MM|flags`, `R|line|G:3|P:20`, `E`; all routes nearest-first, max 24). The board parses it with the same code as USB frames (`firmware/include/frame.h`) and pages itself. Less RAM, no JSON library, one parser for WiFi and USB. Flags: `W` = warming, `O` = offline (nsmart backoff). Errors: `ERR|<code>|<message>` with the same HTTP codes.
- **Timetable cache:** kept the existing layout `cache/gspns/index.json` + `timetable_<vaziod>.json` instead of a single `cache/timetables.json` (same role, atomic writes). Requests go per (urban/suburban, day type) for all needed lines at once, not route by route (fewer requests); 1–2 s random pause between every two requests. A failed part keeps its previous copy; retries after 15 min, up to 3 times; then the next 05:00.
- **503:** only while no timetable is available (first start without cache, until the first refresh attempt ends). With a cached timetable, even a stale one, the server answers at once and refreshes in the background.
- **Timetable for dynamic stops:** routes and feeders of a new stop are learned from its first live request, but (P) timetable entries exist only for stops with a `STOP_LINES` entry in `timetable.py` (gspns direction/variant mapping is still hand-made). Dynamic stops get G/E data.
- **Capacity of a never-seen stop:** counted as 1 + `assumed_feeders` (3) until its first live answer reveals the real feeders.
- **mDNS:** not implemented (stdlib has no mDNS). The board uses `SERVER_URL`, default `http://nstupido.teamof.top:8080` (resolved by the LAN DNS to the server host).
- **WiFi credentials:** compile-time flags in untracked `firmware/secrets.ini` (`extra_configs`); `SERVER_URL` is one flag (`-DSERVER_URL="http://host:port"`) instead of `SERVER_HOST`. No WiFiManager portal.
- **Errors on the board:** last frame kept while younger than 90 s; when older, it stays with a bottom bar showing the current error text (or `veza izgubljena`). 404/429 and "no frame yet" are full-screen messages.
- **USB bridge:** still supported by the v4 firmware (serial frames are drawn, WiFi fetch pauses 30 s, BOOT sends `BTN`).
- **Stop names:** `stations_cache.json` (getAllStations, weekly) is still written to disk; it is a name dictionary, not state.
- Removed: `state.json`, `--state/--no-state/--interval/--log-snapshots`, `?schedule=` on the server.
