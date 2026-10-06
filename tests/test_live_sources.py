"""LIVE NETWORK TESTS — run when checking upstream health.

  python3 -m unittest tests.test_live_sources -v
  # or together with offline tests:
  python3 -m unittest discover -s tests -v

These hit the real network (nsmart.rs, gspns.rs). Failures name the host/URL.
Empty bus lists from nsmart are OK; HTTP errors, timeouts, and non-JSON are not.
"""
import json
import os
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import nstupido as N  # noqa: E402
import timetable as T  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (NStupido live tests)"}
NSMART_ARRIVALS = "https://online.nsmart.rs/sr/najava-dolaska/"
NSMART_STATIONS = "https://online.nsmart.rs/sr/AnnouncementForStation/getAllStations"
GSPNS_GRADSKI = "http://gspns.rs/red-voznje/gradski?selected_lang=lat"
TIMEOUT = 15


def _post(url, form, timeout=TIMEOUT):
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, headers={
        **UA,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return r.status, body


def _get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return r.status, body


class LiveNsmart(unittest.TestCase):
    def test_nsmart_arrivals_reachable(self):
        url = NSMART_ARRIVALS
        try:
            status, body = _post(url, {
                "station_uid": "6539",
                "ibfm": "TS001831",
                "direction": 2,
                "company_info_id": 216,
                "radius": "",
            })
        except urllib.error.HTTPError as e:
            self.fail("nsmart arrivals HTTP %s for %s: %s" % (e.code, url, e.reason))
        except Exception as e:
            self.fail("nsmart arrivals unreachable (%s): %r" % (url, e))
        self.assertEqual(status, 200, "nsmart arrivals expected HTTP 200 from %s, got %s" % (url, status))
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as e:
            snippet = body[:200]
            self.fail("nsmart arrivals returned non-JSON from %s: %r body[:200]=%r" % (url, e, snippet))
        # Upstream shape is a JSON list: buses, or [{just_coordinates:"1", station_name, ...}] when empty.
        # [false, code] is an API error. HTML/502 would already have failed above.
        self.assertIsInstance(data, list,
                              "nsmart arrivals JSON must be a list (empty-stop OK); got %s from %s"
                              % (type(data).__name__, url))
        self.assertGreater(len(data), 0, "nsmart arrivals empty list from %s" % url)
        if data[0] is False:
            self.fail("nsmart arrivals API error [false, ...] from %s: %r" % (url, data[:3]))
        self.assertIsInstance(data[0], dict,
                              "nsmart arrivals[0] must be a dict; got %s from %s" % (type(data[0]).__name__, url))

    def test_nsmart_stations_list_reachable(self):
        url = NSMART_STATIONS
        try:
            status, body = _post(url, {})  # same as nstupido: empty POST body
        except urllib.error.HTTPError as e:
            self.fail("nsmart getAllStations HTTP %s for %s: %s" % (e.code, url, e.reason))
        except Exception as e:
            self.fail("nsmart getAllStations unreachable (%s): %r" % (url, e))
        self.assertEqual(status, 200, "getAllStations expected HTTP 200 from %s, got %s" % (url, status))
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as e:
            self.fail("getAllStations non-JSON from %s: %r" % (url, e))
        self.assertTrue(isinstance(data, (list, dict)),
                        "getAllStations expected list/dict from %s, got %s" % (url, type(data).__name__))
        if isinstance(data, list):
            self.assertGreater(len(data), 0, "getAllStations list empty from %s" % url)
            sample = data[0]
            self.assertTrue(isinstance(sample, dict) and ("id" in sample or "name" in sample),
                            "getAllStations items should have id/name; got keys %s" % list(sample)[:8])
        else:
            self.assertGreater(len(data), 0, "getAllStations dict empty from %s" % url)


class LiveGspns(unittest.TestCase):
    def test_gspns_gradski_page_reachable(self):
        url = GSPNS_GRADSKI
        try:
            status, body = _get(url)
        except urllib.error.HTTPError as e:
            self.fail("gspns gradski HTTP %s for %s: %s" % (e.code, url, e.reason))
        except Exception as e:
            self.fail("gspns gradski unreachable (%s): %r" % (url, e))
        self.assertEqual(status, 200, "gspns gradski expected HTTP 200 from %s, got %s" % (url, status))
        text = body.decode("utf-8", "replace")
        low = text.lower()
        markers = ("red vožnje", "red voznje", "danas", "vaziod", "radni dan", "subota", "nedelja", "praznik")
        self.assertTrue(any(m in low for m in markers),
                        "gspns gradski body from %s missing expected markers %s; len=%d"
                        % (url, markers, len(text)))

    def test_gspns_timetable_fetchable(self):
        """Fetch ispis-polazaka for urban line 3, weekday, using current vaziod from the index page."""
        try:
            html = T._get("/red-voznje/gradski", {"selected_lang": "lat"})
        except Exception as e:
            self.fail("gspns index unreachable (http://gspns.rs/red-voznje/gradski): %r" % e)
        idx = T.parse_index(html)
        vaziod = (idx.get("vaziod") or [None])[0]
        if not vaziod:
            # fall back to known recent validity date from cache if present
            vaziod = "2026-10-01"
        dan = idx.get("day_type") or "R"
        params = {"rv": "rvg", "vaziod": vaziod, "dan": dan, "linija[]": ["3."]}
        url = T.GSPNS + "/red-voznje/ispis-polazaka?" + urllib.parse.urlencode(params, doseq=True)
        try:
            html2 = T._get("/red-voznje/ispis-polazaka", params)
        except Exception as e:
            self.fail("gspns ispis-polazaka unreachable (%s): %r" % (url, e))
        tables = T.parse_timetables(html2)
        self.assertTrue(tables, "ispis-polazaka for line 3 returned no tables (%s)" % url)
        # expect at least one direction with departure-like HH:MM entries
        deps = []
        for base, table in tables.items():
            for d in table.get("directions") or []:
                deps.extend(d.get("departures") or [])
        self.assertGreater(len(deps), 0,
                           "ispis-polazaka parsed but no departures for line 3 (%s); keys=%s"
                           % (url, list(tables.keys())))


class LiveCli(unittest.TestCase):
    def test_nstupido_cli_returns_json(self):
        """Call fetch() for stop 6539 (safer than subprocess) and assert structured result."""
        try:
            result = N.fetch("6539")
        except Exception as e:
            self.fail("nstupido.fetch('6539') raised: %r (host %s)" % (e, N.API_URL))
        self.assertIsInstance(result, dict, "fetch() must return a dict, got %s" % type(result).__name__)
        # Either buses/name structure, or an error object — both are valid JSON shapes from fetch()
        if "error" in result:
            # Network/API down: still a structured answer, but fail so agent notices upstream is broken
            self.fail("nstupido.fetch('6539') returned error (upstream down?): %s" % result["error"])
        self.assertIn("buses", result, "fetch() ok result missing 'buses': keys=%s" % list(result.keys()))
        self.assertIsInstance(result["buses"], list)
        # name may be None on odd responses, but usually present
        # empty buses is OK (nsmart often empty)


if __name__ == "__main__":
    unittest.main()
