# NStupido: data sources, route naming, and the decisions behind them

Status: v4.1, 11 Oct 2026. A research prototype built for fun by Paul Katurov together with Grok Bot. Grok Bot did the data research (on cached downloads, plus a few paced requests), and wrote the code and this document. Paul set the rules for naming, display and caching.

This document explains **what** each source provides, **how** NStupido combines them and **why** it works this way. Every claim below was checked against real downloads from 6–11 Oct 2026 (timetables valid from 2026-10-01).

## 1. Sources

| Source | Endpoint | Gives | Does not give |
|---|---|---|---|
| nsmart arrivals | `POST https://online.nsmart.rs/sr/najava-dolaska/` (`station_uid`) | buses approaching one stop: `line_number` (display name incl. variant, e.g. `14S`), `actual_line_number` (`141B` = route 14, variant 1, direction **B**), `entered_departure_time`, `seconds_left`, `all_stations[]` (the full ordered stop list of that trip pattern, each stop with `tt_time` = scheduled minutes from the terminal) | buses that are not on the road yet; planned departures (terminals list fake `P1`, `P2` "vehicles") |
| nsmart station list | `POST https://online.nsmart.rs/sr/AnnouncementForStation/getAllStations` | all ~1013 stops: `id`, `name`, `station_id` code (`0220B`), coordinates, **`lines_for_station`** (e.g. 6539: `18A, 3, 8`; 6712: `10, 10MAL, 14, 14GS, 14S, 15, 15NK, 18A, 19, 3A, 4, 5N, 7A, 54"A", 54ŽA, 56, 61, 62, 76`) | direction (a stop is on one side of the street, but the list doesn't say which direction stops there) |
| gspns timetables | `GET http://gspns.rs/red-voznje/gradski` (day type, validity dates), `…/lista-linija?rv=rvg\|rvp&vaziod=&dan=R\|S\|N\|P` (line options), `…/ispis-polazaka?…&linija[]=` (tables) | departures from the terminal per direction column (`Смер A: …` / `Смер B: …`; suburban `Polasci za X` = A, `Polasci iz X` = B), a mark per departure (`GL`, `S`, `NRS`, `IS`, `APT`, `Đ`…) and the legend explaining it | where a departure stops; travel time to a stop |
| gspns network map | `GET http://gspns.rs/mreza` (196 buttons: id, title, label) + `GET /mreza-get-stajalista-tacke?linija=<id>` (stops of that button: `labels\|lon\|lat\|name\|photo\|zone`) | per (route, variant, direction) the set of stops; each map stop lists **all labels serving it** (6539: `[3B],[8A],[18A]`) | nsmart stop ids (matched by coordinates); stop order (lists are unordered) |
| holidays | `rs_holidays.py` (Serbian law, Orthodox Easter) | fallback day type when gspns is unreachable | — |

All endpoints are undocumented, need no auth, and may change without notice.

## 2. Route naming: Paul's model and the real data

**Paul's model:** a route name is *number + direction letter (A/B) + variation letter*. The number is the route; A/B is the direction along it (irrelevant at a stop, since a stop is on one side, but needed to pick the timetable column); the variation is a detour that serves particular stops (route 1 has several).

**What the data actually does:**

- The only source that spells names this way is the **gspns network map**. Its order is *number + variant + direction*: `1GLA` = route 1, variant GL (Gornje Livade), direction A; `14SB` = 14 Sajlovo, B; `71ISA` = 71 via Institut, A.
- **A letter is not always a direction:**
  - **Separate routes with a letter in the name:** `3A`, `3B`, `5N`, `6A`, and the loops `7A/7B`, `11A/11B`, `18A/18B` each have their own gspns timetable. Examples: 3A = Žel. stanica–Pobeda, 3B = Petrovaradin–Pućkaroš, 6A = Adice–Veternik. On the map, `3B` means route 3 direction B, but `3BA` means route 3B direction A. `7A` is the loop 7A (nsmart encodes 7A/7B as variants `70`/`71` of route 7).
  - **Letters that are variants:** `9A` (Alibegovac) and `54"A"` (Aroma) are variants of 9 and 54. On the map they are `9AA` and `54AA`.
- **The same variant has different names in each source:**

| Variant | gspns mark | map label | nsmart `line_number` |
|---|---|---|---|
| 9 via Alibegovac | `ALI` | `9AA` / `9AB` | `9A` |
| 10 Mašinoremont + Albus | `MAL` (dir A), `ALM` (dir B) | `10MALA`, `10ALMB` | `10MAL` (both) |
| 15 Neoplanta + Koteks | `NK` (A), `KN` (B) | `15NKA`, `15KNB` | `15NK` |
| 14 Sajlovo | `S`, `NRS` (low floor) | `14SA`, `14SB` | `14S` |
| 14 via Gornje Sajlovo | `NGS` | (none) | `14GS` |
| 1 Gornje Livade | `GL`, `NRG` (low floor) | `1GLA`, `1GLB` | `1GL` |
| 54 Aroma | `"A"` | `54AA`, `54AB` | `54"A"` |
| 55 Centralno groblje | `CG` | `55CGA`, `55CGB` | (shown as `55`) |
| school trips | `Đ`, `DDJ`, `MDJ`, `UDJ` | `6ĐB`, `11ĐA`, `14ĐA`, `13UĐA` | — |

- **Low-floor prefixes** `N`/`NR` wrap a variant code (`NRG` = GL on a low-floor bus).
- **The direction letter can disagree between sources:** at 6712 the map says `3AA`, but 3A's only timetable column is headed "Смер B". So NStupido picks the column by matching destinations (map title vs column header) and uses the letter only as a fallback.
- **nsmart `actual_line_number`** = route + variant digit + optional `B`. In every case checked, the `B` matches gspns "Смер B": `30` → Detelinara (Smer A), `30B` → Petrovaradin; `520`/`520B`; `141B` = `14S`, direction B.

### Display rule (Paul's decision)

The board shows the **base route**. A letter is kept only when it names a different route, i.e. when it is a gspns timetable option: `3A`, `3B`, `5N`, `6A`, `7A`, `7B`, `11A`, `11B`, `18A`, `18B`. True variants merge into their number: `1GL`/`1J` → `1`; `14S`/`14GS` → `14`; `10MAL`/`10APT` → `10`; `9A` → `9`; `54"A"`/`54ŽA` → `54`; `71IS` → `71`; `15NK` → `15`. All variants of one base route share **one row** on the board (nearest two arrivals). The JSON keeps the original name in `nsmart_line`.

Why: on a 128×64 screen the rider needs "which bus number to wait for"; the variant detour rarely matters at the stop the rider is standing at. Merging distinct routes (3 vs 3A, 6 vs 6A), however, would show the wrong bus.

The set of distinct routes comes from the cached gspns line options plus a built-in fallback (plain numbers + the lettered routes above). Implementation: `routes.Naming`.

## 3. Which lines pass a stop, and in which direction

For a stop not in the hand-made table (`timetable.STOP_LINES`), `routes.Resolver.derive` does this:

1. **Lines:** nsmart `lines_for_station`, reduced to base routes. This needs no live bus, so the full list is known right after the first request.
2. **Map stop:** among map stops within 120 m, take the one sharing the most base routes, then the nearest. This breaks the tie for stops with identical coordinates: 6712 (city side) and 15889 (suburban side) are both at 45.26020, 19.83222. The city map stop shares 9 routes with 6712's lines and the suburban map stop shares 23 with 15889's. The opposite side of the street (e.g. 6551 for 6539, 84 m away) carries the other direction letters.
3. **Direction:** the labels of that map stop give (route, variant, direction letter) at this stop, plus the button title (e.g. "DETELINARA - CENTAR - PETROVARADIN").
4. **Timetable column:** the column whose header destination matches the button title's destination. If none matches, the column with the same direction letter. A single column is used only if it doesn't contradict the map. Example: 3A's only column runs towards Pobeda, so it serves 6712 (`3AA`) but not 6709 (`3AB`).
5. **Departure marks (Paul's decision: show school and factory trips too):** every departure of the chosen column counts, except one whose mark maps to a variant that the map **demonstrably** places elsewhere:
   - map the mark to a map variant: the mark itself, then without a low-floor prefix (`NRJ` → `J`), then a school suffix (`DDJ` → `Đ`), then a unique prefix (`NRG` → `GL`, `ALI` → `A`);
   - if that variant exists on the map for this route and direction but not at this stop, the departure is skipped. Example: at 6712, `APT` is skipped because `15APTB` serves other stops; `DDJ` is skipped because `14ĐB` runs Sajlovo–Detelinara;
   - an **unknown** mark is included. Examples: `NGS` (14GS is not on the map), `MR`, `Đ` on 3B.
6. **Lines with no map evidence** (nsmart says the line stops here, but the matched map stop has no label for it; e.g. 54, 56, 61, 62 and 76 at 6712) are not scheduled until a live bus proves it (see 4).
7. **Timetable source** (`rvg` city / `rvp` suburban): from the cached gspns line options, else the map button kind, else the nsmart line type.

### Learning from live data (Paul's rule 5)

If a live bus passing a watched stop belongs to a route that has no entry there, NStupido takes the direction from `actual_line_number` (trailing `B` → B, else A) and immediately fetches that route's timetable. Departures are then counted with the same mark rule; the observed variant (e.g. `54"A"` → `A`) always counts. Learned routes live in memory only; they are learned again after a restart.

### Automatic vs hand-made lines

The three default stops keep their hand-made entries (`timetable.STOP_LINES`) as an override, as Paul asked. The automatic derivation was checked against them, departure by departure, on the full 2026-10-01 timetables for weekday (R), Saturday (S) and Sunday (N):

| Stop | Automatic (map) | Weekday / Sat / Sun departures | Differences |
|---|---|---|---|
| 6539 | `3B`, `8A`, `18A` | 150 / 114 / 80, identical | none |
| 6712 | `3AA`, `4B`, `5NA`, `7A`, `10ALB`+`10ALMB`, `14B`+`14SB`, `15KNB`+`15NPB`, `18A`, `19A` | 248 / 188 / 206, identical | none. nsmart also lists 54"A", 54ŽA, 56, 61, 62, 76; the map doesn't place them here and the manual table omits them too (live-learned if seen) |
| 15889 | direction A of 23 suburban routes incl. `53CGA`, `54AA`, `54ŽAA`, `55CGA`, `68ISA`, `71ISA`, `71ČA`, `71VMČA` | 376 / 225 / 207 vs manual 377 / 225 / 211 | weekday: 55 at 14:15 (mark `KN`). The map's `55KNA` (Zona sever – Zona jug – Futog) does not serve this stop, so the automatic rule drops it; the manual table keeps it. Sunday: the manual table took column 0 of line 77, which on Sundays is "Polasci iz Stari Rakovac" (direction **B**, towards Novi Sad), so it showed 4 wrong departures. **Fixed:** manual entries can now carry `"letter": "A"`, and 15889's do. |

Reproduce it: `tests/test_routes.py` covers the rules on a synthetic map; the full comparison uses the cached map, station list and timetables (`python3 tools/compare_lines.py`, see HOW_TO_TEST_DATA §7).

## 4. Travel time from the terminal to the stop

gspns gives departures from the terminal, so a scheduled (P) arrival needs the travel time to the stop. NStupido uses the best one available:

1. nsmart `tt_time` of this stop, from **any** bus of this line seen anywhere (the stop itself, its feeders, other watched stops). `all_stations[]` carries scheduled minutes for every stop of the trip. For a new busy stop this usually arrives with its first live response.
2. Learned arrivals (departure → bus at the stop) adjust it by up to ±5 min once there are 3 samples.
3. Hand-made `tt` in `STOP_LINES` (default stops).
4. Otherwise a default of 2 min. The map can't help here: its stop lists are unordered, and of three line polylines checked (`/mreza-get-linija-tacke`), 3B and 14B start at their first terminal but 8A starts at its last one (Liman), so polylines can't be oriented reliably.

P entries with the default offset may appear too early until step 1 has data. Matching keeps a live bus and its own scheduled trip from both showing when the offset is close.

## 5. Caching, and why

| Data | Where | Refresh | Why |
|---|---|---|---|
| Live buses, learned travel times, routes, learned directions, watch list, client stats | RAM only | every poll | changes by the second; after a restart it rebuilds within minutes. Persisting it (the old `state.json`) brought back stale buses and stale learning after restarts and added disk writes every few seconds |
| gspns timetables | `cache/gspns/timetable_<vaziod>.json` + `index.json` (day types, validity dates, line options) | daily at 05:00 for all watched stops (default + dynamic); immediately for lines never fetched (new stop, route seen live) | valid for a whole service day. 05:00 because the day type (weekday / Saturday / Sunday / holiday) changes overnight, and the first buses leave around 04:30–05:00. The disk cache means a restart mid-day needs no gspns requests (a dynamic stop that comes back mid-day is answered from the cache) |
| nsmart station list | `cache/nsmart/stations.json` (`fetched` timestamp) | if older than **14 days**: 1 h after start, and checked daily at 05:10 | stops and lines change a few times a year. The 1 h delay keeps a crash/restart loop from re-downloading 1.5 MB each time. If there is **no** cache at all it is fetched once at once (one request) |
| gspns network map | `cache/gspns/map.json` (`fetched`, buttons, stops per button) | same 14-day rule; ~197 requests, sequential, 1–2 s apart (~5 min) | changes with network reorganisations (rare). A failed button keeps its old stops, and the old `fetched` stamp stays so the next daily check tries again. A failed page keeps the whole old map |

All cache writes are atomic (temp file + rename). The first v4.1 caches were seeded from the research download of 6 Oct 2026 (stamped with that date), so they follow the 14-day rule from then on.

## 6. Rate limiting and backoff, and why

- **nsmart:** one global gate, at least 0.4 s between requests, at most 100 per 40 s cycle (≈25 stops with feeders). A small queue is polled gently (`max(0.4 s, 40 s / N)`; 9 requests → one every 4.4 s). After a failure the whole server backs off 5 → 10 → 20 … 180 s. Global, because a ban would hit the server, not one stop. HTTP clients never cause upstream requests directly: they only add a stop to the queue (or get `429`).
- **gspns:** one lock and a random 1–2 s pause between requests, shared by the 05:00 refresh, new-stop timetable fetches and the map refresh. gspns is a small municipal site; a new stop costs about 8 requests per kind of line (4 day types × line list + tables), i.e. ~15 s.

## 7. How it all combines for one stop

```
first request /frame?stop=6660
  ├─ watch list: add (TTL 4 h), jump the nsmart queue          → live buses (G/E), route tt_time learned
  ├─ resolver: station list lines → map stop → (route, column, marks)
  ├─ timetables: fetch lines never fetched (paced) ............ answer "warming": true meanwhile
  └─ view: live memory + timetable trips (expected = departure + travel time), matched so one trip shows once
         → base route names, one row per route, nearest two arrivals
```

Measured on 11 Oct 2026, 15:15 (Sunday), new stop 6660 "Bulevar Mihajla Pupina-Pothodnik" (not a default):

- The first answer came at once with flag `W` (3 rows from live data). The timetables for 1, 12 and 6 were fetched (3, 4 and 8 were already cached).
- After 15 s the full frame:

```
H|6660|15:15|
R|1|G:0|P:15
R|8|G:0|P:31
R|6|P:6|P:31
R|3|P:9|P:36
R|4|P:12|P:29
R|12|P:16|P:39
E
```

- 3A doesn't run on Sundays and 18A is a night line, so both are correctly absent. `1GL` trips are shown as `1`.

## 8. Access control (v4.2), and why

The server moved to its own host behind `nstupido.teamof.top:8080`, which may be reachable from outside the home network. Each request is now checked:

- **LAN trust without tokens:** the board and home devices sit in `192.168.42.0/24` (plus localhost). The ESP32 firmware has no secret storage worth the name, re-flashing it for a token gains nothing on a home LAN, and the board must keep working untouched. IPv4-mapped IPv6 addresses (`::ffff:192.168.42.x`, from dual-stack sockets) are unwrapped first, so the LAN check can't be bypassed or missed that way.
- **Named tokens for everyone else:** Paul hands one token per friend or device (10 slots in the template). The name shows up in the logs (first use, 12 h usage counts), so a leaked token can be found and removed without touching the others. Compared in constant time; never logged.
- **Untracked `auth.toml`:** the server deploys by `git pull`. Tokens in a tracked file would either be public in the repo or produce merge conflicts and overwrites on every pull. So `config.toml` holds only the non-secret `[auth]` settings, and `auth.toml` (git-ignored) holds the tokens and may override any `[auth]` key. The tracked `auth.example.toml` has `CHANGE-ME` placeholders, which the server refuses, so copying the template unedited can't create a valid token.
- **Secure default:** with no usable token, outside requests are all refused. A missing or unedited `auth.toml` therefore means "LAN only", never "open".
- **No `X-Forwarded-For` by default:** the header is client-controlled. It is believed only from configured `trusted_proxies`, taking the right-most hop that isn't a proxy.
- **Quiet logs:** rejected requests (scanners) are only counted and summarised at most every 30 min, the same pattern as the nsmart error log, so syslog isn't flooded.
- **Minimal 401:** a bare `unauthorized` body with `WWW-Authenticate: Bearer` tells a scanner nothing about the stops, the queue or the software version beyond the server header. The per-client list in `GET /` (addresses) is shown only to trusted networks.
