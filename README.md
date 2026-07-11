# BTC RSI Signal Bot

A small script that checks the current Bitcoin price, computes the 14-period RSI from 1-minute candles, generates a simple BUY / SELL / HOLD signal, and posts a formatted alert to a Discord channel via webhook.

## What it does

1. Fetches the current BTC-USD spot price from Coinbase.
2. Fetches the last 100 one-minute candles from Coinbase Exchange.
3. Calculates RSI(14) on the candle close prices using the `ta` library.
4. Generates a signal:
   - **RSI < 30** → 🟢 BUY
   - **RSI > 70** → 🔴 SELL
   - otherwise → 🟡 HOLD
5. Prints the result and sends it to a Discord channel via webhook.

## Requirements

- Python 3.8+
- Packages:
  ```bash
  pip install requests pandas ta
  ```

## Setup

1. Create a Discord webhook in the channel you want alerts posted to:
   `Channel Settings → Integrations → Webhooks → New Webhook → Copy Webhook URL`
2. Run the script. You'll be prompted to paste the webhook URL (input is hidden via `getpass`, so it won't be echoed to the terminal or saved to disk):
   ```bash
   python rsi_signal_bot.py
   ```

## Usage

Running the script performs a single check-and-alert cycle (`run_cycle()`). To run it continuously (e.g., every minute), wrap the call in a loop or schedule it with cron / a task scheduler:

```python
while True:
    run_cycle()
    time.sleep(60)
```

## Sample Discord output

```
🚀 AI Trading Signal
Asset: BTC-USD
Price: $67,432.10
RSI: 28.4
Signal: 🟢 BUY
Confidence: 80%
Time: 2026-07-11 14:32:05 UTC
```

## Notes & limitations

- **Data sources**: uses Coinbase's public REST endpoints (no API key required). These are unauthenticated and rate-limited, and the endpoints/response formats could change without notice.
- **RSI-only signal**: the BUY/SELL logic is a bare RSI threshold crossover. It does not account for trend, volume, volatility, or risk management, and is not a trading strategy on its own.
- **Fixed confidence score**: the 80%/50% "confidence" values are hardcoded, not statistically derived.
- **Not financial advice**: this tool is for informational/educational purposes. It does not place trades — it only sends alerts.
- **Error handling**: network failures or malformed API responses are caught and logged; the cycle is skipped rather than crashing.

## Possible extensions

- Add more indicators (MACD, moving averages, volume) to reduce false signals.
- Persist historical signals to a file or database for backtesting.
- Support multiple symbols.
- Add retry/backoff logic for API requests.
