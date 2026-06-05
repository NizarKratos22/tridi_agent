"""
Keyword extractor — pulls meaningful tokens from a raw message
after it has been classified by the signal parser.

Stores them in channel_keywords so the parser gets smarter over time.

Strategy:
  1. Tokenise the message (words + emoji clusters)
  2. Remove pure numbers and very short tokens
  3. Keep tokens that are near the signal's action / type words
  4. Store with msg_type + action label
"""
import re
from database.db import store_keywords, build_channel_context

# ── tokens to always ignore ───────────────────────────────────────────────────
_STOP = {
    # English
    "the", "a", "an", "is", "in", "on", "at", "to", "for", "of",
    "and", "or", "but", "with", "this", "that", "it", "we", "our",
    # French
    "le", "la", "les", "de", "du", "des", "un", "une", "et", "ou",
    "en", "au", "aux", "ce", "se", "sur", "dans", "par", "est",
    # Arabic common particles
    "في", "من", "على", "إلى", "مع", "هذا", "هذه", "هو", "هي",
    "و", "أو", "لا", "ما", "كان", "عند",
    # Misc
    "please", "now", "today", "🔔", "📢", "⚠️",
}

# ── regex tokeniser ───────────────────────────────────────────────────────────
# Matches: Arabic words, Latin words (including accented), emoji sequences, symbols
_TOKEN_RE = re.compile(
    r"[\U0001F300-\U0001FFFF]+"     # emoji
    r"|[؀-ۿ]+"            # Arabic
    r"|[a-zA-ZÀ-ÿ]{2,}"            # Latin (min 2 chars)
    r"|[A-Z]{2,6}/[A-Z]{2,6}"      # pairs like EUR/USD
    r"|[A-Z]{4,6}(?:USD|JPY|GBP)"  # symbols like XAUUSD
)

_NUMBER_RE = re.compile(r"^\d[\d.,]*$")


def _tokenise(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall(text)
    result = []
    for t in tokens:
        t = t.strip()
        if not t:
            continue
        if _NUMBER_RE.match(t):
            continue                # skip bare numbers
        if t.lower() in _STOP:
            continue
        result.append(t.lower())
    return result


def extract_and_store(channel: str, language: str,
                      raw_message: str, parsed: dict):
    """
    Extract keyword tokens from raw_message and persist them.
    Called after every successful parse (any msg_type).
    """
    msg_type = parsed.get("type", "IRRELEVANT")
    action   = parsed.get("action")          # BUY | SELL | None

    tokens = _tokenise(raw_message)

    # Deduplicate while preserving order
    seen = set()
    unique_tokens = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            unique_tokens.append(t)

    # Skip pure-noise messages — nothing useful to learn
    if msg_type == "IRRELEVANT" and not parsed.get("symbol"):
        return

    store_keywords(
        channel=channel,
        language=language,
        keywords=unique_tokens,
        msg_type=msg_type,
        action=action,
    )


def get_channel_context(channel: str) -> str:
    """
    Return a learned-vocabulary string to inject into the parser prompt.
    Empty string if channel is new (no history yet).
    """
    return build_channel_context(channel)
