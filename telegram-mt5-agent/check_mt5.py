"""
MetaTrader 5 connection diagnostic.
Run:  python check_mt5.py
Tells you exactly why MT5 won't connect. No Telegram, no interactive input.
"""
import os
from dotenv import load_dotenv
import MetaTrader5 as mt5

load_dotenv()

LOGIN    = os.getenv("MT5_LOGIN", "").split("#")[0].strip()
PASSWORD = os.getenv("MT5_PASSWORD", "").split("#")[0].strip()
SERVER   = os.getenv("MT5_SERVER", "").split("#")[0].strip()

print("=" * 64)
print("  MetaTrader 5 - connection diagnostic")
print("=" * 64)
print(f"  .env LOGIN   : {LOGIN!r}")
print(f"  .env SERVER  : {SERVER!r}")
print(f"  .env PASSWORD: {'*' * len(PASSWORD)} ({len(PASSWORD)} chars)")
print(f"  MT5 package  : v{mt5.__version__}")
print("-" * 64)

# ── 1. plain initialize ───────────────────────────────────────────────────────
print("\n[1] mt5.initialize()  - attach to the running terminal")
ok1 = mt5.initialize()
if ok1:
    print("    OK - terminal attached")
    ti = mt5.terminal_info()
    if ti:
        print(f"    terminal name : {ti.name}")
        print(f"    path          : {ti.path}")
        print(f"    connected     : {ti.connected}")
        print(f"    trade_allowed : {ti.trade_allowed}  (Algo Trading button)")
    ai = mt5.account_info()
    if ai:
        print(f"    logged in as  : #{ai.login} on {ai.server}")
    else:
        print("    *** terminal is NOT logged into any account ***")
else:
    print(f"    FAILED: {mt5.last_error()}")

# ── 2. initialize WITH credentials (launches/logs in if path is known) ────────
print("\n[2] mt5.initialize(login, password, server)  - direct login")
ok2 = False
try:
    ok2 = mt5.initialize(login=int(LOGIN), password=PASSWORD, server=SERVER)
    print(f"    result: {ok2}   last_error: {mt5.last_error()}")
except ValueError:
    print(f"    LOGIN is not numeric: {LOGIN!r}")

# ── 3. explicit login ─────────────────────────────────────────────────────────
print("\n[3] mt5.login(login, password, server)  - switch account")
ok3 = False
try:
    ok3 = mt5.login(int(LOGIN), PASSWORD, SERVER)
    print(f"    result: {ok3}   last_error: {mt5.last_error()}")
except ValueError:
    print(f"    LOGIN is not numeric: {LOGIN!r}")

# ── verdict ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 64)
ai = mt5.account_info()
if ai:
    print("  RESULT: CONNECTED")
    print(f"    name    : {ai.name}")
    print(f"    login   : #{ai.login}")
    print(f"    server  : {ai.server}")
    print(f"    balance : {ai.balance:.2f} {ai.currency}")
    print("    -> MT5 is good. The agent will connect.")
else:
    print("  RESULT: NOT CONNECTED")
    print("  Most likely cause: the MT5 DESKTOP terminal is open but NOT")
    print("  logged into a trade account (bottom-right must say 'connected'")
    print("  with your account number, not 'No connection').")
    print("")
    print("  FIX:")
    print("   1. In MetaTrader 5:  File > Login to Trade Account")
    print(f"   2. Login:  {LOGIN}   Password: (your pw)   Server: {SERVER}")
    print("   3. If that login FAILS, the demo account expired -> create a")
    print("      new one (File > Open an Account) and update .env.")
    print("   4. Make sure the bottom-right status bar shows a live ping (kb/s).")

mt5.shutdown()
print("=" * 64)
