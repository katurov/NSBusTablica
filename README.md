# NStupido

Домашнее табло ближайших автобусов ГСП Нови-Сад: живые данные nsmart + память между снимками + официальное расписание gspns.rs → компактный JSON, HTTP API и OLED 128×64 по USB.

## Назначение

Скрипт опрашивает недокументированный эндпоинт nsmart для одной или нескольких остановок, запоминает увиденные автобусы (чтобы пустые снимки не «обнуляли» табло), подмешивает ETA с предыдущих остановок маршрута и добавляет рейсы «по расписанию» из gspns.rs. Результат:

- CLI → JSON в stdout;
- HTTP-сервер (`--serve`) → `GET /buses`, `GET /debug`;
- прошивка ESP32 + `oled_bridge.py` → строки вида `4(G)` / `12(E)` / `8(P)` на OLED.

Ключей и авторизации нет. Только стандартная библиотека Python 3 для сервера; для USB-моста нужен `pyserial` (см. `requirements.txt`).

## Состав проекта

| путь | роль |
|---|---|
| `nstupido.py` | ядро: fetch nsmart, память, фидеры, merge с расписанием, CLI и HTTP-сервер |
| `timetable.py` | загрузка/кэш gspns.rs, тип дня, `STOP_LINES`, выдача scheduled-рейсов |
| `rs_holidays.py` | праздники Республики Сербия (в т.ч. православная Пасха) для fallback типа дня |
| `oled_bridge.py` | читает `/buses` с локального сервера и шлёт кадры на ESP32 по serial |
| `firmware/` | PlatformIO + `src/main.cpp` — OLED board (Wemos/LOLIN S2 mini) |
| `tests/` | офлайн unit-тесты + live connectivity (`test_live_sources.py`) |
| `tests/fixtures/` | HTML-снимки gspns для офлайн-парсеров |
| `samples/` | примеры сырых ответов nsmart |
| `HOW_TO_TEST_DATA.md` | playbook для агента: как проверить, что данные приходят |
| `requirements.txt` | `pyserial` только для моста; ядро — stdlib |
| `state.json` | **runtime**, не в git: память автобусов + выученное время в пути |
| `cache/gspns/` | **runtime**: кэш расписаний и index (vaziod, тип дня) |
| `stations_cache.json` | **runtime**: полные имена остановок из getAllStations |

## Источники данных

| источник | как | auth |
|---|---|---|
| **nsmart** live | `POST https://online.nsmart.rs/sr/najava-dolaska/` (`station_uid`, …) | нет |
| **nsmart** имена | `POST …/AnnouncementForStation/getAllStations` | нет |
| **gspns** тип дня / vaziod | `GET http://gspns.rs/red-voznje/gradski` | нет |
| **gspns** отправления | `GET …/ispis-polazaka?rv=&vaziod=&dan=&linija[]=` | нет |
| **праздники** | `rs_holidays.py` (закон РС) | — |

Эндпоинты недокументированы и могут измениться без предупреждения.

## Статусы на табло

| `status` | `label` | OLED | смысл |
|---|---|---|---|
| `live` | Sveže | `G` | автобус в свежем снимке (своя остановка или предыдущая) |
| `lost` | Videli-izgubili | `E` | видели раньше, сейчас оценка по памяти |
| `scheduled` | Po rasporedu | `P` | рейс из расписания, ещё не видели |

## Остановки по умолчанию

| uid | код | название | линии |
|---|---|---|---|
| 6539 | 0220B | Bulevar kralja Petra prvog-Mašinska škola | 18A, 3, 8 |
| 15889 | 0509-1A | Bulevar Oslobodjenja … prigrad (пригородные) | 52–56, 60–64, 68, 69, 71–74, 76–81, 84, 86 (+IS) |
| 6712 | 0509A | Bulevar Oslobođenja - Bulevar Kralja Petra prvog (городские) | 4, 7A, 10/10MAL, 14/14S/14GS, 15, 18A, 19, 3A, 5N |

15889 и 6712 ~3 м друг от друга, от 6539 ~385 м. Другие полезные: 6551 (напротив 6539), 6709 (другая сторона бульвара).

Для новой остановки: передать uid в CLI/`?stop=`; для своего расписания — дописать `STOP_LINES` в `timetable.py` и при желании `SEED_FEEDERS` / `DEFAULT_STOPS` в `nstupido.py`.

## Параметры

### CLI / сервер (`nstupido.py`)

| флаг | по умолчанию | смысл |
|---|---|---|
| `stops…` | 6539 15889 6712 | uid остановок |
| `--merge` | off | один общий список автобусов, сортировка по времени |
| `--serve` | off | HTTP-сервер + фоновый опрос |
| `--port` | 8080 | порт сервера |
| `--host` | `0.0.0.0` | bind-адрес |
| `--interval` | 15 | период опроса, с |
| `--state FILE` | `state.json` | файл памяти |
| `--no-state` | — | не грузить/не писать память |
| `--no-schedule` | — | без gspns |
| `--schedule-only` | — | только расписание, без nsmart (отладка) |
| `--no-feeders` | — | не опрашивать предыдущие остановки |
| `--pretty` | — | indented JSON (CLI) |
| `--log-snapshots FILE` | — | каждый снимок upstream → JSONL |

На сервере: `GET /buses[?stop=6539[,6551]][&merge=1][&schedule=0\|only]`, `GET /debug`.

### USB-мост (`oled_bridge.py`)

| флаг | по умолчанию | смысл |
|---|---|---|
| `--stop` | 6539 | uid для табло |
| `--port` | auto (`/dev/cu.usbmodem*` / `ttyACM*`) | serial |
| `--server-port` | 8080 | локальный nstupido |
| `--every` | 5 | период обновления кадра, с |
| `--once` | — | один кадр и выход |

### Прошивка (`firmware/platformio.ini` → `build_flags`)

| define | по умолчанию | смысл |
|---|---|---|
| `OLED_SDA` | 18 | I2C SDA (LOLIN S2 mini) |
| `OLED_SCL` | 16 | I2C SCL |
| `OLED_CONTRAST` | 40 | яркость 0…255 (сток ~255 слишком яркий) |
| `OLED_SH1106` | выкл. | раскомментировать для панели SH1106 1.3″ |

## Быстрый старт

```bash
# ядро — только Python 3
python3 nstupido.py 6539 --pretty
python3 nstupido.py --serve --port 8080

# OLED-мост
pip install -r requirements.txt          # pyserial
python3 nstupido.py --serve --port 8080  # если ещё не запущен
python3 oled_bridge.py --stop 6539

# прошивка (нужен PlatformIO)
cd firmware && pio run -t upload && cd ..
# яркость: -DOLED_CONTRAST=40 в platformio.ini
```

Не открывайте serial-монитор, пока мост держит порт.

## Тесты

```bash
python3 -m unittest discover -s tests -v          # офлайн + live
python3 -m unittest tests.test_schedule -v        # только офлайн
python3 -m unittest tests.test_live_sources -v    # только upstream
```

Офлайн: тип дня, полночь, смена расписания, matching, фидеры (`tests/fixtures/`).
Live: доступность nsmart и gspns — см. **[HOW_TO_TEST_DATA.md](HOW_TO_TEST_DATA.md)** (чеклист для агента, curl, отличия «пустой nsmart» vs «API лежит»).

## Память (кратко)

Снимки nsmart обновляются ~15–20 с и часто почти пустые. NStupido запоминает каждый автобус по ключу (остановка, гараж, линия) и сам отсчитывает ETA, пока не придёт свежее значение. Удаление: 0 с уже 30 с без появления в снимке; не видели дольше `последнее значение + 120 с` (≤20 мин); тот же рейс уже после нашей остановки; новый рейс того же автобуса на линии. `state.json` рядом со скриптом; сервер копит память даже без клиентов.

## Предыдущие остановки (фидеры)

Ответ самой остановки часто пуст, а предыдущая по маршруту показывает те же автобусы раньше. ETA = ETA до предыдущей + разница `second_left_by_route`. Помечены `"source":"via 6540"`. Сиды: 6539 ← 6540/6750/6679; 6712 ← 6586/6728; 15889 — нет (перед ней конечные с фейковыми `P1`…). Опрос через раз (~30 с). `--no-feeders` отключает.

## Расписание (кратко)

Раз в сутки (и при старте) читается тип дня на gspns + `vaziod`, качаются R/S/N/P для линий из `STOP_LINES` → `cache/gspns/`. Fallback типа дня: праздник → P, сб → S, вс → N, иначе R. Рейсы после полуночи относятся к предыдущему сервисному дню. В окне ~40 мин до ETA; через 3 мин после due без автобуса — пропадают. Сопоставление с live по `entered_departure_time` ±1 мин или ближайший ±6 мин. `--no-schedule` / `--schedule-only`; на сервере `?schedule=0` / `?schedule=only`.

## Формат JSON

По остановкам (по умолчанию):

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

`--merge` / `?merge=1`: один `buses[]` с `stop_uid` / `stop_name`; ошибки остановок — в `errors`.

| поле | значение |
|---|---|
| `line` / `direction` | линия и конечная |
| `minutes` / `seconds` | до прибытия |
| `stops_between` | остановок до вас (`null` у scheduled) |
| `garage_no`, `lat`, `lng`, `current_stop` | позиция (`null` у scheduled) |
| `status` / `label` / `live` | live / lost / scheduled |
| `last_seen` | секунд с последнего снимка (`null` у scheduled) |
| `scheduled_departure` / `expected` | HH:MM |
| `source` | `direct` или `via <uid>` |
| `variant` / `trip_id` | вариант gspns и id рейса |

У остановки: `updated` — секунд с последнего успешного upstream. При сетевой ошибке есть `error`, но память всё равно отдаётся. Сортировка по `seconds`. Код выхода CLI 1, если все запросы с ошибкой и показать нечего. HTTP: 200 / 400 / 404 / 502.

## Замечания

- nsmart часто с коротким горизонтом → нужны память, фидеры и расписание.
- Список линий для 6712/15889 частично предположительный; 56 не включена (по mreza здесь не проходит). Объявления на `gspns.rs/aktuelno` скрипт не читает.
- `radius` на прогноз прибытия не влияет (только на поиск ближайших остановок).
- Названия в ответе обрезаются до 50 символов — восстанавливаются из словаря / `stations_cache.json`.
- Найти uid: `POST …/getAllStations` (поля `id`, `name`).
