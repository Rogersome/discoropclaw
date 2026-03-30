import requests
import pandas as pd
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange
import time
import feedparser
import threading
import anthropic
import json
import asyncio
from typing import Optional
from getpass import getpass

from sqlmodel import SQLModel, Field, Session, create_engine, select
from interactions import Client, Intents, slash_command, SlashContext, listen
from interactions.api.events import Startup

# ── Config ────────────────────────────────────────────────────────────────────

SYMBOL         = "BTC-USD"
DB_PATH        = "/home/jefferykuo92/discoropclaw/trading_bot_v2.db"
CAPITAL        = 1000
RISK_PER_TRADE = 0.02
MIN_BALANCE    = CAPITAL * 0.5
SENTIMENT_TTL  = 1800

BOT_TOKEN         = getpass("Paste your Discord Bot Token: ")
CHANNEL_ID        = 1487253623456137288
ANTHROPIC_API_KEY = getpass("Paste your Anthropic API key: ")
claude            = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── SQLModel — Database Models ────────────────────────────────────────────────

class Signal(SQLModel, table=True):
    id:         Optional[int]   = Field(default=None, primary_key=True)
    timestamp:  str             = ""
    asset:      str             = ""
    price:      float           = 0
    rsi:        float           = 0
    bb_pct:     float           = 0
    atr:        float           = 0
    signal:     str             = ""
    confidence: int             = 0

class Trade(SQLModel, table=True):
    id:          Optional[int]   = Field(default=None, primary_key=True)
    timestamp:   str             = ""
    asset:       str             = ""
    direction:   str             = ""
    entry_price: float           = 0
    qty:         float           = 0
    stop_loss:   float           = 0
    take_profit: float           = 0
    atr:         float           = 0
    status:      str             = "OPEN"
    exit_price:  Optional[float] = None
    pnl:         Optional[float] = None

class Balance(SQLModel, table=True):
    id:        Optional[int]   = Field(default=None, primary_key=True)
    timestamp: Optional[str]   = None
    equity:    float           = 0
    pnl:       Optional[float] = None

class SentimentHistory(SQLModel, table=True):
    id:             Optional[int] = Field(default=None, primary_key=True)
    timestamp:      str           = ""
    score:          float         = 0
    trend:          str           = ""
    financial_json: str           = ""
    political_json: str           = ""

engine = create_engine(f"sqlite:///{DB_PATH}")

# ── Database Functions ────────────────────────────────────────────────────────

def init():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        if not session.exec(select(Balance)).first():
            session.add(Balance(equity=CAPITAL))
            session.commit()
    print("Initialised.")

def save_signal(price, rsi, bb_pct, atr, signal, confidence):
    with Session(engine) as session:
        session.add(Signal(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"), asset=SYMBOL,
            price=round(price, 2), rsi=round(rsi, 2), bb_pct=round(bb_pct, 3),
            atr=round(atr, 2), signal=signal, confidence=confidence
        ))
        session.commit()

def open_trade(direction, entry_price, qty, stop_loss, take_profit, atr):
    with Session(engine) as session:
        session.add(Trade(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"), asset=SYMBOL,
            direction=direction, entry_price=entry_price, qty=qty,
            stop_loss=stop_loss, take_profit=take_profit, atr=round(atr, 2)
        ))
        session.commit()

def get_open_trade():
    with Session(engine) as session:
        return session.exec(
            select(Trade).where(Trade.status == "OPEN").order_by(Trade.id.desc())
        ).first()

def close_trade(trade_id, exit_price):
    with Session(engine) as session:
        trade = session.get(Trade, trade_id)
        if not trade:
            return None
        pnl = (exit_price - trade.entry_price) * trade.qty if trade.direction == "BUY" \
              else (trade.entry_price - exit_price) * trade.qty
        trade.status     = "CLOSED"
        trade.exit_price = exit_price
        trade.pnl        = round(pnl, 2)
        session.commit()
        return round(pnl, 2)

def get_balance():
    with Session(engine) as session:
        row = session.exec(select(Balance).order_by(Balance.id.desc())).first()
        return row.equity if row else CAPITAL

def update_balance(pnl):
    with Session(engine) as session:
        current     = session.exec(select(Balance).order_by(Balance.id.desc())).first()
        new_balance = (current.equity if current else CAPITAL) + pnl
        session.add(Balance(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            equity=round(new_balance, 2), pnl=round(pnl, 2)
        ))
        session.commit()
        return round(new_balance, 2)

def get_equity(current_price):
    balance = get_balance()
    trade   = get_open_trade()
    if not trade:
        return balance
    unrealized = (current_price - trade.entry_price) * trade.qty if trade.direction == "BUY" \
                 else (trade.entry_price - current_price) * trade.qty
    return round(balance + unrealized, 2)

def get_performance():
    with Session(engine) as session:
        trades = session.exec(select(Trade).where(Trade.status == "CLOSED")).all()
    if not trades:
        return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0}
    pnls = [t.pnl for t in trades if t.pnl is not None]
    wins = sum(1 for p in pnls if p > 0)
    return {
        "total_trades": len(pnls),
        "wins":         wins,
        "losses":       len(pnls) - wins,
        "win_rate":     round(wins / len(pnls) * 100, 1) if pnls else 0,
        "total_pnl":    round(sum(pnls), 2),
    }

def get_recent_trades(limit=5):
    with Session(engine) as session:
        return session.exec(
            select(Trade).order_by(Trade.id.desc()).limit(limit)
        ).all()

def save_sentiment(score, trend, result):
    with Session(engine) as session:
        session.add(SentimentHistory(
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"), score=score, trend=trend,
            financial_json=json.dumps(result.get("financial", {})),
            political_json=json.dumps(result.get("political", {}))
        ))
        session.commit()

def get_yesterday_sentiment():
    with Session(engine) as session:
        yesterday = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
        return session.exec(
            select(SentimentHistory)
            .where(SentimentHistory.timestamp.startswith(yesterday))
            .order_by(SentimentHistory.id.desc())
        ).first()

# ── Market Data ───────────────────────────────────────────────────────────────

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
        "high":  [float(k[2]) for k in data],
        "low":   [float(k[1]) for k in data],
        "close": [float(k[4]) for k in data],
    }

def generate_signal(klines):
    df     = pd.DataFrame(klines)
    rsi    = RSIIndicator(df["close"], window=14).rsi().iloc[-1]
    bb     = BollingerBands(df["close"], window=20, window_dev=2)
    bb_pct = bb.bollinger_pband().iloc[-1]
    atr    = AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range().iloc[-1]

    if   rsi < 40 and bb_pct < 0.3: signal, confidence = "BUY",  90
    elif rsi < 45 and bb_pct < 0.4: signal, confidence = "BUY",  80
    elif rsi > 60 and bb_pct > 0.7: signal, confidence = "SELL", 90
    elif rsi > 55 and bb_pct > 0.6: signal, confidence = "SELL", 80
    else:                            signal, confidence = "HOLD", 50

    return signal, confidence, rsi, bb_pct, atr

# ── Risk Engine ───────────────────────────────────────────────────────────────

def calculate_position(direction, entry_price, rsi, atr):
    sl_mult     = 4.5 if rsi > 80 else (3.0 if rsi > 70 else 2.5)
    sl_distance = min(atr * sl_mult, atr * 7)
    tp_distance = sl_distance * 2

    if direction == "BUY":
        stop_loss, take_profit = round(entry_price - sl_distance, 2), round(entry_price + tp_distance, 2)
    else:
        stop_loss, take_profit = round(entry_price + sl_distance, 2), round(entry_price - tp_distance, 2)

    qty = round((CAPITAL * RISK_PER_TRADE) / sl_distance, 6)
    return qty, stop_loss, take_profit

def check_exit(trade, current_price):
    if trade.direction == "BUY":
        if current_price <= trade.stop_loss:  return "SL"
        if current_price >= trade.take_profit: return "TP"
    elif trade.direction == "SELL":
        if current_price >= trade.stop_loss:  return "SL"
        if current_price <= trade.take_profit: return "TP"
    return None

def risk_approved(signal, confidence):
    if signal == "HOLD":              return False
    if confidence < 70:               return False
    if get_open_trade():              return False
    if get_balance() < MIN_BALANCE:
        print(f"Balance too low: ${get_balance():.2f}. Paused.")
        return False
    if not sentiment_agrees(signal):  return False
    return True

# ── Sentiment Cache ───────────────────────────────────────────────────────────

_sentiment_cache = {"score": 0, "trend": "Neutral", "result": {}, "timestamp": 0}

def get_cached_sentiment():
    global _sentiment_cache
    if time.time() - _sentiment_cache["timestamp"] > SENTIMENT_TTL:
        print("Refreshing sentiment...")
        articles = fetch_news()
        if sum(len(v) for v in articles.values()) > 0:
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
                summary = (entry.get("summary", "") or entry.get("description", ""))[:300].strip()
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
    trend      = "Bullish" if combined >= 0.05 else ("Bearish" if combined <= -0.05 else "Neutral")
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

def build_news_report(articles_by_type, score, trend, result, performance, balance, equity):
    fin        = result.get("financial", {})
    pol        = result.get("political", {})
    yesterday  = get_yesterday_sentiment()
    trend_icon = "BULLISH" if trend == "Bullish" else ("BEARISH" if trend == "Bearish" else "NEUTRAL")

    vs_yesterday = ""
    if yesterday:
        diff         = round(score - yesterday.score, 3)
        vs_yesterday = f"  |  vs Yesterday: {yesterday.trend} ({'UP' if diff > 0 else 'DOWN'} {abs(diff):.3f})"

    filled    = int((score + 1) / 2 * 10)
    bar       = "[" + "#" * filled + "-" * (10 - filled) + "]"
    events    = fin.get("key_events", [])
    events_str = "\n".join(f"  >> {e}" for e in events) if events else "  >> N/A"
    wr        = performance["win_rate"]
    wr_bar    = "[" + "#" * int(wr / 10) + "-" * (10 - int(wr / 10)) + "]"

    def headlines(key, n=3):
        return "\n".join(f"  - {a['title']}" for a in articles_by_type.get(key, [])[:n]) or "  - N/A"

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
  PnL        : ${equity - balance:+,.2f}

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

# ── Background Cycle ──────────────────────────────────────────────────────────

bot = Client(token=BOT_TOKEN, intents=Intents.DEFAULT | Intents.MESSAGE_CONTENT)

def send_to_channel(message):
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        asyncio.run_coroutine_threadsafe(channel.send(content=message), bot._loop)

def background_cycle():
    price  = get_price()
    klines = get_klines()
    if not price or not klines:
        return

    signal, confidence, rsi, bb_pct, atr = generate_signal(klines)
    save_signal(price, rsi, bb_pct, atr, signal, confidence)

    trade = get_open_trade()
    if trade:
        exit_reason = check_exit(trade, price)
        if exit_reason:
            pnl         = close_trade(trade.id, price)
            new_balance = update_balance(pnl)
            send_to_channel(f"""Trade Closed [{exit_reason}] {"WIN" if pnl > 0 else "LOSS"}

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

# ── Slash Commands ────────────────────────────────────────────────────────────

@listen(Startup)
async def on_startup():
    print(f"Bot online as {bot.app.name}")
    threading.Thread(target=background_loop, daemon=True).start()
    await bot.get_channel(CHANNEL_ID).send("Bot online. Type /help for commands.")

@slash_command(name="signal", description="Current price, RSI, BB%, ATR and trading signal")
async def signal_cmd(ctx: SlashContext):
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

@slash_command(name="balance", description="Account balance and equity")
async def balance_cmd(ctx: SlashContext):
    price = get_price() or 0
    await ctx.send(f"""Account

Balance : ${get_balance():,.2f}
Equity  : ${get_equity(price):,.2f}
Capital : ${CAPITAL:,.2f}""")

@slash_command(name="perf", description="Win rate and total PnL")
async def perf_cmd(ctx: SlashContext):
    p = get_performance()
    await ctx.send(f"""Performance

Trades   : {p['total_trades']}
W/L      : {p['wins']} / {p['losses']}
Win Rate : {p['win_rate']}%
Total PnL: ${p['total_pnl']:+.2f}""")

@slash_command(name="trades", description="Last 5 trades")
async def trades_cmd(ctx: SlashContext):
    rows = get_recent_trades(5)
    if not rows:
        await ctx.send("No trades yet.")
        return
    msg = "Recent Trades\n"
    for t in rows:
        pnl_str = f"${t.pnl:+.2f}" if t.pnl is not None else "open"
        msg += f"- {t.direction} @ ${t.entry_price:,.2f} -> {t.status} {pnl_str}\n"
    await ctx.send(msg)

@slash_command(name="news", description="News analysis with Claude AI")
async def news_cmd(ctx: SlashContext):
    await ctx.send("Fetching news & running Claude analysis...")
    loop     = asyncio.get_event_loop()
    articles = await loop.run_in_executor(None, fetch_news)
    if sum(len(v) for v in articles.values()) == 0:
        await ctx.send("No articles fetched.")
        return
    price        = get_price() or 0
    score, trend, result = await loop.run_in_executor(None, analyze_sentiment, articles)
    save_sentiment(score, trend, result)
    _sentiment_cache.update({"score": score, "trend": trend, "result": result, "timestamp": time.time()})
    report = build_news_report(articles, score, trend, result, get_performance(), get_balance(), get_equity(price))
    for chunk in [report[i:i+1900] for i in range(0, len(report), 1900)]:
        await ctx.send(f"```\n{chunk}\n```")

@slash_command(name="help", description="Show all available commands")
async def help_cmd(ctx: SlashContext):
    await ctx.send("""Commands

/signal  - price, RSI, BB%, ATR, signal & sentiment
/balance - account balance and equity
/perf    - win rate and total PnL
/trades  - last 5 trades
/news    - news analysis with Claude""")

# ── Start ─────────────────────────────────────────────────────────────────────

init()
bot.start()
