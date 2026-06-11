"""
Telegram sign-in helpers for the dashboard.

The dashboard cannot use telethon's interactive console prompt, so this module
exposes small synchronous functions the UI can call step by step:

    check_authorized()        -> (bool, name|None)
    request_code()            -> {"ok":bool, "hash":str|None, "error":str|None}
    submit_code(code, hash)   -> {"ok":bool, "need_password":bool, "error":str|None, "name":str|None}
    submit_password(password) -> {"ok":bool, "error":str|None, "name":str|None}

Each call opens a short-lived connection on a fresh event loop and reuses the
shared `session_tridi` session file, so once sign-in succeeds the session is
saved and `main.py` can run without ever prompting for a code again.
"""
import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import (SessionPasswordNeededError, PhoneCodeInvalidError,
                             PhoneCodeExpiredError, PhoneNumberInvalidError)

load_dotenv()


def _clean(key: str) -> str:
    return os.environ.get(key, "").split("#")[0].strip()


API_ID   = int(_clean("TELEGRAM_API_ID") or "0")
API_HASH = _clean("TELEGRAM_API_HASH")
PHONE    = _clean("TELEGRAM_PHONE")

# Absolute path so the dashboard and main.py always share the SAME session file
ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION = os.path.join(ROOT, "session_tridi")


def _run(coro):
    """Run an async coroutine on a fresh event loop (safe inside Streamlit)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _new_client() -> TelegramClient:
    return TelegramClient(SESSION, API_ID, API_HASH)


# ── public API ────────────────────────────────────────────────────────────────

def credentials_present() -> bool:
    return bool(API_ID and API_HASH and PHONE)


def check_authorized():
    """Return (is_authorized, display_name)."""
    async def _c():
        client = _new_client()
        await client.connect()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                return True, (me.first_name or me.username or str(me.id))
            return False, None
        finally:
            await client.disconnect()
    try:
        return _run(_c())
    except Exception:
        return False, None


def request_code():
    """Ask Telegram to send the login code to the user's app."""
    async def _c():
        client = _new_client()
        await client.connect()
        try:
            if await client.is_user_authorized():
                return {"ok": True, "hash": None, "already": True, "error": None}
            sent = await client.send_code_request(PHONE)
            return {"ok": True, "hash": sent.phone_code_hash, "already": False, "error": None}
        finally:
            await client.disconnect()
    try:
        return _run(_c())
    except PhoneNumberInvalidError:
        return {"ok": False, "hash": None, "error": "Phone number is invalid — check TELEGRAM_PHONE in .env"}
    except Exception as e:
        return {"ok": False, "hash": None, "error": str(e)}


def submit_code(code: str, phone_code_hash: str):
    """Sign in with the code the user typed."""
    async def _c():
        client = _new_client()
        await client.connect()
        try:
            await client.sign_in(phone=PHONE, code=code.strip(),
                                 phone_code_hash=phone_code_hash)
            me = await client.get_me()
            return {"ok": True, "need_password": False, "error": None,
                    "name": (me.first_name or me.username or str(me.id))}
        except SessionPasswordNeededError:
            return {"ok": False, "need_password": True, "error": None, "name": None}
        except PhoneCodeInvalidError:
            return {"ok": False, "need_password": False,
                    "error": "Wrong code — check and try again.", "name": None}
        except PhoneCodeExpiredError:
            return {"ok": False, "need_password": False,
                    "error": "Code expired — request a new one.", "name": None}
        finally:
            await client.disconnect()
    try:
        return _run(_c())
    except Exception as e:
        return {"ok": False, "need_password": False, "error": str(e), "name": None}


def submit_password(password: str):
    """Complete sign-in for accounts with 2FA (cloud password) enabled."""
    async def _c():
        client = _new_client()
        await client.connect()
        try:
            await client.sign_in(password=password)
            me = await client.get_me()
            return {"ok": True, "error": None,
                    "name": (me.first_name or me.username or str(me.id))}
        finally:
            await client.disconnect()
    try:
        return _run(_c())
    except Exception as e:
        return {"ok": False, "error": "Wrong 2FA password — try again." , "name": None}
