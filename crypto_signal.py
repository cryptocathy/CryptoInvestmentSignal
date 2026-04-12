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

    in_window = [i for i in all_items if is_after(i["published"], since_dt)]
    elapsed_min = int((datetime.now(timezone.utc) - since_dt).total_seconds() / 60)
    print(f"\nTotal fetched: {len(all_items)} | Since last run ({elapsed_min}min ago): {len(in_window)}")

    return in_window


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


# ── Analyzer ───────────────────────────────────────────────────────────────────

def analyze(items, portfolio):
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

    prompt = f"""You are a Crypto Market Monitoring Analyst and Portfolio Advisor. Analyze the news items below alongside the user's Binance portfolio, then produce a signal digest with personalised trading suggestions.

FETCHED ITEMS:
{items_text}

USER'S BINANCE PORTFOLIO (read-only — suggestions only, no trades placed automatically):
{portfolio_text}

YOUR TASK:

STEP 1 — FILTER
Keep only items meeting AT LEAST 2 of:
- From a credible/influential source (major outlet or known crypto figure)
- Contains a strong claim, prediction, or breaking news
- Mentions specific tokens, sectors, or catalysts (BTC, ETH, DeFi, AI tokens, regulation, ETF, rates)
- High urgency or specificity (price targets, deadlines, votes, rulings, hacks)
Discard memes, generic takes, reposts of old news, low-signal filler.

STEP 2 — ANALYZE each kept item:
- Signal Direction: Bullish / Bearish / Neutral
- Signal Strength: Low / Medium / High / Extreme
- Signal Type: Narrative Shift / Breaking News / Insider Insight / Macro Impact / Technical Catalyst
- Affected Assets: list specific tokens or sectors
- Time Sensitivity: Immediate / Short-term / Medium-term
- Reasoning: 1 sentence on why this could move the market

STEP 3 — PRIORITIZE: Keep only TOP 3-5 signals ranked by (1) market impact, (2) urgency, (3) credibility.
Only signals with Strength of Medium, High, or Extreme qualify. Discard Low-strength signals entirely.

STEP 4 — PORTFOLIO SUGGESTIONS
For each qualifying signal, cross-reference the portfolio and suggest:
- BUY [asset]: bullish signal + user has USDT available. Include rough % of USDT to deploy (High=20-30%, Extreme=30-50%).
- SELL [asset] -> USDT: bearish signal + user holds it. Suggest converting to USDT to preserve capital and wait for re-entry.
- HOLD [asset]: bullish signal, user already holds it well. Suggest holding or small add.
- WATCH [asset]: signal is relevant but user has no position and no USDT to deploy. Flag for future opportunity.
Never suggest deploying 100% into one asset.

STEP 5 — DECIDE whether to send an alert:
- If NO signals qualify: output exactly one line: NO_SIGNALS
- If signals exist: output the full email in plain text, no markdown:

============================================================
CRYPTO MARKET SIGNAL DIGEST
{now_utc}
============================================================

PORTFOLIO SNAPSHOT
Asset    | Qty        | Price (USDT) | Value (USDT) | Allocation
[one row per asset, aligned]
Total: $X,XXX USDT

------------------------------------------------------------
1. TOP SIGNALS
[Bullet list 3-5 signals, 1 line each starting with *]

------------------------------------------------------------
2. SIGNAL DETAILS + TRADE SUGGESTIONS

[Signal #1]
Source:
Direction:
Strength:
Type:
Assets:
Summary:
Why it matters:
Time sensitivity:
Link:
>> SUGGESTED ACTION: [BUY/SELL->USDT/HOLD/WATCH] [asset] - [1 sentence rationale]

[Repeat block for each signal]

------------------------------------------------------------
3. QUICK TAKE
[2-3 sentences: overall market interpretation]

------------------------------------------------------------
4. PORTFOLIO ACTION PLAN
[Numbered priority list of concrete actions, e.g.:]
1. Sell ETH -> USDT (bearish signal, you hold $X at risk)
2. Buy BTC with 25% of USDT (~$X) - strong bullish catalyst
3. Hold SOL - bullish but already well positioned
[If nothing to act on: "No portfolio changes suggested this run."]

============================================================
Sources scanned: CoinDesk, Cointelegraph, The Block, Decrypt, CryptoPanic, Reuters, Investing.com, X/@saylor @VitalikButerin @cz_binance @elonmusk @APompliano @RaoulGMI @woonomic @100trillionUSD @CryptoHayes @novogratz @DocumentingBTC @BitcoinMagazine
============================================================

SUBJECT: CRYPTO SIGNAL: <3-6 word summary of the top signal>

Output the SUBJECT line as the very last line."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
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

    print("\nAnalyzing with Claude Haiku...")
    result = analyze(items, portfolio)

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
