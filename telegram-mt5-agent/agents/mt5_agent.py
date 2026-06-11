"""
Polls MetaTrader 5 for open/closed positions and syncs them to the local DB.
Tries to match positions to signals by symbol + action.
"""
import logging
import os
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5
from dotenv import load_dotenv

from database.db import (fetch_recent_signals, upsert_position,
                         fetch_open_db_positions, mark_position_closed)
from database.status import beat

load_dotenv()
log = logging.getLogger(__name__)

POLL_INTERVAL = 30  # seconds


def _clean(key: str) -> str:
    """Read an env var, stripping any inline `# comment` and whitespace."""
    return os.environ.get(key, "").split("#")[0].strip()


class MT5Agent:
    def __init__(self):
        self.login = int(_clean("MT5_LOGIN"))
        self.password = _clean("MT5_PASSWORD")
        self.server = _clean("MT5_SERVER")

    def connect(self) -> bool:
        # Strategy 1 — initialise WITH credentials (most reliable; lets the
        # package log the running terminal into the right account directly).
        ok = mt5.initialize(login=self.login, password=self.password,
                            server=self.server)

        # Strategy 2 — plain initialise (attach to running terminal), then login.
        if not ok:
            log.warning("initialize(with creds) failed: %s — trying attach+login",
                        mt5.last_error())
            if mt5.initialize():
                ok = mt5.login(self.login, self.password, self.server)

        if not ok:
            err = mt5.last_error()
            msg = self._explain_error(err)
            beat("mt5", False, msg)
            log.error("MT5 connect failed: %s — %s", err, msg)
            return False

        info = mt5.account_info()
        if info is None:
            beat("mt5", False, "Connected but account_info() is None — login mismatch")
            log.error("account_info() returned None after connect")
            return False

        beat("mt5", True, f"{info.name} | Balance: {info.balance:.2f} {info.currency}")
        log.info("MT5 connected — %s (#%d) | Balance: %.2f %s",
                 info.name, info.login, info.balance, info.currency)
        return True

    @staticmethod
    def _explain_error(err) -> str:
        """Turn an MT5 error tuple into a human-readable hint."""
        code = err[0] if isinstance(err, (tuple, list)) else err
        hints = {
            -6:     "Authorization failed — wrong login/password/server, "
                    "or the demo account expired. Verify you can log in manually "
                    "in the MT5 terminal (File ▸ Login to Trade Account).",
            -10003: "Terminal not found — open the MetaTrader 5 desktop app first.",
            -10004: "No connection to trade server — check the server name.",
        }
        return hints.get(code, f"MT5 error {err}")

    def run(self):
        """Main loop — auto-reconnects on any failure, runs forever."""
        RECONNECT_DELAY = 15   # seconds to wait before retry after a crash

        while True:
            # ── connect / reconnect ───────────────────────────────────────
            if not self.connect():
                beat("mt5", False, "Could not connect — retrying in 15 s")
                log.warning("MT5 connect failed, retrying in %ds…", RECONNECT_DELAY)
                time.sleep(RECONNECT_DELAY)
                continue

            # ── sync loop ─────────────────────────────────────────────────
            while True:
                try:
                    # detect silent disconnect (terminal closed / network drop)
                    info = mt5.account_info()
                    if info is None:
                        log.warning("MT5 account_info() returned None — reconnecting…")
                        beat("mt5", False, "Lost connection — reconnecting")
                        mt5.shutdown()
                        break  # exit inner loop → reconnect

                    self._sync_open()
                    self._sync_closed()
                    self._reconcile()
                    beat("mt5", True,
                         f"Balance: {info.balance:.2f} | Equity: {info.equity:.2f}")

                except Exception as exc:
                    beat("mt5", False, f"Sync error: {exc}")
                    log.exception("MT5 sync error — will reconnect: %s", exc)
                    mt5.shutdown()
                    break  # exit inner loop → reconnect

                time.sleep(POLL_INTERVAL)

    # ── internal ──────────────────────────────────────────────────────────────

    def _reconcile(self):
        """
        Close any DB row whose ticket no longer exists in MT5 (SL/TP hit,
        manual close in the terminal, etc.) so the dashboard never shows
        phantom 'open' positions.
        """
        live = {p.ticket for p in (mt5.positions_get() or [])}
        for r in fetch_open_db_positions():
            ticket = r["ticket"]
            if not ticket or ticket in live:
                continue
            profit = close_price = None
            for d in (mt5.history_deals_get(position=ticket) or []):
                if d.entry == mt5.DEAL_ENTRY_OUT:
                    profit, close_price = d.profit, d.price
            mark_position_closed(ticket, profit, close_price)
            log.info("Reconciled: ticket #%d closed in MT5 (profit=%s)",
                     ticket, profit)

    def _signal_map(self) -> dict:
        """Build symbol→signal_id map from recent signals for matching."""
        rows = fetch_recent_signals(limit=100)
        mapping: dict[tuple, int] = {}
        for r in rows:
            if r["symbol"] and r["action"]:
                key = (r["symbol"].upper(), r["action"].upper())
                mapping.setdefault(key, (r["id"], r["channel"]))
        return mapping

    def _sync_open(self):
        positions = mt5.positions_get()
        if positions is None:
            return

        sig_map = self._signal_map()

        for pos in positions:
            action = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
            key = (pos.symbol.upper(), action)
            sig_id, channel = sig_map.get(key, (None, "unknown"))

            opened_at = datetime.fromtimestamp(
                pos.time, tz=timezone.utc
            ).isoformat()

            upsert_position(pos.ticket, {
                "signal_id": sig_id,
                "symbol": pos.symbol,
                "action": action,
                "open_price": pos.price_open,
                "sl": pos.sl,
                "tp": pos.tp,
                "lot": pos.volume,
                "profit": pos.profit,
                "status": "open",
                "channel": channel,
                "opened_at": opened_at,
            })

    def _sync_closed(self):
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        deals = mt5.history_deals_get(now - timedelta(days=30), now)
        if deals is None:
            return

        sig_map = self._signal_map()

        for deal in deals:
            if deal.entry != mt5.DEAL_ENTRY_OUT:
                continue  # only closing deals

            action = "BUY" if deal.type == mt5.DEAL_TYPE_BUY else "SELL"
            key = (deal.symbol.upper(), action)
            sig_id, channel = sig_map.get(key, (None, "unknown"))

            closed_at = datetime.fromtimestamp(
                deal.time, tz=timezone.utc
            ).isoformat()

            upsert_position(deal.position_id, {
                "signal_id": sig_id,
                "symbol": deal.symbol,
                "action": action,
                "open_price": deal.price,
                "sl": None,
                "tp": None,
                "lot": deal.volume,
                "profit": deal.profit,
                "status": "closed",
                "channel": channel,
                "opened_at": closed_at,
                "closed_at": closed_at,
                "close_price": deal.price,
            })
