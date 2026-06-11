import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "signals.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel     TEXT NOT NULL,
                language    TEXT NOT NULL,
                raw_message TEXT NOT NULL,
                msg_type    TEXT DEFAULT 'NEW_SIGNAL',
                symbol      TEXT,
                action      TEXT,
                entry       REAL,
                sl          REAL,
                tp1         REAL,
                tp2         REAL,
                tp3         REAL,
                note        TEXT,
                parsed_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS positions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id   INTEGER REFERENCES signals(id),
                ticket      INTEGER UNIQUE,
                symbol      TEXT NOT NULL,
                action      TEXT NOT NULL,
                open_price  REAL,
                sl          REAL,
                tp          REAL,
                lot         REAL,
                profit      REAL,
                status      TEXT DEFAULT 'open',
                channel     TEXT,
                opened_at   TEXT,
                closed_at   TEXT,
                close_price REAL
            );

            -- Per-channel keyword vocabulary table.
            -- Each row = one keyword/phrase seen in that channel,
            -- associated with a message type (NEW_SIGNAL, UPDATE, CLOSE).
            -- hit_count tracks how many times we've seen it → most reliable keywords rise to top.
            CREATE TABLE IF NOT EXISTS channel_keywords (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel     TEXT NOT NULL,
                language    TEXT NOT NULL,
                keyword     TEXT NOT NULL,       -- exact token/phrase extracted
                msg_type    TEXT NOT NULL,       -- what it maps to
                action      TEXT,               -- BUY | SELL | null
                hit_count   INTEGER DEFAULT 1,
                last_seen   TEXT NOT NULL,
                UNIQUE(channel, keyword)        -- one row per channel+keyword
            );

            -- Full raw messages archive — every message ever received, regardless of type.
            -- Used to replay / retrain if parsing logic changes.
            CREATE TABLE IF NOT EXISTS raw_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel     TEXT NOT NULL,
                language    TEXT NOT NULL,
                message     TEXT NOT NULL,
                received_at TEXT NOT NULL
            );

            -- Per-channel control: whether a channel's NEW signals open trades.
            -- trade_enabled=0 → signals are still parsed/logged but NOT executed.
            CREATE TABLE IF NOT EXISTS channel_config (
                channel       TEXT PRIMARY KEY,
                trade_enabled INTEGER DEFAULT 1,
                added_at      TEXT NOT NULL
            );

            -- Global key/value app settings (e.g. parse_mode = manual|agentic).
            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)


# ── signals ───────────────────────────────────────────────────────────────────

def insert_signal(channel, language, raw_message, parsed: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO signals
               (channel, language, raw_message, msg_type, symbol, action,
                entry, sl, tp1, tp2, tp3, note, parsed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                channel, language, raw_message,
                parsed.get("type", "NEW_SIGNAL"),
                parsed.get("symbol"), parsed.get("action"),
                parsed.get("entry"), parsed.get("sl"),
                parsed.get("tp1"), parsed.get("tp2"), parsed.get("tp3"),
                parsed.get("note"),
                datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


# ── raw messages ──────────────────────────────────────────────────────────────

def archive_message(channel: str, language: str, message: str):
    """Store every incoming message for replay / analysis."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO raw_messages (channel, language, message, received_at) VALUES (?,?,?,?)",
            (channel, language, message, datetime.utcnow().isoformat()),
        )


# ── keyword vocabulary ────────────────────────────────────────────────────────

def store_keywords(channel: str, language: str, keywords: list[str],
                   msg_type: str, action: str | None = None):
    """
    Upsert extracted keywords for a channel.
    If the keyword already exists → increment hit_count.
    """
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        for kw in keywords:
            kw = kw.strip().lower()
            if not kw or len(kw) < 2:
                continue
            conn.execute(
                """INSERT INTO channel_keywords
                       (channel, language, keyword, msg_type, action, hit_count, last_seen)
                   VALUES (?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(channel, keyword)
                   DO UPDATE SET
                       hit_count = hit_count + 1,
                       last_seen = excluded.last_seen,
                       msg_type  = excluded.msg_type""",
                (channel, language, kw, msg_type, action, now),
            )


def fetch_channel_keywords(channel: str, limit: int = 60) -> list:
    """Return top keywords for a channel ordered by hit_count."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT keyword, msg_type, action, hit_count, language
               FROM channel_keywords
               WHERE channel = ?
               ORDER BY hit_count DESC
               LIMIT ?""",
            (channel, limit),
        ).fetchall()


def fetch_all_channel_vocabularies() -> dict[str, list]:
    """Return {channel: [keyword rows]} for all channels."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT channel, keyword, msg_type, action, hit_count, language
               FROM channel_keywords
               ORDER BY channel, hit_count DESC"""
        ).fetchall()
    vocab: dict[str, list] = {}
    for r in rows:
        vocab.setdefault(r["channel"], []).append(dict(r))
    return vocab


def build_channel_context(channel: str) -> str:
    """
    Build a short prompt-injection string describing a channel's known vocabulary.
    Fed into Claude so it knows the channel's style before parsing.
    """
    rows = fetch_channel_keywords(channel, limit=30)
    if not rows:
        return ""

    lines = [f"Channel @{channel} vocabulary (learned from past messages):"]
    for r in rows:
        act = f" [{r['action']}]" if r["action"] else ""
        lines.append(
            f"  • \"{r['keyword']}\" → {r['msg_type']}{act} (seen {r['hit_count']}×)"
        )
    return "\n".join(lines)


# ── positions ─────────────────────────────────────────────────────────────────

def upsert_position(ticket: int, data: dict):
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT id FROM positions WHERE ticket=?", (ticket,)
        ).fetchone()
        if exists:
            # COALESCE keeps the recorded SL/TP when the caller passes None
            # (the closed-deals sync can't know stops and was wiping them).
            conn.execute(
                """UPDATE positions
                   SET profit=?, status=?,
                       sl=COALESCE(?, sl), tp=COALESCE(?, tp),
                       close_price=COALESCE(?, close_price),
                       closed_at=COALESCE(?, closed_at)
                   WHERE ticket=?""",
                (
                    data.get("profit"), data.get("status", "open"),
                    data.get("sl"), data.get("tp"),
                    data.get("close_price"), data.get("closed_at"),
                    ticket,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO positions
                   (signal_id, ticket, symbol, action, open_price, sl, tp, lot,
                    profit, status, channel, opened_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    data.get("signal_id"), ticket,
                    data["symbol"], data["action"],
                    data.get("open_price"), data.get("sl"), data.get("tp"),
                    data.get("lot", 0.0), data.get("profit", 0.0),
                    data.get("status", "open"), data.get("channel"),
                    data.get("opened_at", datetime.utcnow().isoformat()),
                ),
            )


# ── dashboard queries ──────────────────────────────────────────────────────────

def fetch_open_db_positions() -> list:
    """Open positions as recorded by the bot (ticket + channel + symbol + side)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT ticket, channel, symbol, action FROM positions WHERE status='open'"
        ).fetchall()


def mark_position_closed(ticket: int, profit=None, close_price=None):
    """Mark a DB position closed (used when MT5 says it's gone)."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE positions
               SET status='closed', closed_at=?,
                   profit=COALESCE(?, profit),
                   close_price=COALESCE(?, close_price)
               WHERE ticket=?""",
            (datetime.utcnow().isoformat(), profit, close_price, ticket),
        )


def fetch_all_positions():
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM positions ORDER BY opened_at DESC"
        ).fetchall()


def fetch_channel_stats():
    with get_conn() as conn:
        return conn.execute("""
            SELECT
                channel,
                COUNT(*)  AS total,
                SUM(CASE WHEN status='closed' AND profit>0  THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN status='closed' AND profit<=0 THEN 1 ELSE 0 END) AS losses,
                SUM(profit) AS total_profit
            FROM positions
            GROUP BY channel
        """).fetchall()


def fetch_recent_signals(limit=20):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM signals ORDER BY parsed_at DESC LIMIT ?", (limit,)
        ).fetchall()


def fetch_raw_message_count() -> dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT channel, COUNT(*) as cnt FROM raw_messages GROUP BY channel"
        ).fetchall()
    return {r["channel"]: r["cnt"] for r in rows}


def fetch_recent_raw_messages(limit=60):
    """Every message received (incl. non-signals) — newest first."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT channel, language, message, received_at "
            "FROM raw_messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


# ── channel trade-config ────────────────────────────────────────────────────────

def register_channel(channel: str, default_enabled: bool = True):
    """Add a channel to the config table if it isn't there yet (no-op if present)."""
    if not channel:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO channel_config (channel, trade_enabled, added_at) "
            "VALUES (?, ?, ?)",
            (channel, 1 if default_enabled else 0, datetime.utcnow().isoformat()),
        )


def set_channel_trade_enabled(channel: str, enabled: bool):
    """Turn auto-trading on/off for a channel (registers it if new)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO channel_config (channel, trade_enabled, added_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(channel) DO UPDATE SET trade_enabled=excluded.trade_enabled",
            (channel, 1 if enabled else 0, datetime.utcnow().isoformat()),
        )


def is_channel_trade_enabled(channel: str) -> bool:
    """True if the channel may open trades. Unknown channels default to enabled."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT trade_enabled FROM channel_config WHERE channel=?", (channel,)
        ).fetchone()
    return True if row is None else bool(row["trade_enabled"])


def fetch_channel_config() -> list:
    """All channels and their trade-enabled flag, alphabetical."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT channel, trade_enabled FROM channel_config ORDER BY channel"
        ).fetchall()


# ── app settings (key/value) ────────────────────────────────────────────────────

def get_setting(key: str, default: str | None = None) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
