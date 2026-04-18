#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Order Watcher — Dry Run Mode
Spawned automatically by crypto_signal.py when pending orders are written.
Loops every 60 seconds, checks price triggers, validates against live holdings,
simulates execution, and stops itself when all orders reach a terminal state.

Terminal states per order:
  executed_dry  — trigger met, simulated fill recorded
  expired       — hits expires_at without triggering
  skipped_*     — validation failed (e.g. no coins to sell)
  failed        — live order API error (live mode only)

Set DRY_RUN = False only when ready for live execution AND Binance API key
has "Enable Spot & Margin Trading" permission turned on.
"""

import os, sys, json, hashlib, hmac, time, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

DRY_RUN          = False  # LIVE trading — real orders placed on Binance
LOOP_INTERVAL_S  = 60     # seconds between price checks
MAX_WATCHER_HOURS = 2     # absolute safety stop regardless of order state

BASE_DIR         = Path(__file__).parent
PENDING_FILE     = BASE_DIR / "pending_orders.json"
TRADES_FILE      = BASE_DIR / "dry_run_trades.json"
LOCK_FILE        = BASE_DIR / "watcher.lock"
LOG_FILE         = BASE_DIR / "logs" / "watcher.log"

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_BASE_URL   = "https://api.binance.com"

TERMINAL_STATUSES = {"executed_dry", "executed_live", "expired", "skipped_invalid",
                     "skipped_no_price", "skipped_duplicate", "failed",
                     "cancelled", "cancelled_dry", "cancelled_live"}

# Cache of symbol → LOT_SIZE stepSize fetched from Binance exchange info
_lot_step_cache: dict = {}


def get_lot_step(symbol):
    """Return the LOT_SIZE stepSize for a symbol (cached per session)."""
    if symbol in _lot_step_cache:
        return _lot_step_cache[symbol]
    try:
        r = requests.get(
            f"{BINANCE_BASE_URL}/api/v3/exchangeInfo",
            params={"symbol": symbol},
            timeout=10,
        )
        r.raise_for_status()
        for f in r.json()["symbols"][0]["filters"]:
            if f["filterType"] == "LOT_SIZE":
                _lot_step_cache[symbol] = float(f["stepSize"])
                return _lot_step_cache[symbol]
    except Exception as e:
        log(f"  lot-step fetch error for {symbol}: {e}")
    _lot_step_cache[symbol] = 0.01  # safe fallback
    return _lot_step_cache[symbol]


def round_lot(qty, step):
    """Truncate qty down to the nearest lot step (never round up — avoids over-selling)."""
    import math
    if step <= 0:
        return qty
    precision = max(0, -int(math.floor(math.log10(step))))
    return round(math.floor(qty / step) * step, precision)


# Tracks (asset, side) pairs where a MARKET order was executed this watcher session.
# Prevents a second market order firing if the signal script runs again before the
# first one is confirmed settled.
_executed_market_this_session: set = set()

# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg):
    LOG_FILE.parent.mkdir(exist_ok=True)
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── Lock file ──────────────────────────────────────────────────────────────────

def write_lock():
    LOCK_FILE.write_text(str(os.getpid()))

def remove_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── Binance helpers ────────────────────────────────────────────────────────────

def binance_signed_request(endpoint, params=None):
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    sig   = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url   = f"{BINANCE_BASE_URL}{endpoint}?{query}&signature={sig}"
    r     = requests.get(url, headers={"X-MBX-APIKEY": BINANCE_API_KEY}, timeout=10)
    r.raise_for_status()
    return r.json()


def get_portfolio():
    data = binance_signed_request("/api/v3/account")
    return {
        b["asset"]: {
            "free":   float(b["free"]),
            "locked": float(b["locked"]),
            "total":  float(b["free"]) + float(b["locked"]),
        }
        for b in data.get("balances", [])
        if float(b["free"]) + float(b["locked"]) > 0
    }


def get_prices(symbols):
    prices = {}
    try:
        r     = requests.get(f"{BINANCE_BASE_URL}/api/v3/ticker/price", timeout=10)
        all_p = {p["symbol"]: float(p["price"]) for p in r.json()}
        for asset in symbols:
            if asset == "USDT":
                prices[asset] = 1.0
            elif f"{asset}USDT" in all_p:
                prices[asset] = all_p[f"{asset}USDT"]
    except Exception as e:
        log(f"Price fetch error: {e}")
    return prices


def cancel_binance_order(symbol, binance_order_id):
    """DELETE a real open order on Binance (live mode only)."""
    params = {
        "symbol":    symbol,
        "orderId":   binance_order_id,
        "timestamp": int(time.time() * 1000),
    }
    query = urllib.parse.urlencode(params)
    sig   = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    r = requests.delete(
        f"{BINANCE_BASE_URL}/api/v3/order",
        headers={"X-MBX-APIKEY": BINANCE_API_KEY},
        data=f"{query}&signature={sig}",
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def place_order_live(symbol, side, order_type, quantity_coin, quantity_usdt, limit_price):
    """POST a real order to Binance (live mode only)."""
    # Snap quantity to Binance lot size before sending
    step = get_lot_step(symbol)
    quantity_coin = round_lot(quantity_coin, step)
    log(f"  [{symbol}] lot step={step}, adjusted qty={quantity_coin}")

    params = {"symbol": symbol, "side": side, "type": order_type}
    if order_type == "MARKET":
        if side == "SELL":
            params["quantity"]      = f"{quantity_coin:.8f}"
        else:
            params["quoteOrderQty"] = f"{quantity_usdt:.2f}"
    elif order_type == "LIMIT":
        params["quantity"]    = f"{quantity_coin:.8f}"
        params["price"]       = f"{limit_price:.8f}"
        params["timeInForce"] = "GTC"

    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    sig   = hmac.new(BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    r = requests.post(
        f"{BINANCE_BASE_URL}/api/v3/order",
        headers={"X-MBX-APIKEY": BINANCE_API_KEY},
        data=f"{query}&signature={sig}",
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


# ── Order logic ────────────────────────────────────────────────────────────────

def check_trigger(order, price):
    otype = order.get("order_type", "MARKET").upper()
    side  = order.get("side", "").upper()
    if otype == "MARKET":
        return True
    if otype == "LIMIT":
        lp = order.get("limit_price")
        if lp is None:
            return True
        return price >= lp if side == "SELL" else price <= lp
    if otype == "STOP":
        sp = order.get("stop_price")
        if sp is None:
            return False
        return price <= sp if side == "SELL" else price >= sp
    return False


def validate_and_clamp(order, portfolio, price):
    """
    Enforce: never sell more than held, never buy more than USDT available.
    Returns (valid, note, final_qty_coin, final_qty_usdt).
    """
    asset    = order["asset"]
    side     = order["side"].upper()
    req_coin = float(order.get("quantity_coin", 0))
    req_usdt = float(order.get("quantity_usdt", 0))

    if side == "SELL":
        free = portfolio.get(asset, {}).get("free", 0.0)
        if free <= 0:
            return False, f"No free {asset} to sell", 0, 0
        if req_coin > free:
            clamped_usdt = round(free * price, 2)
            return True, f"Clamped SELL {req_coin:.6f}→{free:.6f} {asset} (max free held)", free, clamped_usdt
        return True, "ok", req_coin, req_usdt

    if side == "BUY":
        usdt_free = portfolio.get("USDT", {}).get("free", 0.0)
        if usdt_free <= 0:
            return False, "No free USDT to buy with", 0, 0
        actual_usdt = min(req_usdt, usdt_free)
        actual_coin = round(actual_usdt / price, 8) if price else 0
        if actual_usdt < req_usdt:
            return True, f"Clamped BUY ${req_usdt:.2f}→${actual_usdt:.2f} USDT (max free)", actual_coin, actual_usdt
        return True, "ok", req_coin, req_usdt

    return False, f"Unknown side: {side}", 0, 0


# ── Trade persistence ──────────────────────────────────────────────────────────

def load_trades():
    if not TRADES_FILE.exists():
        return []
    try:
        return json.loads(TRADES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def append_trade(trade):
    trades = load_trades()
    trades.append(trade)
    TRADES_FILE.write_text(json.dumps(trades, indent=2), encoding="utf-8")


# ── P&L summary ────────────────────────────────────────────────────────────────

def print_pnl_summary(trades, portfolio, prices):
    if not trades:
        log("No dry-run trades recorded yet.")
        return

    pnl = {}
    for t in trades:
        a = t["asset"]
        if a not in pnl:
            pnl[a] = {"bought_usdt": 0.0, "sold_usdt": 0.0,
                      "bought_coins": 0.0, "sold_coins": 0.0, "n": 0}
        pnl[a]["n"] += 1
        if t["side"] == "SELL":
            pnl[a]["sold_usdt"]   += t["exec_usdt"]
            pnl[a]["sold_coins"]  += t["exec_qty"]
        else:
            pnl[a]["bought_usdt"]  += t["exec_usdt"]
            pnl[a]["bought_coins"] += t["exec_qty"]

    log("=" * 60)
    log(f"DRY-RUN P&L SUMMARY  ({len(trades)} trades)")
    log("=" * 60)

    total_realised = 0.0
    for asset, d in sorted(pnl.items()):
        realised   = d["sold_usdt"] - d["bought_usdt"]
        total_realised += realised
        net_coins  = d["bought_coins"] - d["sold_coins"]
        cur_price  = prices.get(asset, 0)
        unrealised = net_coins * cur_price if net_coins > 0 else 0.0
        sign_r = "+" if realised   >= 0 else ""
        sign_u = "+" if unrealised >= 0 else ""
        log(f"  {asset:8s}  bought ${d['bought_usdt']:>9,.2f} ({d['bought_coins']:.4f})  "
            f"sold ${d['sold_usdt']:>9,.2f} ({d['sold_coins']:.4f})  "
            f"realised {sign_r}${realised:,.2f}  "
            f"unrealised {sign_u}${unrealised:,.2f}  [{d['n']} trades]")

    sign = "+" if total_realised >= 0 else ""
    total_held = sum(
        portfolio.get(a, {}).get("total", 0) * prices.get(a, 1)
        for a in portfolio
    )
    log("-" * 60)
    log(f"  Total realised P&L : {sign}${total_realised:,.2f} USDT")
    log(f"  Portfolio value now: ~${total_held:,.2f} USDT")
    log("=" * 60)


# ── One iteration ──────────────────────────────────────────────────────────────

def run_one_iteration(now):
    """Check all pending orders once. Returns (orders_data, any_still_pending)."""
    if not PENDING_FILE.exists():
        log("pending_orders.json not found — nothing to do.")
        return None, False

    data       = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    all_orders = data.get("orders", [])
    pending    = [o for o in all_orders if o.get("status") == "pending"]

    if not pending:
        return data, False

    # Fetch live data once per iteration (only needed for NEW orders)
    new_pending = [o for o in pending if o.get("action", "NEW").upper() == "NEW"]
    portfolio   = get_portfolio()
    assets      = list({o["asset"] for o in new_pending} | {"USDT"}) if new_pending else ["USDT"]
    prices      = get_prices(assets)

    changed = False

    for order in all_orders:
        if order.get("status") != "pending":
            continue

        action = order.get("action", "NEW").upper()

        # ── CANCEL: already-pending recommendation that Claude wants to drop ──
        if action == "CANCEL":
            oid = order.get("id", order.get("asset", "?"))
            log(f"[CANCEL] Marking recommendation {oid} as cancelled — "
                f"{order.get('rationale', '')[:70]}")
            order["status"]       = "cancelled"
            order["cancelled_at"] = now.isoformat()
            changed = True
            continue

        # ── CANCEL_BINANCE: cancel a real open order on Binance ──────────────
        if action == "CANCEL_BINANCE":
            symbol   = order.get("symbol", "")
            boid     = order.get("binance_order_id")
            rationale = order.get("rationale", "")[:70]
            if DRY_RUN:
                log(f"[CANCEL_BINANCE] DRY-RUN: would cancel Binance order "
                    f"{boid} on {symbol} — {rationale}")
                order["status"]       = "cancelled_dry"
                order["cancelled_at"] = now.isoformat()
            else:
                try:
                    resp = cancel_binance_order(symbol, boid)
                    log(f"[CANCEL_BINANCE] Cancelled Binance order {boid} on {symbol}: {resp}")
                    order["status"]       = "cancelled_live"
                    order["cancelled_at"] = now.isoformat()
                    order["cancel_resp"]  = resp
                except Exception as e:
                    log(f"[CANCEL_BINANCE] Failed to cancel order {boid} on {symbol}: {e}")
                    order["status"] = "failed"
                    order["error"]  = str(e)
            changed = True
            continue

        # ── NEW: execute when trigger conditions are met ──────────────────────
        asset    = order["asset"]
        side     = order["side"].upper()
        otype    = order.get("order_type", "MARKET").upper()
        price    = prices.get(asset, 0)
        lp       = order.get("limit_price")
        sp       = order.get("stop_price")

        # No price data
        if price == 0:
            log(f"[{asset}] SKIP — price unavailable")
            order["status"] = "skipped_no_price"
            changed = True
            continue

        # Expiry check
        expires = order.get("expires_at")
        if expires:
            exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if now > exp_dt:
                log(f"[{asset}] EXPIRED — limit never triggered. "
                    f"Target was {'≥' if side=='SELL' else '≤'} "
                    f"${lp or sp:,.4f}, market stayed at ~${price:,.4f}. "
                    f"Order cancelled.")
                order["status"] = "expired"
                changed = True
                continue

        # Validate + clamp against live holdings
        valid, note, final_coin, final_usdt = validate_and_clamp(order, portfolio, price)
        if not valid:
            log(f"[{asset}] INVALID — {note}")
            order["status"]      = "skipped_invalid"
            order["skip_reason"] = note
            changed = True
            continue
        if note != "ok":
            log(f"[{asset}] CLAMPED — {note}")
            order["quantity_coin"] = final_coin
            order["quantity_usdt"] = final_usdt

        # Trigger check
        if not check_trigger(order, price):
            direction = f"{'≥' if side=='SELL' else '≤'} ${lp:,.4f}" if lp else \
                        f"{'≤' if side=='SELL' else '≥'} ${sp:,.4f}" if sp else "N/A"
            time_left = ""
            if expires:
                exp_dt    = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                mins_left = int((exp_dt - now).total_seconds() / 60)
                time_left = f" | expires in {mins_left}m"
            log(f"[{asset}] WAITING — {otype} {side} trigger {direction} "
                f"| now ${price:,.4f}{time_left}")
            continue

        # ── Duplicate MARKET guard ────────────────────────────────────────────
        if otype == "MARKET":
            key = (asset, side)
            if key in _executed_market_this_session:
                log(f"[{asset}] SKIP — duplicate MARKET {side} already executed this session")
                order["status"] = "skipped_duplicate"
                changed = True
                continue

        # ── Execute ──────────────────────────────────────────────────────────
        exec_qty  = final_coin
        exec_usdt = round(exec_qty * price, 2)
        mode_tag  = "dry"

        if not DRY_RUN:
            try:
                resp     = place_order_live(f"{asset}USDT", side, otype,
                                            exec_qty, exec_usdt, lp)
                mode_tag = "live"
                log(f"[{asset}] LIVE ORDER PLACED: {resp}")
            except requests.exceptions.HTTPError as e:
                body = {}
                try:
                    body = e.response.json()
                except Exception:
                    pass
                log(f"[{asset}] ORDER FAILED: {e} | Binance: code={body.get('code')} msg={body.get('msg')}")
                order["status"] = "failed"
                order["error"]  = f"{e} | {body}"
                changed = True
                continue
            except Exception as e:
                log(f"[{asset}] ORDER FAILED: {e}")
                order["status"] = "failed"
                order["error"]  = str(e)
                changed = True
                continue

        trade = {
            "timestamp":  now.isoformat(),
            "asset":      asset,
            "side":       side,
            "order_type": otype,
            "exec_qty":   exec_qty,
            "exec_price": price,
            "exec_usdt":  exec_usdt,
            "limit_price": lp,
            "stop_price":  sp,
            "rationale":  order.get("rationale", ""),
            "mode":       mode_tag,
        }
        append_trade(trade)

        order["status"]          = f"executed_{mode_tag}"
        order["executed_at"]     = now.isoformat()
        order["executed_price"]  = price
        order["executed_usdt"]   = exec_usdt
        changed = True

        if otype == "MARKET":
            _executed_market_this_session.add((asset, side))

        label = "DRY-RUN" if DRY_RUN else "LIVE"
        log(f"[{asset}] {label} {side} {exec_qty:.6f} coins "
            f"@ ${price:,.4f} = ${exec_usdt:,.2f} USDT | {order.get('rationale','')[:70]}")

    if changed:
        data["orders"] = all_orders
        PENDING_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    still_pending = any(o.get("status") == "pending" for o in all_orders)
    return data, still_pending


# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    start_time = datetime.now(timezone.utc)
    write_lock()
    log("=" * 60)
    log(f"Order Watcher started | {'*** DRY RUN ***' if DRY_RUN else '*** LIVE ***'}")
    log(f"Loop interval: {LOOP_INTERVAL_S}s | Max runtime: {MAX_WATCHER_HOURS}h")
    log("=" * 60)

    try:
        iteration = 0
        while True:
            now = datetime.now(timezone.utc)

            # Safety: absolute max runtime
            runtime_hours = (now - start_time).total_seconds() / 3600
            if runtime_hours >= MAX_WATCHER_HOURS:
                log(f"MAX_WATCHER_HOURS ({MAX_WATCHER_HOURS}h) reached — stopping.")
                break

            iteration += 1
            log(f"--- Iteration {iteration} ---")

            data, still_pending = run_one_iteration(now)

            if data is None:
                log("No pending_orders.json found. Stopping.")
                break

            if not still_pending:
                log("All orders resolved. Watcher stopping.")
                # Print final P&L
                trades    = load_trades()
                portfolio = get_portfolio()
                assets    = list(portfolio.keys())
                prices    = get_prices(assets)
                print_pnl_summary(trades, portfolio, prices)
                break

            # Count what's still pending for the status line
            pending_left = [o for o in data.get("orders", []) if o.get("status") == "pending"]
            log(f"{len(pending_left)} order(s) still pending. "
                f"Next check in {LOOP_INTERVAL_S}s.")
            time.sleep(LOOP_INTERVAL_S)

    except KeyboardInterrupt:
        log("Watcher interrupted by user.")
    except Exception as e:
        log(f"Watcher error: {e}")
        raise
    finally:
        remove_lock()
        log("Lock file removed. Watcher exited.")


if __name__ == "__main__":
    main()
