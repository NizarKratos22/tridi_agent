"""
Trade Executor — opens, updates, or closes positions in MetaTrader 5.

Actions triggered by signal type:
  NEW_SIGNAL → open a market order (if no duplicate already open)
  UPDATE     → move SL (if new SL provided in signal)
  CLOSE      → close all matching open positions for that symbol
  IRRELEVANT → do nothing
"""
import logging
import os
import time

import MetaTrader5 as mt5
from dotenv import load_dotenv

from database.db import upsert_position
from database.status import beat

load_dotenv()
log = logging.getLogger(__name__)

def _clean(key: str, default: str = "") -> str:
    """Read an env var, stripping any inline `# comment` and whitespace."""
    return os.environ.get(key, default).split("#")[0].strip()

LOT_SIZE   = float(_clean("LOT_SIZE", "0.01"))
MAGIC      = int(_clean("MAGIC_NUMBER", "20250605"))
AUTO_TRADE = _clean("AUTO_TRADE", "true").lower() == "true"

# Duplicate handling:
#   "channel" → one open trade per (channel, symbol, side); other channels can
#               still open the same symbol+side independently  (default)
#   "global"  → only one open trade per symbol+side across ALL channels
#   "off"     → no duplicate guard at all (every signal opens a trade)
DUP_GUARD  = _clean("DUP_GUARD", "channel").lower()

# MT5 login — same credentials used to (re)attach the terminal when needed
MT5_LOGIN    = _clean("MT5_LOGIN")
MT5_PASSWORD = _clean("MT5_PASSWORD")
MT5_SERVER   = _clean("MT5_SERVER")

MAX_RETRIES = 3
RETRY_DELAY = 2


# ── helpers ───────────────────────────────────────────────────────────────────

def _ensure_connected() -> bool:
    """
    Make sure the MT5 terminal is attached and logged in.

    Plain mt5.initialize() returns (-6, 'Authorization failed') when the
    terminal isn't auto-authorised, so we initialise WITH credentials first.
    """
    if mt5.account_info() is not None:
        return True   # already connected this process
    try:
        if mt5.initialize(login=int(MT5_LOGIN), password=MT5_PASSWORD, server=MT5_SERVER):
            return True
    except ValueError:
        pass
    if mt5.initialize() and mt5.login(int(MT5_LOGIN), MT5_PASSWORD, MT5_SERVER):
        return True
    log.error("MT5 connect failed: %s", mt5.last_error())
    return False


def _resolve_filling(symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC
    filling = info.filling_mode
    if filling & mt5.ORDER_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    if filling & mt5.ORDER_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def _open_positions_for(symbol: str, action: str | None = None) -> list:
    """Return all open MT5 positions matching symbol (and optionally action)."""
    positions = mt5.positions_get(symbol=symbol) or []
    if action:
        order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
        positions = [p for p in positions if p.type == order_type]
    return list(positions)


def _has_open_position(symbol: str, action: str) -> bool:
    return len(_open_positions_for(symbol, action)) > 0


def _is_duplicate(channel: str, symbol: str, action: str) -> bool:
    """
    Decide if this signal duplicates an already-open trade, per DUP_GUARD mode.
      off     → never a duplicate
      global  → duplicate if ANY open trade matches symbol+side
      channel → duplicate only if THIS channel already has it open

    Channel ownership comes from the bot's DB, NOT the MT5 comment — brokers
    truncate comments (e.g. 'tridi|malek_elda'), which silently broke matching.
    """
    if DUP_GUARD == "off":
        return False
    if DUP_GUARD == "global":
        return _has_open_position(symbol, action)
    # default: per-channel — DB rows joined against live MT5 tickets
    from database.db import fetch_open_db_positions
    live = {p.ticket for p in (mt5.positions_get() or [])}
    for r in fetch_open_db_positions():
        if (r["channel"] == channel
                and (r["symbol"] or "").upper() == symbol
                and (r["action"] or "").upper() == action
                and r["ticket"] in live):
            return True
    return False


# ── main entry point ──────────────────────────────────────────────────────────

def handle_signal(signal_id: int, channel: str, parsed: dict) -> dict | None:
    """
    Route a classified signal to the correct handler.
    Returns a result dict or None.
    """
    sig_type = parsed.get("type", "IRRELEVANT")
    symbol   = (parsed.get("symbol") or "").upper()
    action   = (parsed.get("action") or "").upper() or None
    note     = parsed.get("note", "")

    # ensure the channel is known to the config table (so it appears in the UI)
    from database.db import register_channel
    register_channel(channel)

    log.info("[%s] type=%s  symbol=%s  action=%s  note=%s",
             channel, sig_type, symbol, action, note)

    if sig_type == "NEW_SIGNAL":
        # Per-channel switch: skip opening if this channel's trading is OFF.
        from database.db import is_channel_trade_enabled
        if not is_channel_trade_enabled(channel):
            log.info("[%s] trading DISABLED for this channel — signal logged, not opened",
                     channel)
            beat("mt5", True, f"Signal from @{channel} ignored (channel trading off)")
            return {"skipped": True, "reason": "channel_disabled",
                    "symbol": symbol, "action": action}
        return _open_trade(signal_id, channel, parsed)

    if sig_type == "UPDATE":
        return _update_trade(signal_id, channel, parsed)

    if sig_type == "CLOSE":
        return _close_trades(signal_id, channel, parsed)

    # IRRELEVANT — nothing to do
    return None


# ── open ──────────────────────────────────────────────────────────────────────

def _open_trade(signal_id: int, channel: str, parsed: dict) -> dict | None:
    symbol = (parsed.get("symbol") or "").upper()
    action = (parsed.get("action") or "").upper()
    sl     = parsed.get("sl")
    tp     = parsed.get("tp1")

    # lot priority: explicit lot in the message → fallback to .env LOT_SIZE
    try:
        volume = float(parsed.get("lot") or LOT_SIZE)
    except (TypeError, ValueError):
        volume = LOT_SIZE

    if not symbol or action not in ("BUY", "SELL"):
        log.warning("open_trade: bad symbol/action — %s %s", symbol, action)
        return None

    if not AUTO_TRADE:
        log.info("[DRY-RUN] Would open %s %s SL=%s TP=%s lot=%s",
                 action, symbol, sl, tp, volume)
        return {"dry_run": True, "action": action, "symbol": symbol}

    if not _ensure_connected():
        return None

    # ── duplicate guard (scoped per DUP_GUARD) ────────────────────────────────
    if _is_duplicate(channel, symbol, action):
        # Two-message pattern: channels often shout "gold buy now" first (we
        # open instantly, no stops) and post the full plan with SL/TPs right
        # after. If this "duplicate" carries stops, apply them to the open
        # position instead of throwing them away.
        if sl or tp:
            order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
            targets = (_positions_for_channel(channel) if DUP_GUARD == "channel"
                       else _open_positions_for(symbol, action))
            enriched = 0
            for pos in targets:
                if pos.symbol.upper() != symbol or pos.type != order_type:
                    continue
                req = {
                    "action":   mt5.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "sl":       float(sl) if sl else pos.sl,
                    "tp":       float(tp) if tp else pos.tp,
                }
                r = mt5.order_send(req)
                if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                    enriched += 1
                    upsert_position(pos.ticket, {
                        "profit": pos.profit, "status": "open",
                        "sl": float(sl) if sl else None,
                        "tp": float(tp) if tp else None,
                    })
                    log.info("📌 Follow-up details applied to ticket #%d → SL=%s TP=%s",
                             pos.ticket, sl, tp)
                    beat("mt5", True,
                         f"Follow-up SL/TP applied to {symbol} #{pos.ticket}")
                else:
                    err = r.comment if r else str(mt5.last_error())
                    log.error("Follow-up SL/TP failed on #%d: %s — %s",
                              pos.ticket, err, _retcode_hint(r.retcode if r else None))
            if enriched:
                return {"enriched": enriched, "symbol": symbol, "action": action}

        log.warning("⚠️  @%s already has an open %s on %s — skipping duplicate",
                    channel, action, symbol)
        beat("mt5", True,
             f"Skipped duplicate {action} {symbol} (already open for @{channel})")
        return {"skipped": True, "reason": "duplicate", "symbol": symbol, "action": action}

    # ── select symbol ─────────────────────────────────────────────────────────
    if not mt5.symbol_select(symbol, True):
        log.error("Symbol %s not found in MT5", symbol)
        beat("mt5", False, f"Symbol {symbol} not found")
        return None

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log.error("No tick for %s", symbol)
        return None

    order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
    price      = tick.ask if action == "BUY" else tick.bid

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       volume,
        "type":         order_type,
        "price":        price,
        "sl":           float(sl) if sl else 0.0,
        "tp":           float(tp) if tp else 0.0,
        "deviation":    20,
        "magic":        MAGIC,
        "comment":      f"tridi|{channel}|sig{signal_id}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": _resolve_filling(symbol),
    }

    result = None
    for attempt in range(1, MAX_RETRIES + 1):
        result = mt5.order_send(request)
        if result is None:
            log.error("order_send None attempt %d: %s", attempt, mt5.last_error())
            time.sleep(RETRY_DELAY)
            continue

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info("✅ Opened %s %s ticket=%d @ %.5f lot=%.2f",
                     action, symbol, result.order, result.price, volume)
            beat("mt5", True,
                 f"Opened {action} {symbol} ticket#{result.order} @ {result.price:.5f}")

            from datetime import datetime, timezone
            upsert_position(result.order, {
                "signal_id":  signal_id,
                "symbol":     symbol,
                "action":     action,
                "open_price": result.price,
                "sl":         float(sl) if sl else None,
                "tp":         float(tp) if tp else None,
                "lot":        volume,
                "profit":     0.0,
                "status":     "open",
                "channel":    channel,
                "opened_at":  datetime.now(timezone.utc).isoformat(),
            })
            return result._asdict()

        retryable = {
            mt5.TRADE_RETCODE_REQUOTE,
            mt5.TRADE_RETCODE_PRICE_CHANGED,
            mt5.TRADE_RETCODE_PRICE_OFF,
            mt5.TRADE_RETCODE_TIMEOUT,
            mt5.TRADE_RETCODE_CONNECTION,
        }
        if result.retcode in retryable:
            log.warning("Retryable error %d attempt %d", result.retcode, attempt)
            tick = mt5.symbol_info_tick(symbol)
            if tick:
                request["price"] = tick.ask if action == "BUY" else tick.bid
            time.sleep(RETRY_DELAY)
        else:
            break

    comment = result.comment if result else str(mt5.last_error())
    retcode = result.retcode if result else "N/A"
    hint = _retcode_hint(retcode)
    log.error("❌ Open FAILED %s %s retcode=%s %s%s",
              action, symbol, retcode, comment, f" — {hint}" if hint else "")
    beat("mt5", False, f"Order rejected: {hint or comment} ({action} {symbol})")
    return None


def _retcode_hint(retcode) -> str:
    """Plain-English fix hint for the most common MT5 order rejections."""
    hints = {
        10027: "Enable the 'Algo Trading' button in the MT5 terminal toolbar",
        10018: "Market is closed for this symbol",
        10019: "Not enough money in the account for this lot size",
        10016: "Invalid SL/TP — too close to current price",
        10014: "Invalid volume (lot size) for this symbol",
        10013: "Invalid request",
        10006: "Order rejected by the broker",
    }
    try:
        return hints.get(int(retcode), "")
    except (ValueError, TypeError):
        return ""


# ── update (move SL) ──────────────────────────────────────────────────────────

def _update_trade(signal_id: int, channel: str, parsed: dict) -> dict | None:
    """
    UPDATE handler — moves the stop-loss on open positions.
      • explicit SL value in the message → move SL to that price
      • break-even instruction (no SL value) → move SL to the entry price
    Works with a symbol, or channel-scoped when no symbol is given.
    """
    symbol     = (parsed.get("symbol") or "").upper()
    new_sl     = parsed.get("sl")
    breakeven  = bool(parsed.get("breakeven"))
    action     = (parsed.get("action") or "").upper() or None

    if not (breakeven or new_sl is not None):
        log.info("UPDATE: nothing actionable (no SL, no break-even) — ignoring")
        return {"type": "UPDATE", "updated": 0}

    if not AUTO_TRADE:
        log.info("[DRY-RUN] Would update SL (%s) on %s",
                 "break-even" if breakeven else new_sl, symbol or f"@{channel}")
        return {"dry_run": True, "type": "UPDATE", "symbol": symbol or channel}

    if not _ensure_connected():
        return None

    if symbol:
        positions = _open_positions_for(symbol, action)
    else:
        positions = _positions_for_channel(channel)   # break-even applies to channel's trades

    if not positions:
        log.info("UPDATE: no open positions to update (symbol=%s channel=%s)", symbol, channel)
        return {"type": "UPDATE", "updated": 0}

    updated = 0
    for pos in positions:
        target_sl = float(pos.price_open) if breakeven else float(new_sl)
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": pos.ticket,
            "sl":       target_sl,
            "tp":       pos.tp,       # keep existing TP
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            tag = "break-even" if breakeven else f"{target_sl:.5f}"
            log.info("✏️  SL → %s on ticket #%d (%s)", tag, pos.ticket, pos.symbol)
            beat("mt5", True, f"Moved SL to {tag} on {pos.symbol} #{pos.ticket}")
            updated += 1
        else:
            err = result.comment if result else str(mt5.last_error())
            log.error("SL update failed ticket #%d: %s", pos.ticket, err)

    return {"type": "UPDATE", "symbol": symbol or channel, "updated": updated}


# ── close ─────────────────────────────────────────────────────────────────────

def _positions_for_channel(channel: str) -> list:
    """Open bot positions belonging to this channel (ownership from the DB —
    MT5 comments get truncated by brokers so they can't be trusted)."""
    from database.db import fetch_open_db_positions
    live = {p.ticket: p for p in (mt5.positions_get() or [])}
    return [live[r["ticket"]] for r in fetch_open_db_positions()
            if r["channel"] == channel and r["ticket"] in live]


def _close_one(pos, channel: str, signal_id, reason: str = "close") -> bool:
    """Close a single MT5 position and record it. Returns True on success."""
    from datetime import datetime, timezone
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        return False
    close_type  = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    close_price = tick.bid            if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "position":     pos.ticket,
        "symbol":       pos.symbol,
        "volume":       pos.volume,
        "type":         close_type,
        "price":        close_price,
        "deviation":    20,
        "magic":        MAGIC,
        "comment":      f"tridi|{reason}|{channel}",
        "type_filling": _resolve_filling(pos.symbol),
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info("🔒 Closed ticket #%d %s @ %.5f profit=%.2f",
                 pos.ticket, pos.symbol, result.price, pos.profit)
        beat("mt5", True, f"Closed {pos.symbol} ticket#{pos.ticket} profit={pos.profit:.2f}")
        upsert_position(pos.ticket, {
            "signal_id":   signal_id,
            "symbol":      pos.symbol,
            "action":      "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL",
            "open_price":  pos.price_open,
            "sl":          pos.sl,
            "tp":          pos.tp,
            "lot":         pos.volume,
            "profit":      pos.profit,
            "status":      "closed",
            "channel":     channel,
            "opened_at":   datetime.fromtimestamp(pos.time).isoformat(),
            "closed_at":   datetime.now(timezone.utc).isoformat(),
            "close_price": result.price,
        })
        return True
    err = result.comment if result else str(mt5.last_error())
    log.error("Close FAILED ticket #%d: %s", pos.ticket, err)
    return False


def _close_trades(signal_id: int, channel: str, parsed: dict) -> dict | None:
    """
    CLOSE handler. If a symbol is given, close matching open positions on it.
    If NO symbol (e.g. just "close now"), close this CHANNEL's open positions.
    """
    symbol = (parsed.get("symbol") or "").upper()
    action = (parsed.get("action") or "").upper() or None

    if not AUTO_TRADE:
        log.info("[DRY-RUN] Would close %s positions", symbol or f"@{channel}")
        return {"dry_run": True, "type": "CLOSE", "symbol": symbol or channel}

    if not _ensure_connected():
        return None

    if symbol:
        positions = _open_positions_for(symbol, action)
    else:
        positions = _positions_for_channel(channel)   # "close now" → this channel's trades

    if not positions:
        log.info("CLOSE: no open positions to close (symbol=%s channel=%s)", symbol, channel)
        return {"type": "CLOSE", "symbol": symbol, "closed": 0}

    closed = sum(1 for pos in positions if _close_one(pos, channel, signal_id, "close"))
    return {"type": "CLOSE", "symbol": symbol or channel, "closed": closed}


def _channel_of(pos) -> str:
    """Channel that opened this position — DB first, comment as fallback."""
    from database.db import fetch_open_db_positions
    for r in fetch_open_db_positions():
        if r["ticket"] == pos.ticket:
            return r["channel"] or "manual"
    parts = (pos.comment or "").split("|")
    return parts[1] if len(parts) >= 2 and parts[0] == "tridi" else "manual"


def close_position_by_ticket(ticket: int) -> dict:
    """Manually close one position by ticket — used by the dashboard button."""
    if not _ensure_connected():
        return {"ok": False, "error": "MT5 not connected"}
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return {"ok": False, "error": "Position not found (maybe already closed)"}
    pos = positions[0]
    if _close_one(pos, _channel_of(pos), None, "manual-close"):
        return {"ok": True, "symbol": pos.symbol, "profit": pos.profit}
    err = mt5.last_error()
    return {"ok": False, "error": _retcode_hint(err) or str(err)}


def close_all_positions() -> dict:
    """Close EVERY open position this bot opened (manual panic button)."""
    if not _ensure_connected():
        return {"ok": False, "error": "MT5 not connected", "closed": 0, "total": 0}
    positions = [p for p in (mt5.positions_get() or []) if p.magic == MAGIC]
    closed = sum(1 for p in positions if _close_one(p, _channel_of(p), None, "close-all"))
    log.info("Manual: closed %d/%d positions", closed, len(positions))
    return {"ok": True, "closed": closed, "total": len(positions)}


def breakeven_all_positions() -> dict:
    """Move the stop-loss of EVERY open bot position to its entry price."""
    if not _ensure_connected():
        return {"ok": False, "error": "MT5 not connected", "moved": 0, "total": 0}
    positions = [p for p in (mt5.positions_get() or []) if p.magic == MAGIC]
    moved = 0
    for p in positions:
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket,
               "sl": float(p.price_open), "tp": p.tp}
        r = mt5.order_send(req)
        if r and r.retcode == mt5.TRADE_RETCODE_DONE:
            moved += 1
    log.info("Manual: break-even on %d/%d positions", moved, len(positions))
    return {"ok": True, "moved": moved, "total": len(positions)}


def run_manual_command(command: str, channel: str) -> dict:
    """
    Parse a free-text command (any language) and execute it as if it came from
    the given channel. Used by the dashboard command box.
    """
    from agents.rule_parser import parse_signal_rule
    parsed = parse_signal_rule(command, channel=channel)
    sig_type = parsed.get("type", "IRRELEVANT")
    if sig_type == "IRRELEVANT":
        return {"ok": False, "type": sig_type,
                "error": "Not recognised as a command (buy/sell/close/break-even…)"}
    result = handle_signal(None, channel, parsed) or {}
    return {"ok": True, "type": sig_type, "parsed": parsed, "result": result}


# ── backward-compat alias ─────────────────────────────────────────────────────
def open_trade(signal_id: int, channel: str, parsed: dict) -> dict | None:
    """Legacy alias — routes through handle_signal."""
    return handle_signal(signal_id, channel, parsed)
