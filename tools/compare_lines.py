#!/usr/bin/env python3
"""Compare the automatic stop -> lines derivation with the hand-made STOP_LINES, departure by departure.

Usage: python3 tools/compare_lines.py [--cache cache] [--date 2026-10-09] [STOP ...]   (default: the 3 default stops)
Offline: uses cache/nsmart/stations.json, cache/gspns/map.json and the cached timetables only (no requests).
Only routes present in the timetable cache can be compared."""
import argparse, collections, json, os, sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import routes as R, timetable as T      # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stops", nargs="*")
    ap.add_argument("--cache", default=os.path.join(ROOT, "cache"))
    ap.add_argument("--date", default=None, help="a weekday; Saturday and Sunday after it are compared too")
    a = ap.parse_args()
    offline = lambda *x: (_ for _ in ()).throw(OSError("offline"))
    tt = T.Timetables(cache_dir=os.path.join(a.cache, "gspns"), fetch=offline)
    naming = R.Naming(tt.known_bases())
    st = R.Stations(os.path.join(a.cache, "nsmart", "stations.json"))
    mp = R.NetworkMap(os.path.join(a.cache, "gspns", "map.json"))
    res = R.Resolver(naming, st, mp, manual=T.STOP_LINES, rv_of=lambda b: tt.known_bases().get(b))
    d0 = date.fromisoformat(a.date) if a.date else date.today()
    while d0.weekday() > 4:
        d0 += timedelta(days=1)
    days = {"R": d0, "S": d0 + timedelta(days=5 - d0.weekday()), "N": d0 + timedelta(days=6 - d0.weekday())}
    for k, d in days.items():
        tt.index["day_types"][d.isoformat()] = k

    def trips(uid, manual, day):
        tt.resolver = lambda s: res(s, use_manual=manual)
        out = collections.defaultdict(set)
        for t in tt.trips(uid, day):
            out[naming.display(t["base"])].add((t["dep"].strftime("%H:%M"), t["mark"]))
        return out

    for uid in a.stops or list(T.STOP_LINES):
        res(uid, use_manual=False)
        info = res.info.get(uid) or {}
        print("== %s %s" % (uid, info.get("name")))
        print("   map stop:", json.dumps(info.get("map_stop"), ensure_ascii=False), "labels:", " ".join(info.get("map_labels", [])))
        print("   nsmart lines without map evidence (live-learned only):", info.get("pending_live"))
        for k, day in days.items():
            au, ma = trips(uid, False, day), trips(uid, True, day)
            for b in sorted(set(au) | set(ma), key=lambda x: (len(x), x)):
                if au[b] != ma[b]:
                    print("   %s %-4s auto only %s | manual only %s" % (k, b, sorted(au[b] - ma[b]), sorted(ma[b] - au[b])))
            print("   %s departures: auto %d, manual %d" % (k, sum(map(len, au.values())), sum(map(len, ma.values()))))


if __name__ == "__main__":
    main()
