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

from agents.signal_parser import parse_signal, detect_language
from agents.rule_parser import parse_signal_rule
from agents.trade_executor import handle_signal
from agents.keyword_extractor import extract_and_store
from database.db import insert_signal, archive_message, get_setting
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
        self.api_id   = int(os.environ["TELEGRAM_API_ID"].split("#")[0].strip())
        self.api_hash = os.environ["TELEGRAM_API_HASH"].split("#")[0].strip()
        self.phone    = os.environ["TELEGRAM_PHONE"].split("#")[0].strip()
        self.channels = _channels()
        # absolute path → shares the SAME session file the dashboard signs into
        session = os.path.join(os.path.dirname(os.path.dirname(__file__)), "session_tridi")
        self.client   = TelegramClient(session, self.api_id, self.api_hash)

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

    def _parse(self, msg: str, channel: str, language: str) -> dict:
        """
        Route to the parser chosen by the live 'parse_mode' setting.
          manual  → rule/keyword parser (deterministic, free)
          agentic → Claude parser; falls back to rules if it errors (e.g. no key)
        """
        mode = (get_setting("parse_mode", "manual") or "manual").lower()
        if mode == "agentic":
            try:
                return parse_signal(msg, channel=channel, language=language)
            except Exception as exc:
                log.warning("Agentic parse failed (%s) — falling back to rules", exc)
                beat("telegram", True, f"AI parse failed, used rules: {exc}")
                return parse_signal_rule(msg, channel=channel, language=language)
        return parse_signal_rule(msg, channel=channel, language=language)

    async def _handle(self, event):
        msg = event.message.message
        if not msg:
            return

        chat = await event.get_chat()
        channel_name = getattr(chat, "username", None) or str(chat.id)

        # ── detect language ONCE, reuse for archive + parse ───────────────
        language = detect_language(msg)
        archive_message(channel_name, language, msg)

        # ── parse & classify — MODE decides which parser runs ─────────────
        #   manual   → deterministic rule/keyword parser (no API key)
        #   agentic  → Claude AI parser (needs ANTHROPIC_API_KEY)
        parsed = self._parse(msg, channel_name, language)
        sig_type = parsed.get("type", "IRRELEVANT")
        parsed.pop("language", None)          # already captured above
        note     = parsed.get("note", "")

        # ── learn keywords from this message ──────────────────────────────
        extract_and_store(channel_name, language, msg, parsed)

        log.info("[%s | %s | %s] %s — %s %s",
                 channel_name, language, sig_type,
                 note, parsed.get("action", ""), parsed.get("symbol", ""))

        # ── save to signals table only when actionable ───────────────────
        # IRRELEVANT messages are already archived in raw_messages — no need
        # to also clutter the signals table with fan comments.
        if sig_type != "IRRELEVANT" and (parsed.get("symbol") or parsed.get("action")):
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
            if result.get("enriched"):
                log.info("📌 Follow-up SL/TP applied to %d open position(s) on %s",
                         result.get("enriched"), result.get("symbol"))
            elif result.get("skipped"):
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
