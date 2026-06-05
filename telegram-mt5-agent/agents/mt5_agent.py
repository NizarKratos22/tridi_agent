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

from database.db import fetch_recent_signals, upsert_position
from database.status import beat

load_dotenv()
log = logging.getLogger(__name__)

POLL_INTERVAL = 30  # seconds


class MT5Agent:
    def __init__(self):
        self.login = int(os.environ["MT5_LOGIN"])
        self.password = os.environ["MT5_PASSWORD"]
        self.server = os.environ["MT5_SERVER"]

    def connect(self) -> bool:
        if not mt5.initialize():
            beat("mt5", False, f"initialize() failed: {mt5.last_error()}")
            log.error("MT5 initialize() failed: %s", mt5.last_error())
            return False
        ok = mt5.login(self.login, self.password, self.server)
        if not ok:
            beat("mt5", False, f"login failed: {mt5.last_error()}")
            log.error("MT5 login failed: %s", mt5.last_error())
            return False
        info = mt5.account_info()
        beat("mt5", True, f"{info.name} | Balance: {info.balance:.2f}")
        log.info("MT5 connected — %s | Balance: %.2f", info.name, info.balance)
        return True

    def run(self):
        if not self.connect():
            beat("mt5", False, "Could not connect — check login/server in .env")
            return

        while True:
            try:
                self._sync_open()
                self._sync_closed()
                info = mt5.account_info()
                if info:
                    beat("mt5", True,
                         f"Balance: {info.balance:.2f} | Equity: {info.equity:.2f}")
            except Exception as exc:
                beat("mt5", False, str(exc))
                log.exception("MT5 sync error: %s", exc)
            time.sleep(POLL_INTERVAL)

    # ── internal ──────────────────────────────────────────────────────────────

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
