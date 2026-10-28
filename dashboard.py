"""NSE Stock Tracker - multi-user Streamlit dashboard with login.

Pages: Dashboard, Portfolio, Watchlist, Opportunities, Alerts, Analytics,
Market Data, Reports, Account, and (admins) Users + Settings.
Run:  streamlit run dashboard.py

Security: email + password login (scrypt hashes, lockout after 5 failures).
All personal data lives in PostgreSQL behind ROW LEVEL SECURITY: every query
runs as role nse_app with the logged-in user's id, so the database only
returns that user's holdings, rules, watchlist, alerts and emails.

Data sources (no data is invented; missing data is shown as "n/a"):
  * PostgreSQL nse_app (RLS)         - per-user holdings, rules, watchlist, alerts, emails
  * ClickHouse nse_alerts.*          - shared market data: daily closes, rankings
  * Yahoo Finance (yfinance, cached) - price history for charts, current prices
  * Nifty Indices lists              - company names and industries
  * state/*.json                     - latest recommendations, backtest, live quotes, health
"""
import json
from datetime import datetime, timedelta, time as dtime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st
import yfinance as yf

from alert_system.auth import (SESSION_DAYS, AuthError, admin_create_user, admin_reset_password, authenticate,
                               change_password, create_session, end_session, resume_session,
                               signup)
from alert_system.clickhouse import get_client
from alert_system.config import NOTIFICATION_MODES, load_settings
from alert_system.pgdb import user_tx
from alert_system.portfolio import cumulative_pct, monthly_returns, portfolio_series, trades_from_rows
from alert_system.rules import describe

APP_DIR = Path(__file__).resolve().parent
STATE = APP_DIR / "state"
SESSION_IDLE_HOURS = 12  # same idle limit as the stored sessions
IST = ZoneInfo("Asia/Kolkata")
GREEN, RED, BLUE, ORANGE, PURPLE, MUTED = "#22c55e", "#ef4444", "#3b82f6", "#f59e0b", "#a855f7", "#94a3b8"
PERIODS = {"1W": 7, "MTD": "MTD", "1M": 31, "3M": 92, "6M": 183, "1Y": 366, "ALL": None}
DEFAULT_PERIOD = "MTD"


def period_start(period, last):
    """Start timestamp of a chart period ending at `last`; None = all data."""
    span = PERIODS.get(period or DEFAULT_PERIOD)
    if span == "MTD":
        return pd.Timestamp(last).normalize().replace(day=1)
    return pd.Timestamp(last) - pd.Timedelta(days=span) if span else None

st.set_page_config(page_title="NSE Stock Tracker", page_icon="📈", layout="wide")

CSS = """
<style>
.block-container {padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1680px;}
[data-testid="stSidebar"] {background: #0e1627; border-right: 1px solid #1e293b;}
h1, h2, h3, h4 {letter-spacing: -0.01em;}
.brand {display:flex; align-items:center; gap:10px; font-size:1.35rem; font-weight:800; color:#fff; margin:4px 0 14px;}
.brand span {color:#3b82f6;}
.topbar {display:flex; justify-content:flex-end; align-items:center; gap:16px; flex-wrap:wrap;}
.pill {display:inline-flex; align-items:center; gap:8px; padding:8px 14px; border-radius:10px;
       background:#111a2e; border:1px solid #1e293b; font-size:.82rem; line-height:1.25;}
.dot {width:9px; height:9px; border-radius:50%; display:inline-block;}
.pill b {font-weight:700;} .pill small {color:#94a3b8; display:block;}
.page-title {font-size:2rem; font-weight:800; color:#fff; margin:0;}
.page-sub {color:#94a3b8; margin:2px 0 12px;}
.card {background:#111a2e; border:1px solid #1e293b; border-radius:14px; padding:16px 18px; height:100%;}
.kpi {display:flex; gap:14px; align-items:center;}
.kpi .ico {width:48px; height:48px; border-radius:12px; background:#16233d; display:flex; align-items:center;
           justify-content:center; font-size:1.4rem;}
.kpi .lbl {color:#94a3b8; font-size:.85rem;}
.kpi .val {font-size:1.6rem; font-weight:800; color:#fff; line-height:1.2;}
.badge {display:inline-block; padding:2px 8px; border-radius:6px; font-size:.75rem; font-weight:700;}
.b-green {background:rgba(34,197,94,.15); color:#22c55e; border:1px solid rgba(34,197,94,.35);}
.b-red {background:rgba(239,68,68,.15); color:#ef4444; border:1px solid rgba(239,68,68,.35);}
.b-amber {background:rgba(245,158,11,.18); color:#f59e0b; border:1px solid rgba(245,158,11,.4);}
.b-blue {background:rgba(59,130,246,.15); color:#60a5fa; border:1px solid rgba(59,130,246,.35);}
.b-grey {background:rgba(148,163,184,.12); color:#94a3b8; border:1px solid rgba(148,163,184,.3);}
.sec-title {font-weight:700; font-size:1.05rem; color:#fff; margin-bottom:6px;}
table.dt {width:100%; border-collapse:collapse; font-size:.8rem; table-layout:auto;}
table.dt th {color:#94a3b8; font-weight:600; text-align:left; padding:6px 5px; border-bottom:1px solid #1e293b; white-space:nowrap;}
table.dt td {padding:6px 5px; border-bottom:1px solid #16233d; color:#e2e8f0;}
table.dt td.r, table.dt th.r {text-align:right;}
.pos {color:#22c55e; font-weight:600;} .neg {color:#ef4444; font-weight:600;}
.stat {background:#16233d; border-radius:10px; padding:8px 12px;}
.stat .lbl {color:#94a3b8; font-size:.78rem;} .stat .v {font-size:1.05rem; font-weight:800; white-space:nowrap;}
.warnbox {background:rgba(239,68,68,.10); border-left:4px solid #ef4444; color:#fca5a5; padding:8px 12px;
          border-radius:6px; font-size:.84rem; margin:4px 0 8px;}
.note {color:#94a3b8; font-size:.78rem;}
/* ♥ watchlist buttons: filled red when watched, white outline otherwise */
[class*="st-key-hearton_"] span[role="img"] {color:#ef4444 !important; font-variation-settings:'FILL' 1 !important;}
[class*="st-key-heartoff_"] span[role="img"] {color:#ffffff !important; font-variation-settings:'FILL' 0 !important;}
[class*="st-key-hearton_"] button, [class*="st-key-heartoff_"] button {font-size:1.25rem;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------- helpers ---
def esc(v):
    return escape("" if v is None else str(v))


def money(v, digits=2):
    return "n/a" if v is None or pd.isna(v) else f"₹{v:,.{digits}f}"


def pct_html(v, digits=2):
    if v is None or pd.isna(v):
        return '<span class="note">n/a</span>'
    return f'<span class="{"pos" if v >= 0 else "neg"}">{v:+.{digits}f}%</span>'


def signal_badge(sig):
    cls = {"BUY CANDIDATE": "b-green", "BUY": "b-green", "SELL": "b-red", "WATCHLIST": "b-amber", "HOLD": "b-blue",
           "AVOID": "b-red", "TRIGGERED": "b-green", "ACTIVE": "b-green", "RESET": "b-grey"}.get(sig, "b-grey")
    label = "BUY" if sig == "BUY CANDIDATE" else sig
    return f'<span class="badge {cls}">{esc(label)}</span>'


def html_table(headers, rows, right=()):
    head = "".join(f'<th class="{"r" if i in right else ""}">{esc(h)}</th>' for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(f'<td class="{"r" if i in right else ""}">{c}</td>' for i, c in enumerate(r))
                   + "</tr>" for r in rows)
    return f'<table class="dt"><tr>{head}</tr>{body}</table>'


def card_title(text, icon=""):
    st.markdown(f'<div class="sec-title">{icon} {esc(text)}</div>', unsafe_allow_html=True)


def dark(chart, height=None):
    chart = chart.properties(height=height) if height else chart
    return (chart.properties(background="transparent")
            .configure_axis(labelColor=MUTED, titleColor=MUTED, gridColor="#1e293b", domainColor="#334155",
                            tickColor="#334155", labelFontSize=11)
            .configure_legend(labelColor="#cbd5e1", titleColor=MUTED, orient="top", labelFontSize=11)
            .configure_view(strokeWidth=0))


def read_json(name):
    try:
        return json.loads((STATE / name).read_text())
    except (OSError, ValueError):
        return None


@st.cache_resource
def settings():
    return load_settings()


# ---------------------------------------------------- per-user data (RLS) ---
# Every user query runs as the nse_app role with app.user_id = the logged-in
# user, so PostgreSQL itself only returns/accepts that user's rows. User data
# is never put in st.cache_data (that cache is shared between sessions).
def uid():
    return st.session_state["user"]["id"]


def is_admin():
    return st.session_state.get("user", {}).get("role") == "admin"


def rows(sql, args=()):
    with user_tx(settings(), uid()) as conn:
        return conn.execute(sql, args).fetchall()


def run(sql, args=()):
    with user_tx(settings(), uid()) as conn:
        return conn.execute(sql, args).rowcount


@st.cache_data(ttl=86400, show_spinner=False)
def stock_directory():
    """[{symbol, name, industry, category}] from the official Nifty index lists
    (Nifty 100 / Midcap 150 / Smallcap 250), cached in state/ for offline use."""
    import io
    import requests
    cache = STATE / "stock_directory.json"
    out, seen = [], set()
    try:
        for cat, url in (("Large-cap", "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv"),
                         ("Mid-cap", "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv"),
                         ("Small-cap", "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv")):
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            for rec in pd.read_csv(io.StringIO(r.text)).to_dict("records"):
                sym = f"{str(rec['Symbol']).strip().upper()}.NS"
                if sym not in seen:
                    seen.add(sym)
                    out.append({"symbol": sym, "name": str(rec.get("Company Name", "")).strip(),
                                "industry": str(rec.get("Industry", "") or "Unknown").strip(), "category": cat})
        cache.write_text(json.dumps(out))
    except Exception:  # offline / site change: fall back to the last good copy
        try:
            out = json.loads(cache.read_text())
        except (OSError, ValueError):
            out = []
    return out


def directory_map():
    return {d["symbol"]: d for d in stock_directory()}


@st.cache_data(ttl=60, show_spinner=False)
def live_price(symbol):
    """(price, note) - latest traded price from Yahoo (NSE ~15 min delayed)."""
    try:
        p = yf.Ticker(symbol).fast_info.last_price
        if p and p > 0:
            return round(float(p), 2), f"Yahoo Finance, {datetime.now(IST):%H:%M} IST, NSE prices ~15 min delayed"
    except Exception:  # best-effort autofill; the user can always type the price
        pass
    return None, "current price unavailable - enter it manually"


def normalize_symbol(text):
    s = text.strip().upper()
    return s if not s or s.endswith(".NS") or s.startswith("^") else f"{s}.NS"


# ------------------------------------------------------------- data layer ---
@st.cache_data(ttl=300, show_spinner=False)
def market_snapshot():
    """Latest close + daily/weekly/MTD change per symbol from stored ClickHouse closes."""
    client = get_client(settings())
    try:
        px = client.query("""
            SELECT symbol, trade_date, argMax(close_price, recorded_at) AS close
            FROM nse_alerts.daily_stock_prices WHERE close_price > 0
            GROUP BY symbol, trade_date""").result_rows
        cats = client.query("SELECT DISTINCT symbol, category FROM nse_alerts.stock_categories FINAL").result_rows
    finally:
        client.close()
    order = {"Large-cap": 0, "Mid-cap": 1, "Small-cap": 2}
    category = {}
    for s, c in cats:
        if s not in category or order.get(c, 9) < order.get(category[s], 9):
            category[s] = c
    if not px:
        return pd.DataFrame()
    df = pd.DataFrame(px, columns=["symbol", "date", "close"]).sort_values(["symbol", "date"])
    df["date"] = pd.to_datetime(df["date"])
    out = []
    for sym, g in df.groupby("symbol"):
        g = g.set_index("date")["close"]
        last_d, last = g.index[-1], float(g.iloc[-1])

        def change_from(cut):
            base = g[g.index <= cut]
            return (last / float(base.iloc[-1]) - 1) * 100 if len(base) else None
        month_start = last_d.replace(day=1)
        out.append({"Symbol": sym, "Category": category.get(sym, "n/a"), "Close": last, "Close date": last_d.date(),
                    "Day %": change_from(g.index[-2]) if len(g) > 1 else None,
                    "Week %": change_from(last_d - pd.Timedelta(days=7)),
                    "MTD %": change_from(month_start - pd.Timedelta(days=1))})
    return pd.DataFrame(out)


@st.cache_data(ttl=300, show_spinner=False)
def latest_trade_date():
    client = get_client(settings())
    try:
        value = client.command("SELECT max(trade_date) FROM nse_alerts.daily_stock_prices")
    finally:
        client.close()
    return pd.Timestamp(value).date()  # command() returns text


@st.cache_data(ttl=300, show_spinner=False)
def mtd_rankings():
    client = get_client(settings())
    try:
        r = client.query("""SELECT trade_date, symbol, category, close_price, mtd_change_pct, ranking_type
                            FROM nse_alerts.daily_mtd_rankings""").result_rows
    finally:
        client.close()
    return pd.DataFrame(r, columns=["Date", "Symbol", "Category", "Close", "MTD %", "Ranking"])


@st.cache_data(ttl=3600, show_spinner="Loading price history…")
def ohlcv(symbols, days=760):
    """{symbol: DataFrame[Open, High, Low, Close, Volume]} from Yahoo (cached 1h)."""
    symbols = list(symbols)
    if not symbols:
        return {}
    start = (datetime.now(IST) - timedelta(days=days)).date()
    data = yf.download(symbols, start=str(start), interval="1d", auto_adjust=False, progress=False,
                       group_by="ticker", threads=True, timeout=20)
    out = {}
    if data is None or data.empty:
        return out
    multi = getattr(data.columns, "nlevels", 1) > 1
    for s in symbols:
        if multi and s not in set(data.columns.get_level_values(0)):
            continue
        f = (data[s] if multi else data)[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
        if not f.empty:
            f.index = pd.to_datetime(f.index).tz_localize(None)
            out[s] = f
    return out


@st.cache_data(ttl=86400, show_spinner=False)
def sector_of(symbol):
    """Industry from the Nifty index list; Yahoo profile as fallback."""
    d = directory_map().get(symbol)
    if d and d["industry"] not in ("", "Unknown", "nan"):
        return d["industry"]
    try:
        return yf.Ticker(symbol).info.get("sector") or "Unknown"
    except Exception:  # profile lookups are best-effort; never break the page
        return "Unknown"


def holdings(active_only=True):
    sql = ("SELECT id, symbol, buy_date, buy_price, quantity, notes, status, sell_date, sell_price FROM holdings"
           + (" WHERE status='ACTIVE'" if active_only else "") + " ORDER BY buy_date, id")
    return rows(sql)


def my_watchlist():
    return {r["symbol"] for r in rows("SELECT symbol FROM watchlist")}


def watchlist_symbols():
    syms = {r["symbol"] for r in rows("SELECT DISTINCT symbol FROM user_alerts WHERE enabled")}
    syms |= {h["symbol"] for h in holdings()}
    syms |= my_watchlist()
    return sorted(syms)


def alert_history(limit=50):
    return rows("SELECT id, symbol, metric, condition, threshold, triggered_value, triggered_at, status, "
                "trigger_price, trigger_session FROM alert_history ORDER BY id DESC LIMIT %s", (limit,))


@st.cache_data(ttl=60, show_spinner=False)
def live_quotes(symbols):
    """({symbol: (price, quote_time)}, fetched_at) from Yahoo 1-minute bars.
    Re-fetched at most once a minute; NSE quotes are ~15 minutes delayed."""
    from alert_system.intraday import YahooQuotes
    fetched = datetime.now(IST)
    try:
        quotes = YahooQuotes().get_quotes(list(symbols)) if symbols else {}
    except Exception:  # live prices are an enhancement; fall back to stored closes
        quotes = {}
    return {s: (q.price, q.ts.astimezone(IST)) for s, q in quotes.items()}, fetched


def latest_prices(symbols):
    """{symbol: (price, date)}: live quote when it is at least as recent as the
    stored close, else the stored ClickHouse close, else Yahoo daily history."""
    snap = market_snapshot()
    out = {}
    if not snap.empty:
        out = {r.Symbol: (r.Close, r._4) for r in snap.itertuples()}
    wanted = tuple(sorted(set(symbols)))
    live, _ = live_quotes(wanted)
    for s, (price, ts) in live.items():
        if s not in out or ts.date() >= out[s][1]:
            out[s] = (price, ts.date())
    missing = [s for s in symbols if s not in out]
    if missing:
        for s, f in ohlcv(tuple(missing), days=30).items():
            out[s] = (float(f["Close"].iloc[-1]), f.index[-1].date())
    return out


def portfolio_kpis():
    act = holdings()
    prices = latest_prices([h["symbol"] for h in act])
    invested = sum(h["buy_price"] * h["quantity"] for h in act)
    priced = [h for h in act if h["symbol"] in prices]
    value = sum(prices[h["symbol"]][0] * h["quantity"] for h in priced)
    cost_priced = sum(h["buy_price"] * h["quantity"] for h in priced)
    pnl = value - cost_priced
    return {"invested": invested, "value": value, "pnl": pnl,
            "pnl_pct": (pnl / cost_priced * 100) if cost_priced else None, "count": len(act),
            "unpriced": [h["symbol"] for h in act if h["symbol"] not in prices]}


def market_status(now=None):
    now = now or datetime.now(IST)
    open_ = now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30)
    if open_:
        return True, "NSE MARKET OPEN", "Regular session 09:15 - 15:30 IST"
    return False, "NSE MARKET CLOSED", "Regular session 09:15 - 15:30 IST, Mon-Fri"


def trigger_jobs(job_ids):
    from alert_system.scheduler import Scheduler
    from alert_system.state import JobStore
    s = settings()
    sch = Scheduler(s, JobStore(s.scheduler_db), runners={})
    return [sch.trigger_manual(j) for j in job_ids]


# --------------------------------------------------------------- widgets ---
def top_bar(title, subtitle):
    is_open, label, detail = market_status()
    live = read_json("live_quotes.json") or {}
    last_db = latest_trade_date()
    left, right = st.columns([5, 7], vertical_alignment="center")
    with left:
        st.markdown(f'<div class="page-title">{esc(title)}</div><div class="page-sub">{esc(subtitle)}</div>',
                    unsafe_allow_html=True)
    with right:
        c1, c2, c3 = st.columns([4, 4, 3], vertical_alignment="center")
        c1.markdown(f'<div class="pill"><span class="dot" style="background:{GREEN if is_open else RED}"></span>'
                    f'<div><b style="color:{GREEN if is_open else RED}">{label}</b><small>{detail}</small></div></div>',
                    unsafe_allow_html=True)
        syms = tuple(sorted({h["symbol"] for h in holdings()} | my_watchlist()))
        quotes, fetched = live_quotes(syms)
        if quotes:
            newest = max(ts for _, ts in quotes.values())
            head = f"Live prices fetched {fetched:%d %b %H:%M:%S}"
            sub = f"quotes up to {newest:%d %b %H:%M} IST (~15 min delay) · stored close {last_db:%d %b}"
        else:
            live_t = live.get("checked_at", "")
            head = f"Stored close: {last_db:%d %b %Y}"
            sub = (pd.Timestamp(live_t).tz_convert(IST).strftime("last live check %d %b %H:%M")
                   if live_t else "live prices unavailable")
        c2.markdown(f'<div class="pill">🕒<div><b>{esc(head)}</b><small>{esc(sub)}</small></div></div>',
                    unsafe_allow_html=True)
        if c3.button("Refresh Prices", icon=":material/refresh:", type="primary", width="stretch",
                     disabled=not is_admin(), help=None if is_admin() else "Admins only"):
            try:
                trigger_jobs(["price_import", "intraday_alerts"])
                st.toast("Price download and live check queued - the service runs them within seconds.")
            except Exception as exc:
                st.toast(f"Could not queue refresh: {exc}")
            st.cache_data.clear()
            st.rerun()


def kpi_card(col, icon, label, value, extra=""):
    col.markdown(f'<div class="card kpi"><div class="ico">{icon}</div><div><div class="lbl">{esc(label)}</div>'
                 f'<div class="val">{value} {extra}</div></div></div>', unsafe_allow_html=True)


def kpi_row():
    k = portfolio_kpis()
    snap = market_snapshot()
    cols = st.columns(5)
    kpi_card(cols[0], "💼", "Total Invested", money(k["invested"], 0))
    kpi_card(cols[1], "📊", "Portfolio Value", money(k["value"], 0))
    pnl_color = GREEN if k["pnl"] >= 0 else RED
    badge = (f'<span class="badge {"b-green" if k["pnl"] >= 0 else "b-red"}">{k["pnl_pct"]:+.2f}%</span>'
             if k["pnl_pct"] is not None else "")
    kpi_card(cols[2], "📈", "Unrealized P&L",
             f'<span style="color:{pnl_color}">{"+" if k["pnl"] >= 0 else "-"}₹{abs(k["pnl"]):,.0f}</span>', badge)
    kpi_card(cols[3], "🗂️", "Active Holdings", str(k["count"]))
    kpi_card(cols[4], "🎯", "Stocks Tracked", str(len(snap)))
    if k["unpriced"]:
        st.caption(f"No price available for: {', '.join(k['unpriced'])} (excluded from value and P&L).")


def portfolio_frame():
    """(time-weighted series of CURRENT holdings, closes DataFrame) for this user.
    Sold lots are excluded so performance, drawdown and monthly returns all
    describe what you hold now (sales remain in the Portfolio history)."""
    trades = trades_from_rows(holdings(active_only=True))
    if not trades:
        return None, pd.DataFrame()
    hist = ohlcv(tuple(sorted({t.symbol for t in trades})))
    closes = pd.DataFrame({s: f["Close"] for s, f in hist.items()}).sort_index().ffill()
    series = portfolio_series(trades, closes) if not closes.empty else pd.DataFrame()
    return series, closes


def holding_paths(closes):
    """{label: % return vs buy price since the buy date} for each ACTIVE lot."""
    lots = holdings()
    multi = {h["symbol"] for h in lots if sum(x["symbol"] == h["symbol"] for x in lots) > 1}
    out = {}
    for h in lots:
        if h["symbol"] not in closes:
            continue
        s = closes[h["symbol"]].dropna()
        s = s[s.index >= pd.Timestamp(h["buy_date"])]
        if s.empty:
            continue
        label = h["symbol"].removesuffix(".NS") + (f" #{h['id']}" if h["symbol"] in multi else "")
        out[label] = (s / h["buy_price"] - 1) * 100
    return out


def performance_card():
    with st.container(border=True):
        h1, h2 = st.columns([2, 3], vertical_alignment="center")
        with h1:
            card_title("Portfolio Performance", "📶")
        with h2:
            period = st.segmented_control("Period", list(PERIODS), default=DEFAULT_PERIOD, key="perf_period",
                                          label_visibility="collapsed")
        series, closes = portfolio_frame()
        st.session_state["_portfolio_series"] = series
        st.session_state["_closes"] = closes
        paths = holding_paths(closes) if closes is not None and not closes.empty else {}
        if not paths:
            st.info("Add a purchase on the Portfolio page to see your holdings' performance.")
            return
        last = closes.index[-1]
        lots = [h for h in holdings() if h["symbol"] in closes]
        first_buy = min(pd.Timestamp(h["buy_date"]) for h in lots)
        start = period_start(period, last) or first_buy
        # Every holding's price relative to YOUR buy price across the whole
        # selected period; the part before the purchase date is dashed.
        frames, buys = [], []
        multi = {h["symbol"] for h in lots if sum(x["symbol"] == h["symbol"] for x in lots) > 1}
        for h in lots:
            label = h["symbol"].removesuffix(".NS") + (f" #{h['id']}" if h["symbol"] in multi else "")
            s = closes[h["symbol"]].dropna()
            s = s[s.index >= start]
            if s.empty:
                continue
            bought = pd.Timestamp(h["buy_date"])
            pct = (s / h["buy_price"] - 1) * 100
            value = s * h["quantity"]  # market value of this lot
            held = pct[pct.index >= bought]
            before = pct[pct.index <= (held.index[0] if len(held) else pct.index[-1])]
            if len(before) > 1 or (len(before) == 1 and not len(held)):
                frames.append(pd.DataFrame({"Date": before.index, "Holding": label, "Return %": before.values,
                                            "Value": value[before.index].values, "Phase": "Before purchase"}))
            if len(held):
                frames.append(pd.DataFrame({"Date": held.index, "Holding": label, "Return %": held.values,
                                            "Value": value[held.index].values, "Phase": "Held"}))
                buys.append({"Date": held.index[0], "Holding": label, "Return %": float(held.iloc[0]),
                             "Bought": f"{h['quantity']:g} @ ₹{h['buy_price']:,.2f} on {h['buy_date']}"})
        if len(lots) > 1 and series is not None and not series.empty:
            comb = cumulative_pct(series["index"], max(start, series.index[0]), baseline=1.0)
            frames.append(pd.DataFrame({"Date": comb.index, "Holding": "All holdings (combined)",
                                        "Return %": comb.values, "Value": series["value"].reindex(comb.index).values,
                                        "Phase": "Held"}))
        long = pd.concat(frames, ignore_index=True).dropna(subset=["Return %"])
        ends = long.sort_values("Date").groupby("Holding")["Return %"].last()
        st.caption(f"{period or DEFAULT_PERIOD} view from {start:%d %b %Y} · % vs your buy price "
                   f"(dashed = before you bought) · " + "  ".join(f"{k} {v:+.2f}%" for k, v in ends.items()))
        color = alt.Color("Holding:N", title=None,
                          scale=alt.Scale(range=[GREEN, BLUE, ORANGE, PURPLE, "#06b6d4", "#eab308", RED]))
        x = alt.X("Date:T", title=None)
        y = alt.Y("Return %:Q", title=None, axis=alt.Axis(labelExpr="datum.value + '%'"))
        base = alt.Chart(long).encode(x=x, y=y, color=color)
        lines = base.mark_line(strokeWidth=2).encode(
            detail="Phase:N",
            strokeDash=alt.StrokeDash("Phase:N", legend=alt.Legend(title=None, orient="top", symbolStrokeColor="#e2e8f0"),
                                      scale=alt.Scale(domain=["Held", "Before purchase"], range=[[1, 0], [4, 4]])),
            opacity=alt.condition("datum.Phase == 'Held'", alt.value(1.0), alt.value(0.55)))
        # hover: vertical rule + one tooltip with every holding's value and % on that date
        long["Tip"] = [(f"₹{v:,.2f} " if pd.notna(v) else "") + f"({r:+.2f}%)" for v, r in zip(long["Value"], long["Return %"])]
        wide = long.pivot_table(index="Date", columns="Holding", values="Tip", aggfunc="last").reset_index()
        names = [c for c in wide.columns if c != "Date"]
        wide.columns = ["Date"] + [f"s{i}" for i in range(len(names))]
        nearest = alt.selection_point(nearest=True, on="pointerover", fields=["Date"], empty=False, clear="pointerout")
        rule = alt.Chart(wide).mark_rule(color="#94a3b8").encode(
            x=x, opacity=alt.condition(nearest, alt.value(0.7), alt.value(0)),
            tooltip=[alt.Tooltip("Date:T", title="Date", format="%d %b %Y")]
            + [alt.Tooltip(f"s{i}:N", title=n) for i, n in enumerate(names)]).add_params(nearest)
        dots = base.mark_point(filled=True, size=55).encode(opacity=alt.condition(nearest, alt.value(1), alt.value(0)))
        layers = [alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="#475569").encode(y="y:Q"), lines, rule, dots]
        if buys:
            layers.append(alt.Chart(pd.DataFrame(buys)).mark_point(shape="triangle-up", size=120, filled=True,
                                                                   color="#ffffff").encode(
                x=x, y=y, tooltip=["Holding:N", alt.Tooltip("Bought:N", title="Purchase")]))
        st.altair_chart(dark(alt.layer(*layers), 300), width="stretch")


def candle_chart(symbol, period):
    hist = ohlcv((symbol,)).get(symbol)
    if hist is None or hist.empty:
        st.warning(f"No price history available for {symbol}.")
        return
    f = hist.copy()
    f["MA 20"] = f["Close"].rolling(20).mean()
    f["MA 50"] = f["Close"].rolling(50).mean()
    if PERIODS.get(period):
        f = f[f.index >= f.index[-1] - pd.Timedelta(days=PERIODS[period])]
    f = f.reset_index(names="Date")
    last = f.iloc[-1]
    st.markdown(f'<span style="color:{BLUE}">●</span> Price <b>{money(last["Close"])}</b> &nbsp; '
                f'<span style="color:{PURPLE}">●</span> MA 20 <b>{money(last["MA 20"])}</b> &nbsp; '
                f'<span style="color:{ORANGE}">●</span> MA 50 <b>{money(last["MA 50"])}</b> &nbsp; '
                f'<span class="note">close {last["Date"]:%d %b %Y}</span>', unsafe_allow_html=True)
    up = alt.condition("datum.Open <= datum.Close", alt.value(GREEN), alt.value(RED))
    base = alt.Chart(f).encode(x=alt.X("Date:T", title=None, axis=alt.Axis(labels=False, ticks=False)))
    vol_base = alt.Chart(f).encode(x=alt.X("Date:T", title=None))
    wick = base.mark_rule().encode(y=alt.Y("Low:Q", scale=alt.Scale(zero=False), title=None), y2="High:Q", color=up)
    body = base.mark_bar(size=4).encode(y="Open:Q", y2="Close:Q", color=up,
                                        tooltip=[alt.Tooltip("Date:T"), alt.Tooltip("Open:Q", format=",.2f"),
                                                 alt.Tooltip("High:Q", format=",.2f"), alt.Tooltip("Low:Q", format=",.2f"),
                                                 alt.Tooltip("Close:Q", format=",.2f")])
    ma20 = base.mark_line(color=PURPLE, strokeWidth=1.5).encode(y="MA 20:Q")
    ma50 = base.mark_line(color=ORANGE, strokeWidth=1.5).encode(y="MA 50:Q")
    vol = vol_base.mark_bar(size=3).encode(y=alt.Y("Volume:Q", title="Volume", axis=alt.Axis(format="~s")),
                                       color=up).properties(height=70)
    chart = alt.vconcat((wick + body + ma20 + ma50).properties(height=250), vol, spacing=4)
    st.altair_chart(dark(chart.resolve_scale(x="shared")), width="stretch")


def stock_trend_card(key="dash"):
    with st.container(border=True):
        h1, h2 = st.columns([3, 2], vertical_alignment="center")
        options = watchlist_symbols()
        snap = market_snapshot()
        if not snap.empty:
            options += [s for s in snap["Symbol"].tolist() if s not in options]
        with h1:
            card_title("Selected Stock Price Trend", "📍")
        with h2:
            symbol = st.selectbox("Stock", options or ["RELIANCE.NS"], key=f"trend_sym_{key}",
                                  label_visibility="collapsed")
        period = st.segmented_control("Range", ["1M", "3M", "6M", "1Y", "ALL"], default="3M",
                                      key=f"trend_period_{key}", label_visibility="collapsed")
        candle_chart(symbol, period or "3M")


def holding_swings(closes):
    """Per active holding since its buy date: now / best / worst % vs buy price
    and the current fall from its highest close."""
    out = []
    for label, path in holding_paths(closes).items():
        price_index = 1 + path / 100
        out.append({"Holding": label, "Now %": float(path.iloc[-1]), "Best %": float(path.max()),
                    "Worst %": float(path.min()),
                    "From peak %": float((price_index.iloc[-1] / price_index.max() - 1) * 100),
                    "Days held": int((path.index[-1] - path.index[0]).days)})
    return pd.DataFrame(out)


def drawdown_card():
    with st.container(border=True):
        card_title("Drawdown Analysis", "📉")
        closes = st.session_state.get("_closes")
        sw = holding_swings(closes) if closes is not None and not closes.empty else pd.DataFrame()
        if sw.empty:
            st.info("No active holdings yet.")
            return
        worst, best = sw.loc[sw["Worst %"].idxmin()], sw.loc[sw["Best %"].idxmax()]
        c1, c2, c3 = st.columns(3)
        for col, lbl, v, color in ((c1, "Lowest point", f"{worst['Worst %']:+.1f}%", RED if worst["Worst %"] < 0 else GREEN),
                                   (c2, "Highest point", f"{best['Best %']:+.1f}%", GREEN if best["Best %"] >= 0 else RED),
                                   (c3, "Holdings down now", f"{int((sw['Now %'] < 0).sum())}/{len(sw)}", "#fff")):
            col.markdown(f'<div class="stat"><div class="lbl">{lbl}</div><div class="v" style="color:{color}">{v}</div></div>',
                         unsafe_allow_html=True)
        base = alt.Chart(sw).encode(y=alt.Y("Holding:N", title=None, sort="-x"))
        rng = base.mark_rule(strokeWidth=6, opacity=0.35, color=MUTED).encode(x=alt.X("Worst %:Q", title=None), x2="Best %:Q")
        lo = base.mark_tick(color=RED, thickness=3, size=16).encode(x="Worst %:Q")
        hi = base.mark_tick(color=GREEN, thickness=3, size=16).encode(x="Best %:Q")
        now = base.mark_point(filled=True, size=90).encode(
            x="Now %:Q", color=alt.condition("datum['Now %'] >= 0", alt.value(GREEN), alt.value(RED)),
            tooltip=["Holding:N", alt.Tooltip("Now %:Q", format="+.2f"), alt.Tooltip("Best %:Q", format="+.2f"),
                     alt.Tooltip("Worst %:Q", format="+.2f"), alt.Tooltip("From peak %:Q", format=".2f"),
                     "Days held:Q"])
        zero = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(color="#475569").encode(x="x:Q")
        st.altair_chart(dark(zero + rng + lo + hi + now, max(140, 46 * len(sw))), width="stretch")
        st.caption("Bar = lowest to highest % vs buy price since purchase; dot = now.")


def monthly_card():
    with st.container(border=True):
        card_title("Monthly Returns", "📊")
        series = st.session_state.get("_portfolio_series")
        if series is None or series.empty:
            st.info("No portfolio history yet.")
            return
        m = monthly_returns(series["index"]).tail(12).rename_axis("Month").reset_index()
        chart = alt.Chart(m).mark_bar().encode(
            x=alt.X("Month:T", timeUnit="yearmonth", title=None, axis=alt.Axis(format="%b")),
            y=alt.Y("return_pct:Q", title=None),
            color=alt.condition("datum.return_pct >= 0", alt.value(GREEN), alt.value(RED)),
            tooltip=[alt.Tooltip("Month:T", timeUnit="yearmonth", title="Month"),
                     alt.Tooltip("return_pct:Q", title="Return %", format="+.2f")])
        st.altair_chart(dark(chart, 250), width="stretch")


def allocation_card():
    with st.container(border=True):
        card_title("Sector Allocation", "🧭")
        act = holdings()
        prices = latest_prices([h["symbol"] for h in act])
        alloc = {}
        for h in act:
            if h["symbol"] in prices:
                sec = sector_of(h["symbol"])
                alloc[sec] = alloc.get(sec, 0) + prices[h["symbol"]][0] * h["quantity"]
        if not alloc:
            st.info("No priced holdings.")
            return
        total = sum(alloc.values())
        d = pd.DataFrame({"Sector": list(alloc), "Value": list(alloc.values())})
        d["Share %"] = d["Value"] / total * 100
        palette = [BLUE, PURPLE, ORANGE, GREEN, RED, "#06b6d4", MUTED, "#eab308"]
        donut = alt.Chart(d).mark_arc(innerRadius=62, outerRadius=95).encode(
            theta="Value:Q", color=alt.Color("Sector:N", scale=alt.Scale(range=palette), title=None),
            tooltip=["Sector:N", alt.Tooltip("Value:Q", format=",.0f"), alt.Tooltip("Share %:Q", format=".1f")])
        center = alt.Chart(pd.DataFrame({"t": [f"₹{total:,.0f}"]})).mark_text(
            fontSize=17, fontWeight="bold", color="#fff").encode(text="t:N")
        st.altair_chart(dark(donut + center, 250), width="stretch")
        st.caption("Industry from the Nifty index lists (Yahoo profile if unlisted); value at latest close.")


@st.dialog("Add to watchlist & set alert")
def watch_dialog(a):
    """Popup from the ♥ button: watchlist + optional price-level or % alert."""
    sym = a["symbol"]
    price, note = live_price(sym)
    ref = price or a.get("price")
    st.markdown(f"**{esc(sym)}** · current price **{money(ref)}**  \n"
                f'<span class="note">{esc(note if price else "last close " + str(a.get("price_date", "")))}</span>',
                unsafe_allow_html=True)
    if a.get("stop") or a.get("target1") or a.get("target"):
        entry = (f"{money(a.get('entry_low'))} - {money(a.get('entry_high'))}" if a.get('entry_low') else money(a.get('price')))
        st.caption(f"Model levels (research only): entry {entry}, "
                   f"stop {money(a.get('stop'))}, target {money(a.get('target1') or a.get('target'))}")
    in_list = sym in my_watchlist()
    kind = st.radio("Alert type", ["Price level (₹)", "Daily move (%)", "Move from base price (%)",
                                   "No alert, just watch"], horizontal=True)
    cond = st.selectbox("Trigger when", ["Above or equal", "Below or equal"])
    value = None
    if kind == "Price level (₹)":
        tgt = a.get("target1") or a.get("target")
        if a.get("signal") == "SELL":  # short: target below, stop above
            default = a.get("stop") if cond == "Above or equal" else tgt
        else:
            default = tgt if cond == "Above or equal" else a.get("stop")
        value = st.number_input("Trigger price (₹)", min_value=0.01, value=float(round(default or ref or 100, 2)),
                                step=0.5, format="%.2f")
        if ref:
            st.caption(f"That is {(value / ref - 1) * 100:+.2f}% from the current price.")
    elif kind == "Daily move (%)":
        value = st.number_input("Daily change (%)", min_value=0.1, value=5.0, step=0.5, format="%.2f")
        if ref:
            st.caption(f"≈ {money(ref * (1 + value / 100)) if cond == 'Above or equal' else money(ref * (1 - value / 100))}"
                       " at today's price")
    elif kind == "Move from base price (%)":
        base = st.number_input("Base price (₹) - live price, or type your own (e.g. your buy price)", min_value=0.0,
                               value=float(price or 0.0), step=0.5, format="%.2f")
        value = st.number_input("Change from base price (%)", min_value=0.1, value=5.0, step=0.5, format="%.2f")
        if base:
            st.caption(f"Fires once at "
                       f"{money(base * (1 + value / 100)) if cond == 'Above or equal' else money(base * (1 - value / 100))}"
                       ", then switches off."
                       + (f" Live now {money(price)} = {(price / base - 1) * 100:+.2f}% from base." if price else ""))
    c1, c2 = st.columns(2)
    if c1.button("Save", type="primary", width="stretch"):
        if kind == "Move from base price (%)" and not base:
            st.error("Enter a base price (the live price is unavailable right now).")
            return
        run("INSERT INTO watchlist (symbol) VALUES (%s) ON CONFLICT DO NOTHING", (sym,))
        if value is not None:
            metric = {"Price level (₹)": "Price", "Daily move (%)": "Daily", "Move from base price (%)": "SinceSet"}[kind]
            run("INSERT INTO user_alerts (symbol, metric, condition, threshold, base_price) VALUES (%s, %s, %s, %s, %s)",
                (sym, metric, cond, float(value), base if metric == "SinceSet" else None))
        st.toast(f"♥ {sym} saved to your watchlist" + (" with an alert" if value is not None else ""))
        st.rerun()
    if in_list and c2.button("Remove from watchlist", width="stretch"):
        run("DELETE FROM watchlist WHERE symbol=%s", (sym,))
        st.toast(f"{sym} removed from your watchlist")
        st.rerun()


def opportunity_rows(items, key):
    """Rows with a ♥ button each (filled when already on your watchlist)."""
    watched = my_watchlist()
    widths = [0.25, 1.5, 0.95, 0.85, 0.5, 0.62, 0.45]
    head = st.columns(widths)
    for col, h in zip(head, ["#", "Symbol", "Price ₹", "Signal", "R:R", "Score", ""]):
        col.markdown(f'<span class="note"><b>{h}</b></span>', unsafe_allow_html=True)
    for i, a in enumerate(items, 1):
        c = st.columns(widths, vertical_alignment="center")
        c[0].markdown(f'<span class="note">{i}</span>', unsafe_allow_html=True)
        c[1].markdown(f'<div style="white-space:nowrap;font-weight:700;font-size:.85rem">'
                      f'{esc(a["symbol"].removesuffix(".NS"))}</div>', unsafe_allow_html=True)
        c[2].markdown(f'<div style="font-size:.85rem">{a["price"]:,.2f}</div>', unsafe_allow_html=True)
        c[3].markdown(signal_badge(a["signal"]), unsafe_allow_html=True)
        c[4].markdown(f'<div style="font-size:.85rem">{a["risk_reward"]:.1f}</div>' if a.get("risk_reward") else "n/a",
                      unsafe_allow_html=True)
        c[5].markdown(f'<b style="color:{GREEN}">{a["score"]:.0f}</b>', unsafe_allow_html=True)
        on = a["symbol"] in watched
        if c[6].button("❤️" if on else ":material/favorite_border:",
                       key=f"{'hearton' if on else 'heartoff'}_{key}_{a['symbol']}",
                       type="tertiary",
                       help="On your watchlist - edit or remove" if on else "Add to watchlist & set an alert"):
            watch_dialog(a)


def intraday_status_note(snap):
    if not snap:
        return "Intraday scan has not run yet (weekdays every 15 min, 09:30-14:30 IST)."
    when = pd.Timestamp(snap["data_time"]).tz_convert(IST).strftime("%d %b %H:%M") if snap.get("data_time") else "n/a"
    status = {"open": "live session", "no_new_entries": "after 14:30 - no new entries, reference only",
              "no_session_data": "no data for today's session (holiday/weekend?)"}.get(snap["status"], snap["status"])
    replay = " · REPLAY of a past scan (not live)" if snap.get("replay") else ""
    return (f"5-min bars up to {when} IST (Yahoo, ~15 min delayed) · {status}{replay} · square off by "
            f"{snap['rules']['square_off']} · validation {snap.get('validation', 'UNVALIDATED')}")


def intraday_rows(side_key, key, limit):
    snap = read_json("intraday_latest.json")
    st.markdown('<div class="warnbox">Intraday signals use ~15-minute delayed data and are research output, '
                'not trade instructions. SELL = short-sell (needs an intraday margin product).</div>',
                unsafe_allow_html=True)
    if snap and snap.get(side_key):
        opportunity_rows(snap[side_key][:limit], key)
    else:
        st.info("No intraday candidates right now.")
    st.caption(intraday_status_note(snap))


def opportunities_card(limit=None):
    with st.container(border=True):
        limit = limit or settings().recommendations.get("top_n", 10)
        h1, h2 = st.columns([1.4, 2], vertical_alignment="center")
        with h1:
            card_title(f"Top {limit} Opportunities", "🏆")
        with h2:
            view = st.segmented_control("View", ["Swing", "Intraday BUY", "Intraday SELL"], default="Swing",
                                        key="opp_view", label_visibility="collapsed")
        if view == "Intraday BUY":
            intraday_rows("buys", "dash_ib", limit)
            return
        if view == "Intraday SELL":
            intraday_rows("sells", "dash_is", limit)
            return
        snap = read_json("recommendations_latest.json")
        if not snap:
            st.info("The recommendations job has not run yet (weekdays 16:25).")
            return
        if snap.get("validation") == "FAILED":
            st.markdown('<div class="warnbox">Backtest FAILED - research output, not trade recommendations.</div>',
                        unsafe_allow_html=True)
        items = snap["rankings"].get("cross_asset", [])[:limit]
        if not items:
            st.info("No instrument met all criteria today.")
            return
        opportunity_rows(items, "dash")
        st.caption(f"Run {snap['run_date']} · closes as of {items[0]['price_date']} · validation {snap['validation']}")


def alert_message(h):
    if h["metric"].startswith("Price"):
        return f"Price {money(h['trigger_price'])} met '{describe('Price', h['condition'], h['threshold'])}'"
    if h["metric"] == "SinceSet":
        return (f"Move {h['triggered_value']:+.2f}% from the base price met "
                f"'{describe('SinceSet', h['condition'], h['threshold'])}'")
    return f"{h['metric']} move {h['triggered_value']:+.2f}% met '{h['condition']} {h['threshold']}%'"


def recent_alerts_card(limit=8):
    with st.container(border=True):
        card_title("Recent Alerts", "🔔")
        hist = alert_history(limit)
        if not hist:
            st.info("No alerts have triggered yet.")
            return
        prices = latest_prices(list({h["symbol"] for h in hist}))
        body = []
        for h in hist:
            at = pd.Timestamp(h["triggered_at"]).tz_convert(IST) if h["triggered_at"] else None
            cur = prices.get(h["symbol"], (None, None))[0]
            # move since the alert fired (trigger price -> current); the move that fired it is in the message
            since = (cur / h["trigger_price"] - 1) * 100 if cur and h["trigger_price"] else None
            body.append([at.strftime("%d %b %Y %I:%M %p") if at is not None else "n/a", f"<b>{esc(h['symbol'])}</b>",
                         f"{esc(h['metric'])} {esc(h['condition'])}", money(h["trigger_price"]), money(cur),
                         pct_html(since) if since is not None else "n/a", signal_badge(h["status"]),
                         esc(alert_message(h))])
        st.markdown(html_table(["Time", "Symbol", "Alert Type", "Trigger Price", "Current Price", "Change since alert",
                                "Status", "Message"], body, right=(3, 4, 5)), unsafe_allow_html=True)


# ------------------------------------------------------------------ pages ---
def page_dashboard():
    top_bar("Market Intelligence", "Your portfolio. Market insights. Better decisions.")
    kpi_row()
    st.write("")
    left, right = st.columns(2)
    with left:
        performance_card()
    with right:
        stock_trend_card()
    c1, c2, c3, c4 = st.columns([1.05, 0.8, 0.95, 1.6])
    with c1:
        drawdown_card()
    with c2:
        monthly_card()
    with c3:
        allocation_card()
    with c4:
        opportunities_card()
    recent_alerts_card()


def page_portfolio():
    top_bar("Portfolio", "Holdings stay active until you mark them sold.")
    kpi_row()
    act = holdings()
    prices = latest_prices([h["symbol"] for h in act])
    snap = market_snapshot().set_index("Symbol") if not market_snapshot().empty else pd.DataFrame()
    with st.container(border=True):
        card_title("Active holdings", "💼")
        if not act:
            st.info("No active purchases yet. Record one below.")
        else:
            data = []
            for h in act:
                p, d = prices.get(h["symbol"], (None, None))
                cost = h["buy_price"] * h["quantity"]
                val = p * h["quantity"] if p is not None else None
                info = snap.loc[h["symbol"]] if h["symbol"] in snap.index else {}
                data.append({"ID": h["id"], "Symbol": h["symbol"], "Buy date": h["buy_date"], "Qty": h["quantity"],
                             "Buy price": h["buy_price"], "Last close": p, "Close date": d,
                             "Day %": info.get("Day %") if len(info) else None, "Cost": cost, "Value": val,
                             "P&L": (val - cost) if val is not None else None,
                             "P&L %": ((val / cost - 1) * 100) if val is not None and cost else None})
            st.dataframe(pd.DataFrame(data), hide_index=True, width="stretch", column_config={
                c: st.column_config.NumberColumn(format="₹%.2f") for c in ("Buy price", "Last close", "Cost", "Value", "P&L")
            } | {c: st.column_config.NumberColumn(format="%+.2f%%") for c in ("Day %", "P&L %")})
    left, right = st.columns(2)
    with left, st.container(border=True):
        card_title("Record a purchase", "➕")
        purchase_form()
    with right, st.container(border=True):
        card_title("Record a sale", "↗️")
        if not act:
            st.info("No active holdings to sell.")
        else:
            opts = {f"#{h['id']} · {h['symbol']} · Qty {h['quantity']:g} · {h['buy_date']}": h for h in act}
            chosen = opts[st.selectbox("Choose purchase", list(opts), key="sell_pick", on_change=_fill_sell_price)]
            if "sell_price" not in st.session_state:
                st.session_state["sell_price"] = live_price(chosen["symbol"])[0] or float(chosen["buy_price"])
            c1, c2 = st.columns(2)
            sdate = c1.date_input("Sale date", value=datetime.now(IST).date(), key="sell_date")
            sprice = c2.number_input("Sell price (₹/share)", min_value=0.01, step=1.0, key="sell_price")
            st.caption("Sell price prefilled with the current price; edit it to your actual fill.")
            if st.button("Mark as sold", width="stretch"):
                n = run("UPDATE holdings SET status='SOLD', sell_date=%s, sell_price=%s WHERE id=%s AND status='ACTIVE'",
                        (sdate, sprice, chosen["id"]))
                st.success(f"Purchase #{chosen['id']} marked as sold." if n else "Nothing changed.")
                st.session_state.pop("sell_price", None)
                st.rerun()
    with st.expander("Purchase and sale history"):
        allh = holdings(active_only=False)
        st.dataframe(pd.DataFrame(allh), hide_index=True, width="stretch") if allh else st.info("No history yet.")


def stock_options():
    """Searchable labels 'SYMBOL - Company (Category)' -> NSE symbol."""
    opts = {f"{d['symbol'].removesuffix('.NS')} - {d['name']} ({d['category']})": d["symbol"]
            for d in stock_directory()}
    snap = market_snapshot()
    known = set(opts.values())
    for s in (snap["Symbol"].tolist() if not snap.empty else []):
        if s not in known:
            opts[s.removesuffix(".NS")] = s
    return opts


def resolve_symbol(label):
    """Suggestion label, NSE symbol or (part of) a company name -> NSE symbol."""
    if not label:
        return None
    opts = stock_options()
    if label in opts:
        return opts[label]
    text = label.split(" - ")[0].strip()
    sym = normalize_symbol(text.replace(" ", ""))
    if sym in opts.values():
        return sym
    matches = [d["symbol"] for d in stock_directory() if text.lower() in d["name"].lower()]
    return matches[0] if matches else sym


def _fill_buy_price():
    label = st.session_state.get("buy_pick")
    if not label:
        return
    sym = resolve_symbol(label)
    price, note = live_price(sym)
    st.session_state["buy_symbol"] = sym
    st.session_state["buy_price_note"] = note
    if price:
        st.session_state["buy_price"] = price


def _fill_sell_price():
    st.session_state.pop("sell_price", None)


def purchase_form():
    """Type part of a symbol or company name -> pick from the suggestions; the
    buy price is filled with the current price (editable)."""
    opts = stock_options()
    st.selectbox("Stock", list(opts), index=None, key="buy_pick", on_change=_fill_buy_price,
                 placeholder="Type a symbol or company name, e.g. REL or Reliance", accept_new_options=True,
                 help="Suggestions come from the Nifty 100/Midcap 150/Smallcap 250 lists. "
                      "You can also type any NSE symbol.")
    st.session_state.setdefault("buy_price", 100.0)
    c1, c2, c3 = st.columns(3)
    bdate = c1.date_input("Purchase date", value=datetime.now(IST).date(), key="buy_date")
    bprice = c2.number_input("Buy price (₹/share)", min_value=0.01, step=0.5, format="%.2f", key="buy_price")
    bqty = c3.number_input("Quantity", min_value=0.001, value=1.0, step=1.0, key="buy_qty")
    sym = st.session_state.get("buy_symbol")
    if st.session_state.get("buy_pick"):
        st.caption(f"{sym}: {st.session_state.get('buy_price_note', '')} · total ₹{bprice * bqty:,.2f}")
    notes = st.text_input("Notes (optional)", key="buy_notes")
    if st.button("Save purchase", type="primary", width="stretch"):
        if not sym or not sym.endswith(".NS") or not sym[:-3].replace("-", "").replace("&", "").isalnum():
            st.error("Pick a stock from the list or type a valid NSE symbol (e.g. RELIANCE).")
            return
        run("INSERT INTO holdings (symbol, buy_date, buy_price, quantity, notes) VALUES (%s, %s, %s, %s, %s)",
            (sym, bdate, bprice, bqty, notes))
        st.success(f"Saved purchase: {bqty:g} × {sym} @ ₹{bprice:,.2f}")
        for k in ("buy_pick", "buy_symbol", "buy_price_note", "buy_notes"):
            st.session_state.pop(k, None)
        st.rerun()


def page_watchlist():
    top_bar("Watchlist", "Your watched stocks, alert-rule stocks and holdings.")
    with st.container(border=True):
        card_title("Add to watchlist", "♥")
        opts = stock_options()
        c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
        pick = c1.selectbox("Stock", list(opts), index=None, key="wl_pick", accept_new_options=True,
                            placeholder="Type a symbol or company name")
        if c2.button("Add", type="primary", width="stretch") and pick:
            run("INSERT INTO watchlist (symbol) VALUES (%s) ON CONFLICT DO NOTHING",
                (resolve_symbol(pick),))
            st.rerun()
        mine = sorted(my_watchlist())
        if mine:
            c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
            rm = c1.selectbox("Remove", mine, key="wl_rm")
            if c2.button("Remove", width="stretch"):
                run("DELETE FROM watchlist WHERE symbol=%s", (rm,))
                st.rerun()
    syms = watchlist_symbols()
    snap = market_snapshot()
    live = read_json("live_quotes.json") or {}
    live_quotes = live.get("quotes", {})
    rules = rows("SELECT symbol, metric, condition, threshold, base_price FROM user_alerts WHERE enabled")
    with st.container(border=True):
        card_title("Tracked stocks", "👁️")
        body = []
        for s in syms:
            r = snap[snap["Symbol"] == s].iloc[0] if not snap.empty and (snap["Symbol"] == s).any() else None
            lq = live_quotes.get(s)
            rule_txt = "; ".join(describe(x["metric"], x["condition"], x["threshold"], x["base_price"])
                                 + ("" if x["metric"] in ("Price", "SinceSet") else f" ({x['metric']})")
                                 for x in rules if x["symbol"] == s)
            body.append([f"<b>{esc(s)}</b>", esc(r["Category"]) if r is not None else "n/a",
                         money(r["Close"]) if r is not None else "n/a", esc(r["Close date"]) if r is not None else "",
                         pct_html(r["Day %"]) if r is not None else "n/a", pct_html(r["Week %"]) if r is not None else "n/a",
                         pct_html(r["MTD %"]) if r is not None else "n/a",
                         f'{money(lq["price"])} {pct_html(lq["change_pct"])}' if lq else '<span class="note">-</span>',
                         esc(rule_txt) or '<span class="note">no rule</span>'])
        st.markdown(html_table(["Symbol", "Category", "Last close", "Date", "Day", "Week", "MTD",
                                "Live (delayed ~15 min)", "Alert rules"], body, right=(2, 4, 5, 6)),
                    unsafe_allow_html=True)
        if live.get("checked_at"):
            st.caption(f"Live column from the last intraday check at "
                       f"{pd.Timestamp(live['checked_at']).tz_convert(IST):%d %b %H:%M} IST (Yahoo Finance, delayed).")
    stock_trend_card(key="watch")


def page_opportunities():
    top_bar("Opportunities", "Model rankings from completed daily bars.")
    snap = read_json("recommendations_latest.json")
    bt = read_json("backtest_latest.json")
    if not snap:
        st.info("No recommendation output yet (weekdays 16:25).")
        return
    if snap["validation"] == "FAILED":
        oos = (bt or {}).get("segments", {}).get("out_of_sample", {})
        st.markdown(f'<div class="warnbox">This strategy FAILED its backtest: out-of-sample expectancy '
                    f'{oos.get("expectancy_pct")}% per trade after costs, profit factor {oos.get("profit_factor")}. '
                    f'Lists below are research output only, not trade recommendations.</div>', unsafe_allow_html=True)
    tabs = st.tabs(["Overall", "Large-cap", "Mid-cap", "Small-cap", "Cross-asset", "Gold / Silver", "Backtest",
                    "Intraday BUY", "Intraday SELL", "Intraday backtest"])
    for tab, key in zip(tabs[:5], ["overall", "Large-cap", "Mid-cap", "Small-cap", "cross_asset"]):
        with tab:
            items = snap["rankings"].get(key, [])
            if not items:
                st.info("No instrument met all quality, risk and confirmation criteria.")
                continue
            body = [[str(i), f"<b>{esc(a['symbol'])}</b>", esc(a["category"]), money(a["price"]), esc(a["price_date"]),
                     signal_badge(a["signal"]), f"{a['score']:.1f}", f"{money(a['entry_low'])} - {money(a['entry_high'])}",
                     money(a["stop"]), money(a["target1"]), f"1:{a['risk_reward']:.1f}", esc(a["confidence"])]
                    for i, a in enumerate(items, 1)]
            st.markdown(html_table(["#", "Symbol", "Category", "Price", "Close date", "Signal", "Score", "Entry zone",
                                    "Stop", "Target 1", "R:R", "Confidence"], body, right=(3, 6, 8, 9, 10)),
                        unsafe_allow_html=True)
            st.markdown('<span class="note">♡ = add to your watchlist and set a trigger price or %</span>',
                        unsafe_allow_html=True)
            opportunity_rows(items, f"opp_{key}")
            for a in items:
                with st.expander(f"Why {a['symbol']}?"):
                    st.write("**Score components:** " + ", ".join(f"{k} {v:.0f}" for k, v in a["components"].items()))
                    st.write("**Reasons:** " + "; ".join(a["reasons"]))
                    st.write("**Risks / invalidation:** " + "; ".join(a["risks"]))
                    st.write(f"**Target basis:** {a['target_basis']} · **Position size:** {a['position_size']}")
    with tabs[5]:
        body = [[f"<b>{esc(c['symbol'])}</b>", esc(c["name"]), esc(c["asset_class"]), esc(c["exchange"]),
                 money(c["price"]), esc(c["price_date"]), signal_badge(c["signal"]),
                 esc(c["excluded_reason"] or f"score {c['score']}")] for c in snap.get("commodities", [])]
        st.markdown(html_table(["Instrument", "Name", "Type", "Exchange", "Close", "Date", "Signal", "Detail"], body,
                               right=(4,)), unsafe_allow_html=True)
        st.caption("NSE ETFs only. MCX futures are not available from the data provider; ETF prices are not bullion prices.")
    with tabs[6]:
        if not bt:
            st.info("No backtest yet (Saturdays 07:00).")
        else:
            seg = pd.DataFrame(bt["segments"]).T
            st.write(f"**Validation: {bt['validation']}** · period {bt['period'][0]} to {bt['period'][1]} · "
                     f"{bt['universe_size']} instruments")
            st.dataframe(seg, width="stretch")
            st.dataframe(pd.DataFrame(bt["by_category"]).T, width="stretch")
            st.dataframe(pd.DataFrame(bt["by_regime"]).T, width="stretch")
            st.caption("Known biases: " + "; ".join(bt["biases"]))
    isnap = read_json("intraday_latest.json")
    for tab, side in ((tabs[7], "buys"), (tabs[8], "sells")):
        with tab:
            st.markdown('<div class="warnbox">Intraday signals use ~15-minute delayed data and are research output, '
                        'not trade instructions. SELL = short-sell (needs an intraday margin product); '
                        'square off before the cut-off.</div>', unsafe_allow_html=True)
            st.caption(intraday_status_note(isnap))
            items = (isnap or {}).get(side, [])
            if not items:
                st.info("No intraday candidates in the latest scan.")
                continue
            body = [[str(i), f"<b>{esc(a['symbol'])}</b>", esc(a["category"]), money(a["price"]), esc(a["bar_time"]),
                     signal_badge(a["signal"]), f"{a['score']:.1f}", money(a["stop"]), money(a["target"]),
                     f"1:{a['risk_reward']:.1f}", money(a["vwap"]), f"{a['rsi']:.0f}", f"{a['volume_surge']:.1f}×",
                     pct_html(a["day_pct"]), f"{a['n_conf']}/5"] for i, a in enumerate(items, 1)]
            st.markdown(html_table(["#", "Symbol", "Category", "Price", "Bar", "Signal", "Score", "Stop", "Target",
                                    "R:R", "VWAP", "RSI", "Volume", "Day", "Conf."], body,
                                   right=(3, 6, 7, 8, 10, 11, 12, 13)), unsafe_allow_html=True)
            opportunity_rows(items, f"opp_{side}")
    with tabs[9]:
        ibt = read_json("intraday_backtest.json")
        if not ibt:
            st.info("No intraday backtest yet (Saturdays 08:00, or `python -m alert_system intraday --backtest`).")
        else:
            if ibt["validation"] != "VALIDATED":
                st.markdown(f'<div class="warnbox">Intraday strategy validation: {ibt["validation"]}. '
                            'Do not trade these signals as recommendations.</div>', unsafe_allow_html=True)
            st.write(f"**Validation: {ibt['validation']}** · period {ibt['period'][0]} to {ibt['period'][1]} · "
                     f"{ibt['symbols']} stocks · costs {ibt['assumptions']['round_trip_cost_pct']}% round trip"
                     if ibt.get("period") else f"**Validation: {ibt['validation']}**")
            st.dataframe(pd.DataFrame(ibt["segments"]).T, width="stretch")
            st.dataframe(pd.DataFrame(ibt["by_side"]).T, width="stretch")
            st.caption("Known biases: " + "; ".join(ibt["biases"]))


def evaluate_rules(snapshot):
    from alert_system.rules import since_set_change, triggered as rule_triggered
    out = []
    metric_col = {"Daily": "Day %", "Weekly": "Week %", "Monthly": "MTD %"}
    rules = rows("SELECT id, symbol, metric, condition, threshold, base_price, enabled FROM user_alerts ORDER BY id")
    live, _ = live_quotes(tuple(sorted({a["symbol"] for a in rules})))
    for a in rules:
        r = snapshot[snapshot["Symbol"] == a["symbol"]] if not snapshot.empty else pd.DataFrame()
        trig, cur = None, None
        if not r.empty:
            close = float(r.iloc[0]["Close"])
            lq = live.get(a["symbol"])
            # a live quote from a newer session than the stored close: today's move = live vs that close
            live_day = lq is not None and lq[1].date() > r.iloc[0]["Close date"]
            if live_day:
                prev_close, close = close, lq[0]
            if a["metric"] == "Daily" and live_day:
                cur = (close / prev_close - 1) * 100
                trig = rule_triggered("Daily", a["condition"], a["threshold"], cur, close)
            elif a["metric"] == "Price":
                cur, trig = close, rule_triggered("Price", a["condition"], a["threshold"], None, close)
            elif a["metric"] == "SinceSet":
                cur = since_set_change(close, a["base_price"])
                trig = rule_triggered("SinceSet", a["condition"], a["threshold"], None, close, a["base_price"])
            else:
                cur = r.iloc[0][metric_col[a["metric"]]]
                if cur is not None and pd.notna(cur):
                    trig = rule_triggered("Daily", a["condition"], a["threshold"], cur, close)
        out.append({"ID": a["id"], "Stock": a["symbol"],
                    "Rule": describe(a["metric"], a["condition"], a["threshold"], a["base_price"]),
                    "Type": a["metric"],
                    "Current": "n/a" if cur is None or pd.isna(cur) else (money(cur) if a["metric"] == "Price"
                                                                         else f"{cur:+.2f}%"),
                    "Enabled": bool(a["enabled"]),
                    "Status": "No data" if trig is None else ("TRIGGERED" if trig else "Watching")})
    return pd.DataFrame(out)


def page_alerts():
    top_bar("Alerts", "Daily, price and move-from-set rules are checked end-of-day and live every 5 minutes "
                      "(market hours).")
    snap = market_snapshot()
    left, right = st.columns([1, 2])
    with left, st.container(border=True):
        card_title("Create alert rule", "➕")
        opts = stock_options()
        pick = st.selectbox("Stock", list(opts), index=None, key="rule_pick", accept_new_options=True,
                            placeholder="Type a symbol or company name")
        metric = st.selectbox("Rule type", ["Price", "Daily", "SinceSet", "Weekly", "Monthly"],
                              format_func=lambda m: {"Price": "Price level (₹)", "Daily": "Daily move (%)",
                                                     "SinceSet": "Move from a base price (%) - fires once",
                                                     "Weekly": "Weekly move (%) - dashboard only",
                                                     "Monthly": "Month-to-date move (%) - dashboard only"}[m])
        cond = st.selectbox("Trigger when", ["Above or equal", "Below or equal"])
        sym = resolve_symbol(pick)
        if metric == "Price":
            ref = live_price(sym)[0] if sym else None
            thr = st.number_input("Trigger price (₹)", min_value=0.01, value=float(ref or 100.0), step=0.5, format="%.2f")
            if ref:
                st.caption(f"Current ≈ {money(ref)}; trigger is {(thr / ref - 1) * 100:+.2f}% away.")
        elif metric == "SinceSet":
            live = live_price(sym)[0] if sym else None
            # keyed by symbol so the default refreshes when another stock is picked
            base = st.number_input("Base price (₹) - live price, or type your own (e.g. your buy price)",
                                   min_value=0.0, value=float(live or 0.0), step=0.5, format="%.2f", key=f"base_{sym}")
            thr = st.number_input("Change from base price (%)", min_value=0.1, value=5.0, step=0.5, format="%.2f")
            if base:
                target = base * (1 + thr / 100) if cond == "Above or equal" else base * (1 - thr / 100)
                st.caption(f"Fires once at {money(target)} ({'+' if cond == 'Above or equal' else '-'}{thr:g}% from "
                           f"{money(base)}), then switches off."
                           + (f" Live now {money(live)} = {(live / base - 1) * 100:+.2f}% from base." if live else ""))
        else:
            thr = st.number_input("Threshold (%)", min_value=0.1, value=5.0, step=0.5, format="%.2f")
        if st.button("Save alert", type="primary", width="stretch"):
            if not sym:
                st.error("Pick a stock first.")
            elif metric == "SinceSet" and not base:
                st.error("Enter a base price (the live price is unavailable right now).")
            else:
                run("INSERT INTO user_alerts (symbol, metric, condition, threshold, base_price) VALUES (%s, %s, %s, %s, %s)",
                    (sym, metric, cond, float(thr), base if metric == "SinceSet" else None))
                st.success(f"Alert saved for {sym}.")
                st.rerun()
    with right, st.container(border=True):
        card_title("Your alert rules", "🛎️")
        ev = evaluate_rules(snap)
        if ev.empty:
            st.info("No rules yet.")
        else:
            edited = st.data_editor(ev, hide_index=True, width="stretch", disabled=[c for c in ev.columns if c != "Enabled"],
                                    key="rules_editor")
            c1, c2, c3 = st.columns([2, 2, 2])
            if c1.button("Save enabled/disabled", width="stretch"):
                for r in edited.itertuples():
                    run("UPDATE user_alerts SET enabled=%s WHERE id=%s", (bool(r.Enabled), int(r.ID)))
                st.success("Rules updated.")
                st.rerun()
            del_id = c2.selectbox("Rule to delete", ev["ID"].tolist(), label_visibility="collapsed")
            if c3.button("Delete rule", width="stretch"):
                run("DELETE FROM user_alerts WHERE id=%s", (int(del_id),))
                st.success(f"Deleted rule #{del_id}.")
                st.rerun()
            st.caption("Current/Status use the live price (~15 min delayed) while a newer session than the last stored "
                       "close is trading, else stored daily closes. Emails and live checks cover Price, Daily and move-from-set "
                       "rules. Move-from-set rules switch themselves off after firing once.")
    recent_alerts_card(limit=50)


def page_analytics():
    top_bar("Analytics", "Market breadth from stored daily closes.")
    snap = market_snapshot()
    if snap.empty:
        st.warning("No stored prices in ClickHouse yet.")
        return
    ok = snap.dropna(subset=["MTD %"])
    cols = st.columns(4)
    best, worst = ok.loc[ok["MTD %"].idxmax()], ok.loc[ok["MTD %"].idxmin()]
    kpi_card(cols[0], "🎯", "Stocks with data", str(len(ok)))
    kpi_card(cols[1], "📈", "Average MTD", pct_html(ok["MTD %"].mean()))
    kpi_card(cols[2], "🏅", "Best MTD", f'{esc(best.Symbol)}', pct_html(best["MTD %"]))
    kpi_card(cols[3], "⚠️", "Worst MTD", f'{esc(worst.Symbol)}', pct_html(worst["MTD %"]))
    left, right = st.columns(2)
    with left, st.container(border=True):
        card_title("Average MTD change by category", "📊")
        g = ok.groupby("Category", as_index=False)["MTD %"].mean()
        chart = alt.Chart(g).mark_bar().encode(x=alt.X("Category:N", title=None), y=alt.Y("MTD %:Q", title=None),
                                               color=alt.condition("datum['MTD %'] >= 0", alt.value(GREEN), alt.value(RED)),
                                               tooltip=["Category:N", alt.Tooltip("MTD %:Q", format="+.2f")])
        st.altair_chart(dark(chart, 280), width="stretch")
    with right, st.container(border=True):
        card_title("Advancers vs decliners (last session)", "⚖️")
        day = ok.dropna(subset=["Day %"])
        breadth = pd.DataFrame({"Side": ["Advancers", "Decliners", "Unchanged"],
                                "Count": [(day["Day %"] > 0).sum(), (day["Day %"] < 0).sum(), (day["Day %"] == 0).sum()]})
        chart = alt.Chart(breadth).mark_bar().encode(
            x=alt.X("Count:Q", title=None), y=alt.Y("Side:N", title=None, sort=None),
            color=alt.Color("Side:N", scale=alt.Scale(domain=["Advancers", "Decliners", "Unchanged"],
                                                      range=[GREEN, RED, MUTED]), legend=None))
        st.altair_chart(dark(chart, 280), width="stretch")
    with st.container(border=True):
        card_title("Top and bottom 15 by MTD change", "↕️")
        tb = pd.concat([ok.nlargest(15, "MTD %"), ok.nsmallest(15, "MTD %")])
        chart = alt.Chart(tb).mark_bar().encode(
            y=alt.Y("Symbol:N", sort="-x", title=None), x=alt.X("MTD %:Q", title=None),
            color=alt.condition("datum['MTD %'] >= 0", alt.value(GREEN), alt.value(RED)),
            tooltip=["Symbol:N", "Category:N", alt.Tooltip("MTD %:Q", format="+.2f")])
        st.altair_chart(dark(chart, 520), width="stretch")
    stock_trend_card(key="analytics")


def page_market_data():
    top_bar("Market Data", "Stored daily closes for the tracked universe (ClickHouse).")
    snap = market_snapshot()
    if snap.empty:
        st.warning("No stored prices in ClickHouse yet.")
        return
    fmt = {"Close": st.column_config.NumberColumn(format="₹%.2f")} | {
        c: st.column_config.NumberColumn(format="%+.2f%%") for c in ("Day %", "Week %", "MTD %")}
    tabs = st.tabs(["Large-cap", "Mid-cap", "Small-cap", "MTD Top 25"])
    for tab, cat in zip(tabs[:3], ["Large-cap", "Mid-cap", "Small-cap"]):
        with tab:
            d = snap[snap["Category"] == cat].sort_values("MTD %", ascending=False, na_position="last")
            st.caption(f"{len(d)} symbols")
            st.dataframe(d, hide_index=True, width="stretch", height=560, column_config=fmt)
    with tabs[3]:
        r = mtd_rankings()
        if r.empty:
            st.info("No saved MTD rankings yet (weekdays 16:30).")
        else:
            st.caption(f"Snapshot {r['Date'].max()} · daily closes, not live quotes")
            for cat in ("Large-cap", "Mid-cap", "Small-cap"):
                g = r[r["Category"] == cat]
                st.markdown(f"**{cat}**")
                c1, c2 = st.columns(2)
                cfg = {"Close": st.column_config.NumberColumn(format="₹%.2f"),
                       "MTD %": st.column_config.NumberColumn(format="%+.2f%%")}
                c1.dataframe(g[g["Ranking"] == "GAINER"].sort_values("MTD %", ascending=False)[["Symbol", "Close", "MTD %"]],
                             hide_index=True, width="stretch", column_config=cfg)
                c2.dataframe(g[g["Ranking"] == "LOSER"].sort_values("MTD %")[["Symbol", "Close", "MTD %"]],
                             hide_index=True, width="stretch", column_config=cfg)


def me():
    """The logged-in user's own row (RLS: only this row is visible)."""
    return rows("SELECT id, email, name, role, prefs FROM users WHERE id = app_user_id()")[0]


def page_reports():
    top_bar("Reports", "Your personalised daily email (your watchlist, your alerts, enabled sections).")
    s = settings()
    from alert_system.report import build_report, report_settings_for
    user = me()
    client = get_client(s)
    try:
        built = build_report(s, client, datetime.now(IST), user)
    finally:
        client.close()
    rep = report_settings_for(s, user["prefs"])
    c1, c2 = st.columns([3, 1], vertical_alignment="center")
    c1.caption(f"Mode: {rep.mode} · sent weekdays at the scheduled report time · to {user['email']} "
               "(change the mode on the Account page)")
    if built is None:
        st.info("Your notification mode is TRIGGERED_ALERTS_ONLY: no daily report is sent.")
    else:
        subject, text, html = built
        if c2.button("Email me this report now", icon=":material/send:", type="primary", width="stretch"):
            stamp = datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S")
            try:
                with user_tx(s, uid()) as conn:  # always to the caller's own address, rate-limited
                    conn.execute("SELECT request_email(%s, %s, %s, %s, %s)",
                                 (f"report:{stamp}", subject, text, html, s.report.email_max_attempts))
                st.toast(f"Report queued for {user['email']} - delivered within ~20 seconds.")
            except Exception as exc:
                st.error(f"Could not queue the email: {exc}")
        st.markdown(f"**Subject:** {esc(subject)}")
        with st.container(border=True):
            st.html(f'<div style="background:#fff;border-radius:8px;padding:16px">{html}</div>')
    with st.container(border=True):
        card_title("Your recent emails", "✉️")
        em = rows("SELECT id, subject, status, attempts, last_error, created_at, sent_at "
                  "FROM notification_outbox WHERE channel='email' ORDER BY id DESC LIMIT 25")
        st.dataframe(pd.DataFrame(em), hide_index=True, width="stretch") if em else st.info("No emails yet.")


def page_account():
    top_bar("Account", "Your profile, notification preferences and password.")
    s = settings()
    user = me()
    prefs = dict(user["prefs"] or {})
    left, right = st.columns(2)
    with left, st.container(border=True):
        card_title("Profile & notifications", "👤")
        st.text_input("Email (login and alert address)", user["email"], disabled=True)
        name = st.text_input("Name", user["name"])
        modes = ["(system default: " + s.report.mode + ")"] + list(NOTIFICATION_MODES)
        cur = prefs.get("email_mode")
        mode = st.selectbox("Email notifications", modes, index=modes.index(cur) if cur in modes else 0,
                            help="TRIGGERED_ALERTS_ONLY: only alert emails. WATCHLIST_DAILY_SUMMARY: alerts + "
                                 "daily watchlist report. RECOMMENDATIONS_ONLY / COMBINED_DAILY_REPORT include "
                                 "model rankings.")
        mtd = st.checkbox("Email me the month-to-date rankings (weekdays 16:30)", prefs.get("mtd_rankings_email", True))
        if st.button("Save preferences", type="primary", width="stretch"):
            if mode.startswith("("):
                prefs.pop("email_mode", None)
            else:
                prefs["email_mode"] = mode
            prefs["mtd_rankings_email"] = bool(mtd)
            run("UPDATE users SET name=%s, prefs=%s WHERE id = app_user_id()", (name.strip(), json.dumps(prefs)))
            st.success("Saved.")
    with right, st.container(border=True):
        card_title("Change password", "🔑")
        change_password_form()


def change_password_form(forced=False):
    with st.form("pw_form", clear_on_submit=True):
        cur = st.text_input("Current password" if not forced else "Temporary password", type="password")
        new = st.text_input("New password (min 10 characters, letters + digits/symbols)", type="password")
        again = st.text_input("Repeat new password", type="password")
        if st.form_submit_button("Change password", type="primary", width="stretch"):
            if new != again:
                st.error("The new passwords do not match.")
            else:
                try:
                    change_password(settings(), uid(), cur, new)
                    st.session_state["user"]["must_change_password"] = False
                    # the database revoked every session of this user; keep this browser signed in
                    remember_browser(uid())
                    st.success("Password changed.")
                    if forced:
                        st.rerun()
                except AuthError as exc:
                    st.error(str(exc))


def page_users():
    top_bar("Users", "Invite people, reset passwords and deactivate accounts (admins only).")
    if not is_admin():
        st.error("Admins only.")
        return
    s = settings()
    with user_tx(s, uid()) as conn:
        users = conn.execute("SELECT * FROM admin_list_users()").fetchall()
    pending = [u for u in users if u["signup_pending"]]
    with st.container(border=True):
        card_title(f"Sign-up requests ({len(pending)})", "📝")
        if not pending:
            st.caption(f"No pending requests. Sign-up mode: {s.signup_mode} (SIGNUP_MODE in .env).")
        for p in pending:
            c1, c2, c3 = st.columns([4, 1, 1], vertical_alignment="center")
            c1.markdown(f"**{esc(p['name'])}** · {esc(p['email'])}  \n"
                        f'<span class="note">requested {pd.Timestamp(p["created_at"]).tz_convert(IST):%d %b %Y %H:%M}'
                        f'</span>', unsafe_allow_html=True)
            for col, label, approve in ((c2, "Approve", True), (c3, "Reject", False)):
                if col.button(label, key=f"rev_{label}_{p['id']}", type="primary" if approve else "secondary",
                              width="stretch"):
                    with user_tx(s, uid()) as conn:
                        conn.execute("SELECT admin_review_signup(%s, %s)", (p["id"], approve))
                    st.toast(f"{p['email']} {'approved' if approve else 'rejected'}")
                    st.rerun()
    users = [u for u in users if not u["signup_pending"]]
    with st.container(border=True):
        card_title("Accounts", "👥")
        st.dataframe(pd.DataFrame([{**u, "id": str(u["id"])} for u in users]), hide_index=True, width="stretch")
    left, right = st.columns(2)
    with left, st.container(border=True):
        card_title("Invite a user", "➕")
        with st.form("invite", clear_on_submit=True):
            email = st.text_input("Email")
            name = st.text_input("Name")
            role = st.selectbox("Role", ["member", "admin"])
            if st.form_submit_button("Create account", type="primary", width="stretch"):
                try:
                    temp = admin_create_user(s, uid(), email, name, role)
                    st.success(f"Created {email}. Share this one-time temporary password securely; "
                               "they must change it at first login:")
                    st.code(temp)
                except Exception as exc:
                    st.error(f"Could not create the account: {exc}")
    with right, st.container(border=True):
        card_title("Manage a user", "🛠️")
        by_email = {u["email"]: u for u in users}
        pick = st.selectbox("User", list(by_email))
        u = by_email[pick]
        c1, c2 = st.columns(2)
        if c1.button("Reset password", width="stretch"):
            temp = admin_reset_password(s, uid(), u["id"])
            st.success(f"Temporary password for {pick} (must be changed at next login):")
            st.code(temp)
        label = "Deactivate" if u["is_active"] else "Activate"
        if c2.button(label, width="stretch"):
            try:
                with user_tx(s, uid()) as conn:
                    conn.execute("SELECT admin_update_user(%s, %s, NULL, NULL)", (u["id"], not u["is_active"]))
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
        new_role = st.selectbox("Role", ["member", "admin"], index=0 if u["role"] == "member" else 1, key="role_pick")
        if new_role != u["role"] and st.button("Change role", width="stretch"):
            try:
                with user_tx(s, uid()) as conn:
                    conn.execute("SELECT admin_update_user(%s, NULL, %s, NULL)", (u["id"], new_role))
                st.rerun()
            except Exception as exc:
                st.error(str(exc))


def page_settings():
    top_bar("Settings", "System status (admins only). Edit .env / config/alert_system.json to change.")
    if not is_admin():
        st.error("Admins only.")
        return
    import sqlite3  # scheduler state (system-level, not user data)
    s = settings()
    health = read_json("health.json") or {}
    c1, c2, c3, c4 = st.columns(4)
    kpi_card(c1, "🩺", "Service heartbeat",
             pd.Timestamp(health["ts"]).tz_convert(IST).strftime("%H:%M:%S") if health.get("ts") else "n/a")
    kpi_card(c2, "✉️", "Default email mode", esc(s.report.mode.replace("_", " ").title()))
    kpi_card(c3, "📡", "Live provider", esc(f"{s.live.provider} ({'on' if s.live.enabled else 'off'})"))
    kpi_card(c4, "🧹", "MTD cleanup", "dry-run" if s.cleanup_dry_run else ("on" if s.cleanup_enabled else "off"))
    with st.container(border=True):
        card_title("Scheduled jobs", "🗓️")
        last = {}
        with sqlite3.connect(s.scheduler_db, timeout=30) as conn:
            for job_id, status, fin, err in conn.execute(
                    "SELECT job_id, status, COALESCE(finished_at, updated_at), last_error FROM job_runs "
                    "WHERE id IN (SELECT max(id) FROM job_runs WHERE status IN ('SUCCESS','FAILED') GROUP BY job_id)"):
                last[job_id] = (status, fin, err)
        jt = [{"Job": j.id, "Schedule (IST)": ", ".join(j.schedules), "Last result": last.get(j.id, ("-",))[0],
               "Finished (UTC)": last.get(j.id, ("", ""))[1], "Description": j.description} for j in s.jobs]
        st.dataframe(pd.DataFrame(jt), hide_index=True, width="stretch")
    st.caption("Secrets (.env) are never shown here.")


# ------------------------------------------------------------------ login ---
def login_page():
    st.markdown("<div style='height:8vh'></div>", unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid, st.container(border=True):
        st.image(str(APP_DIR / "static" / "logo.svg"), width=260)
        st.markdown('<div class="page-sub">Your portfolio, alerts and market insights</div>', unsafe_allow_html=True)
        mode = settings().signup_mode
        tabs = st.tabs(["Sign in", "Create account"] if mode != "closed" else ["Sign in"])
        with tabs[0]:
            with st.form("login"):
                email = st.text_input("Email", autocomplete="username")
                password = st.text_input("Password", type="password", autocomplete="current-password")
                if st.form_submit_button("Sign in", type="primary", width="stretch"):
                    try:
                        u = authenticate(settings(), email, password)
                        with user_tx(settings(), u["id"]) as conn:
                            row = conn.execute("SELECT email, name FROM users WHERE id = app_user_id()").fetchone()
                        st.session_state["user"] = {**u, **row, "seen": datetime.now(IST)}
                        remember_browser(u["id"])
                        st.rerun()
                    except AuthError as exc:
                        st.error(str(exc))
            st.caption("First sign-in to an account the administrator created? Use the temporary password they "
                       "gave you; you will choose your own right after. Forgot your password? Ask the administrator "
                       "for a reset. 5 wrong attempts lock the account for 15 minutes.")
        if mode != "closed":
            with tabs[1]:
                signup_form(mode)


def signup_form(mode):
    with st.form("signup", clear_on_submit=False):
        name = st.text_input("Full name", autocomplete="name")
        email = st.text_input("Email (used to sign in and for your alerts)", autocomplete="email")
        pw = st.text_input("Password (min 10 characters, letters + digits/symbols)", type="password",
                           autocomplete="new-password")
        pw2 = st.text_input("Repeat password", type="password", autocomplete="new-password")
        agree = st.checkbox("I understand this tool is informational only and not investment advice.")
        if st.form_submit_button("Create account", type="primary", width="stretch"):
            if pw != pw2:
                st.error("The passwords do not match.")
            elif not agree:
                st.error("Please confirm the note above.")
            else:
                try:
                    status = signup(settings(), email, name, pw)
                    if status == "pending":
                        st.success("Thanks! Your account request was sent to the administrator. "
                                   "You can sign in as soon as it is approved.")
                    else:
                        st.success("Account created - switch to the Sign in tab.")
                except AuthError as exc:
                    st.error(str(exc))
    st.caption("New accounts need administrator approval." if mode == "approval"
               else "Accounts are active immediately.")


# ------------------------------------------------- persistent sign-in ---
# A random token lives in the browser cookie COOKIE; PostgreSQL keeps only its
# hash (user_sessions). A page refresh starts a new Streamlit session, which
# resumes the user from the cookie instead of showing the login page.
COOKIE = "nse_session"


def _cookie_script(value, max_age):
    st.html("<script>document.cookie='" + COOKIE + "=" + value + "; Max-Age=" + str(max_age) +
            "; Path=/; SameSite=Strict' + (location.protocol === 'https:' ? '; Secure' : '');</script>",
            unsafe_allow_javascript=True)


def remember_browser(user_id):
    token = create_session(settings(), user_id)
    st.session_state["_token"] = token
    st.session_state["_set_cookie"] = token  # written on the next render (survives st.rerun)


def sign_out():
    try:
        end_session(settings(), uid(), st.session_state.get("_token"))
    finally:
        st.session_state.clear()
        st.session_state["_clear_cookie"] = True


def session_valid():
    u = st.session_state.get("user")
    if u:
        if datetime.now(IST) - u["seen"] > timedelta(hours=SESSION_IDLE_HOURS):
            sign_out()
            return False
        u["seen"] = datetime.now(IST)
        return True
    if st.session_state.get("_clear_cookie"):
        return False
    token = st.context.cookies.get(COOKIE)
    resumed = resume_session(settings(), token) if token else None
    if not resumed:
        return False
    st.session_state["user"] = {**resumed, "seen": datetime.now(IST)}
    st.session_state["_token"] = token
    return True


# ------------------------------------------------------------- navigation ---
st.logo(str(APP_DIR / "static" / "logo.svg"), size="large")

if not session_valid():
    if st.session_state.pop("_clear_cookie", False):
        _cookie_script("", 0)
    login_page()
    st.stop()

if "_set_cookie" in st.session_state:
    _cookie_script(st.session_state.pop("_set_cookie"), SESSION_DAYS * 86400)

if st.session_state["user"].get("must_change_password"):
    top_bar("Set a new password", "Your account uses a temporary password. Choose your own to continue.")
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        change_password_form(forced=True)
    st.stop()

since = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
n_alerts_today = rows("SELECT count(*) AS n FROM alert_history WHERE triggered_at >= %s", (since,))[0]["n"]

pages = [
    st.Page(page_dashboard, title="Dashboard", icon=":material/home:", default=True),
    st.Page(page_portfolio, title="Portfolio", icon=":material/work:", url_path="portfolio"),
    st.Page(page_watchlist, title="Watchlist", icon=":material/bookmark:", url_path="watchlist"),
    st.Page(page_opportunities, title="Opportunities", icon=":material/emoji_events:", url_path="opportunities"),
    st.Page(page_alerts, title=f"Alerts ({n_alerts_today})" if n_alerts_today else "Alerts",
            icon=":material/notifications:", url_path="alerts"),
    st.Page(page_analytics, title="Analytics", icon=":material/bar_chart:", url_path="analytics"),
    st.Page(page_market_data, title="Market Data", icon=":material/database:", url_path="market-data"),
    st.Page(page_reports, title="Reports", icon=":material/description:", url_path="reports"),
    st.Page(page_account, title="Account", icon=":material/person:", url_path="account"),
]
if is_admin():
    pages += [st.Page(page_users, title="Users", icon=":material/group:", url_path="users"),
              st.Page(page_settings, title="Settings", icon=":material/settings:", url_path="settings")]
nav = st.navigation(pages, position="sidebar")

with st.sidebar:
    u = st.session_state["user"]
    st.markdown(f'<div class="note">Signed in as<br><b style="color:#e2e8f0">{esc(u.get("name") or u["email"])}</b>'
                f'<br>{esc(u["email"])} · {esc(u["role"])}</div>', unsafe_allow_html=True)
    if st.button("Sign out", icon=":material/logout:", width="stretch"):
        sign_out()
        st.rerun()

nav.run()

with st.sidebar:
    st.divider()
    st.caption("Prices: stored NSE daily closes; charts and live checks via Yahoo Finance (delayed). "
               "Informational only, not investment advice.")
