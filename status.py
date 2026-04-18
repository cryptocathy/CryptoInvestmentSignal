#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Watcher control & status script.

  python status.py          — show status
  python status.py --stop   — kill the watcher and cancel all pending orders
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR          = Path(__file__).parent


def fp(v):
    """Format a price with enough decimal places regardless of magnitude."""
    if v is None:
        return "N/A"
    if v == 0:
        return "$0"
    if v >= 1:
        return f"${v:,.4f}"
    import math
    decimals = max(4, -int(math.floor(math.log10(abs(v)))) + 3)
    return f"${v:.{decimals}f}"
LOCK_FILE         = BASE_DIR / "watcher.lock"
MONITOR_LOCK_FILE = BASE_DIR / "monitor.lock"
TRADER_LOCK_FILE  = BASE_DIR / "trader.lock"
PENDING_FILE      = BASE_DIR / "pending_orders.json"
TRADES_FILE       = BASE_DIR / "dry_run_trades.json"
LAST_RUN_FILE     = BASE_DIR / "last_run.txt"
LOG_FILE          = BASE_DIR / "logs" / "watcher.log"
MONITOR_LOG_FILE  = BASE_DIR / "logs" / "monitor.log"
TRADER_LOG_FILE   = BASE_DIR / "logs" / "trader.log"


def watcher_pid():
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


def last_log_line():
    if not LOG_FILE.exists():
        return None
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return next((l for l in reversed(lines) if l.strip()), None)
    except Exception:
        return None


def monitor_pid():
    if not MONITOR_LOCK_FILE.exists():
        return None
    try:
        return int(MONITOR_LOCK_FILE.read_text().strip())
    except Exception:
        return None


def last_monitor_log_line():
    if not MONITOR_LOG_FILE.exists():
        return None
    try:
        lines = MONITOR_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return next((l for l in reversed(lines) if l.strip()), None)
    except Exception:
        return None


def trader_pid():
    if not TRADER_LOCK_FILE.exists():
        return None
    try:
        return int(TRADER_LOCK_FILE.read_text().strip())
    except Exception:
        return None


def last_trader_log_line():
    if not TRADER_LOG_FILE.exists():
        return None
    try:
        lines = TRADER_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return next((l for l in reversed(lines) if l.strip()), None)
    except Exception:
        return None


def stop_watcher():
    """Kill the watcher process and cancel all pending orders."""
    pid = watcher_pid()
    if pid and pid_alive(pid):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
            # Wait for lock file to disappear, then force-remove it
            import time
            for _ in range(10):
                if not LOCK_FILE.exists():
                    break
                time.sleep(0.3)
            try:
                LOCK_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            print(f"Watcher stopped (PID {pid}).")
        except Exception as e:
            print(f"Failed to kill watcher: {e}")
    else:
        print("Watcher was not running.")

    # Mark all pending orders as cancelled
    if PENDING_FILE.exists():
        try:
            data    = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
            orders  = data.get("orders", [])
            now_iso = datetime.now(timezone.utc).isoformat()
            n = 0
            for o in orders:
                if o.get("status") == "pending":
                    o["status"]        = "cancelled"
                    o["cancelled_at"]  = now_iso
                    o["cancel_reason"] = "manually stopped via status.py --stop"
                    n += 1
            if n:
                PENDING_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
                print(f"{n} pending order(s) marked as cancelled.")
            else:
                print("No pending orders to cancel.")
        except Exception as e:
            print(f"Error updating pending orders: {e}")


def fmt_mins(dt, now):
    diff = (now - dt).total_seconds()
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff/60)}m ago"
    return f"{diff/3600:.1f}h ago"


def main():
    now = datetime.now(timezone.utc)
    sep = "=" * 60

    print(sep)
    print(f"  Crypto Signal Status  —  {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(sep)

    # ── Last signal run ───────────────────────────────────────────────────────
    if LAST_RUN_FILE.exists():
        try:
            last = datetime.fromisoformat(LAST_RUN_FILE.read_text().strip())
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            print(f"\nLast signal run : {last.strftime('%Y-%m-%d %H:%M UTC')} ({fmt_mins(last, now)})")
        except Exception:
            print("\nLast signal run : unknown")
    else:
        print("\nLast signal run : never")

    # ── Monitor process ───────────────────────────────────────────────────────
    mpid = monitor_pid()
    if mpid and pid_alive(mpid):
        print(f"Monitor         : RUNNING  (PID {mpid})  — python monitor.py --stop to stop")
        ml = last_monitor_log_line()
        if ml:
            print(f"Last monitor log: {ml.strip()}")
    elif mpid:
        print(f"Monitor         : STALE LOCK (PID {mpid} not found) — monitor stopped")
    else:
        print("Monitor         : NOT running  — python monitor.py [--background] to start")

    # ── Active trader process ─────────────────────────────────────────────────
    tpid = trader_pid()
    if tpid and pid_alive(tpid):
        print(f"Active trader   : RUNNING  (PID {tpid})  — python trader.py --stop to stop")
        tl = last_trader_log_line()
        if tl:
            print(f"Last trader log : {tl.strip()}")
    elif tpid:
        print(f"Active trader   : STALE LOCK (PID {tpid} not found) — trader stopped")
    else:
        print("Active trader   : NOT running  — python trader.py [--background] to start")

    # ── Watcher process ───────────────────────────────────────────────────────
    pid = watcher_pid()
    if pid and pid_alive(pid):
        print(f"Watcher         : RUNNING  (PID {pid})")
        ll = last_log_line()
        if ll:
            print(f"Last watcher log: {ll.strip()}")
    elif pid:
        print(f"Watcher         : STALE LOCK (PID {pid} not found) — watcher stopped")
    else:
        print("Watcher         : NOT running")

    # ── Pending orders ────────────────────────────────────────────────────────
    print()
    if not PENDING_FILE.exists():
        print("Pending orders  : none (file absent)")
    else:
        try:
            data    = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
            orders  = data.get("orders", [])
            pending = [o for o in orders if o.get("status") == "pending"]
            recent_terminal = [
                o for o in orders
                if o.get("status") not in ("pending",)
            ][-5:]  # last 5 resolved

            if pending:
                print(f"Pending orders  : {len(pending)}")
                for o in pending:
                    lp  = o.get("limit_price")
                    sp  = o.get("stop_price")
                    trig = f"limit ${lp:,.4f}" if lp else f"stop ${sp:,.4f}" if sp else "MARKET"
                    exp  = o.get("expires_at", "")
                    time_tag = ""
                    if exp:
                        try:
                            exp_dt = datetime.fromisoformat(exp)
                            ml = int((exp_dt - now).total_seconds() / 60)
                            time_tag = f"  ({ml}m left)" if ml > 0 else "  (EXPIRED)"
                        except Exception:
                            pass
                    print(
                        f"  [{o['side']} {o['asset']} {o.get('order_type','MARKET')}]  "
                        f"{float(o.get('quantity_coin',0)):.6f} coins  "
                        f"{trig}{time_tag}"
                    )
                    print(f"    id: {o['id']}")
                    print(f"    rationale: {o.get('rationale','')[:90]}")
            else:
                print("Pending orders  : none")

            if recent_terminal:
                print(f"\nRecent resolved ({len(recent_terminal)}):")
                for o in recent_terminal:
                    status = o.get("status", "?")
                    ts     = o.get("executed_at") or o.get("cancelled_at") or o.get("expires_at") or "?"
                    ep     = o.get("executed_price")
                    price_str = f" @ {fp(ep)}" if ep else ""
                    print(f"  {o['id']:<32s}  {status}{price_str}  {ts[:16]}")
        except Exception as e:
            print(f"Pending orders  : error — {e}")

    # ── Dry-run trade log ─────────────────────────────────────────────────────
    print()
    if TRADES_FILE.exists():
        try:
            trades = json.loads(TRADES_FILE.read_text(encoding="utf-8"))
            if trades:
                print(f"Dry-run trades  : {len(trades)} total")
                for t in trades[-5:]:
                    print(
                        f"  {t['timestamp'][:16]}  {t['side']} {t['asset']}  "
                        f"{t['exec_qty']:.6f} @ {fp(t['exec_price'])}  "
                        f"= ${t['exec_usdt']:,.2f}  [{t['mode']}]"
                    )
            else:
                print("Dry-run trades  : none yet")
        except Exception as e:
            print(f"Dry-run trades  : error — {e}")
    else:
        print("Dry-run trades  : none yet")

    print(f"\n{sep}")


def stop_monitor():
    """Kill the background monitor process."""
    mpid = monitor_pid()
    if mpid and pid_alive(mpid):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(mpid)], capture_output=True, timeout=5)
            import time
            for _ in range(10):
                if not MONITOR_LOCK_FILE.exists():
                    break
                time.sleep(0.3)
            try:
                MONITOR_LOCK_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            print(f"Monitor stopped (PID {mpid}).")
        except Exception as e:
            print(f"Failed to kill monitor: {e}")
    else:
        print("Monitor was not running.")


def stop_trader():
    tpid = trader_pid()
    if tpid and pid_alive(tpid):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(tpid)], capture_output=True, timeout=5)
            import time
            for _ in range(10):
                if not TRADER_LOCK_FILE.exists():
                    break
                time.sleep(0.3)
            try:
                TRADER_LOCK_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            print(f"Active trader stopped (PID {tpid}).")
        except Exception as e:
            print(f"Failed to kill active trader: {e}")
    else:
        print("Active trader was not running.")


if __name__ == "__main__":
    if "--stop" in sys.argv:
        stop_trader()
        stop_monitor()
        stop_watcher()
    else:
        main()
