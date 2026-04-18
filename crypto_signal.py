#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crypto Market Signal Digest
Fetches crypto news + influential X posts via Nitter RSS, analyzes signals
with Claude, and emails a digest. Designed to run every 15 minutes via
Windows Task Scheduler (or any cron-like scheduler).
"""

import os
import re
import smtplib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText

import sys
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import hashlib
import hmac
import subprocess
import time
import urllib.parse

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
GMAIL_USER         = os.getenv("GMAIL_USER", "")
GMAIL_PASSWORD     = os.getenv("GMAIL_APP_PASSWORD", "")
RECIPIENT          = os.getenv("RECIPIENT_EMAIL", "")
DEFAULT_LOOKBACK   = int(os.getenv("LOOKBACK_MINUTES", "65"))  # fallback for first run
LAST_RUN_FILE      = os.path.join(os.path.dirname(__file__), "last_run.txt")
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_BASE_URL      = "https://api.binance.com"
MIN_USD_VALUE         = 1.0  # ignore dust balances below $1
TRADE_HISTORY_LIMIT = 20  # recent trades per asset to fetch
SEEN_TITLES_FILE   = os.path.join(os.path.dirname(__file__), "seen_titles.txt")
SEEN_TITLES_MAX    = 500  # rolling cache size

# ── News RSS feeds ─────────────────────────────────────────────────────────────
NEWS_FEEDS = [
    ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("The Block",     "https://www.theblock.co/rss.xml"),
    ("Decrypt",       "https://decrypt.co/feed"),
    ("Reuters",        "https://news.google.com/rss/search?q=when:2d+allinurl:reuters.com+crypto+OR+bitcoin+OR+finance&ceid=US:en&hl=en-US&gl=US"),
    ("Investing.com",  "https://www.investing.com/rss/news_301.rss"),
    ("GNews Crypto",   "https://news.google.com/rss/search?q=when:1d+cryptocurrency+OR+bitcoin+OR+ethereum&ceid=US:en&hl=en-US&gl=US"),
    ("GNews Macro",    "https://news.google.com/rss/search?q=when:1d+federal+reserve+OR+inflation+OR+interest+rates+crypto&ceid=US:en&hl=en-US&gl=US"),
]

# ── X / Nitter ─────────────────────────────────────────────────────────────────
NITTER_INSTANCES = [
    "xcancel.com",
    "nitter.poast.org",
    "nitter.privacydev.net",
    "nitter.1d4.us",
    "nitter.unixfox.eu",
]

# Maps Twitter username -> search term used for Google News fallback
INFLUENCERS = {
    "saylor":          "Michael Saylor bitcoin",
    "VitalikButerin":  "Vitalik Buterin ethereum",
    "cz_binance":      "CZ Binance crypto",
    "elonmusk":        "Elon Musk crypto bitcoin",
    "APompliano":      "Anthony Pompliano bitcoin",
    "RaoulGMI":        "Raoul Pal crypto macro",
    "woonomic":        "Willy Woo bitcoin onchain",
    "100trillionUSD":  "PlanB bitcoin stock to flow",
    "CryptoHayes":     "Arthur Hayes crypto",
    "novogratz":       "Mike Novogratz crypto",
    "DocumentingBTC":  "bitcoin adoption",
    "BitcoinMagazine": "Bitcoin Magazine",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CryptoSignalBot/1.0)"}

DATE_FORMATS = [
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S GMT",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f%z",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def strip_html(text):
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def parse_date(pub_str):
    if not pub_str:
        return None
    pub_str = pub_str.strip()
    # Handle "GMT" as "+0000"
    pub_str = pub_str.replace(" GMT", " +0000")
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(pub_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def is_after(pub_str, since_dt):
    """Return True if the item was published after since_dt."""
    dt = parse_date(pub_str)
    if dt is None:
        return True  # include if date unparseable
    return dt >= since_dt


# ── Seen-titles dedup cache ────────────────────────────────────────────────────

def load_seen_titles():
    if os.path.exists(SEEN_TITLES_FILE):
        with open(SEEN_TITLES_FILE, encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_seen_titles(seen):
    # Keep only the last SEEN_TITLES_MAX to prevent unbounded growth
    titles = list(seen)[-SEEN_TITLES_MAX:]
    with open(SEEN_TITLES_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(titles))


def title_key(title):
    """Normalize title for dedup comparison."""
    return re.sub(r"\s+", " ", title.lower().strip())


# ── Last-run tracker ───────────────────────────────────────────────────────────

def load_last_run():
    """Return the datetime of the last successful run, or None if first run."""
    if os.path.exists(LAST_RUN_FILE):
        try:
            text = open(LAST_RUN_FILE).read().strip()
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    return None


def save_last_run(dt):
    """Persist the current run timestamp."""
    with open(LAST_RUN_FILE, "w") as f:
        f.write(dt.isoformat())


def parse_rss(xml_text, source_name):
    items = []
    try:
        root = ET.fromstring(xml_text.strip())
        ns_atom = "http://www.w3.org/2005/Atom"

        # RSS 2.0 items
        for item in root.findall(".//item"):
            title   = strip_html(item.findtext("title", ""))
            link    = (item.findtext("link") or "").strip()
            desc    = strip_html(item.findtext("description", ""))[:300]
            pub     = (item.findtext("pubDate") or
                       item.findtext("{http://purl.org/dc/elements/1.1/}date") or "")
            if title:
                items.append({"source": source_name, "title": title,
                               "link": link, "summary": desc, "published": pub})

        # Atom entries (if no RSS items found)
        if not items:
            for entry in root.findall(f".//{{{ns_atom}}}entry"):
                title   = strip_html(entry.findtext(f"{{{ns_atom}}}title", ""))
                link_el = entry.find(f"{{{ns_atom}}}link")
                link    = link_el.get("href", "") if link_el is not None else ""
                desc    = strip_html(entry.findtext(f"{{{ns_atom}}}summary", ""))[:300]
                pub     = (entry.findtext(f"{{{ns_atom}}}published") or
                           entry.findtext(f"{{{ns_atom}}}updated") or "")
                if title:
                    items.append({"source": source_name, "title": title,
                                   "link": link, "summary": desc, "published": pub})
    except ET.ParseError as e:
        print(f"  XML parse error for {source_name}: {e}")
    return items


def fetch_rss(url, source_name, timeout=8):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return parse_rss(r.text, source_name)
    except Exception as e:
        print(f"  SKIP {source_name}: {e}")
        return []


def fetch_nitter(username, timeout=8):
    """Try each Nitter instance for the username's RSS feed."""
    for instance in NITTER_INSTANCES:
        url = f"https://{instance}/{username}/rss"
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                items = parse_rss(r.text, f"@{username} (X)")
                if items:
                    return items, "nitter"
        except Exception:
            continue
    return [], "nitter"


def fetch_influencer_google_news(username, search_term, timeout=8):
    """Fallback: fetch Google News RSS for an influencer's search term."""
    url = (
        f"https://news.google.com/rss/search"
        f"?q=when:2d+{requests.utils.quote(search_term)}"
        f"&ceid=US:en&hl=en-US&gl=US"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            items = parse_rss(r.text, f"@{username} (via Google News)")
            return items
    except Exception:
        pass
    return []


# ── Fetcher ────────────────────────────────────────────────────────────────────

def fetch_all_items(since_dt):
    all_items = []

    print("Fetching news RSS feeds...")
    for source_name, url in NEWS_FEEDS:
        items = fetch_rss(url, source_name)
        print(f"  {source_name}: {len(items)} items")
        all_items.extend(items)

    print("\nFetching X posts via Nitter (with Google News fallback)...")
    for username, search_term in INFLUENCERS.items():
        items, source = fetch_nitter(username)
        if items:
            print(f"  @{username}: {len(items)} items (Nitter)")
        else:
            items = fetch_influencer_google_news(username, search_term)
            if items:
                print(f"  @{username}: {len(items)} items (Google News fallback)")
            else:
                print(f"  @{username}: unavailable")
        all_items.extend(items)

    # Filter by time window
    in_window = [i for i in all_items if is_after(i["published"], since_dt)]
    elapsed_min = int((datetime.now(timezone.utc) - since_dt).total_seconds() / 60)
    print(f"\nTotal fetched: {len(all_items)} | In window ({elapsed_min}min): {len(in_window)}")

    # Dedup against previously seen titles
    seen = load_seen_titles()
    new_items = [i for i in in_window if title_key(i["title"]) not in seen]
    print(f"After dedup: {len(new_items)} new items")

    # Add new titles to seen cache
    for i in new_items:
        seen.add(title_key(i["title"]))
    save_seen_titles(seen)

    return new_items


# ── Binance ────────────────────────────────────────────────────────────────────

def binance_signed_request(endpoint, params=None):
    """Make a signed GET request to Binance REST API."""
    params = params or {}
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    signature = hmac.new(
        BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    url = f"{BINANCE_BASE_URL}{endpoint}?{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def get_usdt_prices(symbols):
    """Fetch current USDT prices for a list of base assets."""
    prices = {}
    try:
        r = requests.get(f"{BINANCE_BASE_URL}/api/v3/ticker/price", timeout=10)
        all_prices = {p["symbol"]: float(p["price"]) for p in r.json()}
        for asset in symbols:
            if asset == "USDT":
                prices[asset] = 1.0
            elif f"{asset}USDT" in all_prices:
                prices[asset] = all_prices[f"{asset}USDT"]
            elif f"{asset}BTC" in all_prices and "BTCUSDT" in all_prices:
                prices[asset] = all_prices[f"{asset}BTC"] * all_prices["BTCUSDT"]
    except Exception as e:
        print(f"  Price fetch error: {e}")
    return prices


def fetch_binance_portfolio():
    """Return portfolio as list of dicts with asset, qty, usd_value."""
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        print("  Binance keys not configured — skipping portfolio fetch.")
        return []
    try:
        data = binance_signed_request("/api/v3/account")
        balances = [
            b for b in data.get("balances", [])
            if float(b["free"]) + float(b["locked"]) > 0
        ]
        assets = [b["asset"] for b in balances]
        prices = get_usdt_prices(assets)

        portfolio = []
        for b in balances:
            asset = b["asset"]
            qty = float(b["free"]) + float(b["locked"])
            price = prices.get(asset, 0)
            usd_value = qty * price
            if usd_value >= MIN_USD_VALUE:
                portfolio.append({
                    "asset": asset,
                    "qty": qty,
                    "price_usdt": price,
                    "usd_value": usd_value,
                })

        portfolio.sort(key=lambda x: x["usd_value"], reverse=True)
        total = sum(p["usd_value"] for p in portfolio)
        for p in portfolio:
            p["pct"] = round(p["usd_value"] / total * 100, 1) if total else 0

        print(f"  Portfolio: {len(portfolio)} assets, total ~${total:,.0f} USDT")
        return portfolio
    except Exception as e:
        print(f"  Binance error: {e}")
        return []


def fetch_trade_history(assets):
    """Fetch recent trade history per asset and all open orders from Binance."""
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        print("  Binance keys not configured — skipping trade history.")
        return {}, []

    trades_by_asset = {}
    for asset in assets:
        if asset == "USDT":
            continue
        symbol = f"{asset}USDT"
        try:
            trades = binance_signed_request(
                "/api/v3/myTrades", {"symbol": symbol, "limit": TRADE_HISTORY_LIMIT}
            )
            trades_by_asset[asset] = trades
            print(f"  {asset}: {len(trades)} trades fetched")
        except Exception as e:
            print(f"  Trade history error {symbol}: {e}")
            trades_by_asset[asset] = []

    open_orders = []
    try:
        open_orders = binance_signed_request("/api/v3/openOrders")
        print(f"  Open orders: {len(open_orders)}")
    except Exception as e:
        print(f"  Open orders error: {e}")

    return trades_by_asset, open_orders


def format_trade_history(trades_by_asset, open_orders):
    """Format trade history and open orders as plain text for the Claude prompt."""
    if not trades_by_asset and not open_orders:
        return "Trade history unavailable."

    lines = []

    for asset, trades in trades_by_asset.items():
        if not trades:
            lines.append(f"\n  {asset}: No recent trades on record")
            continue

        buys  = [t for t in trades if t.get("isBuyer")]
        sells = [t for t in trades if not t.get("isBuyer")]

        now_utc = datetime.now(timezone.utc)
        last_trade = trades[-1]
        last_trade_dt = datetime.fromtimestamp(last_trade["time"] / 1000, tz=timezone.utc)
        last_trade_side = "BUY" if last_trade.get("isBuyer") else "SELL"
        hours_ago = (now_utc - last_trade_dt).total_seconds() / 3600
        if hours_ago < 24:
            recency = f"{hours_ago:.0f}h ago"
        else:
            recency = f"{hours_ago/24:.1f}d ago"

        lines.append(f"\n  {asset} — last {len(trades)} trades (most recent: {last_trade_side} {recency}):")

        if buys:
            total_qty  = sum(float(t["qty"]) for t in buys)
            total_cost = sum(float(t["quoteQty"]) for t in buys)
            avg_buy    = total_cost / total_qty if total_qty else 0
            lines.append(f"    Avg buy price ({len(buys)} buys):  ${avg_buy:,.4f}")
        if sells:
            total_qty_s  = sum(float(t["qty"]) for t in sells)
            total_recv_s = sum(float(t["quoteQty"]) for t in sells)
            avg_sell     = total_recv_s / total_qty_s if total_qty_s else 0
            lines.append(f"    Avg sell price ({len(sells)} sells): ${avg_sell:,.4f}")

        # Show last 5 trades chronologically
        for t in trades[-5:]:
            dt   = datetime.fromtimestamp(t["time"] / 1000, tz=timezone.utc)
            side = "BUY " if t.get("isBuyer") else "SELL"
            qty  = float(t["qty"])
            price = float(t["price"])
            usdt_val = float(t["quoteQty"])
            lines.append(
                f"    {dt.strftime('%Y-%m-%d %H:%M')}  {side}  "
                f"{qty:.6f} @ ${price:,.4f}  = ${usdt_val:,.2f}"
            )

    if open_orders:
        lines.append(f"\n  Binance Open Orders ({len(open_orders)}) — use binance_order_id in CANCEL_BINANCE actions:")
        for o in open_orders[:10]:
            symbol      = o.get("symbol", "")
            side        = o.get("side", "")
            order_type  = o.get("type", "")
            qty         = float(o.get("origQty", 0))
            price       = float(o.get("price", 0))
            order_id    = o.get("orderId", "N/A")
            filled_pct  = float(o.get("executedQty", 0)) / qty * 100 if qty else 0
            lines.append(
                f"    binance_order_id: {order_id}  {symbol} {side} {order_type}  "
                f"qty: {qty}  @ ${price:,.4f}  filled: {filled_pct:.0f}%"
            )
    else:
        lines.append("\n  No open orders.")

    return "\n".join(lines)


def format_portfolio(portfolio):
    """Format portfolio as plain text for the Claude prompt."""
    if not portfolio:
        return "Portfolio unavailable."
    total = sum(p["usd_value"] for p in portfolio)
    lines = [f"Total portfolio value: ~${total:,.0f} USDT\n"]
    for p in portfolio:
        lines.append(
            f"  {p['asset']:8s}  qty: {p['qty']:.4f}  "
            f"price: ${p['price_usdt']:,.4f}  "
            f"value: ${p['usd_value']:,.2f}  ({p['pct']}%)"
        )
    return "\n".join(lines)


# ── Technical Analysis ─────────────────────────────────────────────────────────

def fetch_klines(symbol, interval="1d", limit=90):
    """Fetch OHLCV candles from Binance public API."""
    try:
        r = requests.get(
            f"{BINANCE_BASE_URL}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        # [open_time, open, high, low, close, volume, ...]
        return [[float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in r.json()]
    except Exception as e:
        print(f"  Klines error {symbol}: {e}")
        return []


def ema(values, period):
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def sig_round(v, sig=6):
    """Round to sig significant figures, preserving micro-price precision."""
    if v == 0:
        return 0
    import math
    d = sig - 1 - int(math.floor(math.log10(abs(v))))
    return round(v, max(d, 0))


def calc_macd(closes):
    if len(closes) < 26:
        return None, None, None
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    diff = len(ema12) - len(ema26)
    macd_line = [a - b for a, b in zip(ema12[diff:], ema26)]
    if len(macd_line) < 9:
        return None, None, None
    signal_line = ema(macd_line, 9)
    histogram = macd_line[-1] - signal_line[-1]
    return sig_round(macd_line[-1]), sig_round(signal_line[-1]), sig_round(histogram)


def calc_bollinger(closes, period=20):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    std = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    return sig_round(mid - 2 * std), sig_round(mid), sig_round(mid + 2 * std)


def support_resistance(highs, lows, lookback=30):
    h = highs[-lookback:]
    l = lows[-lookback:]
    return sig_round(max(h)), sig_round(min(l))


def perform_ta(asset):
    """Return a formatted TA summary string for one asset."""
    pair = f"{asset}USDT" if asset != "USDT" else None
    if not pair:
        return "  N/A (USDT)"

    # Daily candles (90 days) for trend + indicators
    daily = fetch_klines(pair, "1d", 90)
    # 4h candles (60 periods = 10 days) for short-term momentum
    h4 = fetch_klines(pair, "4h", 60)

    if len(daily) < 30:
        return f"  {asset}: Insufficient data"

    opens  = [c[0] for c in daily]
    highs  = [c[1] for c in daily]
    lows   = [c[2] for c in daily]
    closes = [c[3] for c in daily]
    vols   = [c[4] for c in daily]

    price   = closes[-1]
    rsi     = calc_rsi(closes)
    macd_v, macd_s, macd_h = calc_macd(closes)
    bb_low, bb_mid, bb_high = calc_bollinger(closes)
    res, sup = support_resistance(highs, lows)

    ema20_vals = ema(closes, 20)
    ema50_vals = ema(closes, 50)
    ema200_vals = ema(closes, min(200, len(closes)))
    ema20  = round(ema20_vals[-1], 4) if ema20_vals else None
    ema50  = round(ema50_vals[-1], 4) if ema50_vals else None
    ema200 = round(ema200_vals[-1], 4) if ema200_vals else None

    # Volume trend (last 7d vs prior 7d)
    vol_recent = sum(vols[-7:]) / 7 if len(vols) >= 7 else None
    vol_prior  = sum(vols[-14:-7]) / 7 if len(vols) >= 14 else None
    vol_trend  = "Rising" if (vol_recent and vol_prior and vol_recent > vol_prior * 1.1) else \
                 "Falling" if (vol_recent and vol_prior and vol_recent < vol_prior * 0.9) else "Flat"

    # 4h RSI for short-term
    rsi_4h = None
    if len(h4) >= 15:
        rsi_4h = calc_rsi([c[3] for c in h4])

    # Price vs EMAs
    trend_signals = []
    if ema20 and price > ema20: trend_signals.append("above EMA20")
    if ema50 and price > ema50: trend_signals.append("above EMA50")
    if ema200 and price > ema200: trend_signals.append("above EMA200")
    trend = ", ".join(trend_signals) if trend_signals else "below key EMAs"

    def fp(v):
        """Format a price with enough decimal places regardless of magnitude."""
        if v is None:
            return "N/A"
        if v >= 1:
            return f"${v:,.4f}"
        # Find first significant digit and show 4 sig figs after it
        import math
        decimals = max(4, -int(math.floor(math.log10(abs(v)))) + 3) if v > 0 else 8
        return f"${v:.{decimals}f}"

    lines = [
        f"\n  {asset} Technical Analysis (90-day daily + 4h):",
        f"  Price:      {fp(price)}",
        f"  Trend:      {trend}",
        f"  EMA20/50/200: {fp(ema20)} / {fp(ema50)} / {fp(ema200) if ema200 else 'N/A'}",
        f"  RSI(14):    {rsi} (daily) | {rsi_4h} (4h)" if rsi_4h else f"  RSI(14):    {rsi}",
        f"  MACD:       {macd_v} | Signal: {macd_s} | Hist: {macd_h}" if macd_v else "  MACD:       N/A",
        f"  Bollinger:  Low {fp(bb_low)} | Mid {fp(bb_mid)} | High {fp(bb_high)}" if bb_low else "  Bollinger:  N/A",
        f"  Support:    {fp(sup)} | Resistance: {fp(res)}",
        f"  Volume:     {vol_trend} (7d avg vs prior 7d)",
    ]
    return "\n".join(lines)


# ── Fundamental Analysis ───────────────────────────────────────────────────────

# CoinGecko symbol mapping for common assets
COINGECKO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
    "SOL": "solana", "XRP": "ripple", "ADA": "cardano",
    "DOGE": "dogecoin", "DOT": "polkadot", "AVAX": "avalanche-2",
    "LINK": "chainlink", "MATIC": "matic-network", "UNI": "uniswap",
    "ATOM": "cosmos", "LTC": "litecoin", "ETC": "ethereum-classic",
    "PEPE": "pepe", "SHIB": "shiba-inu", "ARB": "arbitrum",
    "OP": "optimism", "INJ": "injective-protocol", "SUI": "sui",
    "TIA": "celestia", "SEI": "sei-network", "NEAR": "near",
    "FTM": "fantom", "ALGO": "algorand", "EOS": "eos",
    "TREE": "tree",
}


def fetch_fundamentals(assets):
    """Fetch fundamental data from CoinGecko free API."""
    results = {}
    ids = [COINGECKO_IDS[a] for a in assets if a in COINGECKO_IDS and a != "USDT"]
    if not ids:
        return results
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ",".join(ids),
                "order": "market_cap_desc",
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d,30d",
            },
            timeout=12,
        )
        r.raise_for_status()
        for coin in r.json():
            asset = next((a for a, cid in COINGECKO_IDS.items() if cid == coin["id"]), None)
            if not asset:
                continue
            results[asset] = {
                "market_cap":      coin.get("market_cap"),
                "market_cap_rank": coin.get("market_cap_rank"),
                "volume_24h":      coin.get("total_volume"),
                "change_1h":       coin.get("price_change_percentage_1h_in_currency"),
                "change_24h":      coin.get("price_change_percentage_24h_in_currency"),
                "change_7d":       coin.get("price_change_percentage_7d_in_currency"),
                "change_30d":      coin.get("price_change_percentage_30d_in_currency"),
                "ath":             coin.get("ath"),
                "ath_change_pct":  coin.get("ath_change_percentage"),
                "circulating_supply": coin.get("circulating_supply"),
                "total_supply":    coin.get("total_supply"),
            }
    except Exception as e:
        print(f"  CoinGecko error: {e}")
    return results


def format_fundamentals(asset, data):
    if not data:
        return f"  {asset}: Fundamental data unavailable"

    def fmt_pct(v):
        if v is None: return "N/A"
        return f"+{v:.1f}%" if v >= 0 else f"{v:.1f}%"

    def fmt_large(v):
        if v is None: return "N/A"
        if v >= 1e9: return f"${v/1e9:.1f}B"
        if v >= 1e6: return f"${v/1e6:.1f}M"
        return f"${v:,.0f}"

    vol_mcap = (data["volume_24h"] / data["market_cap"] * 100) if data.get("market_cap") and data.get("volume_24h") else None

    lines = [
        f"\n  {asset} Fundamentals:",
        f"  Market Cap:   {fmt_large(data['market_cap'])} (Rank #{data['market_cap_rank']})",
        f"  24h Volume:   {fmt_large(data['volume_24h'])} ({f'{vol_mcap:.1f}%' if vol_mcap else 'N/A'} of MCap)",
        f"  Performance:  1h {fmt_pct(data['change_1h'])} | 24h {fmt_pct(data['change_24h'])} | 7d {fmt_pct(data['change_7d'])} | 30d {fmt_pct(data['change_30d'])}",
        f"  ATH:          ${data['ath']:,.4f} ({fmt_pct(data['ath_change_pct'])} from ATH)",
        f"  Supply:       {(str(round(data['circulating_supply'])) if data.get('circulating_supply') else 'N/A')} circ"
        + (f" / {data['total_supply']:,.0f} total" if data.get("total_supply") else ""),
    ]
    return "\n".join(lines)


# ── Analyzer ───────────────────────────────────────────────────────────────────

def item_age_label(published_str, now):
    """Return a human-readable age string and a freshness tier for a news item."""
    dt = parse_date(published_str)
    if dt is None:
        return "unknown age", "UNKNOWN"
    mins = (now - dt).total_seconds() / 60
    if mins < 0:
        mins = 0
    if mins < 30:
        return f"{mins:.0f}m ago", "FRESH"
    if mins < 120:
        return f"{mins/60:.1f}h ago", "RECENT"
    if mins < 1440:
        return f"{mins/60:.1f}h ago", "OLD"
    return f"{mins/1440:.1f}d ago", "STALE"


def analyze(items, portfolio, ta_text, fa_text, trade_history_text, pending_orders_text):
    now = datetime.now(timezone.utc)
    now_utc = now.strftime("%Y-%m-%d %H:%M UTC")
    portfolio_text = format_portfolio(portfolio)

    if items:
        lines = []
        for i, item in enumerate(items[:120], 1):
            age, tier = item_age_label(item["published"], now)
            lines.append(
                f"{i}. SOURCE: {item['source']}  |  AGE: {age} [{tier}]\n"
                f"   TITLE: {item['title']}\n"
                f"   SUMMARY: {item['summary']}\n"
                f"   LINK: {item['link']}"
            )
        items_text = "\n\n".join(lines)
    else:
        items_text = "No items were fetched from any source this run."

    prompt = f"""You are a professional crypto investment manager. Produce a concise but complete investment brief combining news signals, technical analysis, fundamentals, and trade history. Be direct — no fluff.

=== NEW MARKET SIGNALS (this scan only — do not invent or recall past news) ===
{items_text}

=== PORTFOLIO ===
{portfolio_text}

=== TRADE & ORDER HISTORY (includes real Binance open orders with binance_order_id) ===
{trade_history_text}

=== PREVIOUSLY PENDING ORDERS (from last recommendation — watcher is still watching these) ===
{pending_orders_text}

=== TECHNICAL ANALYSIS ===
{ta_text}

=== FUNDAMENTAL ANALYSIS ===
{fa_text}

=== RULES ===
- News signals: HIGH or EXTREME only qualify. Medium/Low = ignore.
- Always include TA and FA sections even if no news signals.
- Output NO_SIGNALS only if there are zero news signals AND no notable TA/FA developments worth acting on.
- Plain text only. No markdown. Be concise.
- ALL trade recommendations MUST include exact USDT dollar amount AND exact coin quantity. No vague sizing.

OPEN & PENDING ORDER MANAGEMENT RULES (evaluate every cycle — do not ignore):
- Review PREVIOUSLY PENDING ORDERS above. For each one explicitly decide:
  - KEEP: if the trigger price is still valid and market hasn't moved past it → do NOT include it in ORDERS_JSON (watcher keeps watching automatically).
  - CANCEL: if the trigger is now irrelevant, stale, or market has already passed it → output {{"action": "CANCEL", "id": "<exact id>", "rationale": "..."}}.
  - REPLACE: if a better price level now exists → output CANCEL of the old id PLUS a new NEW order at the revised price.
- Review Binance Open Orders in TRADE & ORDER HISTORY. For each:
  - If still valid → do nothing.
  - If stale or wrong direction given current TA/FA → output {{"action": "CANCEL_BINANCE", "symbol": "XYZUSDT", "binance_order_id": <id>, "rationale": "..."}}.
  - If better price exists → output CANCEL_BINANCE of old order PLUS a NEW replacement order.
- Limit orders expire in 2h. If remaining time is short and target is unlikely → recommend cancel/replace now rather than letting it expire unused.

NEWS FRESHNESS RULES (timing matters — weight signals by age tier):
- [FRESH] < 30 min: full weight — treat as actionable, price may not have reacted yet.
- [RECENT] 30 min–2 h: moderate weight — market may have partially priced it in already; size conservatively.
- [OLD] 2–24 h: low weight — market has likely priced it in; use as background context only, not a standalone trade trigger.
- [STALE] > 24 h: background context only — do not use as a trade trigger; mention only if it reinforces TA/FA.
- [UNKNOWN] age: treat as OLD.
- If the only HIGH/EXTREME signals are OLD or STALE, do not escalate to a BUY/SELL recommendation on news alone — defer to TA/FA.

TRADE HISTORY RULES (critical — recommendations must be grounded in reality):
- Check each asset's last trade date and side. If bought within the last 48 hours, do NOT recommend buying again unless there is an EXTREME signal — default to HOLD and note the recent entry.
- If sold within the last 48 hours, do NOT recommend selling again. Default to HOLD or flag as recently exited.
- Use avg buy price vs current price to calculate unrealised P&L. If already in profit >15%, consider partial take-profit sizing rather than full sell.
- If an open limit order already exists for an asset, do NOT recommend the same direction — note the open order and suggest waiting or cancelling it first.
- Sizing must be proportional to current holdings. Do not recommend buying more of an asset than what is already held in USDT terms unless there is a very strong signal.
- If USDT balance is low, prioritise reducing a losing position over adding new ones.
- Never recommend a trade that contradicts the most recent completed trade without a clear new TA/FA/news reason.

=== OUTPUT ===

INVESTMENT BRIEF — {now_utc}

PORTFOLIO: [Asset $val (pct) | ... | Total $X]

--- NEWS SIGNALS ---
[If none: "No high-impact signals this scan."]
[Max 3, each 1 line: # Bull/Bear HIGH/EXTREME [AGE TIER, Xm/Xh ago] — Asset: what happened]

--- TECHNICAL ANALYSIS ---
[Per held asset, 3 lines max:]
Asset: [Bullish/Bearish/Neutral trend]. RSI [val] [overbought>70/oversold<30/neutral]. MACD [bullish/bearish/flat]. [Above/Below] EMA20/50/200.
Support $X / Resistance $Y. Volume [Rising/Falling/Flat].

--- FUNDAMENTAL ANALYSIS ---
[Per held asset, 2 lines max:]
Asset (Rank #X, $XB mcap): 24h [X%] | 7d [X%] | 30d [X%]. Vol/MCap [X%]. [X%] from ATH.
[1 sentence: fundamental strength or weakness]

--- TRADE HISTORY SUMMARY ---
[Per held asset, 1-2 lines:]
Asset: Avg entry $X. Current $Y. Unrealised P&L: +/-$Z (+/-X%). Last trade: [BUY/SELL] X days ago. [Open orders if any.]

--- INVESTMENT RECOMMENDATION ---
Stance: [Risk-On / Risk-Off / Neutral]

[Per asset: STRONG BUY / BUY / HOLD / REDUCE / SELL->USDT]
Rationale: [1 sentence TA+FA+news justification, explicitly referencing cost basis or recent trade if relevant]
Action: [BUY/SELL] exactly $X.XX USDT = X.XXXXXX [ASSET] at market / limit $X.XX
[If no action warranted: HOLD — [reason, e.g. "entered 1 day ago at $X, wait for confirmation"]]

Priority actions:
1. [Asset] [BUY/SELL] $X.XX (X.XXXXXX coins) — [reason, suggested order type + price level]
2. [Next]
3. [Max 4 total — omit assets where HOLD is the correct call]

===ORDERS_START===
Output a JSON array covering ALL order actions this cycle. Three action types:

1. NEW order (HOLD recommendations must NOT appear):
{{"action":"NEW","asset":"XYZ","side":"BUY|SELL","order_type":"MARKET|LIMIT|STOP","quantity_coin":0.0,"quantity_usdt":0.0,"limit_price":null,"stop_price":null,"rationale":"..."}}
- MARKET: executes immediately. LIMIT SELL fires when price >= limit_price. LIMIT BUY fires when price <= limit_price. STOP SELL fires when price <= stop_price.
- Limit/stop orders expire in 2h — set a realistic price level reachable within that window.

2. Cancel a previously pending recommendation:
{{"action":"CANCEL","id":"exact_id_from_PREVIOUSLY_PENDING_ORDERS","rationale":"..."}}

3. Cancel a real Binance open order:
{{"action":"CANCEL_BINANCE","symbol":"XYZUSDT","binance_order_id":12345,"rationale":"..."}}

If no actions at all (all HOLDs, no cancels needed), output: []
===ORDERS_END===

SUBJECT: CRYPTO SIGNAL: <5 words describing the key action>

IMPORTANT: The very last line of your response MUST be exactly:
SUBJECT: CRYPTO SIGNAL: [your 5 words here]
Do not add any text after it."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# ── Order extraction ───────────────────────────────────────────────────────────

PENDING_ORDERS_FILE  = os.path.join(os.path.dirname(__file__), "pending_orders.json")
WATCHER_LOCK_FILE    = os.path.join(os.path.dirname(__file__), "watcher.lock")
MARKET_EXPIRY_H      = 1   # market orders must execute within 1h or are stale
LIMIT_EXPIRY_H       = 2   # limit/stop orders expire after 2h if price never reached


def extract_orders(response_text):
    """Parse the ===ORDERS_START=== ... ===ORDERS_END=== block from Claude's response."""
    import json as _json
    match = re.search(r"===ORDERS_START===\s*(.*?)\s*===ORDERS_END===", response_text, re.DOTALL)
    if not match:
        return []
    raw = match.group(1).strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
    try:
        orders = _json.loads(raw)
        if not isinstance(orders, list):
            return []
        return orders
    except Exception as e:
        print(f"  Order JSON parse error: {e}")
        return []


def load_current_pending_for_context():
    """Return a formatted string of still-pending orders for Claude's context."""
    import json as _json
    if not os.path.exists(PENDING_ORDERS_FILE):
        return "None."
    try:
        data    = _json.loads(open(PENDING_ORDERS_FILE).read())
        pending = [o for o in data.get("orders", []) if o.get("status") == "pending"]
        if not pending:
            return "None."
        lines = [f"  (generated {data.get('generated_at','?')})"]
        for o in pending:
            lp   = o.get("limit_price")
            sp   = o.get("stop_price")
            trig = f"limit ${lp:,.4f}" if lp else f"stop ${sp:,.4f}" if sp else "MARKET"
            exp  = o.get("expires_at", "?")
            lines.append(
                f"  ID: {o['id']}\n"
                f"    {o['side']} {o['asset']} {o.get('order_type','MARKET')} "
                f"{float(o.get('quantity_coin',0)):.6f} coins (~${float(o.get('quantity_usdt',0)):.2f} USDT)"
                f"  trigger: {trig}  expires: {exp}\n"
                f"    Original rationale: {o.get('rationale','')}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading pending orders: {e}"


def write_pending_orders(orders, generated_at):
    """
    Process Claude's order actions and merge into pending_orders.json.

    Supported actions:
      NEW            — add a new pending order (default if action field absent)
      CANCEL         — cancel a previously pending order by its id
      CANCEL_BINANCE — cancel a real Binance open order by binance_order_id
    """
    import json as _json
    from datetime import timedelta

    now = generated_at if isinstance(generated_at, datetime) else datetime.fromisoformat(str(generated_at))

    # Load existing orders, keyed by id
    existing = {}
    if os.path.exists(PENDING_ORDERS_FILE):
        try:
            old = _json.loads(open(PENDING_ORDERS_FILE).read())
            existing = {o["id"]: o for o in old.get("orders", [])}
        except Exception:
            pass

    n_new = n_cancel = n_cancel_binance = 0

    for o in orders:
        action = o.get("action", "NEW").upper()

        if action == "CANCEL":
            oid = o.get("id", "")
            if oid in existing and existing[oid]["status"] == "pending":
                existing[oid]["status"]      = "cancelled"
                existing[oid]["cancelled_at"] = now.isoformat()
                existing[oid]["cancel_reason"] = o.get("rationale", "superseded by new recommendation")
                n_cancel += 1
                print(f"  CANCEL pending order: {oid}")
            else:
                print(f"  CANCEL: id '{oid}' not found or not pending — skipped")

        elif action == "CANCEL_BINANCE":
            # Add a watcher task to cancel a real Binance open order
            binance_id = o.get("binance_order_id")
            symbol     = o.get("symbol", "")
            if not binance_id or not symbol:
                print(f"  CANCEL_BINANCE: missing binance_order_id or symbol — skipped")
                continue
            task_id = f"CANCEL_BINANCE_{symbol}_{now.strftime('%Y%m%d%H%M')}"
            existing[task_id] = {
                "id":               task_id,
                "action":           "CANCEL_BINANCE",
                "symbol":           symbol,
                "binance_order_id": binance_id,
                "rationale":        o.get("rationale", ""),
                "generated_at":     now.isoformat(),
                "status":           "pending",
            }
            n_cancel_binance += 1
            print(f"  CANCEL_BINANCE queued: {symbol} orderId {binance_id}")

        else:  # NEW
            asset = o.get("asset", "").upper()
            side  = o.get("side", "").upper()
            if not asset or side not in ("BUY", "SELL"):
                continue
            otype   = o.get("order_type", "MARKET").upper()

            # Guard: reject duplicate pending MARKET orders for the same asset+side
            if otype == "MARKET":
                dup = next(
                    (ex for ex in existing.values()
                     if ex.get("status") == "pending"
                     and ex.get("action", "NEW").upper() == "NEW"
                     and ex.get("asset", "").upper() == asset
                     and ex.get("side", "").upper() == side
                     and ex.get("order_type", "MARKET").upper() == "MARKET"),
                    None,
                )
                if dup:
                    print(f"  SKIP duplicate MARKET {side} {asset} — already pending as {dup['id']}")
                    continue
            exp_h   = MARKET_EXPIRY_H if otype == "MARKET" else LIMIT_EXPIRY_H
            expires = (now + timedelta(hours=exp_h)).isoformat()
            base_oid = f"{asset}_{side}_{now.strftime('%Y%m%d%H%M')}"
            oid, counter = base_oid, 1
            while oid in existing and existing[oid].get("status") == "pending":
                oid = f"{base_oid}_{counter}"
                counter += 1
            existing[oid] = {
                "id":            oid,
                "action":        "NEW",
                "asset":         asset,
                "side":          side,
                "order_type":    otype,
                "quantity_coin": float(o.get("quantity_coin", 0)),
                "quantity_usdt": float(o.get("quantity_usdt", 0)),
                "limit_price":   o.get("limit_price"),
                "stop_price":    o.get("stop_price"),
                "rationale":     o.get("rationale", ""),
                "generated_at":  now.isoformat(),
                "expires_at":    expires,
                "status":        "pending",
            }
            n_new += 1

    payload = {"generated_at": now.isoformat(), "orders": list(existing.values())}
    with open(PENDING_ORDERS_FILE, "w") as f:
        _json.dump(payload, f, indent=2)
    print(f"  Orders written — new: {n_new}, cancelled: {n_cancel}, cancel_binance: {n_cancel_binance}")
    return list(existing.values())


def is_watcher_running():
    """Check whether a watcher process is already alive using its lock file.
    Auto-clears stale lock files when the recorded PID is no longer running."""
    if not os.path.exists(WATCHER_LOCK_FILE):
        return False
    try:
        pid = int(open(WATCHER_LOCK_FILE).read().strip())
        # Windows: tasklist is the reliable way to check PID existence
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        alive = str(pid) in result.stdout
        if not alive:
            # Stale lock — process is gone, remove so we can respawn
            try:
                os.remove(WATCHER_LOCK_FILE)
            except Exception:
                pass
        return alive
    except Exception:
        # Any error → assume not running, attempt to clear stale lock
        try:
            os.remove(WATCHER_LOCK_FILE)
        except Exception:
            pass
        return False


def start_watcher():
    """Launch order_watcher.py as a detached background process."""
    script  = os.path.join(os.path.dirname(__file__), "order_watcher.py")
    python  = os.path.join(os.path.dirname(sys.executable), "python.exe")
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "watcher.log")

    with open(log_path, "a") as log_fh:
        proc = subprocess.Popen(
            [python, script],
            stdout=log_fh,
            stderr=log_fh,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    print(f"  Watcher started (PID {proc.pid}). Output → logs/watcher.log")


def kill_watcher():
    """Kill the running watcher process and remove its lock file. Returns True if killed."""
    if not os.path.exists(WATCHER_LOCK_FILE):
        return False
    try:
        pid = int(open(WATCHER_LOCK_FILE).read().strip())
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
        print(f"  Stopped old watcher (PID {pid}).")
        # Force-remove lock since the killed process may not reach its finally block
        for _ in range(10):
            if not os.path.exists(WATCHER_LOCK_FILE):
                break
            time.sleep(0.3)
        try:
            os.remove(WATCHER_LOCK_FILE)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"  Warning: could not stop old watcher: {e}")
        try:
            os.remove(WATCHER_LOCK_FILE)
        except Exception:
            pass
        return False


def start_watcher_if_needed():
    """Always ensure exactly one watcher runs: kill any existing one, then start fresh."""
    if is_watcher_running():
        kill_watcher()
    start_watcher()


def print_watcher_status():
    """Print a clear watcher + pending-orders status block for the operator."""
    import json as _json
    running = is_watcher_running()
    if running:
        try:
            pid = int(open(WATCHER_LOCK_FILE).read().strip())
            print(f"\nWatcher status : RUNNING (PID {pid}) — logs/watcher.log")
        except Exception:
            print("\nWatcher status : RUNNING")
    else:
        print("\nWatcher status : NOT running")

    if not os.path.exists(PENDING_ORDERS_FILE):
        print("Pending orders : none (file absent)")
        return
    try:
        data    = _json.loads(open(PENDING_ORDERS_FILE).read())
        orders  = data.get("orders", [])
        pending = [o for o in orders if o.get("status") == "pending"]
        if not pending:
            print("Pending orders : none")
            return
        now = datetime.now(timezone.utc)
        print(f"Pending orders : {len(pending)}")
        for o in pending:
            lp   = o.get("limit_price")
            sp   = o.get("stop_price")
            trig = f"limit ${lp:,.4f}" if lp else f"stop ${sp:,.4f}" if sp else "MARKET"
            exp  = o.get("expires_at", "")
            mins_left = ""
            if exp:
                try:
                    exp_dt = datetime.fromisoformat(exp)
                    ml = int((exp_dt - now).total_seconds() / 60)
                    mins_left = f"  ({ml}m left)" if ml > 0 else "  (EXPIRED)"
                except Exception:
                    pass
            print(
                f"  {o['id']:<30s}  {o['side']} {o['asset']} "
                f"{o.get('order_type','MARKET')}  "
                f"{float(o.get('quantity_coin', 0)):.6f} coins  "
                f"{trig}{mins_left}"
            )
    except Exception as e:
        print(f"Pending orders : error reading file — {e}")


def strip_orders_block(text):
    """Remove the ===ORDERS_START=== block from the email body."""
    return re.sub(r"\n*===ORDERS_START===.*?===ORDERS_END===\n*", "\n", text, flags=re.DOTALL).strip()


# ── Email ──────────────────────────────────────────────────────────────────────

def send_email(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = f"Crypto Signal Alert <{GMAIL_USER}>"
    msg["To"]      = RECIPIENT

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())
    print(f"Email sent to {RECIPIENT}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc)

    print("=" * 60)
    print("Crypto Market Signal Digest")
    print(now.strftime("%Y-%m-%d %H:%M UTC"))
    print("=" * 60)

    last_run = load_last_run()
    if last_run:
        elapsed = int((now - last_run).total_seconds() / 60)
        print(f"Last run: {last_run.strftime('%Y-%m-%d %H:%M UTC')} ({elapsed} min ago)")
        since_dt = last_run
    else:
        since_dt = now - timedelta(minutes=DEFAULT_LOOKBACK)
        print(f"First run — scanning last {DEFAULT_LOOKBACK} minutes")

    items = fetch_all_items(since_dt)

    print("\nFetching Binance portfolio...")
    portfolio = fetch_binance_portfolio()

    assets = [p["asset"] for p in portfolio if p["asset"] != "USDT"]

    print("\nFetching trade & order history...")
    trades_by_asset, open_orders = fetch_trade_history(assets)
    trade_history_text = format_trade_history(trades_by_asset, open_orders)

    print("\nRunning technical analysis...")
    ta_parts = [perform_ta(a) for a in assets]
    ta_text = "\n".join(ta_parts) if ta_parts else "No assets to analyse."

    print("\nFetching fundamental data...")
    fa_raw = fetch_fundamentals(assets)
    fa_parts = [format_fundamentals(a, fa_raw.get(a)) for a in assets]
    fa_text = "\n".join(fa_parts) if fa_parts else "No fundamental data available."

    print("\nLoading previously pending orders for context...")
    pending_orders_text = load_current_pending_for_context()
    print(f"  {pending_orders_text.splitlines()[0] if pending_orders_text != 'None.' else 'None pending.'}")

    print("\nAnalyzing with Claude Sonnet...")
    result = analyze(items, portfolio, ta_text, fa_text, trade_history_text, pending_orders_text)

    # Extract and persist structured orders before stripping the block
    print("\nExtracting orders...")
    orders = extract_orders(result)
    if orders:
        write_pending_orders(orders, now)
        start_watcher_if_needed()
    else:
        print("  No actionable orders found in this analysis.")

    # Strip the orders block from the response before emailing
    result_clean = strip_orders_block(result)

    # Split body from subject (subject is always last line)
    lines = result_clean.strip().splitlines()
    subject = "CRYPTO SIGNAL: Market Update"
    body_lines = lines

    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if re.match(r"^SUBJECT[:\s]", stripped, re.IGNORECASE):
            candidate = re.sub(r"^SUBJECT[:\s]*", "", stripped, flags=re.IGNORECASE).strip()
            if candidate:
                subject = candidate
            body_lines = lines[:i]
            break

    body = "\n".join(body_lines).strip()

    print("\n" + "-" * 60)
    print(body)
    print("-" * 60)

    # Skip email if Claude found no actionable signals
    if result.strip() == "NO_SIGNALS":
        print("No high-impact signals this run — email skipped.")
        save_last_run(now)
        print(f"Done. Last run timestamp saved: {now.isoformat()}")
        print_watcher_status()
        return

    print(f"Subject: {subject}")
    print("-" * 60)
    print("\nSending email...")
    send_email(subject, body)
    save_last_run(now)
    print(f"Done. Last run timestamp saved: {now.isoformat()}")
    print_watcher_status()


if __name__ == "__main__":
    main()
