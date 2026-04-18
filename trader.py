#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Active Trader — short-term technical trading loop.
Analyses 5m / 15m / 1h charts with Claude every 2 minutes and places
orders via the same watcher infrastructure as the signal monitor.

Run ONE of monitor.py OR trader.py — not both simultaneously.

Usage:
  python trader.py                     # foreground loop, Ctrl+C to stop
  python trader.py --interval 1        # every 1 minute
  python trader.py --background        # detached background process
  python trader.py --stop              # stop background trader
"""

import os
import re
import sys
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Shared utilities from the signal script ────────────────────────────────────
from crypto_signal import (
    ANTHROPIC_API_KEY, GMAIL_USER, GMAIL_PASSWORD, RECIPIENT,
    fetch_binance_portfolio, format_portfolio,
    fetch_trade_history, format_trade_history,
    write_pending_orders, load_current_pending_for_context,
    start_watcher_if_needed, print_watcher_status, strip_orders_block,
    send_email, save_last_run,
    fetch_klines, ema, calc_rsi, calc_macd, calc_bollinger,
    sig_round, support_resistance,
)
import anthropic

BASE_DIR     = Path(__file__).parent
LOCK_FILE    = BASE_DIR / "trader.lock"
LOG_FILE     = BASE_DIR / "logs" / "trader.log"
DEFAULT_INTERVAL_MIN = 2


# ── Short-term technical analysis (5m / 15m / 1h) ─────────────────────────────

def fp(v):
    if v is None:
        return "N/A"
    if v == 0:
        return "$0"
    if v >= 1:
        return f"${v:,.4f}"
    import math
    decimals = max(4, -int(math.floor(math.log10(abs(v)))) + 3)
    return f"${v:.{decimals}f}"


def ema_cross(vals9, vals21):
    if not vals9 or not vals21 or len(vals9) < 2 or len(vals21) < 2:
        return "N/A"
    if vals9[-2] <= vals21[-2] and vals9[-1] > vals21[-1]:
        return "BULL CROSS"
    if vals9[-2] >= vals21[-2] and vals9[-1] < vals21[-1]:
        return "BEAR CROSS"
    return "above EMA21" if vals9[-1] > vals21[-1] else "below EMA21"


def perform_short_term_ta(asset):
    if asset == "USDT":
        return "  N/A (USDT)"
    pair = f"{asset}USDT"

    m5  = fetch_klines(pair, "5m",  120)   # 10h
    m15 = fetch_klines(pair, "15m", 100)   # 25h
    h1  = fetch_klines(pair, "1h",  48)    # 2 days

    if len(m15) < 30:
        return f"  {asset}: Insufficient short-term data"

    closes_15 = [c[3] for c in m15]
    highs_15  = [c[1] for c in m15]
    lows_15   = [c[2] for c in m15]
    vols_15   = [c[4] for c in m15]

    price             = closes_15[-1]
    rsi_15            = calc_rsi(closes_15)
    macd_v, macd_s, macd_h = calc_macd(closes_15)
    bb_low, bb_mid, bb_high = calc_bollinger(closes_15, period=20)
    res, sup          = support_resistance(highs_15, lows_15, lookback=20)

    # 5m momentum + EMA cross
    rsi_5 = cross_5 = None
    if len(m5) >= 22:
        closes_5  = [c[3] for c in m5]
        rsi_5     = calc_rsi(closes_5)
        ema9_v    = ema(closes_5, 9)
        ema21_v   = ema(closes_5, 21)
        cross_5   = ema_cross(ema9_v, ema21_v)

    # 1h bias
    rsi_1h = bias_1h = None
    if len(h1) >= 20:
        closes_1h = [c[3] for c in h1]
        rsi_1h    = calc_rsi(closes_1h)
        ema20_1h  = ema(closes_1h, 20)
        bias_1h   = "above EMA20(1h)" if (ema20_1h and closes_1h[-1] > ema20_1h[-1]) else "below EMA20(1h)"

    # Volume spike vs 20-candle avg
    vol_avg = sum(vols_15[-21:-1]) / 20 if len(vols_15) >= 21 else None
    if vol_avg and vols_15[-1] > vol_avg * 1.5:
        vol_note = f"SPIKE {vols_15[-1]/vol_avg:.1f}x avg"
    elif vol_avg and vols_15[-1] < vol_avg * 0.5:
        vol_note = "very low"
    else:
        vol_note = "normal"

    # Short-term momentum (1-candle and 4-candle % change on 15m)
    mom1 = round((closes_15[-1] / closes_15[-2] - 1) * 100, 3) if len(closes_15) >= 2 else None
    mom4 = round((closes_15[-1] / closes_15[-5] - 1) * 100, 3) if len(closes_15) >= 5 else None

    # BB position
    bb_pos = "N/A"
    if bb_low and bb_high and bb_high != bb_low:
        pct = (price - bb_low) / (bb_high - bb_low) * 100
        bb_pos = f"{pct:.0f}% of band"

    lines = [
        f"\n  {asset} — Short-Term TA (5m/15m/1h):",
        f"  Price        : {fp(price)}  |  1h bias: {bias_1h or 'N/A'}",
        f"  5m  RSI      : {rsi_5 or 'N/A'}  |  EMA9/21: {cross_5 or 'N/A'}",
        f"  15m RSI      : {rsi_15}  |  MACD hist: {macd_h} ({'bull' if macd_h and macd_h > 0 else 'bear'})",
        f"  1h  RSI      : {rsi_1h or 'N/A'}",
        f"  Bollinger(15m): Low {fp(bb_low)} | Mid {fp(bb_mid)} | High {fp(bb_high)} | pos: {bb_pos}",
        f"  Support      : {fp(sup)} | Resistance: {fp(res)}",
        f"  Momentum     : 1-candle {'+' if mom1 and mom1>0 else ''}{mom1}%  |  4-candle {'+' if mom4 and mom4>0 else ''}{mom4}%",
        f"  Volume(15m)  : {vol_note}",
    ]
    return "\n".join(lines)


# ── Active trader Claude prompt ────────────────────────────────────────────────

def analyze_active(portfolio, ta_text, trade_history_text, pending_orders_text):
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    portfolio_text = format_portfolio(portfolio)

    prompt = f"""You are an active crypto trader focused purely on short-term price action. No news. Trade what the chart shows. Be precise and decisive.

=== PORTFOLIO ===
{portfolio_text}

=== TRADE & ORDER HISTORY ===
{trade_history_text}

=== PREVIOUSLY PENDING ORDERS ===
{pending_orders_text}

=== SHORT-TERM TECHNICAL ANALYSIS (5m / 15m / 1h) ===
{ta_text}

=== ACTIVE TRADING RULES ===
Strategies — look for these setups only, do not force trades:
  1. MOMENTUM: 15m MACD hist turning positive + RSI 45-65 + price above BB mid → BUY
     Reverse: MACD hist turning negative + RSI 35-55 + price below BB mid → SELL
  2. MEAN REVERSION: RSI < 30 on 15m + price at/below lower BB → BUY (oversold bounce)
     RSI > 70 on 15m + price at/above upper BB → SELL (overbought fade)
  3. BREAKOUT: Price breaks above resistance + volume spike (>1.5x avg) → BUY
     Price breaks below support + volume spike → SELL
  4. EMA CROSS (5m): BULL CROSS (EMA9 crosses above EMA21) + 1h bias bullish → BUY scalp
     BEAR CROSS + 1h bias bearish → SELL scalp

Risk management (mandatory for every trade):
  - Stop-loss: state the exact price level in rationale
  - Risk:reward >= 1:2 — if target is <2x the stop distance, skip the trade
  - Position size: 5-15% of total portfolio per trade
  - Never trade more than 2 assets simultaneously
  - Do NOT trade if the last trade on this asset was within 30 minutes

Repeat/conflict rules:
  - If a pending order already exists for the same asset+side → do NOT add another
  - If bought in last 30 min → HOLD, do not add
  - If sold in last 30 min → HOLD, do not add
  - If no clean setup on any asset → output NO_ACTION (no orders, no email)

=== OUTPUT ===

ACTIVE TRADE BRIEF — {now_utc}

PORTFOLIO: [Asset $val (pct) | Total $X]

--- SETUPS DETECTED ---
[Per asset with a setup, 2 lines max:]
Asset: [Setup type]. [Key signal, e.g. "15m RSI 28, price at lower BB, MACD hist turning"]. Bias: [Bull/Bear/Neutral on 1h].
Entry: [BUY/SELL] $X | Target: $X (+Y%) | Stop: $X (-Z%) | R:R [ratio]

[If no setup: "No clean setups this scan — holding all positions."]

--- ACTIVE RECOMMENDATION ---
Stance: [Aggressive / Cautious / Flat]

[Per trade only — skip assets where NO_ACTION is correct:]
Asset — [BUY/SELL]
Rationale: [Setup name + key signals + stop level in 1 sentence]
Action: [BUY/SELL] exactly $X.XX USDT = X.XXXXXX [ASSET] at [market/limit $X]

Priority actions (max 3, omit if none):
1. ...
2. ...

===ORDERS_START===
[Same JSON format as signal script:]
[{{"action":"NEW","asset":"XYZ","side":"BUY|SELL","order_type":"MARKET|LIMIT|STOP","quantity_coin":0.0,"quantity_usdt":0.0,"limit_price":null,"stop_price":null,"rationale":"..."}}]
[Or cancel/cancel_binance actions as needed]
[If no trades at all: []]
===ORDERS_END===

SUBJECT: ACTIVE TRADE: <5 words>

IMPORTANT: Last line must be exactly:
SUBJECT: ACTIVE TRADE: [your 5 words]
Do not add any text after it."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# ── One full cycle ─────────────────────────────────────────────────────────────

def run_one_cycle(run_number):
    now = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"  Active Trader  Run #{run_number}  —  {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    print("Fetching portfolio...")
    portfolio = fetch_binance_portfolio()
    assets = [p["asset"] for p in portfolio if p["asset"] != "USDT"]

    print("Fetching trade history...")
    trades_by_asset, open_orders = fetch_trade_history(assets)
    trade_history_text = format_trade_history(trades_by_asset, open_orders)

    print("Running short-term TA...")
    ta_parts = [perform_short_term_ta(a) for a in assets]
    ta_text  = "\n".join(ta_parts) if ta_parts else "No assets to analyse."

    print("Loading pending orders...")
    pending_orders_text = load_current_pending_for_context()

    print("Analysing with Claude...")
    result = analyze_active(portfolio, ta_text, trade_history_text, pending_orders_text)

    # Skip if no action
    if result.strip().startswith("NO_ACTION"):
        print("No clean setups this scan — skipping orders and email.")
        return

    # Extract and persist orders
    from crypto_signal import extract_orders
    orders = extract_orders(result)
    if orders:
        write_pending_orders(orders, now)
        start_watcher_if_needed()
    else:
        print("No actionable orders in this analysis.")

    result_clean = strip_orders_block(result)
    lines        = result_clean.strip().splitlines()
    subject      = "ACTIVE TRADE: Market Update"
    body_lines   = lines

    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if re.match(r"^SUBJECT[:\s]", stripped, re.IGNORECASE):
            candidate = re.sub(r"^SUBJECT[:\s]*", "", stripped, flags=re.IGNORECASE).strip()
            if candidate:
                subject = candidate
            body_lines = lines[:i]
            break

    body = "\n".join(body_lines).strip()
    print(f"\n{'-'*60}")
    print(body)
    print(f"\nSubject: {subject}")
    print(f"{'-'*60}")

    print("\nSending email...")
    send_email(subject, body)
    save_last_run(now)
    print_watcher_status()


# ── Process management ─────────────────────────────────────────────────────────

def write_lock():
    LOCK_FILE.write_text(str(os.getpid()))


def remove_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def trader_pid():
    if not LOCK_FILE.exists():
        return None
    try:
        return int(LOCK_FILE.read_text().strip())
    except Exception:
        return None


def pid_alive(pid):
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        return str(pid) in r.stdout
    except Exception:
        return False


def cmd_stop():
    pid = trader_pid()
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
        print(f"Active trader stopped (PID {pid}).")
    else:
        print("Active trader was not running.")


def cmd_background(interval_min):
    pid = trader_pid()
    if pid and pid_alive(pid):
        print(f"Active trader already running (PID {pid}). Use --stop first.")
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
    time.sleep(0.5)
    print(f"Active trader started in background (PID {proc.pid}). Output → logs/trader.log")
    print(f"Stop with:  python trader.py --stop")


# ── Loop modes ─────────────────────────────────────────────────────────────────

def foreground_loop(interval_min):
    print("=" * 60)
    print(f"  Active Trader  —  every {interval_min} min  |  Ctrl+C to stop")
    print("  NOTE: do not run monitor.py at the same time")
    print("=" * 60)
    run_number = 0
    try:
        while True:
            run_number += 1
            try:
                run_one_cycle(run_number)
            except Exception as e:
                print(f"\nCycle error: {e} — continuing.")

            wait_secs = interval_min * 60
            deadline  = time.monotonic() + wait_secs
            print(f"\nNext run in {interval_min}m — Ctrl+C to stop.\n")
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                mins, secs = divmod(int(remaining), 60)
                print(f"  Next run in {mins}m {secs:02d}s ...   ", end="\r", flush=True)
                time.sleep(1)
            print()
    except KeyboardInterrupt:
        print("\n\nActive trader stopped by user.")


def background_loop(interval_min):
    write_lock()
    print(f"Active trader started (PID {os.getpid()}) | interval: {interval_min}m", flush=True)
    try:
        run_number = 0
        while True:
            run_number += 1
            try:
                run_one_cycle(run_number)
            except Exception as e:
                print(f"Cycle error: {e} — continuing.", flush=True)
            time.sleep(interval_min * 60)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Fatal error: {e}", flush=True)
        raise
    finally:
        remove_lock()
        print("Active trader exited.", flush=True)


# ── Entry point ────────────────────────────────────────────────────────────────

def parse_interval(args):
    try:
        idx = args.index("--interval")
        return int(args[idx + 1])
    except (ValueError, IndexError):
        return DEFAULT_INTERVAL_MIN


if __name__ == "__main__":
    args     = sys.argv[1:]
    interval = parse_interval(args)

    if "--stop" in args:
        cmd_stop()
    elif "--background" in args:
        cmd_background(interval)
    elif "--_run-background" in args:
        background_loop(interval)
    else:
        foreground_loop(interval)
