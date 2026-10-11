#!/bin/sh
# Run the NStupido server detached (survives closing the terminal / logout of the shell).
#   ./nstupido-server.sh start|stop|restart|status     [extra nstupido.py args, e.g. --port 8080]
# Log: nstupido.log (stderr copy of the syslog lines), PID: nstupido.pid. Python: $PYTHON or python3.
cd "$(dirname "$0")" || exit 1
PY="${PYTHON:-python3}"
PIDFILE=nstupido.pid
LOG=nstupido.log

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "$1" in
  start)
    if running; then echo "already running, pid $(cat "$PIDFILE")"; exit 0; fi
    shift
    "$PY" -c '
import os, sys
if os.fork() > 0: sys.exit(0)
os.setsid()
if os.fork() > 0: sys.exit(0)
open("nstupido.pid", "w").write(str(os.getpid()))
os.execv(sys.executable, [sys.executable, "nstupido.py", "--serve"] + sys.argv[1:])
' "$@" </dev/null >>"$LOG" 2>&1
    sleep 1
    if running; then echo "started, pid $(cat "$PIDFILE"), log $LOG"; else echo "failed, see $LOG"; exit 1; fi ;;
  stop)
    if running; then kill "$(cat "$PIDFILE")"; sleep 1; echo "stopped"; else echo "not running"; fi
    rm -f "$PIDFILE" ;;
  restart) shift; "$0" stop; "$0" start "$@" ;;
  status) if running; then echo "running, pid $(cat "$PIDFILE")"; else echo "not running"; exit 1; fi ;;
  *) echo "usage: $0 start|stop|restart|status [nstupido.py args]"; exit 2 ;;
esac
