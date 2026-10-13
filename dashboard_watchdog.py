"""Keep dashboard.py alive: restart it if the process ever dies.

The MT5 python DLL can occasionally crash the whole interpreter without a
Python traceback (native fault, e.g. during order_send). A watchdog that
relaunches the process is the only layer that can recover from that.

Run DETACHED (its own process tree, survives the shell session):
    powershell -NoProfile -Command "Start-Process pythonw -ArgumentList 'dashboard_watchdog.py' -WorkingDirectory '<dir>'"

Logs every launch/crash to dashboard_watchdog.log so a death is diagnosable.
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

RESTART_DELAY = 3
HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "dashboard_watchdog.log")


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main():
    # The MT5 DLL is loaded by dashboard.py; running under pythonw (no console)
    # is fine. Use the SAME interpreter the user runs with so deps match.
    py = sys.executable
    log(f"watchdog starting (pid={os.getpid()}, python={py})")
    while True:
        log("launching dashboard.py")
        proc = subprocess.Popen([py, "dashboard.py"], cwd=HERE)
        rc = proc.wait()
        log(f"dashboard exited rc={rc}; relaunching in {RESTART_DELAY}s")
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL watchdog error: {type(e).__name__}: {e}")
        raise
