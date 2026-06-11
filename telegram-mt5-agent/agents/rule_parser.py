"""
Rule-based signal parser  (MANUAL mode — no AI, no API key).

Uses regex + multilingual keyword tables + the channel's learned vocabulary
to turn a message into the same structured dict the Claude parser produces:

    {type, symbol, action, entry, sl, tp1, tp2, tp3, note, language}

Deterministic and instant — good for channels whose format you trust.
"""
import re

from agents.signal_parser import detect_language

# ── symbol aliases → broker symbol ─────────────────────────────────────────────
SYMBOL_ALIASES = {
    # Gold — include common Arabic spelling variants
    "GOLD": "XAUUSD", "GLD": "XAUUSD", "XAU": "XAUUSD", "XAUUSD": "XAUUSD",
    "ذهب": "XAUUSD", "الذهب": "XAUUSD", "دهب": "XAUUSD", "الدهب": "XAUUSD",
    "SILVER": "XAGUSD", "XAG": "XAGUSD", "XAGUSD": "XAGUSD", "فضة": "XAGUSD",
    "BITCOIN": "BTCUSD", "BTC": "BTCUSD", "BTCUSD": "BTCUSD",
    "ETHEREUM": "ETHUSD", "ETH": "ETHUSD", "ETHUSD": "ETHUSD",
    "OIL": "USOIL", "WTI": "USOIL", "USOIL": "USOIL", "CRUDE": "USOIL",
    "NASDAQ": "NAS100", "NAS100": "NAS100", "US100": "NAS100",
    "DOW": "US30", "US30": "US30", "DJ30": "US30",
    "SP500": "US500", "SPX": "US500", "US500": "US500",
    "DAX": "GER40", "GER40": "GER40", "DE40": "GER40",
}

_CURRENCIES = {"EUR", "USD", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD",
               "XAU", "XAG", "BTC", "ETH"}

# ── action keywords (multilingual) ─────────────────────────────────────────────
BUY_KW  = ("buy", "long", "شراء", "اشتري", "achat", "acheter")
SELL_KW = ("sell", "short", "بيع", "vente", "vendre")

CLOSE_KW = ("close", "exit", "closed", "close now", "close position", "take profit",
            "tp hit", "all tp", "stop position", "stop the trade", "stop trade",
            "stop out", "stop now", "cancel trade", "cancel position",
            "أغلق", "اغلق", "إغلاق", "اقفل", "اقفال", "خروج", "اخرج", "اخرج الان",
            "أوقف", "اوقف", "ايقاف", "إيقاف",
            "ferm", "clôtur", "clotur", "sortir", "sortez", "fermez",
            "arrêt", "arretez", "arrêtez", "stoppez")  # ferm* = fermer/fermez

# break-even = move SL to entry (no explicit price). Specific phrases only,
# to avoid matching the bare word "be".
BREAKEVEN_KW = ("break even", "breakeven", "break-even", "b/e", "to entry",
                "sl to entry", "sl to be", "secure", "risk free", "risk-free",
                "التعادل", "نقطة الدخول", "وقف على الدخول", "نقل الوقف للدخول",
                "point mort", "seuil", "sécuriser", "sans risque")

UPDATE_KW = ("move sl", "move stop", "trail", "trailing", "adjust sl", "new sl",
             "تحديث", "نقل الوقف", "حرك الوقف", "عدل الوقف",
             "position ouverte", "toujours ouverte", "déplacez le sl",
             "still open", "still in", "running", "hold")

# ── number-extraction patterns ─────────────────────────────────────────────────
_NUM = r"(\d+(?:[.,]\d+)?)"
# [^\d\n]{0,8} tolerates emoji/symbols between keyword and price — channels
# write "SL🛑4113" or "TP🏆4090" and plain \s* missed those entirely.
_SL_RE = re.compile(
    r"(?:sl|s/l|stop\s*loss|stop|وقف(?:\s*الخسارة)?)"
    r"[^\d\n]{0,8}" + _NUM, re.IGNORECASE)
# [1-3]? matches an attached index ("TP1") but never the leading digit of a
# spaced price ("TP 4329" → 4329, not 329); regex backtracks for forex (TP 1.0850).
_TP_RE = re.compile(
    r"(?:tp\s*[1-3]?|take\s*profit|target|هدف|objectif)"
    r"[^\d\n]{0,8}" + _NUM, re.IGNORECASE)
# allow up to 15 non-digit chars between the action word and the price,
# so "buy now 4327" and "بيع ذهب الان 4327" both capture the entry.
_ENTRY_RE = re.compile(
    r"(?:buy|sell|long|short|achat|vente|acheter|vendre|"
    r"شراء|اشتري|بيع|now|entry|@|دخول)"
    r"[^\d\n]{0,15}" + _NUM, re.IGNORECASE)
# lot/volume — two forms, keyword-first takes priority so "TP 4094 lot 0.05"
# resolves to 0.05 (not the "4094 lot" accidental pairing):
#   kw-first : "lot 0.02", "volume: 0.1", "حجم 0.02"
#   num-first: "0.5 lots", "0.02 لوت"
_LOT_KW_RE  = re.compile(r"(?:lot[s]?|volume|vol|حجم|لوت)\s*[:=]?\s*" + _NUM, re.IGNORECASE)
_LOT_NUM_RE = re.compile(_NUM + r"\s*(?:lot[s]?|لوت)\b", re.IGNORECASE)


def _norm(n: str) -> float:
    return float(n.replace(",", "."))


def _find_symbol(text: str) -> str | None:
    upper = text.upper()
    # 1) explicit aliases (Gold, BTC, …)
    for alias, sym in SYMBOL_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", upper):
            return sym
    # 2) forex pairs like EUR/USD or EURUSD
    m = re.search(r"\b([A-Z]{3})\s*/?\s*([A-Z]{3})\b", upper)
    if m and m.group(1) in _CURRENCIES and m.group(2) in _CURRENCIES:
        return m.group(1) + m.group(2)
    return None


def _has_any(text_low: str, words) -> bool:
    return any(w in text_low for w in words)


def parse_signal_rule(message: str, channel: str = "",
                      language: str | None = None) -> dict:
    """Rule-based parse. Returns the standard signal dict."""
    language = language or detect_language(message)
    low = message.lower()

    is_buy   = _has_any(low, BUY_KW)
    is_sell  = _has_any(low, SELL_KW)
    has_close     = _has_any(low, CLOSE_KW)
    has_breakeven = _has_any(low, BREAKEVEN_KW)
    has_update    = _has_any(low, UPDATE_KW)
    action = "BUY" if is_buy else ("SELL" if is_sell else None)

    symbol = _find_symbol(message)

    # ── numbers (needed before classification for the SL-only UPDATE case) ──────
    sl = None
    m = _SL_RE.search(message)
    if m:
        sl = _norm(m.group(1))

    tps = []
    for m in _TP_RE.finditer(message):
        v = _norm(m.group(1))
        if v not in tps:
            tps.append(v)
    tp1, tp2, tp3 = (tps + [None, None, None])[:3]

    entry = None
    m = _ENTRY_RE.search(message)
    if m:
        entry = _norm(m.group(1))

    lot = None
    m = _LOT_KW_RE.search(message)
    if m:
        lot = _norm(m.group(1))
    else:
        m = _LOT_NUM_RE.search(message)
        if m:
            lot = _norm(m.group(1))

    # ── classify type ──────────────────────────────────────────────────────────
    # NEW_SIGNAL strictly requires BOTH symbol AND action (avoids false positives
    # on fan comments). CLOSE / UPDATE / break-even are command words and may omit
    # the symbol — they then apply to the channel's currently-open position(s).
    # An explicit SL change with no buy/sell is also an UPDATE.
    sl_only_update = (sl is not None) and not action and not has_close

    if action and symbol:
        sig_type = "NEW_SIGNAL"
    elif has_close:
        sig_type = "CLOSE"
    elif has_breakeven or has_update or sl_only_update:
        sig_type = "UPDATE"
    else:
        vocab_type = _consult_vocabulary(channel, low)
        sig_type = vocab_type if (vocab_type in ("UPDATE", "CLOSE") and symbol) else "IRRELEVANT"

    return {
        "type": sig_type,
        "symbol": symbol,
        "action": action,
        "entry": entry,
        "sl": sl,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "lot": lot,
        "breakeven": has_breakeven,
        "note": f"rule-parsed ({sig_type.lower()})"
                + (" · break-even" if has_breakeven else ""),
        "language": language,
    }


def _consult_vocabulary(channel: str, text_low: str) -> str:
    """
    Fall back to the channel's LEARNED keywords: if the message contains a token
    this channel has historically used for a given type, use that.
    """
    if not channel:
        return "IRRELEVANT"
    try:
        from database.db import fetch_channel_keywords
        for row in fetch_channel_keywords(channel, limit=80):
            kw = (row["keyword"] or "").lower()
            if kw and len(kw) >= 3 and kw in text_low:
                return row["msg_type"] or "IRRELEVANT"
    except Exception:
        pass
    return "IRRELEVANT"
