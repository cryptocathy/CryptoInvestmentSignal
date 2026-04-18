#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Signal monitor — runs crypto_signal.py on a fixed interval so the full
signal → orders → watcher workflow keeps looping automatically.

Usage:
  python monitor.py                     # foreground loop, Ctrl+C to stop
  python monitor.py --interval 10       # custom interval in minutes (default: 5)
  python monitor.py --background        # spawn as detached background process
  python monitor.py --stop              # stop a running background monitor
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR     = Path(__file__).parent
LOCK_FILE    = BASE_DIR / "monitor.lock"
LOG_FILE     = BASE_DIR / "logs" / "monitor.log"
SIGNAL_SCRIPT = BASE_DIR / "crypto_signal.py"
DEFAULT_INTERVAL_MIN = 5


# ── Helpers ────────────────────────────────────────────────────────────────────

def log(msg, also_print=True):
    LOG_FILE.parent.mkdir(exist_ok=True)
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    if also_print:
        print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_lock():
    LOCK_FILE.write_text(str(os.getpid()))


def remove_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def monitor_pid():
    if not LOCK_FILE.exists():
        return None
    try:
        return int(LOCK_FILE.read_text().strip())
    except Exception:
        return None


def pid_alive(pid):
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def is_monitor_running():
    pid = monitor_pid()
    if pid and pid_alive(pid):
        return True
    # Stale lock
    if LOCK_FILE.exists():
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    return False


# ── Stop ───────────────────────────────────────────────────────────────────────

def cmd_stop():
    pid = monitor_pid()
    if pid and pid_alive(pid):
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
        for _ in range(10):
            if not LOCK_FILE.exists():
                break
            time.sleep(0.3)
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        print(f"Monitor stopped (PID {pid}).")
    else:
        print("Monitor was not running.")


# ── Run one signal cycle ───────────────────────────────────────────────────────

def run_signal_once(run_number):
    log(f"=== Run #{run_number} starting ===")
    result = subprocess.run(
        [sys.executable, str(SIGNAL_SCRIPT)],
        capture_output=False,   # inherit stdout/stderr so output is visible
    )
    rc = result.returncode
    log(f"=== Run #{run_number} finished (exit code {rc}) ===")
    return rc


# ── Foreground loop ────────────────────────────────────────────────────────────

def foreground_loop(interval_min):
    sep = "=" * 60
    print(sep)
    print(f"  Signal Monitor  —  every {interval_min} min  |  Ctrl+C to stop")
    print(sep)

    run_number = 0
    try:
        while True:
            run_number += 1
            run_signal_once(run_number)

            wait_secs = interval_min * 60
            deadline  = time.monotonic() + wait_secs
            print(f"\nNext run in {interval_min}m — press Ctrl+C to stop.\n")

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                mins, secs = divmod(int(remaining), 60)
                # Overwrite same line with countdown
                print(f"  Next run in {mins}m {secs:02d}s ...   ", end="\r", flush=True)
                time.sleep(1)

            print()  # newline after countdown

    except KeyboardInterrupt:
        print("\n\nMonitor stopped by user.")


# ── Background loop (called after re-spawn) ────────────────────────────────────

def background_loop(interval_min):
    write_lock()
    log(f"Monitor started in background (PID {os.getpid()}) | interval: {interval_min}m")
    try:
        run_number = 0
        while True:
            run_number += 1
            run_signal_once(run_number)
            log(f"Sleeping {interval_min}m until next run.", also_print=False)
            time.sleep(interval_min * 60)
    except KeyboardInterrupt:
        log("Monitor interrupted.")
    except Exception as e:
        log(f"Monitor error: {e}")
        raise
    finally:
        remove_lock()
        log("Monitor exited.")


# ── Spawn as detached background process ──────────────────────────────────────

def cmd_background(interval_min):
    if is_monitor_running():
        pid = monitor_pid()
        print(f"Monitor already running (PID {pid}). Use --stop first.")
        return

    LOG_FILE.parent.mkdir(exist_ok=True)
    with open(LOG_FILE, "a") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, str(__file__), "--_run-background",
             "--interval", str(interval_min)],
            stdout=log_fh,
            stderr=log_fh,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    # Brief pause so the lock file is written before we read it
    time.sleep(0.5)
    print(f"Monitor started in background (PID {proc.pid}). Output -> logs/monitor.log")
    print(f"Stop with:  python monitor.py --stop")


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_interval(args):
    try:
        idx = args.index("--interval")
        return int(args[idx + 1])
    except (ValueError, IndexError):
        return DEFAULT_INTERVAL_MIN


if __name__ == "__main__":
    args = sys.argv[1:]
    interval = parse_interval(args)

    if "--stop" in args:
        cmd_stop()
    elif "--background" in args:
        cmd_background(interval)
    elif "--_run-background" in args:
        # Internal flag — called by cmd_background via Popen
        background_loop(interval)
    else:
        foreground_loop(interval)
