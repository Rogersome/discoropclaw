import requests
import pandas as pd
from ta.momentum import RSIIndicator
import time
import sqlite3
import feedparser
import threading
import discord
from discord.ext import commands
import nest_asyncio
from getpass import getpass
import anthropic
import json

nest_asyncio.apply()

# ── Config ────────────────────────────────────────────────────────────────────

SYMBOL          = "BTC-USD"
DB_PATH         = "/content/trading_bot.db"
CAPITAL         = 1000
RISK_PER_TRADE  = 0.02
STOP_LOSS_PCT   = 0.02
TAKE_PROFIT_PCT = 0.04
MIN_BALANCE     = CAPITAL * 0.5

BOT_TOKEN         = getpass("Paste your Discord Bot Token: ")
CHANNEL_ID        = int(input("Paste your Discord Channel ID: "))
ANTHROPIC_API_KEY = getpass("Paste your Anthropic API key: ")
claude            = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── News ──────────────────────────────────────────────────────────────────────

CRYPTO_FEEDS = {
    "CoinDesk":      "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "CoinTelegraph": "https://cointelegraph.com/rss",
}

FINANCE_FEEDS = {
    "MarketWatch":   "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "Reuters":       "https://feeds.reuters.com/reuters/businessNews",
}

POLITICAL_FEEDS = {
    "Trump/Truth Social": "https://truthsocial.com/@realDonaldTrump.rss",
    "White House":        "https://www.whitehouse.gov/feed/",
    "Federal Reserve":    "https://www.federalreserve.gov/feeds/press_all.xml",
    "SEC":                "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&dateb=&owner=include&count=10&search_text=&output=atom",
}

def fetch_feed(feeds, max_per_feed=5):
    articles = []
    for source, url in feeds.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]:
                title   = entry.get("title", "").strip()
                summary = entry.get("summary", "") or entry.get("description", "")
                summary = summary[:300].strip() if summary else ""
                if title:
                    articles.append({
                        "source":  source,
                        "title":   title,
                        "summary": summary
                    })
        except Exception as e:
            print(f"Feed error ({source}): {e}")
    return articles

def fetch_news():
    crypto    = fetch_feed(CRYPTO_FEEDS)
    finance   = fetch_feed(FINANCE_FEEDS)
    political = fetch_feed(POLITICAL_FEEDS)
    return {"crypto": crypto, "finance": finance, "political": political}

def _format_articles(articles):
    return "\n".join(
        f"- [{a['source']}] {a['title']}" + (f"\n  {a['summary']}" if a['summary'] else "")
        for a in articles
    )

def analyze_financial_news(crypto_articles, finance_articles):
    if not crypto_articles and not finance_articles:
        return {}
    content = (
        "=== Crypto News ===\n" + _format_articles(crypto_articles) +
        "\n\n=== Finance News ===\n" + _format_articles(finance_articles)
    )
    try:
        message = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": f"""You are a crypto market analyst. Analyze these financial and crypto news articles and return ONLY a JSON object, no other text.

{content}

Return this exact JSON structure:
{{
  "score": <float from -1.0 to 1.0>,
  "trend": "<Bullish | Bearish | Neutral>",
  "reasoning": "<one sentence explaining the key market driver>",
  "top_coin": "<most relevant coin symbol or BTC>",
  "confidence": <integer 0-100>,
  "key_events": ["<event1>", "<event2>"]
}}"""}]
        )
        return json.loads(message.content[0].text)
    except Exception as e:
        print(f"Claude financial analysis error: {e}")
        return {}

def analyze_political_news(political_articles):
    if not political_articles:
        return {}
    content = _format_articles(political_articles)
    try:
        message = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": f"""You are a macro analyst specializing in how political events affect financial markets. Analyze these political posts and statements and return ONLY a JSON object, no other text.

{content}

Return this exact JSON structure:
{{
  "market_impact": "<Positive | Negative | Neutral>",
  "impact_score": <float from -1.0 to 1.0>,
  "affected_assets": ["<asset1>", "<asset2>"],
  "reasoning": "<one sentence on market impact>",
  "urgency": "<High | Medium | Low>",
  "confidence": <integer 0-100>
}}"""}]
        )
        return json.loads(message.content[0].text)
    except Exception as e:
        print(f"Claude political analysis error: {e}")
        return {}

def analyze_sentiment(articles_by_type):
    fin_result  = analyze_financial_news(
        articles_by_type.get("crypto", []),
        articles_by_type.get("finance", [])
    )
    pol_result  = analyze_political_news(articles_by_type.get("political", []))

    fin_score   = float(fin_result.get("score", 0))
    pol_score   = float(pol_result.get("impact_score", 0))
    combined    = round((fin_score * 0.6) + (pol_score * 0.4), 3)

    if combined >= 0.05:   trend = "Bullish"
    elif combined <= -0.05: trend = "Bearish"
    else:                   trend = "Neutral"

    return combined, trend, {"financial": fin_result, "political": pol_result}

def save_sentiment(score, trend, result):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO sentiment_history (timestamp, score, trend, financial_json, political_json) VALUES (?,?,?,?,?)",
        (
            time.strftime("%Y-%m-%d %H:%M:%S"),
            score,
            trend,
            json.dumps(result.get("financial", {})),
            json.dumps(result.get("political", {}))
        )
    )
    conn.commit()
    conn.close()

def get_yesterday_sentiment():
    conn = sqlite3.connect(DB_PATH)
    yesterday = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
    row = conn.execute(
        "SELECT score, trend FROM sentiment_history WHERE timestamp LIKE ? ORDER BY id DESC LIMIT 1",
        (f"{yesterday}%",)
    ).fetchone()
    conn.close()
    return row

def build_news_report(articles_by_type, score, trend, result, performance, balance, equity):
    fin  = result.get("financial", {})
    pol  = result.get("political", {})
    yesterday = get_yesterday_sentiment()
    trend_change = ""
    if yesterday:
        prev_score, prev_trend = yesterday
        diff = round(score - prev_score, 3)
        trend_change = f"\nVs Yesterday: {prev_trend} ({diff:+.3f})"

    crypto_headlines  = "\n".join(f"  - [{a['source']}] {a['title']}" for a in articles_by_type.get("crypto", [])[:5])
    finance_headlines = "\n".join(f"  - [{a['source']}] {a['title']}" for a in articles_by_type.get("finance", [])[:5])
    political_posts   = "\n".join(f"  - [{a['source']}] {a['title']}" for a in articles_by_type.get("political", [])[:5])

    return f"""Daily Market Intelligence Report
{time.strftime('%Y-%m-%d %H:%M')} UTC

=== Market Sentiment ===
Overall Score : {score} -> {trend}{trend_change}

--- Financial Analysis ---
Trend     : {fin.get('trend', 'N/A')}
Confidence: {fin.get('confidence', 'N/A')}%
Reasoning : {fin.get('reasoning', 'N/A')}
Top Coin  : {fin.get('top_coin', 'BTC')}
Key Events: {', '.join(fin.get('key_events', []))}

--- Political Analysis ---
Impact    : {pol.get('market_impact', 'N/A')} ({pol.get('urgency', 'N/A')} urgency)
Score     : {pol.get('impact_score', 'N/A')}
Assets    : {', '.join(pol.get('affected_assets', []))}
Reasoning : {pol.get('reasoning', 'N/A')}
Confidence: {pol.get('confidence', 'N/A')}%

=== Account ===
Balance : ${balance:,.2f}
Equity  : ${equity:,.2f}

=== Performance ===
Total Trades : {performance['total_trades']}
Wins / Losses: {performance['wins']} / {performance['losses']}
Win Rate     : {performance['win_rate']}%
Total PnL    : ${performance['total_pnl']:+.2f}

=== Top Headlines ===
Crypto:
{crypto_headlines}

Finance:
{finance_headlines}

Political:
{political_posts}"""

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp  TEXT,
            asset      TEXT,
            price      REAL,
            rsi        REAL,
            signal     TEXT,
            confidence INTEGER
        );
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT,
            asset       TEXT,
            direction   TEXT,
            entry_price REAL,
            qty         REAL,
            stop_loss   REAL,
            take_profit REAL,
            status      TEXT DEFAULT 'OPEN',
            exit_price  REAL,
            pnl         REAL
        );
        CREATE TABLE IF NOT EXISTS balance (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            equity    REAL,
            pnl       REAL
        );
        CREATE TABLE IF NOT EXISTS sentiment_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp      TEXT,
            score          REAL,
            trend          TEXT,
            financial_json TEXT,
            political_json TEXT
        );
    """)
    conn.commit()
    conn.close()
    print("Database initialised.")

def init_balance():
    conn = sqlite3.connect(DB_PATH)
    exists = conn.execute("SELECT COUNT(*) FROM balance").fetchone()[0]
    if exists == 0:
        conn.execute("INSERT INTO balance (equity) VALUES (?)", (CAPITAL,))
        conn.commit()
    conn.close()

def save_signal(price, rsi, signal, confidence):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO signals (timestamp, asset, price, rsi, signal, confidence) VALUES (?,?,?,?,?,?)",
        (time.strftime("%Y-%m-%d %H:%M:%S"), SYMBOL, price, round(rsi, 2), signal, confidence)
    )
    conn.commit()
    conn.close()

def open_trade(direction, entry_price, qty, stop_loss, take_profit):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO trades (timestamp, asset, direction, entry_price, qty, stop_loss, take_profit)
           VALUES (?,?,?,?,?,?,?)""",
        (time.strftime("%Y-%m-%d %H:%M:%S"), SYMBOL, direction, entry_price, qty, stop_loss, take_profit)
    )
    conn.commit()
    conn.close()

def get_open_trade():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT * FROM trades WHERE status='OPEN' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row

def close_trade(trade_id, exit_price):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT direction, entry_price, qty FROM trades WHERE id=?", (trade_id,)
    ).fetchone()
    if not row:
        conn.close()
        return None
    direction, entry_price, qty = row
    pnl = (exit_price - entry_price) * qty if direction == "BUY" else (entry_price - exit_price) * qty
    conn.execute(
        "UPDATE trades SET status='CLOSED', exit_price=?, pnl=? WHERE id=?",
        (exit_price, round(pnl, 2), trade_id)
    )
    conn.commit()
    conn.close()
    return round(pnl, 2)

def get_balance():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT equity FROM balance ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return row[0] if row else CAPITAL

def update_balance(pnl):
    conn = sqlite3.connect(DB_PATH)
    current = conn.execute("SELECT equity FROM balance ORDER BY id DESC LIMIT 1").fetchone()
    new_balance = (current[0] if current else CAPITAL) + pnl
    conn.execute(
        "INSERT INTO balance (timestamp, equity, pnl) VALUES (?,?,?)",
        (time.strftime("%Y-%m-%d %H:%M:%S"), round(new_balance, 2), round(pnl, 2))
    )
    conn.commit()
    conn.close()
    return round(new_balance, 2)

def get_equity(current_price):
    balance = get_balance()
    trade = get_open_trade()
    if not trade:
        return balance
    _, _, _, direction, entry_price, qty, *_ = trade
    unrealized = (current_price - entry_price) * qty if direction == "BUY" else (entry_price - current_price) * qty
    return round(balance + unrealized, 2)

def get_performance():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT pnl FROM trades WHERE status='CLOSED'").fetchall()
    conn.close()
    if not rows:
        return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0}
    pnls = [r[0] for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    return {
        "total_trades": len(pnls),
        "wins": wins,
        "losses": len(pnls) - wins,
        "win_rate": round(wins / len(pnls) * 100, 1),
        "total_pnl": round(sum(pnls), 2)
    }

def get_recent_trades(limit=5):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT timestamp, direction, entry_price, exit_price, pnl, status FROM trades ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return rows

# ── Market data ───────────────────────────────────────────────────────────────

def safe_request(url):
    try:
        return requests.get(url, timeout=5).json()
    except Exception as e:
        print(f"Request failed: {e}")
        return None

def get_price():
    data = safe_request("https://api.coinbase.com/v2/prices/BTC-USD/spot")
    if isinstance(data, dict) and "data" in data:
        return float(data["data"]["amount"])
    return None

def get_klines():
    data = safe_request("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60&limit=100")
    if not isinstance(data, list) or len(data) == 0:
        return None
    return [float(k[4]) for k in data]

def get_rsi(closes):
    df = pd.DataFrame(closes, columns=["close"])
    return RSIIndicator(df["close"], window=14).rsi().iloc[-1]

def generate_signal(rsi):
    if rsi < 30:
        return "BUY", 80
    elif rsi > 70:
        return "SELL", 80
    return "HOLD", 50

# ── Risk Engine ───────────────────────────────────────────────────────────────

def calculate_position(entry_price):
    risk_amount        = CAPITAL * RISK_PER_TRADE
    stop_loss_distance = entry_price * STOP_LOSS_PCT
    qty         = round(risk_amount / stop_loss_distance, 6)
    stop_loss   = round(entry_price * (1 - STOP_LOSS_PCT), 2)
    take_profit = round(entry_price * (1 + TAKE_PROFIT_PCT), 2)
    return qty, stop_loss, take_profit

def check_exit(trade, current_price):
    _, _, _, direction, _, _, sl, tp, *_ = trade
    if direction == "BUY":
        if current_price <= sl: return "SL"
        if current_price >= tp: return "TP"
    elif direction == "SELL":
        if current_price >= sl: return "SL"
        if current_price <= tp: return "TP"
    return None

def risk_approved(signal, confidence):
    if signal == "HOLD":             return False
    if confidence < 80:              return False
    if get_open_trade():             return False
    if get_balance() < MIN_BALANCE:
        print(f"Balance too low: ${get_balance():.2f}. Trading paused.")
        return False
    return True

# ── Background cycle ──────────────────────────────────────────────────────────

def send_to_channel(message):
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        import asyncio
        asyncio.run_coroutine_threadsafe(channel.send(message), bot.loop)

def background_cycle():
    price = get_price()
    closes = get_klines()
    if not price or not closes:
        return
    rsi = get_rsi(closes)
    signal, confidence = generate_signal(rsi)
    save_signal(price, rsi, signal, confidence)

    current_trade = get_open_trade()
    if current_trade:
        exit_reason = check_exit(current_trade, price)
        if exit_reason:
            pnl = close_trade(current_trade[0], price)
            new_balance = update_balance(pnl)
            emoji = "✅" if pnl > 0 else "❌"
            send_to_channel(f"""{emoji} Trade Closed [{exit_reason}]

Asset: {SYMBOL}
Exit Price: ${price:,.2f}
PnL: ${pnl:+.2f}
Balance: ${new_balance:,.2f}

Time: {time.strftime('%Y-%m-%d %H:%M:%S')} UTC""")

    if risk_approved(signal, confidence):
        qty, sl, tp = calculate_position(price)
        open_trade(signal, price, qty, sl, tp)
        signal_emoji = {"BUY": "🟢 BUY", "SELL": "🔴 SELL", "HOLD": "🟡 HOLD"}[signal]
        send_to_channel(f"""📥 New Paper Trade Opened

Asset: {SYMBOL}
Direction: {signal_emoji}
Entry: ${price:,.2f}
Qty: {qty} BTC
Stop-Loss: ${sl:,.2f}
Take-Profit: ${tp:,.2f}
Balance: ${get_balance():,.2f}

Time: {time.strftime('%Y-%m-%d %H:%M:%S')} UTC""")

def background_loop():
    while True:
        try:
            background_cycle()
        except Exception as e:
            print(f"Background error: {e}")
        time.sleep(60)

# ── Discord bot ───────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Bot online as {bot.user}")
    thread = threading.Thread(target=background_loop, daemon=True)
    thread.start()
    await bot.get_channel(CHANNEL_ID).send("🤖 Bot is online. Type `!help_bot` to see commands.")

@bot.command()
async def signal(ctx):
    price = get_price()
    closes = get_klines()
    if not price or not closes:
        await ctx.send("Data fetch failed.")
        return
    rsi = get_rsi(closes)
    sig, confidence = generate_signal(rsi)
    signal_emoji = {"BUY": "🟢 BUY", "SELL": "🔴 SELL", "HOLD": "🟡 HOLD"}[sig]
    equity = get_equity(price)
    await ctx.send(f"""🚀 AI Trading Signal

Asset: {SYMBOL}
Price: ${price:,.2f}
RSI: {round(rsi, 2)}
Equity: ${equity:,.2f}

Signal: {signal_emoji}
Confidence: {confidence}%

Time: {time.strftime('%Y-%m-%d %H:%M:%S')} UTC""")

@bot.command()
async def balance(ctx):
    price = get_price() or 0
    await ctx.send(f"""💰 Account

Balance : ${get_balance():,.2f}
Equity  : ${get_equity(price):,.2f}
Capital : ${CAPITAL:,.2f}""")

@bot.command()
async def perf(ctx):
    p = get_performance()
    await ctx.send(f"""📊 Performance Summary

Total Trades : {p['total_trades']}
Wins / Losses: {p['wins']} / {p['losses']}
Win Rate     : {p['win_rate']}%
Total PnL    : ${p['total_pnl']:+.2f}""")

@bot.command()
async def trades(ctx):
    rows = get_recent_trades(5)
    if not rows:
        await ctx.send("No trades yet.")
        return
    msg = "📋 Recent Trades\n"
    for r in rows:
        timestamp, direction, entry, exit_price, pnl, status = r
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "open"
        msg += f"• {direction} @ ${entry:,.2f} → {status} {pnl_str}\n"
    await ctx.send(msg)

@bot.command()
async def news(ctx):
    await ctx.send("Fetching news & running Claude analysis...")
    articles_by_type = fetch_news()
    total = sum(len(v) for v in articles_by_type.values())
    if total == 0:
        await ctx.send("No articles fetched.")
        return
    price = get_price() or 0
    score, trend, result = analyze_sentiment(articles_by_type)
    save_sentiment(score, trend, result)
    report = build_news_report(
        articles_by_type, score, trend, result,
        get_performance(), get_balance(), get_equity(price)
    )
    await ctx.send(report)

@bot.command()
async def help_bot(ctx):
    await ctx.send("""Available Commands

!signal  — current price, RSI & trading signal
!balance — account balance and equity
!perf    — performance summary (win rate, PnL)
!trades  — last 5 trades
!news    — crypto, finance & political news with Claude analysis""")

# ── Init & start ──────────────────────────────────────────────────────────────

init_db()
init_balance()

await bot.start(BOT_TOKEN)
