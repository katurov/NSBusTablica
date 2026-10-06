# HOW_TO_TEST_DATA — playbook для агента

Цель: быстро понять, **приходят ли живые данные** и **какой из источников сломан**.
Запускать из корня проекта (`/…/NStupido`).

## 1. Офлайн unit-тесты (без сети)

```bash
python3 -m unittest discover -s tests -v
```

или только расписание/память:

```bash
python3 -m unittest tests.test_schedule -v
```

**Pass** = тип дня, полночь, смена `vaziod`, сопоставление рейсов, фидеры — логика ок.
**Fail** = регрессия в коде, не в upstream. Чинить код / фикстуры в `tests/fixtures/`.

## 2. Live connectivity (сеть обязательна)

```bash
python3 -m unittest tests.test_live_sources -v
```

или вместе со всеми тестами:

```bash
python3 -m unittest discover -s tests -v
```

| Тест | Что проверяет | Pass | Fail |
|---|---|---|---|
| `test_nsmart_arrivals_reachable` | `POST https://online.nsmart.rs/sr/najava-dolaska/` (station_uid=6539) | HTTP 200 + JSON-**list** (пустой stop = `[{just_coordinates…}]` **OK**) | timeout / 5xx / HTML / не JSON / `[false,…]` |
| `test_nsmart_stations_list_reachable` | `POST …/AnnouncementForStation/getAllStations` | JSON list/dict с хотя бы одной станцией (`id`/`name`) | сеть / пустой ответ |
| `test_gspns_gradski_page_reachable` | `GET http://gspns.rs/red-voznje/gradski` | 200 + маркеры `danas` / `vaziod` / `red vožnje` | сайт лежит / другой HTML |
| `test_gspns_timetable_fetchable` | `ispis-polazaka` для линии 3 (dan=R или сегодня) | распарсены отправления HH:MM | пустые таблицы / сеть |
| `test_nstupido_cli_returns_json` | `nstupido.fetch("6539")` | dict с `buses` (список, может быть `[]`) | traceback / `error` в ответе |

Сообщение fail всегда содержит URL/host — по нему видно, какой upstream мёртв.

## 3. Ручной smoke CLI

```bash
python3 nstupido.py 6539 --pretty
# без записи памяти:
python3 nstupido.py 6539 --pretty --no-state
# только расписание (без nsmart):
python3 nstupido.py 6539 --pretty --schedule-only
```

**Здоровый JSON** (один stop):

- есть `"stop": {"uid":"6539","name":…}` и `"buses":[…]`;
- в `buses` могут быть записи со `status`: `live` / `lost` / `scheduled`;
- **не** одно только `"error":{"type":"network",…}` без автобусов из памяти;
- пустой `"buses":[]` при рабочем nsmart + днём без рейсов по расписанию в окне 40 мин — редко, но бывает; тогда проверь `--schedule-only` и другую остановку (`15889`, `6712`).

Признаки:

| Симптом | Значение |
|---|---|
| `error.type=network` / DNS / timeout | API/сайт недоступен с этой машины |
| `error.type=http` / `api` / `bad_response` | nsmart ответил плохо (не HTML-страница логина — у нас нет auth) |
| `buses:[]`, ошибок нет, `--schedule-only` тоже пусто | окно без рейсов / `STOP_LINES` не покрывает линии |
| Есть только `scheduled`, нет `live`/`lost` | nsmart пустой (нормально часто), расписание работает |
| Есть `live` с `source:"via 6540"` | фидеры работают, сама остановка пустая |

## 4. Smoke HTTP-сервера

```bash
python3 nstupido.py --serve --port 8080 --host 127.0.0.1
# в другом терминале:
curl -sS http://127.0.0.1:8080/buses | python3 -m json.tool | head -80
curl -sS "http://127.0.0.1:8080/buses?stop=6539" | python3 -m json.tool | head -60
curl -sS http://127.0.0.1:8080/debug | python3 -m json.tool | head -80
```

`/debug` показывает `timetable` (vaziod, day_type, last_error), feeders, learn.
502 на `/buses` = у всех запрошенных остановок ошибка и нечего показать.

## 5. Прямые пробы upstream (если тесты красные)

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

# gspns timetable (подставь актуальный vaziod из index / cache/gspns/index.json)
curl -sS 'http://gspns.rs/red-voznje/ispis-polazaka?rv=rvg&vaziod=2026-10-01&dan=R&linija%5B%5D=3.' | head -c 2000
```

## 6. OLED / USB-мост

1. Сервер: `python3 nstupido.py --serve --port 8080`
2. Прошивка уже залита (`pio run -t upload` в `firmware/`).
3. Мост: `pip install -r requirements.txt` → `python3 oled_bridge.py --stop 6539`
4. **Не открывай** тот же serial-порт в `pio device monitor` / другом процессе, пока мост его держит — будет «veza izgubljena» / отказ порта.
5. Яркость: `OLED_CONTRAST` в `firmware/platformio.ini` (сейчас 80 из 255).

## 7. Чеклист успеха для агента

- [ ] `python3 -c "import nstupido, timetable, rs_holidays"` без ошибок
- [ ] Offline: `unittest discover -s tests` — все `test_schedule` зелёные
- [ ] Live: `tests.test_live_sources` — все 5 зелёные (или ясно указано, что сеть с box/машины недоступна)
- [ ] CLI `nstupido.py 6539 --pretty` отдаёт JSON со `stop` + `buses` (или осмысленный `error`)
- [ ] При необходимости: `/buses` и `/debug` отвечают 200
- [ ] Если чинил upstream-парсинг — обнови фикстуры в `tests/fixtures/`, не мокай live-тесты

Ключей API нет. Источники недокументированы и могут поменяться без предупреждения.
