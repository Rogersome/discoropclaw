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

SYMBOL         = "BTC/USD"
SYMBOL_CB      = "BTC-USD"           # Coinbase format for price/klines
DB_PATH        = "/home/jefferykuo92/discoropclaw/alpaca_bot.db"
RISK_PER_TRADE = 0.02                # 2% of portfolio per trade
SENTIMENT_TTL  = 1800

BOT_TOKEN         = getpass("Paste your Discord Bot Token: ")
CHANNEL_ID        = 1487253623456137288
ANTHROPIC_API_KEY = getpass("Paste your Anthropic API key: ")
ALPACA_API_KEY    = getpass("Paste your Alpaca API Key: ")
ALPACA_SECRET     = getpass("Paste your Alpaca Secret Key: ")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

ALPACA_BASE = "https://paper-api.alpaca.markets"
ALPACA_HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

# ── Alpaca API ────────────────────────────────────────────────────────────────

def alpaca_get(endpoint):
    try:
        r = requests.get(f"{ALPACA_BASE}{endpoint}", headers=ALPACA_HEADERS, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Alpaca GET error: {e}")
        return {}

def alpaca_post(endpoint, data):
    try:
        r = requests.post(f"{ALPACA_BASE}{endpoint}", headers=ALPACA_HEADERS, json=data, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Alpaca POST error: {e}")
        return {}

def alpaca_delete(endpoint):
    try:
        r = requests.delete(f"{ALPACA_BASE}{endpoint}", headers=ALPACA_HEADERS, timeout=10)
        return r.status_code
    except Exception as e:
        print(f"Alpaca DELETE error: {e}")
        return None

def get_account():
    return alpaca_get("/v2/account")

def get_balance():
    account = get_account()
    return float(account.get("cash", 0))

def get_portfolio_value():
    account = get_account()
    return float(account.get("portfolio_value", 0))

def get_open_position():
    """Returns open BTC/USD position or None."""
    positions = alpaca_get("/v2/positions")
    if isinstance(positions, list):
        for p in positions:
            if p.get("symbol") in ("BTCUSD", "BTC/USD"):
                return p
    return None

def open_trade(direction, qty, stop_loss, take_profit):
    """Place a market order with stop loss and take profit."""
    side = "buy" if direction == "BUY" else "sell"
    order = alpaca_post("/v2/orders", {
        "symbol":        SYMBOL,
        "qty":           str(qty),
        "side":          side,
        "type":          "market",
        "time_in_force": "gtc",
        "order_class":   "bracket",
        "stop_loss":     {"stop_price": str(stop_loss)},
        "take_profit":   {"limit_price": str(take_profit)},
    })
    return order

def close_position():
    """Close entire BTC/USD position."""
    status = alpaca_delete("/v2/positions/BTCUSD")
    return status

def get_recent_orders(limit=5):
    orders = alpaca_get(f"/v2/orders?status=all&limit={limit}&direction=desc")
    return orders if isinstance(orders, list) else []

def get_performance():
    """Calculate performance from closed Alpaca orders."""
    orders = alpaca_get("/v2/orders?status=closed&limit=100&direction=desc")
    if not isinstance(orders, list) or not orders:
        return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0}
    filled = [o for o in orders if o.get("status") == "filled"]
    # Group buy/sell pairs to estimate PnL
    total = len(filled)
    return {
        "total_trades": total,
        "wins":         0,
        "losses":       0,
        "win_rate":     0,
        "total_pnl":    0,
    }

# ── Database (signals + sentiment only) ──────────────────────────────────────

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
            CREATE TABLE IF NOT EXISTS sentiment_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp      TEXT,
                score          REAL,
                trend          TEXT,
                financial_json TEXT,
                political_json TEXT
            );
        """)
    print("Initialised.")

def save_signal(price, rsi, bb_pct, atr, signal, confidence):
    with db() as conn:
        conn.execute(
            "INSERT INTO signals (timestamp, asset, price, rsi, bb_pct, atr, signal, confidence) VALUES (?,?,?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), SYMBOL, price, round(rsi, 2), round(bb_pct, 3), round(atr, 2), signal, confidence)
        )

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
    bb_pct = bb.bollinger_pband().iloc[-1]
    atr    = AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range().iloc[-1]

    if rsi < 30 and bb_pct < 0.2:
        signal, confidence = "BUY", 90
    elif rsi < 35 and bb_pct < 0.3:
        signal, confidence = "BUY", 80
    elif rsi > 70 and bb_pct > 0.8:
        signal, confidence = "SELL", 90
    elif rsi > 65 and bb_pct > 0.7:
        signal, confidence = "SELL", 80
    else:
        signal, confidence = "HOLD", 50

    return signal, confidence, rsi, bb_pct, atr

# ── Risk Engine ───────────────────────────────────────────────────────────────

def calculate_position(direction, entry_price, rsi, atr):
    if rsi > 80:
        sl_mult = 4.5
    elif rsi > 70:
        sl_mult = 3.0
    else:
        sl_mult = 2.5

    sl_distance = min(atr * sl_mult, atr * 7)
    tp_distance = sl_distance * 2

    if direction == "BUY":
        stop_loss   = round(entry_price - sl_distance, 2)
        take_profit = round(entry_price + tp_distance, 2)
    else:
        stop_loss   = round(entry_price + sl_distance, 2)
        take_profit = round(entry_price - tp_distance, 2)

    portfolio = get_portfolio_value()
    qty = round((portfolio * RISK_PER_TRADE) / sl_distance, 6)
    return qty, stop_loss, take_profit

def risk_approved(signal, confidence):
    if signal == "HOLD":             return False
    if confidence < 80:              return False
    if get_open_position():          return False
    if not sentiment_agrees(signal): return False
    account = get_account()
    if account.get("trading_blocked") or account.get("account_blocked"):
        print("Alpaca account blocked.")
        return False
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
    fin_score  = float(fin_result.get("score", 0))
    pol_score  = float(pol_result.get("impact_score", 0))
    combined   = round((fin_score * 0.6) + (pol_score * 0.4), 3)
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
            messages=[{"role": "user", "content": f"Crypto market analyst. Analyze these articles and return ONLY a JSON object with these fields: score (float -1.0 to 1.0), trend (Bullish/Bearish/Neutral), reasoning (one sentence), top_coin (symbol), confidence (0-100), key_events (list of strings).\n\n{content}"}]
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
            messages=[{"role": "user", "content": f"Macro analyst. Analyze how these political events affect financial markets and return ONLY a JSON object with these fields: market_impact (Positive/Negative/Neutral), impact_score (float -1.0 to 1.0), affected_assets (list of strings), reasoning (one sentence), urgency (High/Medium/Low), confidence (0-100).\n\n{_format_articles(political)}"}]
        )
        return _parse_claude_json(msg.content[0].text)
    except Exception as e:
        print(f"Claude political error: {e}")
        return {}

def build_news_report(articles_by_type, score, trend, result, balance, portfolio):
    fin       = result.get("financial", {})
    pol       = result.get("political", {})
    yesterday = get_yesterday_sentiment()

    trend_icon = "BULLISH" if trend == "Bullish" else ("BEARISH" if trend == "Bearish" else "NEUTRAL")
    vs_yesterday = ""
    if yesterday:
        diff = round(score - yesterday[0], 3)
        direction = "UP" if diff > 0 else "DOWN"
        vs_yesterday = f"  |  vs Yesterday: {yesterday[1]} ({direction} {abs(diff):.3f})"

    filled = int((score + 1) / 2 * 10)
    bar    = "[" + "#" * filled + "-" * (10 - filled) + "]"
    events = fin.get("key_events", [])
    events_str = "\n".join(f"  >> {e}" for e in events) if events else "  >> N/A"

    def headlines(key, n=3):
        return "\n".join(f"  - {a['title']}" for a in articles_by_type.get(key, [])[:n]) or "  - N/A"

    return f"""============================================
  MARKET INTELLIGENCE REPORT  [ALPACA]
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
[ ALPACA ACCOUNT ]
  Cash       : ${balance:,.2f}
  Portfolio  : ${portfolio:,.2f}
  PnL        : ${portfolio - balance:+,.2f}

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

    position = get_open_position()
    if position:
        unrealized = float(position.get("unrealized_pl", 0))
        current_sl = float(position.get("avg_entry_price", price))
        # Alpaca bracket orders handle SL/TP automatically
        # Just notify if unrealized PnL is significant
        if abs(unrealized) > 10:
            side = position.get("side", "long")
            send_to_channel(f"""Position Update

Asset      : {SYMBOL}
Side       : {side.upper()}
Entry      : ${float(position['avg_entry_price']):,.2f}
Current    : ${price:,.2f}
Unrealized : ${unrealized:+,.2f}
Qty        : {position['qty']} BTC
Time       : {time.strftime('%Y-%m-%d %H:%M:%S')} UTC""")
        return  # Don't open new trade while position is open

    if risk_approved(signal, confidence):
        qty, sl, tp = calculate_position(signal, price, rsi, atr)
        order = open_trade(signal, qty, sl, tp)
        if "id" in order:
            sl_atr = round(abs(price - sl) / atr, 1)
            send_to_channel(f"""Trade Opened [ALPACA]

Asset    : {SYMBOL}
Direction: {signal}
Entry    : ${price:,.2f}
Qty      : {qty} BTC
SL       : ${sl:,.2f} ({sl_atr}x ATR)
TP       : ${tp:,.2f}
Portfolio: ${get_portfolio_value():,.2f}
Order ID : {order['id'][:8]}...
Time     : {time.strftime('%Y-%m-%d %H:%M:%S')} UTC""")
        else:
            print(f"Order failed: {order}")

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
    await bot.get_channel(CHANNEL_ID).send("Alpaca Bot online. Type !help_bot for commands.")

@bot.command()
async def signal(ctx):
    price  = get_price()
    klines = get_klines()
    if not price or not klines:
        await ctx.send("Data fetch failed.")
        return
    sig, confidence, rsi, bb_pct, atr = generate_signal(klines)
    sentiment  = get_cached_sentiment()
    position   = get_open_position()
    portfolio  = get_portfolio_value()
    await ctx.send(f"""AI Trading Signal [ALPACA]

Asset     : {SYMBOL}
Price     : ${price:,.2f}
RSI       : {round(rsi, 2)}
BB%       : {round(bb_pct, 3)}
ATR       : ${round(atr, 2)}
Portfolio : ${portfolio:,.2f}
Position  : {"OPEN" if position else "NONE"}

Signal    : {sig}
Confidence: {confidence}%
Sentiment : {sentiment['trend']}
Trade OK  : {"Yes" if risk_approved(sig, confidence) else "No"}

Time: {time.strftime('%Y-%m-%d %H:%M:%S')} UTC""")

@bot.command()
async def balance(ctx):
    account   = get_account()
    cash      = float(account.get("cash", 0))
    portfolio = float(account.get("portfolio_value", 0))
    buying    = float(account.get("buying_power", 0))
    await ctx.send(f"""Alpaca Account

Cash         : ${cash:,.2f}
Portfolio    : ${portfolio:,.2f}
Buying Power : ${buying:,.2f}
PnL          : ${portfolio - cash:+,.2f}""")

@bot.command()
async def position(ctx):
    pos = get_open_position()
    if not pos:
        await ctx.send("No open position.")
        return
    await ctx.send(f"""Open Position

Asset      : {pos['symbol']}
Side       : {pos['side'].upper()}
Qty        : {pos['qty']} BTC
Entry      : ${float(pos['avg_entry_price']):,.2f}
Current    : ${float(pos['current_price']):,.2f}
Unrealized : ${float(pos['unrealized_pl']):+,.2f}
Return     : {float(pos['unrealized_plpc'])*100:+.2f}%""")

@bot.command()
async def trades(ctx):
    orders = get_recent_orders(5)
    if not orders:
        await ctx.send("No recent orders.")
        return
    msg = "Recent Orders [ALPACA]\n"
    for o in orders:
        filled_at = o.get("filled_at", "pending")[:19] if o.get("filled_at") else "pending"
        filled_qty = o.get("filled_qty", "0")
        filled_price = o.get("filled_avg_price", "N/A")
        price_str = f"${float(filled_price):,.2f}" if filled_price and filled_price != "N/A" else "N/A"
        msg += f"- {o['side'].upper():5} {filled_qty} BTC @ {price_str}  [{o['status']}]  {filled_at}\n"
    await ctx.send(msg)

@bot.command()
async def news(ctx):
    await ctx.send("Fetching news & running Claude analysis...")
    loop = asyncio.get_event_loop()
    articles = await loop.run_in_executor(None, fetch_news)
    if sum(len(v) for v in articles.values()) == 0:
        await ctx.send("No articles fetched.")
        return
    score, trend, result = await loop.run_in_executor(None, analyze_sentiment, articles)
    save_sentiment(score, trend, result)
    _sentiment_cache.update({"score": score, "trend": trend, "result": result, "timestamp": time.time()})
    report = build_news_report(articles, score, trend, result, get_balance(), get_portfolio_value())
    for chunk in [report[i:i+1900] for i in range(0, len(report), 1900)]:
        await ctx.send(f"```\n{chunk}\n```")

@bot.command()
async def help_bot(ctx):
    await ctx.send("""Commands [ALPACA MODE]

!signal   - price, RSI, BB%, ATR, signal & sentiment
!balance  - Alpaca account cash, portfolio, buying power
!position - current open BTC/USD position
!trades   - last 5 Alpaca orders
!news     - news analysis with Claude""")

# ── Start ─────────────────────────────────────────────────────────────────────

init()
asyncio.run(bot.start(BOT_TOKEN))
