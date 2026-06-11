# Tridi — Architecture & Developer Guide

Telegram → MetaTrader 5 trading-signal agent with a live dashboard.
Reads trading signals from up to 4 Telegram channels (Arabic / French / English),
parses them with Claude, executes them on MT5, and tracks everything on a
real-time Streamlit dashboard.

---

## 1. System overview

```
┌──────────────────┐     new message     ┌──────────────────────┐
│ Telegram channels │ ──────────────────▶ │   TelegramAgent      │
│ (AR / FR / EN)    │                     │  (telethon listener) │
└──────────────────┘                     └──────────┬───────────┘
                                                     │ raw text
                          ┌──────────────────────────┼───────────────────────────┐
                          ▼                          ▼                            ▼
                 ┌─────────────────┐       ┌──────────────────┐         ┌──────────────────┐
                 │ archive_message │       │  parse_signal     │         │ extract_and_store │
                 │  (raw_messages) │       │  (Claude Haiku)   │         │ (channel keywords)│
                 └─────────────────┘       └────────┬─────────┘         └──────────────────┘
                                                     │ classified signal
                                                     ▼
                                          ┌──────────────────────┐
                                          │   handle_signal       │
                                          │   (TradeExecutor)     │
                                          │  NEW / UPDATE / CLOSE │
                                          └──────────┬───────────┘
                                                     │ order_send / SLTP / close
                                                     ▼
                                          ┌──────────────────────┐
                                          │    MetaTrader 5       │◀── MT5Agent polls
                                          │    terminal           │     every 30 s and
                                          └──────────┬───────────┘     syncs positions
                                                     │
                                                     ▼
                                          ┌──────────────────────┐
                                          │  SQLite  signals.db   │
                                          └──────────┬───────────┘
                                                     │
                                                     ▼
                                          ┌──────────────────────┐
                                          │  Streamlit dashboard  │
                                          └──────────────────────┘
```

Two long-running processes plus one UI:

| Process | File | Responsibility |
|---|---|---|
| **TelegramAgent** | `agents/telegram_agent.py` | Listen to channels, parse, learn, execute |
| **MT5Agent** | `agents/mt5_agent.py` | Poll MT5 every 30 s, sync open/closed positions |
| **Dashboard** | `dashboard/app.py` | Read-only live view of everything |

`main.py` runs the two agents together; the dashboard runs separately.

---

## 2. Directory layout

```
telegram-mt5-agent/
├── main.py                      # entry point — runs both agents
├── list_channels.py             # one-off helper: list joined channels
├── requirements.txt
├── .env                         # secrets (NEVER commit)
├── .env.example                 # template
├── .gitignore
│
├── agents/
│   ├── telegram_agent.py        # Telegram listener + router
│   ├── signal_parser.py         # multilingual classifier (Claude)
│   ├── trade_executor.py        # opens / updates / closes MT5 trades
│   ├── mt5_agent.py             # background MT5 position sync
│   └── keyword_extractor.py     # per-channel vocabulary learning
│
├── database/
│   ├── db.py                    # SQLite schema + queries
│   └── status.py                # heartbeat file (agent ↔ dashboard)
│
├── dashboard/
│   └── app.py                   # Streamlit UI
│
└── docs/
    └── ARCHITECTURE.md          # this file
```

---

## 3. Data flow in detail

### 3.1 Message ingestion (`telegram_agent.py::_handle`)

1. A new message arrives on a watched channel.
2. Language is detected **once** with `langdetect` and reused everywhere
   (avoids a redundant detection pass — see *Optimisations*).
3. The raw text is archived to `raw_messages` (full audit trail / replay).
4. `parse_signal(msg, channel, language)` classifies and structures it.
5. `extract_and_store(...)` learns the channel's keywords.
6. If it's not pure noise, the structured signal is written to `signals`.
7. `handle_signal(...)` routes it to MT5.

### 3.2 Signal classification (`signal_parser.py`)

Every message is classified into one of four **types**:

| Type | Meaning | MT5 action |
|---|---|---|
| `NEW_SIGNAL` | fresh BUY/SELL entry | open a market order |
| `UPDATE` | move SL, trail, "still open", partial TP | modify SL on the open position |
| `CLOSE` | "close now", "exit", all TPs hit | close matching open positions |
| `IRRELEVANT` | news, ads, greetings | ignored |

A cheap regex pre-filter (`_TRADING_RE`) short-circuits obvious non-trading
messages **before** any Claude call, saving tokens. Only messages that pass the
filter hit the model (`claude-haiku-4-5`), which returns strict JSON:

```json
{
  "type": "NEW_SIGNAL",
  "symbol": "XAUUSD",
  "action": "BUY",
  "entry": 2340.0, "sl": 2320.0,
  "tp1": 2360.0, "tp2": 2380.0, "tp3": null,
  "note": "Gold buy setup"
}
```

### 3.3 Multilingual handling

The system prompt teaches Claude the trading vocabulary in all three languages:

| | BUY | SELL | CLOSE | UPDATE |
|---|---|---|---|---|
| **Arabic** | شراء / اشتري | بيع | أغلق / إغلاق | تحديث / لا تزال مفتوحة |
| **French** | acheter / achat | vendre / vente | fermer / clôturer | position ouverte |
| **English** | buy / long | sell / short | close / exit | move SL / still in |

### 3.4 Per-channel vocabulary learning (`keyword_extractor.py`)

Each parsed message is tokenised (Arabic / Latin / emoji aware), stop-words are
removed, and the remaining keywords are upserted into `channel_keywords` with a
`hit_count`. On the next parse for that channel, `build_channel_context()` injects
the top keywords into the prompt so Claude recognises that channel's specific
style. The dashboard's **Vocabulary** tab visualises this (pill size ∝ frequency).

### 3.5 Trade execution (`trade_executor.py`)

`handle_signal()` dispatches by type:

- **`_open_trade`** — duplicate guard first: if an open position with the same
  symbol+side already exists, the new signal is **skipped** (no double trades).
  Otherwise sends a market order with SL/TP, retrying on requote/price-changed
  errors, then records the position in `positions`.
- **`_update_trade`** — moves SL on the matching open position(s) via
  `TRADE_ACTION_SLTP`.
- **`_close_trades`** — closes matching open positions with an opposite market
  order and records the realised profit.

Safety flag `AUTO_TRADE=false` → **dry-run**: everything is parsed, learned and
logged, but **no order is sent**. Use this to validate parsing before going live.

### 3.6 Position sync (`mt5_agent.py`)

Independently of Telegram, `MT5Agent` polls every 30 s and:
- syncs all currently open positions (`positions_get`)
- syncs the last 30 days of closing deals (`history_deals_get`)
- best-effort matches each to the originating channel via recent signals
- writes account balance/equity to the heartbeat

This means manually-opened trades and TP/SL hits show up too.

---

## 4. Database schema (`database/db.py`)

SQLite at `database/signals.db`. Four tables:

```sql
signals(id, channel, language, raw_message, msg_type,
        symbol, action, entry, sl, tp1, tp2, tp3, note, parsed_at)

positions(id, signal_id→signals, ticket UNIQUE, symbol, action,
          open_price, sl, tp, lot, profit, status,
          channel, opened_at, closed_at, close_price)

channel_keywords(id, channel, language, keyword, msg_type, action,
                 hit_count, last_seen, UNIQUE(channel, keyword))

raw_messages(id, channel, language, message, received_at)
```

- `positions.status` ∈ `open | closed | cancelled`
- `signals.msg_type` ∈ `NEW_SIGNAL | UPDATE | CLOSE | IRRELEVANT`
- `channel_keywords` uses an UPSERT to increment `hit_count` on repeat keywords.

Connections are opened per call (`get_conn()`), which is intentional: the
Telegram agent (async) and MT5 agent (thread) write concurrently, and short-lived
connections avoid cross-thread SQLite handle sharing.

---

## 5. Heartbeat & status (`database/status.py`)

The dashboard cannot import the running agents, so health is shared through a
small `status.json` file:

```json
{
  "telegram": {"ok": true, "detail": "Watching 2 channel(s)", "ts": "..."},
  "mt5":      {"ok": true, "detail": "Balance: 10000.00 ...", "ts": "..."}
}
```

Agents call `beat(service, ok, detail)` on every cycle. The dashboard reads it and
flags a service **stale** (red) if its `ts` is older than `HEARTBEAT_TIMEOUT`
(3 min), even if the last beat said "ok" — this catches a frozen/crashed agent.

---

## 6. Dashboard (`dashboard/app.py`)

### 6.1 Auto-refresh — no page reload

The live section is wrapped in:

```python
@st.fragment(run_every=REFRESH_SECONDS)
def live_dashboard():
    data = load_data()
    ...
live_dashboard()
```

`st.fragment(run_every=...)` reruns **only that fragment** over the existing
websocket every 30 s. Compared with the old `<meta http-equiv="refresh">`:

| | `<meta refresh>` (old) | `st.fragment` (new) |
|---|---|---|
| Browser reload | yes — full white flash | **no** |
| Scroll position | reset to top | **preserved** |
| Active tab | reset to first | **preserved** |
| Filter selections | reset | **preserved** |
| Network cost | re-downloads page + assets | tiny websocket delta |

The **Refresh now** button uses `st.rerun(scope="fragment")` to update on demand
without reloading either.

### 6.2 Layout

- **Top bar** (static, rendered once) — logo + title.
- **Connection Status** — three cards (Telegram / MT5 / Database) each with a
  ✓/✗ checklist, last detail and heartbeat age.
- **Metrics row** — total/open/closed positions, total P&L, global win-rate.
- **Tabs**:
  - *Positions* — filterable table with side, SL/TP, P&L, source channel.
  - *Channel Win Rates* — per-channel win-rate bars + distribution donut.
  - *Signal Feed* — recent parsed messages with language + type badges.
  - *Vocabulary* — learned keywords per channel, grouped by type.

### 6.3 Styling

Pure inline-CSS + **Font Awesome 6** icons (no emoji). A single colour palette is
defined at the top of `app.py` (`BG`, `PANEL`, `GREEN`, `RED`, …) and reused so the
theme stays consistent and is trivial to retune.

---

## 7. Configuration (`.env`)

| Key | Purpose |
|---|---|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | from https://my.telegram.org |
| `TELEGRAM_PHONE` | your number, intl format |
| `CHANNEL_1..4` | channel usernames or numeric IDs |
| `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` | broker MT5 credentials |
| `ANTHROPIC_API_KEY` | for signal parsing |
| `LOT_SIZE` | fixed lot per trade (e.g. `0.01`) |
| `MAGIC_NUMBER` | tags bot trades in MT5 |
| `AUTO_TRADE` | `true` = live, `false` = dry-run |

`CHANNEL_*` accept either a username (`gold_signals`) or a numeric ID
(`-1001234567890`) for private channels with no username.

---

## 8. Running

```bash
# 1. install
pip install -r requirements.txt

# 2. configure
cp .env.example .env        # then fill it in

# 3. discover your channels (first run asks for the Telegram SMS code)
python list_channels.py

# 4. run the agents  (terminal 1)
python main.py              # or: --tg-only / --mt5-only

# 5. run the dashboard (terminal 2)
streamlit run dashboard/app.py   # → http://localhost:8501
```

---

## 9. Optimisations applied

- **Single language detection** — `langdetect` runs once per message; the result
  is passed into `parse_signal(..., language=...)` instead of being recomputed.
- **Regex pre-filter** — non-trading messages never reach Claude, cutting API cost.
- **Fragment refresh** — websocket-only updates instead of full page reloads.
- **Centralised `load_data()`** — one place fetches all dashboard data; the three
  table counts share a single DB connection per render.
- **Single colour palette** — constants instead of repeated hex literals.
- **Component functions** — each dashboard section is its own `render_*`
  function, keeping the live fragment short and readable.

---

## 10. Safety notes

- `.env` and `session_tridi.session` are git-ignored — they grant full access to
  your Telegram and trading accounts. Never share them.
- Always validate with `AUTO_TRADE=false` before enabling live execution.
- The duplicate guard prevents re-entering a position that is already open when a
  channel posts a follow-up "still in trade" message.
