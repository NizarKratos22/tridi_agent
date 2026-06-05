# Tridi — Telegram → MT5 Signal Dashboard

Monitor up to 4 Telegram channels (Arabic / French / English), parse trading signals with Claude AI, sync positions from MetaTrader 5, and view everything on a live dashboard.

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in credentials
cp .env.example .env
```

Edit `.env`:
| Key | Where to get it |
|---|---|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | https://my.telegram.org → App |
| `TELEGRAM_PHONE` | Your Telegram phone (international format) |
| `CHANNEL_1..4` | Channel usernames (without @) |
| `MT5_LOGIN/PASSWORD/SERVER` | Your broker's MT5 credentials |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com |

## Run

**Terminal 1 — Agent (Telegram + MT5 sync)**
```bash
python main.py
```

**Terminal 2 — Dashboard**
```bash
streamlit run dashboard/app.py
```

Open http://localhost:8501 in your browser.

## Options

```bash
python main.py --tg-only   # Telegram listener only
python main.py --mt5-only  # MT5 poller only (no Telegram)
```

## How it works

```
Telegram channels (AR/FR/EN)
        │
        ▼
  TelegramAgent (telethon)
        │  new message
        ▼
  signal_parser (Claude Haiku)
        │  BUY/SELL + symbol + SL/TP
        ▼
  SQLite  signals table
        │
        ├──────────────────────────────┐
        ▼                              ▼
  MT5Agent (polls every 30s)    Streamlit Dashboard
  matches positions → signals   ├ positions table (per channel)
  upserts positions table       ├ channel win-rate cards
                                └ signal feed (flag + language)
```

## Signal parsing — multilingual examples

| Language | Example message |
|---|---|
| English | `XAUUSD BUY @ 2340 SL 2320 TP1 2360 TP2 2380` |
| French  | `EURUSD - Achat 1.0850 SL 1.0820 TP 1.0900` |
| Arabic  | `شراء GBPUSD دخول 1.2700 وقف 1.2650 هدف 1.2760` |
