import requests
import pandas as pd
from ta.momentum import RSIIndicator
import time
from getpass import getpass

SYMBOL = "BTC-USD"
DISCORD_WEBHOOK = getpass("Paste your Discord Webhook URL: ")

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

def send_to_discord(message):
    try:
        resp = requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=5)
        if resp.status_code not in (200, 204):
            print(f"Discord error: {resp.status_code}")
    except Exception as e:
        print(f"Discord send failed: {e}")

def run_cycle():
    price = get_price()
    closes = get_klines()
    if not price or not closes:
        print("Data fetch failed, skipping cycle")
        return
    rsi = get_rsi(closes)
    signal, confidence = generate_signal(rsi)
    signal_emoji = {"BUY": "🟢 BUY", "SELL": "🔴 SELL", "HOLD": "🟡 HOLD"}[signal]
    msg = f"""🚀 AI Trading Signal

Asset: BTC-USD
Price: ${price:,.2f}
RSI: {round(rsi, 2)}

Signal: {signal_emoji}
Confidence: {confidence}%

Time: {time.strftime("%Y-%m-%d %H:%M:%S")} UTC"""
    print(msg)
    send_to_discord(msg)

run_cycle()
