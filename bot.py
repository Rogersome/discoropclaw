import requests
import pandas as pd
from ta.momentum import RSIIndicator
import time
import sqlite3
from getpass import getpass

SYMBOL = "BTC-USD"
DISCORD_WEBHOOK = getpass("Paste your Discord Webhook URL: ")
DB_PATH = "/content/trading_bot.db"

# ── Risk config ───────────────────────────────────────────────────────────────

CAPITAL         = 1000
RISK_PER_TRADE  = 0.02
STOP_LOSS_PCT   = 0.02
TAKE_PROFIT_PCT = 0.04
MAX_OPEN_TRADES = 1

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
    """)
    conn.commit()
    conn.close()
    print("Database initialised.")

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

# ── Risk Engine ───────────────────────────────────────────────────────────────

def calculate_position(entry_price):
    risk_amount = CAPITAL * RISK_PER_TRADE
    stop_loss_distance = entry_price * STOP_LOSS_PCT
    qty = round(risk_amount / stop_loss_distance, 6)
    stop_loss   = round(entry_price * (1 - STOP_LOSS_PCT), 2)
    take_profit = round(entry_price * (1 + TAKE_PROFIT_PCT), 2)
    return qty, stop_loss, take_profit

def check_exit(trade, current_price):
    trade_id, _, _, direction, entry_price, qty, sl, tp, *_ = trade
    if direction == "BUY":
        if current_price <= sl:
            return "SL"
        if current_price >= tp:
            return "TP"
    elif direction == "SELL":
        if current_price >= sl:
            return "SL"
        if current_price <= tp:
            return "TP"
    return None

def risk_approved(signal, confidence):
    if signal == "HOLD":
        return False
    if confidence < 80:
        return False
    if get_open_trade() is not None:
        return False
    return True

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
        print(f"Unexpected klines response: {data}")
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

# ── Discord ───────────────────────────────────────────────────────────────────

def send_to_discord(message):
    try:
        resp = requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=5)
        if resp.status_code not in (200, 204):
            print(f"Discord error: {resp.status_code}")
    except Exception as e:
        print(f"Discord send failed: {e}")

# ── Main cycle ────────────────────────────────────────────────────────────────

def run_cycle():
    price = get_price()
    closes = get_klines()

    if not price or not closes:
        print("Data fetch failed, skipping cycle")
        return

    rsi = get_rsi(closes)
    signal, confidence = generate_signal(rsi)
    save_signal(price, rsi, signal, confidence)

    signal_emoji = {"BUY": "🟢 BUY", "SELL": "🔴 SELL", "HOLD": "🟡 HOLD"}[signal]

    # Check exit on open trade
    current_trade = get_open_trade()
    if current_trade:
        exit_reason = check_exit(current_trade, price)
        if exit_reason:
            pnl = close_trade(current_trade[0], price)
            emoji = "✅" if pnl > 0 else "❌"
            send_to_discord(f"""{emoji} Trade Closed [{exit_reason}]

Asset: {SYMBOL}
Exit Price: ${price:,.2f}
PnL: ${pnl:+.2f}

Time: {time.strftime('%Y-%m-%d %H:%M:%S')} UTC""")
            print(f"Trade closed [{exit_reason}] PnL: ${pnl:+.2f}")

    # Open new trade if approved
    if risk_approved(signal, confidence):
        qty, sl, tp = calculate_position(price)
        open_trade(signal, price, qty, sl, tp)
        send_to_discord(f"""📥 New Paper Trade Opened

Asset: {SYMBOL}
Direction: {signal_emoji}
Entry: ${price:,.2f}
Qty: {qty} BTC
Stop-Loss: ${sl:,.2f}
Take-Profit: ${tp:,.2f}

Time: {time.strftime('%Y-%m-%d %H:%M:%S')} UTC""")
        print(f"Trade opened: {signal} @ ${price:,.2f} | SL: ${sl} | TP: ${tp}")

    # Send signal
    msg = f"""🚀 AI Trading Signal

Asset: {SYMBOL}
Price: ${price:,.2f}
RSI: {round(rsi, 2)}

Signal: {signal_emoji}
Confidence: {confidence}%

Time: {time.strftime('%Y-%m-%d %H:%M:%S')} UTC"""

    print(msg)
    send_to_discord(msg)

# ── Init ──────────────────────────────────────────────────────────────────────

init_db()
