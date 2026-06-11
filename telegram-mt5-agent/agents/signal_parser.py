"""
Multilingual signal parser (EN / FR / AR) using Claude.

Every message is classified into one of 4 types:
  NEW_SIGNAL  — open a new trade
  UPDATE      — modify existing trade (move SL, partial close, etc.)
  CLOSE       — close the trade now
  IRRELEVANT  — ignore (news, commentary, ads, etc.)
"""
import json
import os
import re
import anthropic
from langdetect import detect, LangDetectException

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


SYSTEM_PROMPT = """You are a Forex/Crypto trading signal classifier and parser.
Messages can be in English, French, or Arabic. Analyse the message and return ONLY valid JSON.

Classify the message into one of these types:
- "NEW_SIGNAL"  → a fresh BUY or SELL entry signal
- "UPDATE"      → modifying an open trade (move SL to breakeven, partial TP hit, trail SL, etc.)
- "CLOSE"       → instructions to close / exit the trade now
- "IRRELEVANT"  → market commentary, news, ads, greetings, unrelated text

Return this JSON schema (all fields required, use null when not present):
{
  "type":    "NEW_SIGNAL" | "UPDATE" | "CLOSE" | "IRRELEVANT",
  "symbol":  "<e.g. XAUUSD, EURUSD, BTCUSD or null>",
  "action":  "BUY" | "SELL" | null,
  "entry":   <float or null>,
  "sl":      <new stop-loss float or null>,
  "tp1":     <take-profit-1 float or null>,
  "tp2":     <take-profit-2 float or null>,
  "tp3":     <take-profit-3 float or null>,
  "note":    "<short English summary of what the message says>"
}

Language hints:
  Arabic  : شراء/اشتري=BUY  بيع=SELL  أغلق/إغلاق=CLOSE  تحديث=UPDATE  وقف الخسارة=SL  هدف=TP
             "لا تزال الصفقة مفتوحة" / "الصفقة مفتوحة" = UPDATE (position still open)
             "أغلق الصفقة" / "اخرج الآن" = CLOSE
  French  : acheter/achat=BUY  vendre/vente=SELL  fermer/clôturer=CLOSE
             "position toujours ouverte" = UPDATE  "déplacez le SL" = UPDATE
  English : "still in trade" / "position open" / "move SL to BE" = UPDATE
             "close now" / "exit" / "take profit hit" (all TPs) = CLOSE

CRITICAL rules:
- If the channel says the trade is still open or gives an update — type = UPDATE, NOT NEW_SIGNAL
- Only use NEW_SIGNAL when a brand-new entry is being called for the first time
- Return ONLY the JSON object, no markdown, no extra text
"""


def detect_language(text: str) -> str:
    try:
        lang = detect(text)
        if lang == "ar":
            return "arabic"
        if lang == "fr":
            return "french"
        return "english"
    except LangDetectException:
        return "english"


# Fast keyword pre-filter — skip obvious non-trading messages without calling API
_TRADING_RE = re.compile(
    r"buy|sell|long|short|entry|شراء|بيع|اشتري|achat|vente|acheter|vendre|"
    r"xauusd|eurusd|gbpusd|usdjpy|btc|gold|forex|sl|tp|stop|target|"
    r"أغلق|إغلاق|fermer|close|exit|sortir|"
    r"صفقة|position|trade|signal|إشارة",
    re.IGNORECASE,
)


def parse_signal(message: str, channel: str = "", language: str | None = None) -> dict:
    """
    Classify and parse a Telegram message.

    Returns a dict with at least:
      type     : NEW_SIGNAL | UPDATE | CLOSE | IRRELEVANT
      language : arabic | french | english
      + symbol, action, entry, sl, tp1, tp2, tp3, note

    Args:
      channel  : if provided, injects that channel's learned vocabulary into
                 the prompt so Claude recognises its specific style.
      language : pre-detected language; if None it is detected here. Passing it
                 avoids a redundant langdetect pass when the caller already knows.
    """
    language = language or detect_language(message)

    # Skip obvious non-trading messages without burning API tokens
    if not _TRADING_RE.search(message):
        return {"type": "IRRELEVANT", "language": language, "note": "no trading keywords"}

    # Build prompt — inject channel vocabulary if available
    system = SYSTEM_PROMPT
    if channel:
        try:
            from agents.keyword_extractor import get_channel_context
            ctx = get_channel_context(channel)
            if ctx:
                system = SYSTEM_PROMPT + f"\n\n{ctx}"
        except Exception:
            pass   # never let vocabulary lookup break parsing

    client = _get_client()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=system,
        messages=[{"role": "user", "content": message}],
    )

    raw = response.content[0].text.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                return {"type": "IRRELEVANT", "language": language,
                        "note": "parse error"}
        else:
            return {"type": "IRRELEVANT", "language": language,
                    "note": "no JSON in response"}

    parsed["language"] = language
    # Normalise type field
    parsed.setdefault("type", "IRRELEVANT")
    return parsed
