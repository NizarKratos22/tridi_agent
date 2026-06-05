"""
Entry point — runs Telegram agent and MT5 agent concurrently.

Usage:
  python main.py            # run both agents
  python main.py --tg-only  # Telegram only (no MT5)
  python main.py --mt5-only # MT5 sync only

Dashboard (separate terminal):
  streamlit run dashboard/app.py
"""
import asyncio
import logging
import sys
import threading
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

from database.db import init_db
from agents.telegram_agent import TelegramAgent
from agents.mt5_agent import MT5Agent


def run_mt5():
    agent = MT5Agent()
    agent.run()


async def main(mode: str):
    init_db()

    if mode in ("both", "mt5-only"):
        t = threading.Thread(target=run_mt5, daemon=True)
        t.start()

    if mode in ("both", "tg-only"):
        tg = TelegramAgent()
        await tg.start()
    else:
        # Just keep MT5 thread alive
        import time
        while True:
            time.sleep(60)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--both"
    mode_map = {
        "--both":     "both",
        "--tg-only":  "tg-only",
        "--mt5-only": "mt5-only",
    }
    mode = mode_map.get(arg, "both")
    asyncio.run(main(mode))
