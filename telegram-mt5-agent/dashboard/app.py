"""
Tridi Signal Dashboard
======================
Telegram → MetaTrader 5 live position tracker.

Run:
    streamlit run dashboard/app.py

Auto-refresh:
    The live section is wrapped in an `st.fragment(run_every=...)`. Streamlit
    reruns ONLY that fragment over the existing websocket every N seconds, so
    the data updates in place without a full browser page reload (scroll
    position, active tab and filter selections are all preserved).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import plotly.express as px
import streamlit as st
from datetime import datetime, timezone

from database.db import (init_db, get_conn, fetch_all_positions,
                         fetch_channel_stats, fetch_recent_signals,
                         fetch_all_channel_vocabularies, fetch_raw_message_count,
                         fetch_recent_raw_messages,
                         register_channel, set_channel_trade_enabled, fetch_channel_config,
                         get_setting, set_setting)
from database.status import read_all as read_status
from agents import tg_login
from agents.trade_executor import (close_position_by_ticket, close_all_positions,
                                   breakeven_all_positions, run_manual_command)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_configured_channels() -> list[str]:
    """Channel usernames/IDs from .env (CHANNEL_1..N)."""
    return [
        v.split("#")[0].strip()
        for k, v in sorted(os.environ.items())
        if k.startswith("CHANNEL_") and v.split("#")[0].strip()
    ]

# How often the live fragment re-queries the DB and re-renders (seconds).
REFRESH_SECONDS = 30
# A service is considered "stale" if its last heartbeat is older than this.
HEARTBEAT_TIMEOUT = 180

# ── colour palette (single source of truth) ───────────────────────────────────
BG        = "#080c14"
PANEL     = "#0d1117"
BORDER    = "#1c2128"
TEXT      = "#cdd9e5"
MUTED     = "#768390"
DIM       = "#444c56"
GREEN     = "#3fb950"
RED       = "#f85149"
BLUE      = "#4493f8"

# ══════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG + STATIC STYLES  (run once per full page load)
# ══════════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="Tridi · Signal Dashboard",
    page_icon="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path fill='%234493f8' d='M3 3v18h18V3H3zm16 16H5V5h14v14zM7 17l3-4 2 3 3-5 4 6H7z'/></svg>",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(f"""
<link rel="stylesheet"
  href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css"
  crossorigin="anonymous" referrerpolicy="no-referrer"/>

<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

/* hide Streamlit chrome (Deploy button, menu, footer, status widget)
   — but KEEP the sidebar open/close controls alive */
#MainMenu, footer,
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
[data-testid="manage-app-button"],
.stDeployButton {{ display: none !important; }}

/* header: strip the bar itself but keep it as the home of the sidebar
   expand arrow (hiding the whole header made the sidebar impossible to reopen) */
header[data-testid="stHeader"] {{
    background: transparent !important;
    box-shadow: none !important;
    height: 2.4rem !important;
}}

/* « collapse button inside the sidebar */
[data-testid="stSidebarCollapseButton"] {{ display: flex !important; }}
[data-testid="stSidebarCollapseButton"] button {{ color: {MUTED} !important; }}

/* » expand pill shown when the sidebar is closed (all known testids) */
[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"],
[data-testid="stExpandSidebarButton"] {{
    display: flex !important;
    position: fixed !important;
    top: 12px !important; left: 12px !important;
    background: {PANEL} !important;
    border: 1px solid {BLUE} !important;
    border-radius: 8px !important;
    padding: 4px 8px !important;
    z-index: 9999 !important;
}}
[data-testid="stSidebarCollapsedControl"] button,
[data-testid="collapsedControl"] button,
[data-testid="stExpandSidebarButton"] button {{ color: {BLUE} !important; }}

.stApp, [data-testid="stAppViewContainer"] {{
    background: {BG} !important;
    font-family: 'Inter', sans-serif !important;
    color: {TEXT} !important;
}}
[data-testid="stSidebar"] {{
    background: {PANEL} !important;
    border-right: 1px solid {BORDER};
    min-width: 320px !important; max-width: 360px !important;
}}
[data-testid="stSidebar"] .block-container {{ padding-top: 1rem !important; }}
[data-testid="stSidebarUserContent"] {{ padding-top: 0.5rem; }}
.block-container {{ padding-top: 1.4rem !important; padding-bottom: 2rem !important; max-width: 100% !important; }}

/* sidebar section divider label */
.side-label {{
    font-size: 10px; font-weight: 700; color: {DIM};
    text-transform: uppercase; letter-spacing: 1.3px;
    margin: 18px 0 8px; display: flex; align-items: center; gap: 7px;
}}

[data-testid="stTabs"] button {{
    font-family: 'Inter', sans-serif; font-size: 13px; font-weight: 500;
    color: {MUTED}; border: none; padding: 8px 18px;
}}
[data-testid="stTabs"] button:hover {{ color: {TEXT}; }}
[data-testid="stTabs"] button[aria-selected="true"] {{
    color: {BLUE} !important; border-bottom: 2px solid {BLUE} !important; font-weight: 600 !important;
}}
[data-testid="stTabsContent"] {{ padding-top: 18px; }}

[data-testid="stSelectbox"] label,
[data-testid="stRadio"] label {{ color: {MUTED} !important; font-size: 12px; }}

::-webkit-scrollbar {{ width: 5px; height: 5px; }}
::-webkit-scrollbar-track {{ background: {PANEL}; }}
::-webkit-scrollbar-thumb {{ background: #30363d; border-radius: 3px; }}
.fa, .fas, .fab, .far {{ line-height: 1; }}

/* ── animations ───────────────────────────────────────────── */
@keyframes fadeInUp  {{ from {{ opacity:0; transform:translateY(7px); }} to {{ opacity:1; transform:translateY(0); }} }}
@keyframes pulseRing {{ 0% {{ box-shadow:0 0 0 0 rgba(63,185,80,.55); }}
                        70% {{ box-shadow:0 0 0 7px rgba(63,185,80,0); }}
                        100%{{ box-shadow:0 0 0 0 rgba(63,185,80,0); }} }}
@keyframes pulseRingRed {{ 0% {{ box-shadow:0 0 0 0 rgba(248,81,73,.55); }}
                           70% {{ box-shadow:0 0 0 7px rgba(248,81,73,0); }}
                           100%{{ box-shadow:0 0 0 0 rgba(248,81,73,0); }} }}
@keyframes shimmer   {{ 0% {{ background-position:200% 0; }} 100% {{ background-position:-200% 0; }} }}
@keyframes spin      {{ to {{ transform: rotate(360deg); }} }}
@keyframes glowPulse {{ 0%,100% {{ opacity:.5; }} 50% {{ opacity:1; }} }}

.tridi-card {{
    animation: fadeInUp .45s cubic-bezier(.2,.7,.3,1) both;
    transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
}}
.tridi-card:hover {{
    transform: translateY(-3px);
    box-shadow: 0 10px 28px rgba(0,0,0,.5);
    border-color: #3a4452 !important;
}}

.pulse-dot     {{ animation: pulseRing 1.8s ease-out infinite; }}
.pulse-dot-red {{ animation: pulseRingRed 2.2s ease-out infinite; }}
.spin-icon     {{ display:inline-block; animation: spin 4.5s linear infinite; }}
.live-badge    {{ animation: glowPulse 2.2s ease-in-out infinite; }}
.shimmer-bar   {{ background-size:200% 100% !important; animation: shimmer 2.4s linear infinite; }}

[data-testid="stTabsContent"] {{ animation: fadeInUp .4s ease; }}
table tbody tr {{ transition: background .12s ease; }}
</style>
""", unsafe_allow_html=True)

init_db()

# ── sidebar toggle fallback ─────────────────────────────────────────────────
# A floating ☰ button that always works: it clicks whichever native Streamlit
# sidebar control exists (expand pill when collapsed, « button when open).
import streamlit.components.v1 as _components
_components.html("""
<script>
const doc = window.parent.document;
function tridiBtn() {
  if (doc.getElementById('tridi-sb-toggle')) return;
  const b = doc.createElement('button');
  b.id = 'tridi-sb-toggle';
  b.innerHTML = '&#9776;';
  Object.assign(b.style, {
    position: 'fixed', top: '10px', left: '10px', zIndex: 99999,
    background: '#0d1117', color: '#4493f8',
    border: '1px solid #1c2128', borderRadius: '8px',
    padding: '5px 11px', cursor: 'pointer', fontSize: '15px',
    lineHeight: '1', fontFamily: 'Inter, sans-serif'
  });
  b.title = 'Open / close the control panel';
  b.onclick = () => {
    const expand = doc.querySelector(
      '[data-testid="stSidebarCollapsedControl"] button,' +
      '[data-testid="collapsedControl"] button,' +
      '[data-testid="stExpandSidebarButton"] button');
    if (expand) { expand.click(); return; }
    const collapse = doc.querySelector(
      '[data-testid="stSidebarCollapseButton"] button,' +
      'section[data-testid="stSidebar"] button[kind="headerNoPadding"],' +
      'section[data-testid="stSidebar"] [data-testid="baseButton-headerNoPadding"]');
    if (collapse) { collapse.click(); }
  };
  doc.body.appendChild(b);
}
tridiBtn();
setInterval(tridiBtn, 2000);   // survive Streamlit re-renders
</script>
""", height=0, width=0)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def H(html: str) -> None:
    """Shorthand for an unsafe-HTML markdown block."""
    st.markdown(html, unsafe_allow_html=True)


def read_agent_log(n: int = 60) -> list[str]:
    """Last n lines of agent.log (created when the agent runs)."""
    p = os.path.join(ROOT, "agent.log")
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except Exception:
        return []


def humanise_age(ts_iso: str) -> str:
    """'42s ago' / '3m ago' / '2h ago' for an ISO timestamp."""
    try:
        ts = datetime.fromisoformat(ts_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - ts).total_seconds())
        if secs < 60:   return f"{secs}s ago"
        if secs < 3600: return f"{secs // 60}m ago"
        return f"{secs // 3600}h ago"
    except Exception:
        return "—"


def is_stale(ts_iso: str, timeout: int = HEARTBEAT_TIMEOUT) -> bool:
    """True if the timestamp is older than `timeout` seconds (or unparseable)."""
    try:
        ts = datetime.fromisoformat(ts_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() > timeout
    except Exception:
        return True


def load_data() -> dict:
    """
    Fetch everything the dashboard needs in one place.

    Counts come from a single shared connection; the list queries reuse the
    db-layer helpers. Centralising here keeps the render code declarative.
    """
    conn = get_conn()
    try:
        sig_count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        pos_count = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        raw_count = conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0]
        db_ok = True
    except Exception:
        sig_count = pos_count = raw_count = 0
        db_ok = False
    finally:
        conn.close()

    return {
        "positions": pd.DataFrame([dict(r) for r in fetch_all_positions()]),
        "stats":     pd.DataFrame([dict(r) for r in fetch_channel_stats()]),
        "signals":   pd.DataFrame([dict(r) for r in fetch_recent_signals(limit=40)]),
        "raw":       [dict(r) for r in fetch_recent_raw_messages(limit=60)],
        "status":    read_status(),
        "vocab":     fetch_all_channel_vocabularies(),
        "msg_counts": fetch_raw_message_count(),
        "db_ok":     db_ok,
        "sig_count": sig_count,
        "pos_count": pos_count,
        "raw_count": raw_count,
    }


# ══════════════════════════════════════════════════════════════════════════════
# STATIC TOP BAR  (rendered once, persists across fragment reruns)
# ══════════════════════════════════════════════════════════════════════════════
H(f"""
<div style="display:flex;align-items:center;justify-content:space-between;
     padding:16px 26px;background:{PANEL};border:1px solid {BORDER};
     border-radius:12px;margin-bottom:20px">
  <div style="display:flex;align-items:center;gap:14px">
    <div style="background:{BORDER};border:1px solid #30363d;border-radius:10px;padding:10px 12px">
      <i class="fas fa-chart-line" style="color:{BLUE};font-size:18px"></i>
    </div>
    <div>
      <div style="font-size:18px;font-weight:700;color:{TEXT};letter-spacing:-0.3px">
        Tridi Signal Dashboard
      </div>
      <div style="font-size:12px;color:{DIM};margin-top:2px">
        Telegram &rarr; MetaTrader 5 &nbsp;&middot;&nbsp; Live position tracker
      </div>
    </div>
  </div>
  <div class="live-badge" style="display:flex;align-items:center;gap:7px;background:{BORDER};
       border:1px solid #30363d;border-radius:8px;padding:7px 14px;font-size:12px;color:{GREEN}">
    <i class="fas fa-rotate spin-icon" style="font-size:11px"></i> Live · {REFRESH_SECONDS}s
  </div>
</div>
""")


# ══════════════════════════════════════════════════════════════════════════════
# RENDER COMPONENTS
# ══════════════════════════════════════════════════════════════════════════════
def render_connection_status(data: dict) -> None:
    status  = data["status"]
    tg_svc  = status.get("telegram")
    mt5_svc = status.get("mt5")

    def svc_state(svc):
        if svc is None:
            return False, False, "—", ""
        stale = is_stale(svc["ts"])
        return True, svc["ok"] and not stale, humanise_age(svc["ts"]), svc.get("detail", "")

    tg_started,  tg_ok,  tg_age,  tg_detail  = svc_state(tg_svc)
    mt5_started, mt5_ok, mt5_age, mt5_detail = svc_state(mt5_svc)

    def card(col, fa_icon, title, started, ok, checks, detail, age):
        if not started:
            bg, bdr, dot, lc = PANEL, BORDER, DIM, MUTED
        elif ok:
            bg, bdr, dot, lc = "#0d2318", "#238636", GREEN, GREEN
        else:
            bg, bdr, dot, lc = "#2d1117", RED, RED, RED

        chk = "".join(
            f'<div style="display:flex;align-items:center;gap:9px;padding:6px 0;'
            f'border-bottom:1px solid {BORDER}">'
            f'<i class="fas {"fa-circle-check" if c_ok else "fa-circle-xmark"}" '
            f'style="color:{GREEN if c_ok else RED};font-size:13px;width:14px"></i>'
            f'<span style="font-size:12px;color:{"#adbac7" if c_ok else MUTED}">{lbl}</span></div>'
            for c_ok, lbl in checks
        )
        detail_html = (
            f'<div style="font-size:11px;color:{MUTED}">{detail}</div>' if detail
            else f'<div style="font-size:11px;color:{DIM}">'
                 f'<i class="fas fa-terminal" style="margin-right:5px"></i>Run '
                 f'<code style="background:{BORDER};padding:1px 5px;border-radius:3px;'
                 f'color:{TEXT}">python main.py</code></div>'
        )
        with col:
            H(f"""
            <div style="background:{bg};border:1px solid {bdr};border-radius:12px;padding:18px 20px">
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
                <div style="display:flex;align-items:center;gap:10px">
                  <div style="background:{BORDER};border-radius:8px;padding:8px 9px;line-height:1">
                    <i class="fas {fa_icon}" style="color:{BLUE};font-size:15px"></i>
                  </div>
                  <span style="font-size:14px;font-weight:600;color:{lc}">{title}</span>
                </div>
                <div style="width:10px;height:10px;border-radius:50%;background:{dot}"></div>
              </div>
              <div style="margin-bottom:14px">{chk}</div>
              <div style="background:{BG};border-radius:7px;padding:9px 11px;margin-bottom:10px">{detail_html}</div>
              <div style="font-size:11px;color:{DIM}">
                <i class="fas fa-clock" style="font-size:10px;margin-right:5px"></i>Last heartbeat: {age}
              </div>
            </div>""")

    H(f'<div style="display:flex;align-items:center;gap:8px;font-size:11px;font-weight:600;'
      f'color:{DIM};text-transform:uppercase;letter-spacing:1.2px;margin-bottom:12px">'
      f'<i class="fas fa-signal"></i> Connection Status</div>')

    c1, c2, c3 = st.columns(3)
    card(c1, "fa-paper-plane", "Telegram", tg_started, tg_ok,
         [(tg_started, "Agent process running"),
          (tg_ok, "Authenticated & connected"),
          (tg_svc is not None and not is_stale(tg_svc["ts"]), "Heartbeat within 3 min"),
          ("channel" in tg_detail.lower() or "signal" in tg_detail.lower(), "Listening to channels")],
         tg_detail, tg_age)
    card(c2, "fa-server", "MetaTrader 5", mt5_started, mt5_ok,
         [(mt5_started, "Agent process running"),
          (mt5_ok, "Account authenticated"),
          (mt5_svc is not None and not is_stale(mt5_svc["ts"]), "Heartbeat within 3 min"),
          ("balance" in mt5_detail.lower() or "equity" in mt5_detail.lower(), "Account data received")],
         mt5_detail, mt5_age)
    card(c3, "fa-database", "Database", True, data["db_ok"],
         [(data["db_ok"], "signals.db accessible"),
          (data["sig_count"] > 0, f'{data["sig_count"]} signals stored'),
          (data["pos_count"] > 0, f'{data["pos_count"]} positions tracked'),
          (data["raw_count"] > 0, f'{data["raw_count"]} messages archived')],
         f'signals.db · {data["sig_count"]} signals · {data["pos_count"]} positions', "always-on")


def render_metrics(pos_df: pd.DataFrame) -> None:
    total   = len(pos_df)
    open_n  = int((pos_df["status"] == "open").sum())   if not pos_df.empty else 0
    closed  = int((pos_df["status"] == "closed").sum()) if not pos_df.empty else 0
    pnl     = float(pos_df["profit"].sum())             if not pos_df.empty else 0.0
    wins    = int(((pos_df["status"] == "closed") & (pos_df["profit"] > 0)).sum()) if not pos_df.empty else 0
    wr      = round(wins / closed * 100, 1) if closed > 0 else 0.0

    def metric(col, icon, value, label, color=TEXT, sub=""):
        with col:
            H(f"""
            <div class="tridi-card" style="background:{PANEL};border:1px solid {BORDER};border-radius:12px;
                 padding:18px 16px;text-align:center">
              <div style="color:{DIM};font-size:18px;margin-bottom:8px"><i class="fas {icon}"></i></div>
              <div style="font-size:26px;font-weight:700;color:{color};letter-spacing:-0.5px">{value}</div>
              <div style="font-size:10px;color:{DIM};text-transform:uppercase;letter-spacing:1.2px;margin-top:5px">{label}</div>
              {f'<div style="font-size:11px;color:{MUTED};margin-top:3px">{sub}</div>' if sub else ''}
            </div>""")

    m = st.columns(5)
    metric(m[0], "fa-layer-group",  total,  "Positions")
    metric(m[1], "fa-circle-dot",   open_n, "Open", BLUE)
    metric(m[2], "fa-circle-check", closed, "Closed", MUTED)
    metric(m[3], "fa-coins", f"{'+' if pnl >= 0 else ''}{pnl:.2f}", "Total P&L",
           GREEN if pnl >= 0 else RED)
    metric(m[4], "fa-trophy", f"{wr}%", "Win Rate",
           GREEN if wr >= 50 else RED, f"{wins}W / {closed - wins}L")


def render_command_panel(pos_df: pd.DataFrame) -> None:
    """Manual command controls: quick actions + free-text command box."""
    H(f'<div style="font-size:11px;font-weight:600;color:{DIM};text-transform:uppercase;'
      f'letter-spacing:1px;margin-bottom:8px"><i class="fas fa-terminal"></i> '
      f'Manual commands</div>')

    qa1, qa2, qa3 = st.columns([1, 1, 2])
    with qa1:
        if st.button("🔴  Close ALL", use_container_width=True, key="close_all_btn"):
            res = close_all_positions()
            if res.get("ok"):
                st.toast(f"Closed {res['closed']}/{res['total']} positions", icon="✅")
            else:
                st.toast(res.get("error", "Failed"), icon="⚠️")
            st.rerun(scope="fragment")
    with qa2:
        if st.button("🛡️  Break-even ALL", use_container_width=True, key="be_all_btn"):
            res = breakeven_all_positions()
            if res.get("ok"):
                st.toast(f"Moved {res['moved']}/{res['total']} to break-even", icon="✅")
            else:
                st.toast(res.get("error", "Failed"), icon="⚠️")
            st.rerun(scope="fragment")

    with st.expander("Send a manual command (any language)"):
        st.caption("e.g. `close now` · `breakeven` · `sl 4080` · `buy XAUUSD` · "
                   "`أغلق الصفقة` · `Fermez la position`")
        c1, c2 = st.columns([3, 2])
        with c1:
            cmd = st.text_input("Command", key="manual_cmd",
                               placeholder="close now", label_visibility="collapsed")
        with c2:
            chans = get_configured_channels() or ["manual"]
            cmd_ch = st.selectbox("Apply to channel", chans, key="manual_cmd_ch",
                                  label_visibility="collapsed")
        if st.button("Execute command", key="exec_cmd_btn"):
            if not cmd.strip():
                st.warning("Type a command first.")
            else:
                res = run_manual_command(cmd, cmd_ch)
                if not res.get("ok"):
                    st.error(res.get("error", "Command not recognised"))
                else:
                    p = res["parsed"]
                    out = res.get("result") or {}
                    summary = f"**{res['type']}** · symbol={p.get('symbol') or '—'} · action={p.get('action') or '—'}"
                    if out.get("closed") is not None:
                        summary += f" · closed {out['closed']}"
                    if out.get("updated") is not None:
                        summary += f" · SL moved on {out['updated']}"
                    if out.get("order"):
                        summary += f" · opened #{out['order']}"
                    if out.get("skipped"):
                        summary += " · skipped (duplicate)"
                    st.success(f"Executed: {summary}")
                st.rerun(scope="fragment")
    st.markdown("---")


def render_positions_tab(pos_df: pd.DataFrame) -> None:
    if pos_df.empty:
        H(f'<div style="color:{DIM};text-align:center;padding:40px 0">'
          f'<i class="fas fa-inbox" style="font-size:28px;margin-bottom:10px;display:block"></i>'
          f'No positions yet — start the agent and wait for the first signal.</div>')
        return

    fa, fb = st.columns([2, 1])
    with fa:
        ch_opts = ["All"] + sorted(pos_df["channel"].dropna().unique().tolist())
        sel_ch = st.selectbox("Channel", ch_opts, key="pos_ch")
    with fb:
        sel_st = st.radio("Status", ["All", "Open", "Closed"], horizontal=True, key="pos_st")

    df = pos_df.copy()
    if sel_ch != "All":
        df = df[df["channel"] == sel_ch]
    if sel_st != "All":
        df = df[df["status"] == sel_st.lower()]

    if df.empty:
        H(f'<div style="color:{DIM};padding:24px 0;text-align:center">No results</div>')
        return

    # ── unified table: every row carries its own ✕ Close command ──────────────
    widths = [1.2, 1.0, 0.9, 1.0, 0.9, 0.9, 0.7, 0.9, 1.0, 1.4, 1.2, 0.8]
    labels = ["Ticket", "Symbol", "Side", "Entry", "SL", "TP", "Lot",
              "P&L", "Status", "Channel", "Opened", ""]
    hdr = st.columns(widths)
    for c, l in zip(hdr, labels):
        c.markdown(f"<div style='font-size:10px;color:{DIM};text-transform:uppercase;"
                   f"letter-spacing:1px;font-weight:600;padding-bottom:4px'>{l}</div>",
                   unsafe_allow_html=True)
    H(f"<div style='border-bottom:1px solid {BORDER};margin-bottom:6px'></div>")

    for _, r in df.iterrows():
        profit  = float(r.get("profit") or 0.0)
        pc      = GREEN if profit >= 0 else RED
        ps      = f"+{profit:.2f}" if profit >= 0 else f"{profit:.2f}"
        act     = r.get("action", "")
        ac      = GREEN if act == "BUY" else RED
        a_icon  = "fa-arrow-trend-up" if act == "BUY" else "fa-arrow-trend-down"
        is_open = r.get("status") == "open"
        st_cl   = BLUE if is_open else DIM
        st_ic   = "fa-circle-dot" if is_open else "fa-circle-check"
        ticket  = r.get("ticket")

        cols = st.columns(widths)
        cols[0].markdown(f"<span style='font-family:monospace;color:{DIM};font-size:11px'>{ticket or '—'}</span>", unsafe_allow_html=True)
        cols[1].markdown(f"<b style='color:{TEXT};font-size:13px'>{r.get('symbol','—')}</b>", unsafe_allow_html=True)
        cols[2].markdown(f"<span style='display:inline-flex;align-items:center;gap:4px;"
                         f"background:{'#0d2318' if act=='BUY' else '#2d1117'};color:{ac};"
                         f"border-radius:5px;padding:2px 8px;font-size:11px;font-weight:600'>"
                         f"<i class='fas {a_icon}' style='font-size:10px'></i>{act}</span>", unsafe_allow_html=True)
        cols[3].markdown(f"<span style='color:{MUTED};font-size:12px'>{r.get('open_price','—')}</span>", unsafe_allow_html=True)
        cols[4].markdown(f"<span style='color:{RED};font-size:12px'>{r.get('sl') or '—'}</span>", unsafe_allow_html=True)
        cols[5].markdown(f"<span style='color:{GREEN};font-size:12px'>{r.get('tp') or '—'}</span>", unsafe_allow_html=True)
        cols[6].markdown(f"<span style='color:{MUTED};font-size:12px'>{r.get('lot','—')}</span>", unsafe_allow_html=True)
        cols[7].markdown(f"<span style='color:{pc};font-weight:600;font-size:12px'>{ps}</span>", unsafe_allow_html=True)
        cols[8].markdown(f"<span style='display:inline-flex;align-items:center;gap:4px;color:{st_cl};font-size:12px'>"
                         f"<i class='fas {st_ic}' style='font-size:10px'></i>{str(r.get('status','')).capitalize()}</span>", unsafe_allow_html=True)
        cols[9].markdown(f"<span style='color:{BLUE};font-size:12px'>@{r.get('channel','—')}</span>", unsafe_allow_html=True)
        cols[10].markdown(f"<span style='color:{DIM};font-size:11px;font-family:monospace'>"
                          f"{(r.get('opened_at','') or '')[:16].replace('T',' ')}</span>", unsafe_allow_html=True)

        # per-row command: ✕ closes THIS position only
        if is_open and ticket:
            if cols[11].button("✕", key=f"rowclose_{ticket}",
                               help="Close this position at market"):
                res = close_position_by_ticket(int(ticket))
                if res.get("ok"):
                    st.toast(f"Closed {res['symbol']} · P&L {res['profit']:+.2f}", icon="✅")
                else:
                    st.toast(f"Close failed: {res.get('error')}", icon="⚠️")
                st.rerun(scope="fragment")
        else:
            cols[11].markdown(f"<span style='color:{DIM}'>—</span>", unsafe_allow_html=True)


def render_channels_tab(stat_df: pd.DataFrame) -> None:
    if stat_df.empty:
        H(f'<div style="color:{DIM};text-align:center;padding:40px 0">'
          f'<i class="fas fa-chart-pie" style="font-size:28px;margin-bottom:10px;display:block"></i>'
          f'No channel data yet.</div>')
        return

    # ── rank channels by win rate: the copy-trade focus list ─────────────────
    ranked = []
    for _, r in stat_df.iterrows():
        wins_c   = int(r["wins"] or 0)
        losses_c = int(r["losses"] or 0)
        profit_c = float(r["total_profit"] or 0)
        total_c  = int(r["total"] or 1)
        closed_c = wins_c + losses_c
        wr_c = round(wins_c / closed_c * 100, 1) if closed_c > 0 else 0.0
        ranked.append({"channel": r["channel"], "wins": wins_c, "losses": losses_c,
                       "profit": profit_c, "total": total_c, "closed": closed_c, "wr": wr_c})
    # channels with closed trades first (real WR), then by WR, then by profit
    ranked.sort(key=lambda x: (x["closed"] > 0, x["wr"], x["profit"]), reverse=True)

    RANK_ICON = [("fa-trophy", "#e3b341"), ("fa-medal", "#adbac7"), ("fa-medal", "#d29922")]

    lc, rc = st.columns([1, 1])
    with lc:
        for i, ch in enumerate(ranked):
            wins_c, losses_c = ch["wins"], ch["losses"]
            profit_c, total_c = ch["profit"], ch["total"]
            closed_c, wr_c = ch["closed"], ch["wr"]
            wrc = GREEN if wr_c >= 50 else RED
            prc = GREEN if profit_c >= 0 else RED
            r = ch  # keep template vars below working

            # rank badge + focus tag for the leader
            if i < len(RANK_ICON) and closed_c > 0:
                icon, icol = RANK_ICON[i]
                rank_html = (f'<i class="fas {icon}" style="color:{icol};font-size:14px;'
                             f'margin-right:6px" title="Rank #{i+1}"></i>')
            else:
                rank_html = (f'<span style="color:{DIM};font-size:11px;font-family:monospace;'
                             f'margin-right:8px">#{i+1}</span>')
            focus_html = ""
            if i == 0 and closed_c > 0 and wr_c >= 50:
                focus_html = (f'<span style="background:#0d2318;color:{GREEN};border:1px solid #238636;'
                              f'border-radius:12px;padding:2px 10px;font-size:10px;font-weight:700;'
                              f'letter-spacing:0.8px;margin-left:8px">FOCUS</span>')
            elif closed_c == 0:
                focus_html = (f'<span style="color:{DIM};font-size:10px;margin-left:8px">'
                              f'no closed trades yet</span>')
            H(f"""
            <div class="tridi-card" style="background:{PANEL};border:1px solid {BORDER};border-radius:12px;padding:18px 20px;margin-bottom:12px">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
                <div style="display:flex;align-items:center">
                  {rank_html}
                  <i class="fab fa-telegram" style="color:{BLUE};font-size:16px;margin-right:6px"></i>
                  <span style="font-size:14px;font-weight:600;color:{TEXT}">@{r['channel']}</span>
                  {focus_html}
                </div>
                <span style="font-size:24px;font-weight:700;color:{wrc}">{wr_c}%</span>
              </div>
              <div style="display:flex;gap:18px;font-size:12px;margin-bottom:12px">
                <span style="color:{MUTED}"><i class="fas fa-signal" style="margin-right:4px"></i>{total_c} signals</span>
                <span style="color:{GREEN}"><i class="fas fa-check" style="margin-right:4px"></i>{wins_c} wins</span>
                <span style="color:{RED}"><i class="fas fa-xmark" style="margin-right:4px"></i>{losses_c} losses</span>
                <span style="color:{prc}"><i class="fas fa-coins" style="margin-right:4px"></i>{'+' if profit_c>=0 else ''}{profit_c:.2f}</span>
              </div>
              <div style="background:{BORDER};border-radius:4px;height:5px;overflow:hidden">
                <div class="shimmer-bar" style="width:{int(wr_c)}%;height:5px;border-radius:4px;
                     background:linear-gradient(100deg,{wrc} 0%,#ffffff66 50%,{wrc} 100%)"></div>
              </div>
            </div>""")
    with rc:
        fig = px.pie(stat_df, names="channel", values="total", hole=0.65,
                     color_discrete_sequence=[BLUE, GREEN, RED, "#d2a8ff", "#ffa657"])
        fig.update_layout(paper_bgcolor=BG, plot_bgcolor=BG, font_color=MUTED,
                          margin=dict(t=10, b=10, l=10, r=10), height=320,
                          showlegend=True, legend=dict(font=dict(color=MUTED, size=12)))
        fig.update_traces(textinfo="none")
        st.plotly_chart(fig, use_container_width=True)


def render_signals_tab(sig_df: pd.DataFrame) -> None:
    if sig_df.empty:
        H(f'<div style="color:{DIM};text-align:center;padding:40px 0">'
          f'<i class="fas fa-satellite-dish" style="font-size:28px;margin-bottom:10px;display:block"></i>'
          f'No signals parsed yet.</div>')
        return

    LANG_ICON = {"arabic": "fa-a", "french": "fa-f", "english": "fa-e"}
    LANG_CLR  = {"arabic": "#ffa657", "french": "#79c0ff", "english": "#56d364"}
    TYPE_ICON = {"NEW_SIGNAL": "fa-bolt", "UPDATE": "fa-pen", "CLOSE": "fa-lock", "IRRELEVANT": "fa-minus"}
    TYPE_CLR  = {"NEW_SIGNAL": GREEN, "UPDATE": BLUE, "CLOSE": RED, "IRRELEVANT": DIM}

    rows = ""
    for _, r in sig_df.iterrows():
        lang  = r.get("language", "english")
        mtype = r.get("msg_type", "NEW_SIGNAL") or "NEW_SIGNAL"
        tc, ti = TYPE_CLR.get(mtype, MUTED), TYPE_ICON.get(mtype, "fa-minus")
        lcl, li = LANG_CLR.get(lang, MUTED), LANG_ICON.get(lang, "fa-globe")
        act = r.get("action") or "—"
        ac = GREEN if act == "BUY" else (RED if act == "SELL" else MUTED)
        ts = (r.get("parsed_at", "") or "")[:16].replace("T", " ")
        tps = " / ".join(str(r[f"tp{i}"]) for i in (1, 2, 3) if r.get(f"tp{i}")) or "—"
        note = (r.get("note", "") or "")[:55]
        rows += f"""<tr style="border-bottom:1px solid {BORDER}">
          <td style="padding:9px 14px;color:{DIM};font-size:11px;font-family:monospace">{ts}</td>
          <td style="padding:9px 14px"><span style="display:inline-flex;align-items:center;gap:5px;color:{BLUE};font-size:12px">
            <i class="fab fa-telegram" style="font-size:12px"></i>@{r.get('channel','—')}</span></td>
          <td style="padding:9px 14px"><span style="display:inline-flex;align-items:center;justify-content:center;
            width:22px;height:22px;background:{BORDER};border-radius:5px;color:{lcl};font-size:11px;font-weight:700">
            <i class="fas {li}"></i></span></td>
          <td style="padding:9px 14px"><span style="display:inline-flex;align-items:center;gap:5px;color:{tc};font-size:12px;font-weight:500">
            <i class="fas {ti}" style="font-size:11px"></i>{mtype.replace('_',' ').title()}</span></td>
          <td style="padding:9px 14px;font-weight:600;color:{TEXT}">{r.get('symbol','—')}</td>
          <td style="padding:9px 14px;color:{ac};font-weight:600;font-size:12px">{act}</td>
          <td style="padding:9px 14px;color:{MUTED};font-size:12px">{r.get('entry','—')}</td>
          <td style="padding:9px 14px;color:{RED};font-size:12px">{r.get('sl','—')}</td>
          <td style="padding:9px 14px;color:{GREEN};font-size:12px">{tps}</td>
          <td style="padding:9px 14px;color:{DIM};font-size:11px">{note}</td>
        </tr>"""

    headers = ["Time", "Channel", "Lang", "Type", "Symbol", "Side", "Entry", "SL", "TP(s)", "Note"]
    H(f"""<div style="overflow-x:auto;border:1px solid {BORDER};border-radius:12px">
    <table style="width:100%;border-collapse:collapse;font-size:13px;background:{PANEL};color:{TEXT}">
      <thead><tr style="background:{BG};color:{DIM};font-size:10px;text-transform:uppercase;letter-spacing:1px">
        {"".join(f'<th style="padding:11px 14px;text-align:left;font-weight:600">{h}</th>' for h in headers)}
      </tr></thead><tbody>{rows}</tbody></table></div>""")


def render_vocab_tab(vocab: dict, msg_counts: dict) -> None:
    if not vocab:
        H(f'<div style="color:{DIM};text-align:center;padding:40px 0">'
          f'<i class="fas fa-book-open" style="font-size:28px;margin-bottom:10px;display:block"></i>'
          f'No vocabulary learned yet — keywords build up automatically as signals arrive.</div>')
        return

    TC = {"NEW_SIGNAL": GREEN, "UPDATE": BLUE, "CLOSE": RED, "IRRELEVANT": DIM}
    for ch, kws in vocab.items():
        total_m = msg_counts.get(ch, 0)
        with st.expander(f"@{ch}  ·  {len(kws)} keywords  ·  {total_m} messages"):
            groups: dict = {}
            for kw in kws:
                groups.setdefault(kw["msg_type"], []).append(kw)
            gcols = st.columns(max(len(groups), 1))
            for gcol, (mtype, items) in zip(gcols, groups.items()):
                color = TC.get(mtype, MUTED)
                with gcol:
                    H(f'<div style="color:{color};font-size:10px;font-weight:700;'
                      f'text-transform:uppercase;letter-spacing:1.2px;margin-bottom:8px">'
                      f'{mtype.replace("_", " ")}</div>')
                    pills = ""
                    for kw in items[:25]:
                        hits = kw["hit_count"]
                        size = min(13, 10 + hits // 3)
                        act_clr = {"BUY": GREEN, "SELL": RED}.get(kw.get("action", ""), "")
                        dot = (f'<span style="width:6px;height:6px;border-radius:50%;background:{act_clr};'
                               f'display:inline-block;margin-right:3px"></span>' if act_clr else "")
                        pills += (f'<span style="display:inline-flex;align-items:center;margin:3px 2px;'
                                  f'padding:4px 10px;border-radius:20px;background:{PANEL};border:1px solid {BORDER};'
                                  f'color:{color};font-size:{size}px" title="seen {hits}×">{dot}{kw["keyword"]} '
                                  f'<span style="color:{DIM};font-size:10px;margin-left:4px">{hits}</span></span>')
                    H(pills)


def render_logs_tab(raw_rows: list, msg_counts: dict) -> None:
    """Raw message feed (every message, all channels) + agent runtime log."""
    LANG_CLR = {"arabic": "#ffa657", "french": "#79c0ff", "english": "#56d364"}

    # per-channel received counts
    if msg_counts:
        chips = ""
        for ch, cnt in sorted(msg_counts.items(), key=lambda x: -x[1]):
            chips += (f'<span style="display:inline-flex;align-items:center;gap:5px;'
                      f'background:{PANEL};border:1px solid {BORDER};border-radius:16px;'
                      f'padding:4px 11px;margin:3px;font-size:12px;color:{TEXT}">'
                      f'<i class="fab fa-telegram" style="color:{BLUE}"></i>@{ch} '
                      f'<b style="color:{BLUE}">{cnt}</b></span>')
        H(f'<div style="margin-bottom:6px;color:{DIM};font-size:11px;font-weight:600;'
          f'text-transform:uppercase;letter-spacing:1px">Messages received per channel</div>{chips}')

    H(f'<div style="margin:18px 0 8px;color:{DIM};font-size:11px;font-weight:600;'
      f'text-transform:uppercase;letter-spacing:1px">Live message feed — every message, all channels</div>')

    if not raw_rows:
        H(f'<div style="color:{DIM};padding:20px 0">No messages received yet. '
          f'Post something in a watched channel to see it appear here.</div>')
    else:
        rows = ""
        for r in raw_rows:
            lang = r.get("language", "english")
            lcl  = LANG_CLR.get(lang, MUTED)
            ts   = (r.get("received_at", "") or "")[:19].replace("T", " ")
            txt  = (r.get("message", "") or "").replace("<", "&lt;").replace(">", "&gt;")
            txt  = txt.replace("\n", " ⏎ ")[:160]
            rows += f"""<tr style="border-bottom:1px solid {BORDER}">
              <td style="padding:8px 12px;color:{DIM};font-size:11px;font-family:monospace;white-space:nowrap">{ts}</td>
              <td style="padding:8px 12px;color:{BLUE};font-size:12px;white-space:nowrap">@{r.get('channel','—')}</td>
              <td style="padding:8px 12px;color:{lcl};font-size:11px">{lang[:2].upper()}</td>
              <td style="padding:8px 12px;color:{TEXT};font-size:12px">{txt}</td>
            </tr>"""
        H(f"""<div style="overflow-x:auto;border:1px solid {BORDER};border-radius:12px;max-height:360px;overflow-y:auto">
        <table style="width:100%;border-collapse:collapse;background:{PANEL};color:{TEXT}">
          <thead><tr style="background:{BG};color:{DIM};font-size:10px;text-transform:uppercase;letter-spacing:1px;position:sticky;top:0">
            <th style="padding:10px 12px;text-align:left">Time</th>
            <th style="padding:10px 12px;text-align:left">Channel</th>
            <th style="padding:10px 12px;text-align:left">Lang</th>
            <th style="padding:10px 12px;text-align:left">Message</th>
          </tr></thead><tbody>{rows}</tbody></table></div>""")

    # agent runtime log
    H(f'<div style="margin:20px 0 8px;color:{DIM};font-size:11px;font-weight:600;'
      f'text-transform:uppercase;letter-spacing:1px">Agent runtime log — agent.log (connections · parses · trades · errors)</div>')
    lines = read_agent_log(80)
    if lines:
        st.code("".join(lines), language="log")
    else:
        st.caption("agent.log not created yet — start the agent to generate it.")


# ══════════════════════════════════════════════════════════════════════════════
# COMPACT STATUS STRIP  (main area — 3 small pills + collapsible diagnostics)
# ══════════════════════════════════════════════════════════════════════════════
def render_status_strip(data: dict) -> None:
    status = data["status"]

    def svc(name):
        s = status.get(name)
        if s is None:
            return "Idle", DIM, "not started", "—"
        stale = is_stale(s["ts"])
        ok = s["ok"] and not stale
        return (("Connected" if ok else "Offline"),
                (GREEN if ok else RED),
                s.get("detail", ""), humanise_age(s["ts"]))

    tg_state, tg_c, tg_d, tg_age = svc("telegram")
    mt_state, mt_c, mt_d, mt_age = svc("mt5")
    db_c = GREEN if data["db_ok"] else RED
    db_state = "Connected" if data["db_ok"] else "Error"
    db_d = f'{data["sig_count"]} signals · {data["pos_count"]} positions'

    def pill(col, icon, name, state, color, detail, age):
        dot_cls = "pulse-dot" if color == GREEN else ("pulse-dot-red" if color == RED else "")
        with col:
            H(f"""
            <div class="tridi-card" style="background:{PANEL};border:1px solid {BORDER};
                 border-left:3px solid {color};border-radius:10px;padding:13px 16px">
              <div style="display:flex;align-items:center;justify-content:space-between">
                <div style="display:flex;align-items:center;gap:9px">
                  <i class="fas {icon}" style="color:{MUTED};font-size:14px"></i>
                  <span style="font-size:13px;font-weight:600;color:{TEXT}">{name}</span>
                </div>
                <span style="display:flex;align-items:center;gap:6px;font-size:12px;
                     color:{color};font-weight:600">
                  <span class="{dot_cls}" style="width:8px;height:8px;border-radius:50%;
                       background:{color};display:inline-block"></span>
                  {state}
                </span>
              </div>
              <div style="font-size:11px;color:{MUTED};margin-top:7px;white-space:nowrap;
                   overflow:hidden;text-overflow:ellipsis">{detail or '—'}</div>
            </div>""")

    c1, c2, c3 = st.columns(3)
    pill(c1, "fa-paper-plane", "Telegram",    tg_state, tg_c, tg_d, tg_age)
    pill(c2, "fa-server",      "MetaTrader 5", mt_state, mt_c, mt_d, mt_age)
    pill(c3, "fa-database",    "Database",     db_state, db_c, db_d, "—")


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR CONTROLS  (static — outside the auto-refresh fragment)
# ══════════════════════════════════════════════════════════════════════════════
def start_agent() -> None:
    """Launch `python main.py` detached so it keeps running after the click."""
    import subprocess
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    logf = open(os.path.join(ROOT, "agent.log"), "a", encoding="utf-8")
    subprocess.Popen([sys.executable, "main.py"], cwd=ROOT,
                     stdout=logf, stderr=logf, creationflags=flags)


def _side_label(icon: str, text: str) -> None:
    H(f'<div class="side-label"><i class="fas {icon}"></i>{text}</div>')


def render_telegram_login() -> None:
    """Compact Telegram sign-in for the sidebar."""
    _side_label("fa-paper-plane", "Telegram")

    status = read_status()
    tg = status.get("telegram")
    agent_live = bool(tg and tg.get("ok") and not is_stale(tg["ts"]))

    if not tg_login.credentials_present():
        st.error("Add TELEGRAM_API_ID / API_HASH / PHONE in .env")
        return

    if agent_live:
        st.success("Connected — agent is running")
        return

    if "tg_authed" not in st.session_state:
        with st.spinner("Checking sign-in…"):
            authed, name = tg_login.check_authorized()
        st.session_state.tg_authed = authed
        st.session_state.tg_name = name

    phase = st.session_state.get("tg_phase", "idle")

    if st.session_state.tg_authed:
        st.success(f"Signed in as {st.session_state.get('tg_name') or 'your account'}")
        if st.button("▶  Start agent", key="start_agent_btn", use_container_width=True):
            start_agent()
            st.toast("Agent starting… status turns green in ~15 s.")
        st.caption("Session saved — no code needed again.")
        return

    if phase == "idle":
        st.caption(f"Send a login code to {tg_login.PHONE}")
        if st.button("Send me the code", key="tg_send", use_container_width=True):
            res = tg_login.request_code()
            if res["ok"] and res.get("already"):
                st.session_state.tg_authed = True
                st.rerun()
            elif res["ok"]:
                st.session_state.tg_hash = res["hash"]
                st.session_state.tg_phase = "code_sent"
                st.rerun()
            else:
                st.error(res["error"])

    elif phase == "code_sent":
        st.caption("Paste the code Telegram just sent you.")
        code = st.text_input("Login code", key="tg_code", placeholder="12345",
                             label_visibility="collapsed")
        if st.button("Verify code", key="tg_verify", use_container_width=True):
            res = tg_login.submit_code(code, st.session_state.get("tg_hash", ""))
            if res["ok"]:
                st.session_state.tg_authed = True
                st.session_state.tg_name = res["name"]
                st.session_state.tg_phase = "done"
                st.rerun()
            elif res["need_password"]:
                st.session_state.tg_phase = "need_password"
                st.rerun()
            else:
                st.error(res["error"])
        if st.button("Resend", key="tg_resend", use_container_width=True):
            st.session_state.tg_phase = "idle"
            st.rerun()

    elif phase == "need_password":
        st.caption("2-factor enabled — enter your cloud password.")
        pw = st.text_input("2FA password", type="password", key="tg_pw",
                          label_visibility="collapsed")
        if st.button("Submit password", key="tg_pw_btn", use_container_width=True):
            res = tg_login.submit_password(pw)
            if res["ok"]:
                st.session_state.tg_authed = True
                st.session_state.tg_name = res["name"]
                st.session_state.tg_phase = "done"
                st.rerun()
            else:
                st.error(res["error"])


def render_mode_selector() -> None:
    """Compact Manual/Agentic switch for the sidebar."""
    _side_label("fa-sliders", "Execution Mode")
    current = (get_setting("parse_mode", "manual") or "manual").lower()
    key_set = bool(os.environ.get("ANTHROPIC_API_KEY", "").split("#")[0].strip())

    choice = st.radio(
        "mode",
        ["Manual  ·  rules (no key)", "Agentic  ·  Claude AI"],
        index=0 if current == "manual" else 1,
        key="parse_mode_radio", label_visibility="collapsed")
    new_mode = "manual" if choice.startswith("Manual") else "agentic"
    if new_mode != current:
        set_setting("parse_mode", new_mode)
        current = new_mode

    if current == "manual":
        st.caption("⚡ Instant, free, deterministic — pattern + learned keywords.")
    elif key_set:
        st.caption("🤖 Claude reads each message (EN/FR/AR). Uses API credits.")
    else:
        st.warning("No ANTHROPIC_API_KEY — will fall back to Manual rules.", icon="⚠️")


def render_channel_manager() -> None:
    """Compact per-channel trade toggles for the sidebar."""
    _side_label("fa-tower-broadcast", "Trading Channels")
    for ch in get_configured_channels():
        register_channel(ch)

    rows = fetch_channel_config()
    if not rows:
        st.caption("No channels configured in .env yet.")
        return

    st.caption("OFF = receive & log signals, but don't open trades.")
    for r in rows:
        ch = r["channel"]
        enabled = bool(r["trade_enabled"])
        new_val = st.toggle(f"@{ch}", value=enabled, key=f"tg_trade_{ch}")
        if new_val != enabled:
            set_channel_trade_enabled(ch, new_val)


# ══════════════════════════════════════════════════════════════════════════════
# LIVE FRAGMENT  —  re-runs every REFRESH_SECONDS over the websocket
# (no full-page reload: tab, scroll and filter state are preserved)
# ══════════════════════════════════════════════════════════════════════════════
@st.fragment(run_every=REFRESH_SECONDS)
def live_dashboard() -> None:
    data = load_data()

    render_status_strip(data)
    with st.expander("Connection diagnostics"):
        render_connection_status(data)
    H("<br>")
    render_metrics(data["positions"])
    H("<br>")

    tab_pos, tab_ch, tab_sig, tab_vocab, tab_logs = st.tabs(
        ["  Positions  ", "  Channel Win Rates  ", "  Signal Feed  ",
         "  Vocabulary  ", "  Logs  "]
    )
    with tab_pos:
        render_positions_tab(data["positions"])
    with tab_ch:
        render_channels_tab(data["stats"])
    with tab_sig:
        render_signals_tab(data["signals"])
    with tab_vocab:
        render_vocab_tab(data["vocab"], data["msg_counts"])
    with tab_logs:
        render_logs_tab(data["raw"], data["msg_counts"])

    H("<br>")
    cr, ct = st.columns([1, 5])
    with cr:
        if st.button("Refresh now"):
            st.rerun(scope="fragment")   # reruns this fragment only — no page reload
    with ct:
        H(f'<div style="color:{DIM};font-size:11px;padding-top:8px">'
          f'<i class="fas fa-circle-info" style="margin-right:5px"></i>'
          f'Live data updates every {REFRESH_SECONDS}s without reloading the page '
          f'&nbsp;&middot;&nbsp; heartbeat timeout: {HEARTBEAT_TIMEOUT // 60} min</div>')


# ── sidebar = control panel ───────────────────────────────────────────────────
with st.sidebar:
    H(f'<div style="display:flex;align-items:center;gap:10px;padding:4px 2px 14px;'
      f'border-bottom:1px solid {BORDER};margin-bottom:6px">'
      f'<i class="fas fa-gauge-high" style="color:{BLUE};font-size:18px"></i>'
      f'<span style="font-size:15px;font-weight:700;color:{TEXT}">Control Panel</span></div>')
    render_telegram_login()
    render_mode_selector()
    render_channel_manager()

# ── main area = live dashboard ──────────────────────────────────────────────────
live_dashboard()
