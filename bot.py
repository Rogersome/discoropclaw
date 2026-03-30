import requests
import pandas as pd
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange
import time
import sqlite3
import feedparser
import threading
import discord
from discord.ext import commands
from getpass import getpass
import anthropic
import json
import asyncio
from contextlib import contextmanager

# ── Config ────────────────────────────────────────────────────────────────────

SYMBOL         = "BTC-USD"
DB_PATH        = "/home/jefferykuo92/discoropclaw/trading_bot.db"
CAPITAL        = 1000
RISK_PER_TRADE = 0.02
MIN_BALANCE    = CAPITAL * 0.5
SENTIMENT_TTL  = 1800  # Refresh sentiment every 30 minutes

BOT_TOKEN         = getpass("Paste your Discord Bot Token: ")
CHANNEL_ID        = 1487253623456137288
ANTHROPIC_API_KEY = getpass("Paste your Anthropic API key: ")
claude            = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Database ──────────────────────────────────────────────────────────────────

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp  TEXT,
                asset      TEXT,
                price      REAL,
                rsi        REAL,
                bb_pct     REAL,
                atr        REAL,
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
                atr         REAL,
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
    with db() as conn:
        exists = conn.execute("SELECT COUNT(*) FROM balance").fetchone()[0]
        if exists == 0:
            conn.execute("INSERT INTO balance (equity) VALUES (?)", (CAPITAL,))
    print("Initialised.")

def save_signal(price, rsi, bb_pct, atr, signal, confidence):
    with db() as conn:
        conn.execute(
            "INSERT INTO signals (timestamp, asset, price, rsi, bb_pct, atr, signal, confidence) VALUES (?,?,?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), SYMBOL, price, round(rsi, 2), round(bb_pct, 3), round(atr, 2), signal, confidence)
        )

def open_trade(direction, entry_price, qty, stop_loss, take_profit, atr):
    with db() as conn:
        conn.execute(
            "INSERT INTO trades (timestamp, asset, direction, entry_price, qty, stop_loss, take_profit, atr) VALUES (?,?,?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), SYMBOL, direction, entry_price, qty, stop_loss, take_profit, round(atr, 2))
        )

def get_open_trade():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM trades WHERE status='OPEN' ORDER BY id DESC LIMIT 1"
        ).fetchone()

def close_trade(trade_id, exit_price):
    with db() as conn:
        row = conn.execute(
            "SELECT direction, entry_price, qty FROM trades WHERE id=?", (trade_id,)
        ).fetchone()
        if not row:
            return None
        direction, entry_price, qty = row
        pnl = (exit_price - entry_price) * qty if direction == "BUY" else (entry_price - exit_price) * qty
        conn.execute(
            "UPDATE trades SET status='CLOSED', exit_price=?, pnl=? WHERE id=?",
            (exit_price, round(pnl, 2), trade_id)
        )
        return round(pnl, 2)

def get_balance():
    with db() as conn:
        row = conn.execute("SELECT equity FROM balance ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else CAPITAL

def update_balance(pnl):
    with db() as conn:
        current = conn.execute("SELECT equity FROM balance ORDER BY id DESC LIMIT 1").fetchone()
        new_balance = (current[0] if current else CAPITAL) + pnl
        conn.execute(
            "INSERT INTO balance (timestamp, equity, pnl) VALUES (?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), round(new_balance, 2), round(pnl, 2))
        )
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
    with db() as conn:
        rows = conn.execute("SELECT pnl FROM trades WHERE status='CLOSED'").fetchall()
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
    with db() as conn:
        return conn.execute(
            "SELECT timestamp, direction, entry_price, exit_price, pnl, status FROM trades ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()

def save_sentiment(score, trend, result):
    with db() as conn:
        conn.execute(
            "INSERT INTO sentiment_history (timestamp, score, trend, financial_json, political_json) VALUES (?,?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), score, trend,
             json.dumps(result.get("financial", {})),
             json.dumps(result.get("political", {})))
        )

def get_yesterday_sentiment():
    with db() as conn:
        yesterday = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
        return conn.execute(
            "SELECT score, trend FROM sentiment_history WHERE timestamp LIKE ? ORDER BY id DESC LIMIT 1",
            (f"{yesterday}%",)
        ).fetchone()

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
    # Coinbase: [timestamp, low, high, open, close, volume]
    return {
        "high":   [float(k[2]) for k in data],
        "low":    [float(k[1]) for k in data],
        "close":  [float(k[4]) for k in data],
        "volume": [float(k[5]) for k in data],
    }

def generate_signal(klines):
    df = pd.DataFrame(klines)

    rsi    = RSIIndicator(df["close"], window=14).rsi().iloc[-1]
    bb     = BollingerBands(df["close"], window=20, window_dev=2)
    bb_pct = bb.bollinger_pband().iloc[-1]   # 0 = lower band, 1 = upper band
    atr    = AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range().iloc[-1]

    # Both RSI and BB% must agree to generate a signal
    if rsi < 40 and bb_pct < 0.3:
        signal, confidence = "BUY", 90
    elif rsi < 45 and bb_pct < 0.4:
        signal, confidence = "BUY", 80
    elif rsi > 60 and bb_pct > 0.7:
        signal, confidence = "SELL", 90
    elif rsi > 55 and bb_pct > 0.6:
        signal, confidence = "SELL", 80
    else:
        signal, confidence = "HOLD", 50

    return signal, confidence, rsi, bb_pct, atr

# ── Risk Engine ───────────────────────────────────────────────────────────────

def calculate_position(direction, entry_price, rsi, atr):
    # ATR-based stop loss — adapts to volatility
    if rsi > 80:
        sl_mult = 4.5   # wider SL to avoid spike-outs
    elif rsi > 70:
        sl_mult = 3.0
    else:
        sl_mult = 2.5

    sl_distance = min(atr * sl_mult, atr * 7)  # cap at 7x ATR
    tp_distance = sl_distance * 2               # 2:1 reward/risk

    if direction == "BUY":
        stop_loss   = round(entry_price - sl_distance, 2)
        take_profit = round(entry_price + tp_distance, 2)
    else:
        stop_loss   = round(entry_price + sl_distance, 2)
        take_profit = round(entry_price - tp_distance, 2)

    qty = round((CAPITAL * RISK_PER_TRADE) / sl_distance, 6)
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
    if signal == "HOLD":                return False
    if confidence < 70:                 return False
    if get_open_trade():                return False
    if get_balance() < MIN_BALANCE:
        print(f"Balance too low: ${get_balance():.2f}. Paused.")
        return False
    if not sentiment_agrees(signal):    return False
    return True

# ── Sentiment cache ───────────────────────────────────────────────────────────

_sentiment_cache = {"score": 0, "trend": "Neutral", "result": {}, "timestamp": 0}

def get_cached_sentiment():
    global _sentiment_cache
    if time.time() - _sentiment_cache["timestamp"] > SENTIMENT_TTL:
        print("Refreshing sentiment...")
        articles = fetch_news()
        total = sum(len(v) for v in articles.values())
        if total > 0:
            score, trend, result = analyze_sentiment(articles)
            save_sentiment(score, trend, result)
            _sentiment_cache = {"score": score, "trend": trend, "result": result, "timestamp": time.time()}
    return _sentiment_cache

def sentiment_agrees(signal):
    trend = get_cached_sentiment()["trend"]
    if signal == "BUY"  and trend == "Bearish": return False
    if signal == "SELL" and trend == "Bullish": return False
    return True

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
                    articles.append({"source": source, "title": title, "summary": summary})
        except Exception as e:
            print(f"Feed error ({source}): {e}")
    return articles

def fetch_news():
    return {
        "crypto":    fetch_feed(CRYPTO_FEEDS),
        "finance":   fetch_feed(FINANCE_FEEDS),
        "political": fetch_feed(POLITICAL_FEEDS),
    }

def _format_articles(articles):
    return "\n".join(
        f"- [{a['source']}] {a['title']}" + (f"\n  {a['summary']}" if a['summary'] else "")
        for a in articles
    )

def analyze_sentiment(articles_by_type):
    fin_result = _claude_financial(articles_by_type.get("crypto", []), articles_by_type.get("finance", []))
    pol_result = _claude_political(articles_by_type.get("political", []))

    fin_score = float(fin_result.get("score", 0))
    pol_score = float(pol_result.get("impact_score", 0))
    combined  = round((fin_score * 0.6) + (pol_score * 0.4), 3)

    if combined >= 0.05:    trend = "Bullish"
    elif combined <= -0.05: trend = "Bearish"
    else:                   trend = "Neutral"

    return combined, trend, {"financial": fin_result, "political": pol_result}

def _parse_claude_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())

def _claude_financial(crypto, finance):
    if not crypto and not finance:
        return {}
    content = "=== Crypto ===\n" + _format_articles(crypto) + "\n\n=== Finance ===\n" + _format_articles(finance)
    try:
        msg = claude.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=400,
            messages=[{"role": "user", "content": f"""Crypto market analyst. Analyze these articles and return ONLY a JSON object with these fields: score (float -1.0 to 1.0), trend (Bullish/Bearish/Neutral), reasoning (one sentence), top_coin (symbol), confidence (0-100), key_events (list of strings).

{content}"""}]
        )
        return _parse_claude_json(msg.content[0].text)
    except Exception as e:
        print(f"Claude financial error: {e}")
        return {}

def _claude_political(political):
    if not political:
        return {}
    try:
        msg = claude.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=400,
            messages=[{"role": "user", "content": f"""Macro analyst. Analyze how these political events affect financial markets and return ONLY a JSON object with these fields: market_impact (Positive/Negative/Neutral), impact_score (float -1.0 to 1.0), affected_assets (list of strings), reasoning (one sentence), urgency (High/Medium/Low), confidence (0-100).

{_format_articles(political)}"""}]
        )
        return _parse_claude_json(msg.content[0].text)
    except Exception as e:
        print(f"Claude political error: {e}")
        return {}

def build_news_report(articles_by_type, score, trend, result, performance, balance, equity):
    fin       = result.get("financial", {})
    pol       = result.get("political", {})
    yesterday = get_yesterday_sentiment()

    # Trend indicator
    trend_icon = "BULLISH" if trend == "Bullish" else ("BEARISH" if trend == "Bearish" else "NEUTRAL")

    # Score change vs yesterday
    vs_yesterday = ""
    if yesterday:
        diff = round(score - yesterday[0], 3)
        direction = "UP" if diff > 0 else "DOWN"
        vs_yesterday = f"  |  vs Yesterday: {yesterday[1]} ({direction} {abs(diff):.3f})"

    # Score bar (visual)
    filled = int((score + 1) / 2 * 10)
    bar = "[" + "#" * filled + "-" * (10 - filled) + "]"

    # Key events
    events = fin.get("key_events", [])
    events_str = "\n".join(f"  >> {e}" for e in events) if events else "  >> N/A"

    # Headlines
    def headlines(key, n=3):
        return "\n".join(f"  - {a['title']}" for a in articles_by_type.get(key, [])[:n]) or "  - N/A"

    # Win rate bar
    wr = performance['win_rate']
    wr_filled = int(wr / 10)
    wr_bar = "[" + "#" * wr_filled + "-" * (10 - wr_filled) + "]"

    return f"""============================================
  MARKET INTELLIGENCE REPORT
  {time.strftime('%Y-%m-%d %H:%M')} UTC
============================================

[ OVERALL SENTIMENT ]  {trend_icon}
  Score  : {score:+.3f}  {bar}{vs_yesterday}
  Signal : {"BUY signal supported" if trend == "Bullish" else ("SELL signal supported" if trend == "Bearish" else "No directional bias")}

--------------------------------------------
[ FINANCIAL ANALYSIS ]
  Trend      : {fin.get('trend', 'N/A')}
  Confidence : {fin.get('confidence', 'N/A')}%
  Top Coin   : {fin.get('top_coin', 'BTC')}
  Score      : {fin.get('score', 'N/A')}
  Reasoning  : {fin.get('reasoning', 'N/A')}

  Key Events :
{events_str}

--------------------------------------------
[ POLITICAL ANALYSIS ]
  Impact     : {pol.get('market_impact', 'N/A')}
  Urgency    : {pol.get('urgency', 'N/A')}
  Score      : {pol.get('impact_score', 'N/A')}
  Assets     : {', '.join(pol.get('affected_assets', []))}
  Reasoning  : {pol.get('reasoning', 'N/A')}

--------------------------------------------
[ ACCOUNT ]
  Balance    : ${balance:,.2f}
  Equity     : ${equity:,.2f}
  PnL        : ${equity - balance:+,.2f} (unrealized)

[ PERFORMANCE ]
  Trades     : {performance['total_trades']}  (W {performance['wins']} / L {performance['losses']})
  Win Rate   : {wr}%  {wr_bar}
  Total PnL  : ${performance['total_pnl']:+.2f}

--------------------------------------------
[ TOP HEADLINES ]
  Crypto:
{headlines('crypto')}

  Finance:
{headlines('finance')}

  Political:
{headlines('political')}
============================================"""

# ── Background cycle ──────────────────────────────────────────────────────────

def send_to_channel(message):
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        asyncio.run_coroutine_threadsafe(channel.send(message), bot.loop)

def background_cycle():
    price  = get_price()
    klines = get_klines()
    if not price or not klines:
        return

    signal, confidence, rsi, bb_pct, atr = generate_signal(klines)
    save_signal(price, rsi, bb_pct, atr, signal, confidence)

    current_trade = get_open_trade()
    if current_trade:
        exit_reason = check_exit(current_trade, price)
        if exit_reason:
            pnl         = close_trade(current_trade[0], price)
            new_balance = update_balance(pnl)
            emoji       = "WIN" if pnl > 0 else "LOSS"
            send_to_channel(f"""Trade Closed [{exit_reason}] {emoji}

Asset  : {SYMBOL}
Exit   : ${price:,.2f}
PnL    : ${pnl:+.2f}
Balance: ${new_balance:,.2f}
Time   : {time.strftime('%Y-%m-%d %H:%M:%S')} UTC""")

    if risk_approved(signal, confidence):
        qty, sl, tp = calculate_position(signal, price, rsi, atr)
        open_trade(signal, price, qty, sl, tp, atr)
        sl_atr = round(abs(price - sl) / atr, 1)
        send_to_channel(f"""Trade Opened

Asset    : {SYMBOL}
Direction: {signal}
Entry    : ${price:,.2f}
Qty      : {qty} BTC
SL       : ${sl:,.2f} ({sl_atr}x ATR)
TP       : ${tp:,.2f}
Balance  : ${get_balance():,.2f}
Time     : {time.strftime('%Y-%m-%d %H:%M:%S')} UTC""")

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
    threading.Thread(target=background_loop, daemon=True).start()
    await bot.get_channel(CHANNEL_ID).send("Bot online. Type !help_bot for commands.")

@bot.command()
async def signal(ctx):
    price  = get_price()
    klines = get_klines()
    if not price or not klines:
        await ctx.send("Data fetch failed.")
        return
    sig, confidence, rsi, bb_pct, atr = generate_signal(klines)
    sentiment = get_cached_sentiment()
    await ctx.send(f"""AI Trading Signal

Asset     : {SYMBOL}
Price     : ${price:,.2f}
RSI       : {round(rsi, 2)}
BB%       : {round(bb_pct, 3)}
ATR       : ${round(atr, 2)}
Equity    : ${get_equity(price):,.2f}

Signal    : {sig}
Confidence: {confidence}%
Sentiment : {sentiment['trend']}
Trade OK  : {"Yes" if risk_approved(sig, confidence) else "No"}

Time: {time.strftime('%Y-%m-%d %H:%M:%S')} UTC""")

@bot.command()
async def balance(ctx):
    price = get_price() or 0
    await ctx.send(f"""Account

Balance : ${get_balance():,.2f}
Equity  : ${get_equity(price):,.2f}
Capital : ${CAPITAL:,.2f}""")

@bot.command()
async def perf(ctx):
    p = get_performance()
    await ctx.send(f"""Performance

Trades   : {p['total_trades']}
W/L      : {p['wins']} / {p['losses']}
Win Rate : {p['win_rate']}%
Total PnL: ${p['total_pnl']:+.2f}""")

@bot.command()
async def trades(ctx):
    rows = get_recent_trades(5)
    if not rows:
        await ctx.send("No trades yet.")
        return
    msg = "Recent Trades\n"
    for r in rows:
        _, direction, entry, exit_price, pnl, status = r
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "open"
        msg += f"- {direction} @ ${entry:,.2f} -> {status} {pnl_str}\n"
    await ctx.send(msg)

@bot.command()
async def news(ctx):
    await ctx.send("Fetching news & running Claude analysis...")
    loop = asyncio.get_event_loop()
    articles = await loop.run_in_executor(None, fetch_news)
    if sum(len(v) for v in articles.values()) == 0:
        await ctx.send("No articles fetched.")
        return
    price = get_price() or 0
    score, trend, result = await loop.run_in_executor(None, analyze_sentiment, articles)
    save_sentiment(score, trend, result)
    _sentiment_cache.update({"score": score, "trend": trend, "result": result, "timestamp": time.time()})
    report = build_news_report(articles, score, trend, result, get_performance(), get_balance(), get_equity(price))
    # Discord limit is 2000 chars — split if needed
    for chunk in [report[i:i+1900] for i in range(0, len(report), 1900)]:
        await ctx.send(f"```\n{chunk}\n```")

@bot.command()
async def help_bot(ctx):
    await ctx.send("""Commands

!signal  - price, RSI, BB%, ATR, signal & sentiment
!balance - account balance and equity
!perf    - win rate and total PnL
!trades  - last 5 trades
!news    - news analysis with Claude""")

# ── Start ─────────────────────────────────────────────────────────────────────

init()
asyncio.run(bot.start(BOT_TOKEN))
