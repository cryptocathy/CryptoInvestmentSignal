#!/usr/bin/env python3
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


# ── Analyzer ───────────────────────────────────────────────────────────────────

def analyze(items):
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

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

    prompt = f"""You are a Crypto Market Monitoring Analyst. Analyze the news items and X posts below.

FETCHED ITEMS:
{items_text}

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

STEP 3 — PRIORITIZE: Keep only TOP 3–5 signals ranked by (1) market impact, (2) urgency, (3) credibility.
Only signals with Strength of Medium, High, or Extreme qualify. Discard Low-strength signals entirely.

STEP 4 — DECIDE whether to send an alert:
- If NO signals qualify (all filtered out, or only Low strength): output exactly one line: NO_SIGNALS
- If signals exist: output the full email body below in plain text, no markdown:

============================================================
CRYPTO MARKET SIGNAL DIGEST
{now_utc}
============================================================

1. TOP SIGNALS
[Bullet list of 3-5 signals, 1 line each starting with •]

------------------------------------------------------------
2. SIGNAL DETAILS

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

[Repeat block for each signal]

------------------------------------------------------------
3. QUICK TAKE
[2-3 sentences: overall market interpretation]

------------------------------------------------------------
4. ACTIONABLE INSIGHT
[1-2 sentences: specific recommendation]

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

    print("\nAnalyzing with Claude Haiku...")
    result = analyze(items)

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
