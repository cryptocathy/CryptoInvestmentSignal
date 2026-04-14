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
BINANCE_BASE_URL   = "https://api.binance.com"
MIN_USD_VALUE      = 1.0  # ignore dust balances below $1
SEEN_TITLES_FILE   = os.path.join(os.path.dirname(__file__), "seen_titles.txt")
SEEN_TITLES_MAX    = 500  # rolling cache size

# ── News RSS feeds ─────────────────────────────────────────────────────────────
NEWS_FEEDS = [
    ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("The Block",     "https://www.theblock.co/rss.xml"),
    ("Decrypt",       "https://decrypt.co/feed"),
    ("CryptoPanic",   "https://cryptopanic.com/news/rss/"),
    ("Reuters",       "https://feeds.reuters.com/reuters/businessNews"),
    ("Investing.com", "https://www.investing.com/rss/news_301.rss"),
]

# ── X / Nitter ─────────────────────────────────────────────────────────────────
NITTER_INSTANCES = [
    "nitter.privacydev.net",
    "nitter.poast.org",
    "nitter.1d4.us",
    "nitter.unixfox.eu",
]

INFLUENCERS = [
    "saylor", "VitalikButerin", "cz_binance", "elonmusk", "APompliano",
    "RaoulGMI", "woonomic", "100trillionUSD", "CryptoHayes", "novogratz",
    "DocumentingBTC", "BitcoinMagazine",
]

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
        root = ET.fromstring(xml_text)
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


def fetch_nitter(username, timeout=5):
    for instance in NITTER_INSTANCES:
        url = f"https://{instance}/{username}/rss"
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                items = parse_rss(r.text, f"@{username} (X)")
                if items:
                    return items
        except Exception:
            continue
    return []


# ── Fetcher ────────────────────────────────────────────────────────────────────

def fetch_all_items(since_dt):
    all_items = []

    print("Fetching news RSS feeds...")
    for source_name, url in NEWS_FEEDS:
        items = fetch_rss(url, source_name)
        print(f"  {source_name}: {len(items)} items")
        all_items.extend(items)

    print("\nFetching X posts via Nitter...")
    for username in INFLUENCERS:
        items = fetch_nitter(username)
        status = f"{len(items)} items" if items else "unavailable"
        print(f"  @{username}: {status}")
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


def calc_macd(closes):
    if len(closes) < 26:
        return None, None, None
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    # Align lengths
    diff = len(ema12) - len(ema26)
    macd_line = [a - b for a, b in zip(ema12[diff:], ema26)]
    if len(macd_line) < 9:
        return None, None, None
    signal_line = ema(macd_line, 9)
    histogram = macd_line[-1] - signal_line[-1]
    return round(macd_line[-1], 4), round(signal_line[-1], 4), round(histogram, 4)


def calc_bollinger(closes, period=20):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    std = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    return round(mid - 2 * std, 4), round(mid, 4), round(mid + 2 * std, 4)


def support_resistance(highs, lows, lookback=30):
    h = highs[-lookback:]
    l = lows[-lookback:]
    return round(max(h), 4), round(min(l), 4)


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

    lines = [
        f"\n  {asset} Technical Analysis (90-day daily + 4h):",
        f"  Price:      ${price:,.4f}",
        f"  Trend:      {trend}",
        f"  EMA20/50/200: ${ema20:,.2f} / ${ema50:,.2f} / {('$'+f'{ema200:,.2f}') if ema200 else 'N/A'}",
        f"  RSI(14):    {rsi} (daily) | {rsi_4h} (4h)" if rsi_4h else f"  RSI(14):    {rsi}",
        f"  MACD:       {macd_v} | Signal: {macd_s} | Hist: {macd_h}" if macd_v else "  MACD:       N/A",
        f"  Bollinger:  Low ${bb_low:,.2f} | Mid ${bb_mid:,.2f} | High ${bb_high:,.2f}" if bb_low else "  Bollinger:  N/A",
        f"  Support:    ${sup:,.4f} | Resistance: ${res:,.4f}",
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

def analyze(items, portfolio, ta_text, fa_text):
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    portfolio_text = format_portfolio(portfolio)

    if items:
        lines = []
        for i, item in enumerate(items[:120], 1):
            lines.append(
                f"{i}. SOURCE: {item['source']}\n"
                f"   TITLE: {item['title']}\n"
                f"   SUMMARY: {item['summary']}\n"
                f"   LINK: {item['link']}\n"
                f"   DATE: {item['published']}"
            )
        items_text = "\n\n".join(lines)
    else:
        items_text = "No items were fetched from any source this run."

    prompt = f"""You are a professional crypto investment manager. Produce a concise but complete investment brief combining news signals, technical analysis, and fundamentals. Be direct — no fluff.

=== NEW MARKET SIGNALS (this scan only — do not invent or recall past news) ===
{items_text}

=== PORTFOLIO ===
{portfolio_text}

=== TECHNICAL ANALYSIS ===
{ta_text}

=== FUNDAMENTAL ANALYSIS ===
{fa_text}

=== RULES ===
- News signals: HIGH or EXTREME only qualify. Medium/Low = ignore.
- Always include TA and FA sections even if no news signals.
- Output NO_SIGNALS only if there are zero news signals AND no notable TA/FA developments worth acting on.
- Plain text only. No markdown. Be concise.
- Use claude-sonnet-4-6 level reasoning but keep output tight.

=== OUTPUT ===

INVESTMENT BRIEF — {now_utc}

PORTFOLIO: [Asset $val (pct) | ... | Total $X]

--- NEWS SIGNALS ---
[If none: "No high-impact signals this scan."]
[Max 3, each 1 line: # Bull/Bear HIGH/EXTREME — Asset: what happened]

--- TECHNICAL ANALYSIS ---
[Per held asset, 3 lines max:]
Asset: [Bullish/Bearish/Neutral trend]. RSI [val] [overbought>70/oversold<30/neutral]. MACD [bullish/bearish/flat]. [Above/Below] EMA20/50/200.
Support $X / Resistance $Y. Volume [Rising/Falling/Flat].

--- FUNDAMENTAL ANALYSIS ---
[Per held asset, 2 lines max:]
Asset (Rank #X, $XB mcap): 24h [X%] | 7d [X%] | 30d [X%]. Vol/MCap [X%]. [X%] from ATH.
[1 sentence: fundamental strength or weakness]

--- INVESTMENT RECOMMENDATION ---
Stance: [Risk-On / Risk-Off / Neutral]

[Per asset: STRONG BUY / BUY / HOLD / REDUCE / SELL->USDT — 1 sentence TA+FA+news rationale]

Priority actions:
1. [Most urgent, with sizing]
2. [Next]
3. [Max 4 total]

SUBJECT: CRYPTO SIGNAL: <5 words>

Output SUBJECT as last line."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


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

    print("\nRunning technical analysis...")
    ta_parts = [perform_ta(a) for a in assets]
    ta_text = "\n".join(ta_parts) if ta_parts else "No assets to analyse."

    print("\nFetching fundamental data...")
    fa_raw = fetch_fundamentals(assets)
    fa_parts = [format_fundamentals(a, fa_raw.get(a)) for a in assets]
    fa_text = "\n".join(fa_parts) if fa_parts else "No fundamental data available."

    print("\nAnalyzing with Claude Sonnet...")
    result = analyze(items, portfolio, ta_text, fa_text)

    # Split body from subject (subject is always last line)
    lines = result.strip().splitlines()
    subject = "CRYPTO SIGNAL: Market Update"
    body_lines = lines

    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("SUBJECT:"):
            subject = lines[i].replace("SUBJECT:", "").strip()
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
        return

    print(f"Subject: {subject}")
    print("-" * 60)
    print("\nSending email...")
    send_email(subject, body)
    save_last_run(now)
    print(f"Done. Last run timestamp saved: {now.isoformat()}")


if __name__ == "__main__":
    main()
