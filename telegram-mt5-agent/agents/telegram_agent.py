"""
Listens to configured Telegram channels and routes new messages
through the signal parser → trade executor → database.

Signal types handled:
  NEW_SIGNAL  → open trade in MT5 (with duplicate guard)
  UPDATE      → move SL on existing position
  CLOSE       → close open position
  IRRELEVANT  → logged and ignored
"""
import asyncio
import os
import logging
from telethon import TelegramClient, events
from dotenv import load_dotenv

from agents.signal_parser import parse_signal
from agents.trade_executor import handle_signal
from agents.keyword_extractor import extract_and_store
from database.db import insert_signal, archive_message
from database.status import beat

load_dotenv()
log = logging.getLogger(__name__)


def _channels() -> list:
    """Return channel identifiers — username strings or integer IDs."""
    raw = [
        v.strip() for k, v in sorted(os.environ.items())
        if k.startswith("CHANNEL_") and v.strip()
    ]
    result = []
    for v in raw:
        try:
            result.append(int(v))
        except ValueError:
            result.append(v)
    return result


class TelegramAgent:
    def __init__(self):
        self.api_id   = int(os.environ["TELEGRAM_API_ID"])
        self.api_hash = os.environ["TELEGRAM_API_HASH"]
        self.phone    = os.environ["TELEGRAM_PHONE"].split("#")[0].strip()
        self.channels = _channels()
        self.client   = TelegramClient("session_tridi", self.api_id, self.api_hash)

    async def start(self):
        try:
            await self.client.start(phone=self.phone)
            beat("telegram", True, f"Watching {len(self.channels)} channel(s)")
            log.info("Telegram connected. Watching: %s", self.channels)
        except Exception as exc:
            beat("telegram", False, str(exc))
            raise

        @self.client.on(events.NewMessage(chats=self.channels))
        async def handler(event):
            await self._handle(event)

        await self.client.run_until_disconnected()
        beat("telegram", False, "Disconnected")

    async def _handle(self, event):
        msg = event.message.message
        if not msg:
            return

        chat = await event.get_chat()
        channel_name = getattr(chat, "username", None) or str(chat.id)

        # ── archive raw message (always) ──────────────────────────────────
        # detect language first so archive_message gets it right
        from agents.signal_parser import detect_language
        language_hint = detect_language(msg)
        archive_message(channel_name, language_hint, msg)

        # ── parse & classify (with channel vocabulary context) ────────────
        parsed   = parse_signal(msg, channel=channel_name)
        sig_type = parsed.get("type", "IRRELEVANT")
        language = parsed.pop("language", "english")
        note     = parsed.get("note", "")

        # ── learn keywords from this message ──────────────────────────────
        extract_and_store(channel_name, language, msg, parsed)

        log.info("[%s | %s | %s] %s — %s %s",
                 channel_name, language, sig_type,
                 note, parsed.get("action", ""), parsed.get("symbol", ""))

        # ── always save to DB (except pure irrelevant noise) ──────────────
        if sig_type != "IRRELEVANT" or parsed.get("symbol"):
            signal_id = insert_signal(
                channel=channel_name,
                language=language,
                raw_message=msg,
                parsed=parsed,
            )
            beat("telegram", True,
                 f"[{sig_type}] {parsed.get('action','')} {parsed.get('symbol','')} "
                 f"from @{channel_name}")
        else:
            log.debug("[%s] Irrelevant message skipped", channel_name)
            return

        # ── route to MT5 ──────────────────────────────────────────────────
        result = handle_signal(
            signal_id=signal_id,
            channel=channel_name,
            parsed=parsed,
        )

        if result is None:
            return

        if result.get("dry_run"):
            log.info("[DRY-RUN] %s %s — AUTO_TRADE=false",
                     sig_type, parsed.get("symbol"))
            return

        # Log outcome per type
        if sig_type == "NEW_SIGNAL":
            if result.get("skipped"):
                log.info("⚠️  Skipped — already have open %s on %s",
                         result.get("action"), result.get("symbol"))
            else:
                log.info("✅ Trade opened → ticket #%s", result.get("order"))

        elif sig_type == "UPDATE":
            log.info("✏️  Updated %d position(s) on %s",
                     result.get("updated", 0), result.get("symbol"))

        elif sig_type == "CLOSE":
            log.info("🔒 Closed %d position(s) on %s",
                     result.get("closed", 0), result.get("symbol"))
