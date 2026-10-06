#!/usr/bin/env python3
"""Feed the NStupido OLED board (ESP32 over USB serial) with upcoming buses.

Reads JSON from a running `nstupido.py --serve` (starts one itself if needed)
and sends Belgrade-style rows to the ESP32:
  R|<line>|G:<min>|E:<min>|P:<min>
    G = live GPS ("Sveze")
    E = estimate after lose ("Videli-izgubili")
    P = timetable ("Po rasporedu")

Usage: python3 oled_bridge.py [--stop 6539] [--port /dev/cu.usbmodem01]
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


def build_frame(data, stop, max_rows=4):
    buses = data.get("buses") or []
    by_line = {}
    for b in sorted(buses, key=lambda b: b.get("seconds") if b.get("seconds") is not None else 10**9):
        by_line.setdefault(b.get("line") or "?", []).append(b)
    lines = sorted(by_line.items(),
                   key=lambda kv: kv[1][0].get("seconds") if kv[1][0].get("seconds") is not None else 10**9)[:max_rows]
    out = [f"H|{stop}|{datetime.now().strftime('%H:%M')}"]
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
    ap.add_argument("--stop", default="6539")
    ap.add_argument("--port", default=None, help="serial port (auto: /dev/cu.usbmodem*)")
    ap.add_argument("--server-port", type=int, default=8080)
    ap.add_argument("--every", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    server = f"http://127.0.0.1:{a.server_port}"

    # Keep running even if the parent shell exits.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    proc = None
    ser = None
    try:
        while True:
            try:
                proc = ensure_server(server, a.server_port, proc)
                if ser is None:
                    port = a.port or find_port()
                    if not port:
                        raise serial.SerialException("ESP32 not found")
                    print(f"opening {port}", flush=True)
                    ser = open_serial(port)
                data = fetch(server, a.stop)
                frame = build_frame(data, a.stop)
                ser.write(("\n".join(frame) + "\n").encode("ascii", "replace"))
                ser.flush()
                print(datetime.now().strftime("%H:%M:%S"),
                      " ".join(frame[1:-1]) or "(nema autobusa)", flush=True)
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
