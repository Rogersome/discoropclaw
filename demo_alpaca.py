"""
Alpaca Paper Trading Demo
Run this BEFORE integrating into bot.py to verify your API keys work.

Sign up at: https://alpaca.markets
Go to: Paper Trading -> API Keys -> Generate
"""

import requests
import json
from getpass import getpass

# ── Config ────────────────────────────────────────────────────────────────────

API_KEY    = getpass("Alpaca API Key: ")
API_SECRET = getpass("Alpaca Secret Key: ")

BASE_URL = "https://paper-api.alpaca.markets"
HEADERS  = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type":        "application/json",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def get(endpoint):
    r = requests.get(f"{BASE_URL}{endpoint}", headers=HEADERS)
    return r.json()

def post(endpoint, data):
    r = requests.post(f"{BASE_URL}{endpoint}", headers=HEADERS, json=data)
    return r.json()

def delete(endpoint):
    r = requests.delete(f"{BASE_URL}{endpoint}", headers=HEADERS)
    return r.status_code, r.text

# ── Demo ──────────────────────────────────────────────────────────────────────

def demo():
    print("\n" + "="*50)
    print("  ALPACA PAPER TRADING DEMO")
    print("="*50)

    # 1. Account info
    print("\n[ ACCOUNT ]")
    account = get("/v2/account")
    if "code" in account:
        print(f"ERROR: {account.get('message', account)}")
        return
    print(f"  Status       : {account['status']}")
    print(f"  Cash         : ${float(account['cash']):,.2f}")
    print(f"  Portfolio    : ${float(account['portfolio_value']):,.2f}")
    print(f"  Buying Power : ${float(account['buying_power']):,.2f}")

    # 2. Current positions
    print("\n[ OPEN POSITIONS ]")
    positions = get("/v2/positions")
    if not positions:
        print("  No open positions.")
    else:
        for p in positions:
            unrealized = float(p['unrealized_pl'])
            print(f"  {p['symbol']:10} qty={p['qty']:8} entry=${float(p['avg_entry_price']):,.2f}  P&L=${unrealized:+,.2f}")

    # 3. Place a test BUY order (1 share of BTC/USD notional $10)
    print("\n[ PLACE TEST ORDER ]")
    print("  Placing BUY order: $10 notional of BTC/USD...")
    order = post("/v2/orders", {
        "symbol":        "BTC/USD",
        "notional":      "10",        # $10 worth of BTC
        "side":          "buy",
        "type":          "market",
        "time_in_force": "gtc",
    })

    if "id" in order:
        print(f"  Order ID  : {order['id']}")
        print(f"  Symbol    : {order['symbol']}")
        print(f"  Side      : {order['side'].upper()}")
        print(f"  Status    : {order['status']}")
        print(f"  Notional  : ${order.get('notional', 'N/A')}")
    else:
        print(f"  Order failed: {order}")
        return

    # 4. Recent orders
    print("\n[ RECENT ORDERS ]")
    orders = get("/v2/orders?limit=3&status=all")
    for o in orders[:3]:
        filled = o.get('filled_at', 'pending')
        print(f"  {o['symbol']:10} {o['side'].upper():5} {o['status']:12} {filled}")

    # 5. Close all positions (cleanup)
    print("\n[ CLEANUP — Close all positions ]")
    confirm = input("  Close all test positions? (y/n): ")
    if confirm.lower() == "y":
        status, text = delete("/v2/positions")
        print(f"  Status: {status} — {text[:80]}")
    else:
        print("  Skipped. Positions remain open.")

    print("\n" + "="*50)
    print("  Demo complete. Alpaca connection works!")
    print("="*50 + "\n")

if __name__ == "__main__":
    demo()
