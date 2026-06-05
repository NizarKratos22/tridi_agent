"""
Streamlit dashboard — positions & channel win-rate overview.
Run: streamlit run dashboard/app.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

from database.db import (init_db, fetch_all_positions, fetch_channel_stats,
                         fetch_recent_signals, fetch_all_channel_vocabularies,
                         fetch_raw_message_count)
from database.status import read_all as read_status

# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Tridi Signal Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap');

  html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

  .stApp { background: #0d1117; color: #e6edf3; }

  /* header */
  .dash-header {
    background: linear-gradient(135deg, #161b22 0%, #1c2128 100%);
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 24px 32px;
    margin-bottom: 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .dash-title { font-size: 26px; font-weight: 700; color: #58a6ff; margin: 0; }
  .dash-sub   { font-size: 13px; color: #8b949e; margin: 4px 0 0; }

  /* metric cards */
  /* status bar */
  .status-bar {
    display: flex;
    gap: 12px;
    margin-bottom: 20px;
    flex-wrap: wrap;
  }
  .status-pill {
    display: flex;
    align-items: center;
    gap: 8px;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 20px;
    padding: 8px 16px;
    font-size: 13px;
  }
  .status-dot {
    width: 9px; height: 9px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .dot-green  { background: #3fb950; box-shadow: 0 0 6px #3fb950; }
  .dot-red    { background: #f85149; box-shadow: 0 0 6px #f85149; }
  .dot-grey   { background: #484f58; }
  .status-label { color: #e6edf3; font-weight: 600; }
  .status-detail { color: #8b949e; font-size: 12px; }
  .status-age   { color: #484f58; font-size: 11px; margin-left: 4px; }

  .metric-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 20px 24px;
    text-align: center;
  }
  .metric-value { font-size: 32px; font-weight: 700; margin: 0; }
  .metric-label { font-size: 12px; color: #8b949e; margin: 4px 0 0; text-transform: uppercase; letter-spacing: 1px; }

  /* channel cards */
  .channel-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 18px 22px;
    margin-bottom: 12px;
  }
  .channel-name  { font-size: 15px; font-weight: 600; color: #58a6ff; }
  .channel-stats { font-size: 13px; color: #8b949e; margin-top: 6px; }
  .wr-bar-wrap   { background: #21262d; border-radius: 6px; height: 8px; margin-top: 10px; overflow: hidden; }
  .wr-bar        { height: 8px; border-radius: 6px; }

  /* table */
  .styled-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  .styled-table th {
    background: #21262d;
    color: #8b949e;
    font-weight: 600;
    padding: 10px 14px;
    text-align: left;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    border-bottom: 1px solid #30363d;
  }
  .styled-table td {
    padding: 10px 14px;
    border-bottom: 1px solid #21262d;
    color: #e6edf3;
  }
  .styled-table tr:hover td { background: #21262d; }

  .badge-buy    { background:#1a3a2a; color:#3fb950; border-radius:4px; padding:2px 8px; font-size:12px; font-weight:600; }
  .badge-sell   { background:#3a1a1a; color:#f85149; border-radius:4px; padding:2px 8px; font-size:12px; font-weight:600; }
  .badge-open   { background:#1a2a3a; color:#58a6ff; border-radius:4px; padding:2px 8px; font-size:12px; }
  .badge-closed { background:#21262d; color:#8b949e; border-radius:4px; padding:2px 8px; font-size:12px; }
  .badge-bot      { background:#2d1f63; color:#d2a8ff; border-radius:4px; padding:2px 8px; font-size:11px; }
  .badge-dryrun   { background:#2a2a1a; color:#e3b341; border-radius:4px; padding:2px 8px; font-size:11px; }
  .badge-new      { background:#1a3a2a; color:#3fb950; border-radius:4px; padding:2px 8px; font-size:11px; font-weight:600; }
  .badge-update   { background:#1a2a3a; color:#58a6ff; border-radius:4px; padding:2px 8px; font-size:11px; font-weight:600; }
  .badge-close    { background:#3a1a1a; color:#f85149; border-radius:4px; padding:2px 8px; font-size:11px; font-weight:600; }
  .badge-irrel    { background:#21262d; color:#484f58; border-radius:4px; padding:2px 8px; font-size:11px; }

  .profit-pos { color: #3fb950; font-weight: 600; }
  .profit-neg { color: #f85149; font-weight: 600; }

  /* section titles */
  .section-title {
    font-size: 15px;
    font-weight: 600;
    color: #e6edf3;
    margin: 0 0 16px;
    padding-bottom: 10px;
    border-bottom: 1px solid #30363d;
  }
</style>
""", unsafe_allow_html=True)

# ── init DB ───────────────────────────────────────────────────────────────────
init_db()

# ── data ──────────────────────────────────────────────────────────────────────
positions_raw = fetch_all_positions()
channel_stats = fetch_channel_stats()
signals_raw   = fetch_recent_signals(limit=30)

pos_df  = pd.DataFrame([dict(r) for r in positions_raw])  if positions_raw  else pd.DataFrame()
stat_df = pd.DataFrame([dict(r) for r in channel_stats])  if channel_stats  else pd.DataFrame()
sig_df  = pd.DataFrame([dict(r) for r in signals_raw])    if signals_raw    else pd.DataFrame()

# ── header ────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="dash-header">
  <div>
    <p class="dash-title">📊 Tridi Signal Dashboard</p>
    <p class="dash-sub">Telegram → MetaTrader 5 · Live position tracker</p>
  </div>
</div>
""", unsafe_allow_html=True)

# ── status bar ───────────────────────────────────────────────────────────────
def _age(ts_iso: str) -> str:
    """Human-readable age of a timestamp."""
    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(ts_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - ts).total_seconds())
        if secs < 60:   return f"{secs}s ago"
        if secs < 3600: return f"{secs//60}m ago"
        return f"{secs//3600}h ago"
    except Exception:
        return ""

def _stale(ts_iso: str, threshold_s: int = 120) -> bool:
    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(ts_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() > threshold_s
    except Exception:
        return True

status = read_status()

SERVICES = [
    ("telegram", "📡 Telegram"),
    ("mt5",      "📈 MetaTrader 5"),
]

pills_html = '<div class="status-bar">'
for key, label in SERVICES:
    svc = status.get(key)
    if svc is None:
        dot   = "dot-grey"
        label_txt = label
        detail = "Not started"
        age    = ""
    else:
        stale  = _stale(svc["ts"], threshold_s=180)
        ok     = svc["ok"] and not stale
        dot    = "dot-green" if ok else "dot-red"
        label_txt = label
        detail = svc.get("detail", "")
        age    = _age(svc["ts"])

    pills_html += f"""
    <div class="status-pill">
      <div class="status-dot {dot}"></div>
      <span class="status-label">{label_txt}</span>
      <span class="status-detail">{detail}</span>
      <span class="status-age">{age}</span>
    </div>"""

# DB check
db_ok = True
try:
    from database.db import get_conn
    get_conn().execute("SELECT 1").fetchone()
except Exception:
    db_ok = False

db_dot = "dot-green" if db_ok else "dot-red"
pills_html += f"""
    <div class="status-pill">
      <div class="status-dot {db_dot}"></div>
      <span class="status-label">🗄️ Database</span>
      <span class="status-detail">{"signals.db connected" if db_ok else "Error"}</span>
    </div>"""

pills_html += "</div>"
st.markdown(pills_html, unsafe_allow_html=True)

# ── top metrics ───────────────────────────────────────────────────────────────
total_pos   = len(pos_df)
open_pos    = int((pos_df["status"] == "open").sum())   if not pos_df.empty else 0
closed_pos  = int((pos_df["status"] == "closed").sum()) if not pos_df.empty else 0
total_profit = float(pos_df["profit"].sum())            if not pos_df.empty else 0.0
wins        = int((pos_df["profit"] > 0).sum())         if not pos_df.empty else 0
wr_global   = round(wins / closed_pos * 100, 1)         if closed_pos > 0  else 0.0

c1, c2, c3, c4, c5 = st.columns(5)
def metric_card(col, value, label, color="#e6edf3"):
    col.markdown(f"""
    <div class="metric-card">
      <p class="metric-value" style="color:{color}">{value}</p>
      <p class="metric-label">{label}</p>
    </div>""", unsafe_allow_html=True)

metric_card(c1, total_pos,  "Total Positions")
metric_card(c2, open_pos,   "Open",  "#58a6ff")
metric_card(c3, closed_pos, "Closed","#8b949e")
metric_card(c4, f"{'+'if total_profit>=0 else ''}{total_profit:.2f}", "Total P&L",
            "#3fb950" if total_profit >= 0 else "#f85149")
metric_card(c5, f"{wr_global}%", "Global Win Rate",
            "#3fb950" if wr_global >= 50 else "#f85149")

st.markdown("<br>", unsafe_allow_html=True)

# ── main columns ─────────────────────────────────────────────────────────────
left, right = st.columns([2, 1])

# ── positions table ───────────────────────────────────────────────────────────
with left:
    st.markdown('<p class="section-title">Positions</p>', unsafe_allow_html=True)

    if pos_df.empty:
        st.info("No positions yet. Start the agent and let it run.")
    else:
        # filter
        channels_available = ["All"] + sorted(pos_df["channel"].dropna().unique().tolist())
        selected_channel = st.selectbox("Filter by channel", channels_available, key="ch_filter")
        status_filter = st.radio("Status", ["All", "Open", "Closed"], horizontal=True, key="st_filter")

        filtered = pos_df.copy()
        if selected_channel != "All":
            filtered = filtered[filtered["channel"] == selected_channel]
        if status_filter != "All":
            filtered = filtered[filtered["status"] == status_filter.lower()]

        rows_html = ""
        for _, row in filtered.iterrows():
            action_badge = (
                '<span class="badge-buy">BUY</span>'
                if row.get("action") == "BUY"
                else '<span class="badge-sell">SELL</span>'
            )
            status_badge = (
                '<span class="badge-open">Open</span>'
                if row.get("status") == "open"
                else '<span class="badge-closed">Closed</span>'
            )
            profit = row.get("profit") or 0.0
            profit_cls  = "profit-pos" if profit >= 0 else "profit-neg"
            profit_str  = f"+{profit:.2f}" if profit >= 0 else f"{profit:.2f}"
            channel_str = row.get("channel") or "—"
            symbol_str  = row.get("symbol")  or "—"
            opened_str  = (row.get("opened_at") or "")[:16].replace("T", " ")
            ticket_str  = str(row.get("ticket") or "—")
            sl_str      = f"{row['sl']:.5f}"  if row.get("sl")  else "—"
            tp_str      = f"{row['tp']:.5f}"  if row.get("tp")  else "—"
            # Bot tag: any position opened by this bot has a magic-number comment
            bot_badge   = '<span class="badge-bot">🤖 Bot</span>' if row.get("ticket") else ""

            rows_html += f"""
            <tr>
              <td>{ticket_str} {bot_badge}</td>
              <td><b>{symbol_str}</b></td>
              <td>{action_badge}</td>
              <td>{row.get('open_price') or '—'}</td>
              <td>{row.get('lot') or '—'}</td>
              <td style="color:#f85149">{sl_str}</td>
              <td style="color:#3fb950">{tp_str}</td>
              <td class="{profit_cls}">{profit_str}</td>
              <td>{status_badge}</td>
              <td style="color:#8b949e">@{channel_str}</td>
              <td style="color:#8b949e">{opened_str}</td>
            </tr>"""

        st.markdown(f"""
        <table class="styled-table">
          <thead><tr>
            <th>Ticket</th><th>Symbol</th><th>Side</th>
            <th>Entry</th><th>Lot</th>
            <th>SL</th><th>TP</th><th>P&amp;L</th>
            <th>Status</th><th>Channel</th><th>Opened</th>
          </tr></thead>
          <tbody>{rows_html}</tbody>
        </table>""", unsafe_allow_html=True)

# ── channel win-rate cards ────────────────────────────────────────────────────
with right:
    st.markdown('<p class="section-title">Channel Win Rates</p>', unsafe_allow_html=True)

    if stat_df.empty:
        st.info("No channel data yet.")
    else:
        for _, row in stat_df.iterrows():
            total  = row["total"]  or 1
            wins_c = row["wins"]   or 0
            losses = row["losses"] or 0
            profit_c = row["total_profit"] or 0.0
            closed_c = wins_c + losses
            wr = round(wins_c / closed_c * 100, 1) if closed_c > 0 else 0.0
            wr_color = "#3fb950" if wr >= 50 else "#f85149"
            profit_color = "#3fb950" if profit_c >= 0 else "#f85149"
            profit_sign  = "+" if profit_c >= 0 else ""

            st.markdown(f"""
            <div class="channel-card">
              <div class="channel-name">@{row['channel']}</div>
              <div class="channel-stats">
                {total} signals &nbsp;·&nbsp;
                <span style="color:#3fb950">{wins_c}W</span> /
                <span style="color:#f85149">{losses}L</span> &nbsp;·&nbsp;
                <span style="color:{profit_color}">{profit_sign}{profit_c:.2f}</span>
              </div>
              <div class="wr-bar-wrap">
                <div class="wr-bar" style="width:{wr}%;background:{wr_color}"></div>
              </div>
              <div style="font-size:13px;color:{wr_color};font-weight:600;margin-top:6px">
                {wr}% Win Rate
              </div>
            </div>""", unsafe_allow_html=True)

    # ── win rate donut ────────────────────────────────────────────────────────
    if not stat_df.empty:
        st.markdown('<p class="section-title" style="margin-top:24px">Signal Distribution</p>',
                    unsafe_allow_html=True)

        fig = px.pie(
            stat_df,
            names="channel",
            values="total",
            hole=0.6,
            color_discrete_sequence=["#58a6ff", "#3fb950", "#f85149", "#d2a8ff"],
        )
        fig.update_layout(
            paper_bgcolor="#0d1117",
            plot_bgcolor="#0d1117",
            font_color="#8b949e",
            legend=dict(font=dict(color="#8b949e")),
            margin=dict(t=0, b=0, l=0, r=0),
            height=220,
            showlegend=True,
        )
        fig.update_traces(textinfo="none")
        st.plotly_chart(fig, use_container_width=True)

# ── recent signals feed ───────────────────────────────────────────────────────
st.markdown("<br>", unsafe_allow_html=True)
st.markdown('<p class="section-title">Recent Signals Feed</p>', unsafe_allow_html=True)

if sig_df.empty:
    st.info("No signals parsed yet.")
else:
    LANG_FLAG = {"arabic": "🇸🇦", "french": "🇫🇷", "english": "🇬🇧"}

    TYPE_BADGE = {
        "NEW_SIGNAL": '<span class="badge-new">🟢 New Signal</span>',
        "UPDATE":     '<span class="badge-update">✏️ Update</span>',
        "CLOSE":      '<span class="badge-close">🔒 Close</span>',
        "IRRELEVANT": '<span class="badge-irrel">— Noise</span>',
    }

    rows_html = ""
    for _, row in sig_df.iterrows():
        lang      = row.get("language", "english")
        flag      = LANG_FLAG.get(lang, "🌐")
        action    = row.get("action") or "—"
        msg_type  = row.get("msg_type") or "NEW_SIGNAL"
        a_badge   = (
            '<span class="badge-buy">BUY</span>'   if action == "BUY"  else
            '<span class="badge-sell">SELL</span>'  if action == "SELL" else
            f'<span style="color:#8b949e">{action}</span>'
        )
        type_badge = TYPE_BADGE.get(msg_type, msg_type)
        ts = (row.get("parsed_at") or "")[:16].replace("T", " ")
        tp_str = " / ".join(
            str(row[f"tp{i}"]) for i in (1, 2, 3) if row.get(f"tp{i}")
        ) or "—"
        note_str = (row.get("note") or "")[:60]

        rows_html += f"""
        <tr>
          <td style="color:#8b949e">{ts}</td>
          <td>@{row.get('channel') or '—'}</td>
          <td>{flag} {lang.capitalize()}</td>
          <td>{type_badge}</td>
          <td><b>{row.get('symbol') or '—'}</b></td>
          <td>{a_badge}</td>
          <td>{row.get('entry') or '—'}</td>
          <td style="color:#f85149">{row.get('sl') or '—'}</td>
          <td style="color:#3fb950">{tp_str}</td>
          <td style="color:#8b949e;font-size:11px">{note_str}</td>
        </tr>"""

    st.markdown(f"""
    <table class="styled-table">
      <thead><tr>
        <th>Time</th><th>Channel</th><th>Language</th><th>Type</th>
        <th>Symbol</th><th>Side</th><th>Entry</th><th>SL</th><th>TP(s)</th><th>Note</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>""", unsafe_allow_html=True)

# ── channel vocabulary ────────────────────────────────────────────────────────
st.markdown("<br>", unsafe_allow_html=True)
st.markdown('<p class="section-title">📚 Channel Vocabulary (Learned Keywords)</p>',
            unsafe_allow_html=True)

vocab      = fetch_all_channel_vocabularies()
msg_counts = fetch_raw_message_count()

TYPE_COLOR  = {"NEW_SIGNAL": "#3fb950", "UPDATE": "#58a6ff",
               "CLOSE": "#f85149", "IRRELEVANT": "#484f58"}
ACTION_ICON = {"BUY": "🟢", "SELL": "🔴"}

if not vocab:
    st.info("No vocabulary learned yet — keywords are collected automatically as signals arrive.")
else:
    for ch, keywords in vocab.items():
        total_msgs = msg_counts.get(ch, 0)
        with st.expander(
            f"@{ch}  ·  {len(keywords)} keywords  ·  {total_msgs} messages archived",
            expanded=True
        ):
            groups: dict = {}
            for kw in keywords:
                groups.setdefault(kw["msg_type"], []).append(kw)

            cols = st.columns(max(len(groups), 1))
            for col, (mtype, kws) in zip(cols, groups.items()):
                color = TYPE_COLOR.get(mtype, "#8b949e")
                with col:
                    st.markdown(
                        f'<div style="color:{color};font-weight:700;'
                        f'font-size:13px;margin-bottom:8px">{mtype}</div>',
                        unsafe_allow_html=True,
                    )
                    pills = ""
                    for kw in kws[:20]:
                        icon = ACTION_ICON.get(kw.get("action"), "")
                        hits = kw["hit_count"]
                        size = min(14, 10 + hits // 3)
                        pills += (
                            f'<span style="display:inline-block;margin:3px;'
                            f'padding:3px 9px;border-radius:12px;'
                            f'background:#21262d;color:{color};font-size:{size}px" '
                            f'title="seen {hits}×">'
                            f'{icon} {kw["keyword"]} '
                            f'<span style="color:#484f58;font-size:10px">{hits}</span>'
                            f'</span>'
                        )
                    st.markdown(pills, unsafe_allow_html=True)

# ── auto-refresh ──────────────────────────────────────────────────────────────
st.markdown("<br>", unsafe_allow_html=True)
if st.button("🔄 Refresh"):
    st.rerun()

st.caption("Auto-refresh every 30 s — or press Refresh manually.")

# ── true auto-refresh every 30s ───────────────────────────────────────────────
import time as _time
st_autorefresh = st.empty()
st_autorefresh.markdown(
    f'<meta http-equiv="refresh" content="30">',
    unsafe_allow_html=True,
)
