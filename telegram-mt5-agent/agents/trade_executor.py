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

LOT_SIZE   = float(os.getenv("LOT_SIZE", "0.01"))
MAGIC      = int(os.getenv("MAGIC_NUMBER", "20250605"))
AUTO_TRADE = os.getenv("AUTO_TRADE", "true").lower() == "true"

MAX_RETRIES = 3
RETRY_DELAY = 2


# ── helpers ───────────────────────────────────────────────────────────────────

def _ensure_connected() -> bool:
    if not mt5.initialize():
        log.error("MT5 init failed: %s", mt5.last_error())
        return False
    return True


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

    log.info("[%s] type=%s  symbol=%s  action=%s  note=%s",
             channel, sig_type, symbol, action, note)

    if sig_type == "NEW_SIGNAL":
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

    if not symbol or action not in ("BUY", "SELL"):
        log.warning("open_trade: bad symbol/action — %s %s", symbol, action)
        return None

    if not AUTO_TRADE:
        log.info("[DRY-RUN] Would open %s %s SL=%s TP=%s lot=%s",
                 action, symbol, sl, tp, LOT_SIZE)
        return {"dry_run": True, "action": action, "symbol": symbol}

    if not _ensure_connected():
        return None

    # ── duplicate guard ───────────────────────────────────────────────────────
    if _has_open_position(symbol, action):
        log.warning("⚠️  Already have an open %s on %s — skipping duplicate",
                    action, symbol)
        beat("mt5", True,
             f"Skipped duplicate {action} {symbol} — position already open")
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
        "volume":       LOT_SIZE,
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
                     action, symbol, result.order, result.price, LOT_SIZE)
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
                "lot":        LOT_SIZE,
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
    log.error("❌ Open FAILED %s %s retcode=%s %s", action, symbol, retcode, comment)
    beat("mt5", False, f"Open failed {action} {symbol} — {comment}")
    return None


# ── update (move SL) ──────────────────────────────────────────────────────────

def _update_trade(signal_id: int, channel: str, parsed: dict) -> dict | None:
    symbol    = (parsed.get("symbol") or "").upper()
    new_sl    = parsed.get("sl")
    action    = (parsed.get("action") or "").upper() or None
    note      = parsed.get("note", "")

    if not symbol:
        log.info("UPDATE: no symbol found — ignoring (%s)", note)
        return None

    if not AUTO_TRADE:
        log.info("[DRY-RUN] Would update SL on %s to %s", symbol, new_sl)
        return {"dry_run": True, "type": "UPDATE", "symbol": symbol}

    if not _ensure_connected():
        return None

    positions = _open_positions_for(symbol, action)
    if not positions:
        log.info("UPDATE: no open %s position on %s to update", action or "any", symbol)
        return None

    updated = 0
    for pos in positions:
        if new_sl is None:
            log.info("UPDATE: no new SL in signal for %s — nothing to change", symbol)
            continue

        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": pos.ticket,
            "sl":       float(new_sl),
            "tp":       pos.tp,       # keep existing TP
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info("✏️  Updated SL on ticket #%d → %.5f", pos.ticket, float(new_sl))
            beat("mt5", True, f"Updated SL on {symbol} ticket#{pos.ticket} → {new_sl}")
            updated += 1
        else:
            err = result.comment if result else str(mt5.last_error())
            log.error("SL update failed ticket #%d: %s", pos.ticket, err)

    return {"type": "UPDATE", "symbol": symbol, "updated": updated}


# ── close ─────────────────────────────────────────────────────────────────────

def _close_trades(signal_id: int, channel: str, parsed: dict) -> dict | None:
    symbol = (parsed.get("symbol") or "").upper()
    action = (parsed.get("action") or "").upper() or None

    if not symbol:
        log.info("CLOSE: no symbol — ignoring")
        return None

    if not AUTO_TRADE:
        log.info("[DRY-RUN] Would close all %s %s positions",
                 action or "any", symbol)
        return {"dry_run": True, "type": "CLOSE", "symbol": symbol}

    if not _ensure_connected():
        return None

    positions = _open_positions_for(symbol, action)
    if not positions:
        log.info("CLOSE: no open %s positions on %s", action or "", symbol)
        return None

    closed = 0
    from datetime import datetime, timezone

    for pos in positions:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            continue

        # Close = opposite direction at market
        close_type  = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        close_price = tick.bid            if pos.type == mt5.ORDER_TYPE_BUY else tick.ask

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "position":     pos.ticket,
            "symbol":       symbol,
            "volume":       pos.volume,
            "type":         close_type,
            "price":        close_price,
            "deviation":    20,
            "magic":        MAGIC,
            "comment":      f"tridi|close|{channel}|sig{signal_id}",
            "type_filling": _resolve_filling(symbol),
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info("🔒 Closed ticket #%d %s %s @ %.5f profit=%.2f",
                     pos.ticket, pos.type, symbol, result.price, pos.profit)
            beat("mt5", True,
                 f"Closed {symbol} ticket#{pos.ticket} profit={pos.profit:.2f}")

            upsert_position(pos.ticket, {
                "signal_id":   signal_id,
                "symbol":      symbol,
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
            closed += 1
        else:
            err = result.comment if result else str(mt5.last_error())
            log.error("Close FAILED ticket #%d: %s", pos.ticket, err)

    return {"type": "CLOSE", "symbol": symbol, "closed": closed}


# ── backward-compat alias ─────────────────────────────────────────────────────
def open_trade(signal_id: int, channel: str, parsed: dict) -> dict | None:
    """Legacy alias — routes through handle_signal."""
    return handle_signal(signal_id, channel, parsed)
