#!/usr/bin/env python3
"""Feed the NStupido OLED board (ESP32 over USB serial) with upcoming buses.

Reads JSON from a running `nstupido.py --serve` (starts one itself if needed)
and sends Belgrade-style rows to the ESP32:
  R|<line>|G:<min>|E:<min>|P:<min>
    G = live GPS ("Sveze")
    E = estimate after lose ("Videli-izgubili")
    P = timetable ("Po rasporedu")

The header row shows the current stop and its position, e.g. `H|6539 1/3|17:42`.

Button: the board sends `BTN` over the same serial link when its BOOT button
(GPIO0) is pressed; the bridge then switches to the next stop of the list
(wrapping around) and pushes that stop's frame right away. The stop list is
`--stops` if given, else the server's `default_stops` (GET /), and always
starts at `--stop`.

Usage: python3 oled_bridge.py [--stop 6539] [--stops 6539,15889,6712] [--port /dev/cu.usbmodem01]
"""
import argparse, glob, json, os, signal, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime

import serial

HERE = os.path.dirname(os.path.abspath(__file__))


def find_port():
    ports = sorted(glob.glob("/dev/cu.usbmodem*")) + sorted(glob.glob("/dev/ttyACM*"))
    return ports[0] if ports else None


def fetch(server, stop):
    with urllib.request.urlopen(f"{server}/buses?stop={stop}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def server_up(server):
    try:
        urllib.request.urlopen(f"{server}/", timeout=3).read()
        return True
    except Exception:
        return False


def start_server(port):
    print(f"starting nstupido server on :{port}", flush=True)
    return subprocess.Popen(
        [sys.executable, os.path.join(HERE, "nstupido.py"), "--serve",
         "--port", str(port), "--host", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=HERE,
        start_new_session=True,
    )


def ensure_server(server, port, proc):
    if server_up(server):
        return proc
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try: proc.kill()
            except Exception: pass
    proc = start_server(port)
    for _ in range(40):
        time.sleep(1)
        if server_up(server):
            return proc
        if proc.poll() is not None:
            print(f"server exited with {proc.returncode}, restarting", flush=True)
            proc = start_server(port)
    print("server still not up, will keep trying", flush=True)
    return proc


def server_stops(server):
    """Stop uids the server polls by default (GET / -> default_stops), or [] if unknown."""
    try:
        with urllib.request.urlopen(f"{server}/", timeout=5) as r:
            stops = json.loads(r.read().decode("utf-8")).get("default_stops") or []
        return [str(x) for x in stops if str(x).strip().isdigit()]
    except Exception:
        return []


class StopCycler:
    """Ordered list of stops with a current index; next() wraps around."""

    def __init__(self, stops, start=None):
        seen = []
        for st in stops or []:
            st = str(st).strip()
            if st and st not in seen:
                seen.append(st)
        start = str(start).strip() if start is not None else None
        if start and start not in seen:
            seen.insert(0, start)
        if not seen:
            raise ValueError("no stops")
        self.stops = seen
        self.index = seen.index(start) if start else 0

    @property
    def current(self):
        return self.stops[self.index]

    def next(self):
        self.index = (self.index + 1) % len(self.stops)
        return self.current

    def update(self, stops):
        """Replace the list (e.g. once the server answers), keeping the current stop."""
        cur = self.current
        new = StopCycler(stops, start=cur)
        self.stops, self.index = new.stops, new.index

    def label(self):
        if len(self.stops) == 1:
            return self.current
        return f"{self.current} {self.index + 1}/{len(self.stops)}"


class LineReader:
    """Non-blocking line reader over a pyserial-like object (in_waiting / read)."""

    def __init__(self, maxlen=200):
        self.buf = b""
        self.maxlen = maxlen

    def poll(self, ser):
        n = ser.in_waiting
        if n:
            self.buf += ser.read(n)
        lines = []
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            line = raw.decode("ascii", "replace").strip()
            if line:
                lines.append(line)
        if len(self.buf) > self.maxlen:      # garbage without newline
            self.buf = b""
        return lines


def count_presses(lines):
    """Number of button events among lines received from the board."""
    return sum(1 for ln in lines if ln.upper() in ("BTN", "NEXT"))


def build_frame(data, stop, max_rows=4, label=None):
    """Text frame for the board; `label` (e.g. "6539 1/3") replaces the stop in the header."""
    buses = data.get("buses") or []
    by_line = {}
    for b in sorted(buses, key=lambda b: b.get("seconds") if b.get("seconds") is not None else 10**9):
        by_line.setdefault(b.get("line") or "?", []).append(b)
    lines = sorted(by_line.items(),
                   key=lambda kv: kv[1][0].get("seconds") if kv[1][0].get("seconds") is not None else 10**9)[:max_rows]
    out = [f"H|{label or stop}|{datetime.now().strftime('%H:%M')}"]
    for line, bl in lines:
        cells = []
        for b in bl[:2]:
            secs = b.get("seconds")
            mins = max(0, int(secs // 60)) if secs is not None else 0
            st = b.get("status")
            if st == "live":
                tag = "G"
            elif st == "lost":
                tag = "E"
            else:
                tag = "P"
            cells.append(f"{tag}:{mins}")
        out.append("|".join(["R", line[:5]] + cells))
    out.append("E")
    return out


def open_serial(port):
    ser = serial.Serial(port, 115200, timeout=1)
    time.sleep(1.5)
    try:
        ser.reset_input_buffer()
    except Exception:
        pass
    return ser


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop", default="6539", help="stop shown at start")
    ap.add_argument("--stops", default=None,
                    help="comma-separated stops for the BOOT button to cycle through "
                         "(default: server's default_stops)")
    ap.add_argument("--port", default=None, help="serial port (auto: /dev/cu.usbmodem*)")
    ap.add_argument("--server-port", type=int, default=8080)
    ap.add_argument("--every", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    server = f"http://127.0.0.1:{a.server_port}"

    # Keep running even if the parent shell exits.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    fixed = [x for x in (a.stops or "").split(",") if x.strip()]
    cycler = StopCycler(fixed, start=a.stop)
    have_list = bool(fixed)

    proc = None
    ser = None
    reader = LineReader()
    try:
        while True:
            try:
                proc = ensure_server(server, a.server_port, proc)
                if not have_list:
                    lst = server_stops(server)
                    if lst:
                        cycler.update(lst)
                        have_list = True
                        print("stops:", " ".join(cycler.stops), flush=True)
                if ser is None:
                    port = a.port or find_port()
                    if not port:
                        raise serial.SerialException("ESP32 not found")
                    print(f"opening {port}", flush=True)
                    ser = open_serial(port)
                    reader = LineReader()
                stop = cycler.current
                data = fetch(server, stop)
                frame = build_frame(data, stop, label=cycler.label())
                ser.write(("\n".join(frame) + "\n").encode("ascii", "replace"))
                ser.flush()
                print(datetime.now().strftime("%H:%M:%S"), f"[{cycler.label()}]",
                      " ".join(frame[1:-1]) or "(nema autobusa)", flush=True)
                if a.once:
                    break
                # Wait for the next refresh, reading the serial line meanwhile.
                deadline = time.monotonic() + a.every
                while time.monotonic() < deadline:
                    try:
                        presses = count_presses(reader.poll(ser))
                    except serial.SerialException:
                        raise
                    except OSError as e:     # unplugged: ioctl fails -> reopen the port
                        raise serial.SerialException(f"read failed: {e}")
                    if presses:
                        for _ in range(presses):
                            cycler.next()
                        print(datetime.now().strftime("%H:%M:%S"),
                              f"button -> stop {cycler.label()}", flush=True)
                        break           # push the new stop's frame now
                    time.sleep(0.05)
                continue
            except serial.SerialException as e:
                print("serial:", e, flush=True)
                if ser:
                    try: ser.close()
                    except Exception: pass
                ser = None
                time.sleep(2)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
                print("data:", type(e).__name__, e, flush=True)
                # force server re-check next loop
                time.sleep(2)
            except Exception as e:
                print("error:", type(e).__name__, e, flush=True)
                time.sleep(2)
            if a.once:
                break
            time.sleep(a.every)
    finally:
        if ser:
            try: ser.close()
            except Exception: pass
        # Leave the nstupido server running; memory/schedule keep accumulating.


if __name__ == "__main__":
    main()
