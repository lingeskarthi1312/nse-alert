"""Intraday (live) alerts on the user's watchlist rules.

Every few minutes during NSE hours the job fetches the latest quote for each
symbol that has an ENABLED rule in user_alerts, compares it with the previous
session's close (from ClickHouse) and applies the rule ("Below or equal" /
"Above or equal" X% daily change).

* Provider: Yahoo Finance via yfinance (free, no account; NSE quotes are
  delayed ~15 minutes). LIVE_QUOTE_PROVIDER selects it; a broker provider can
  be added by implementing `get_quotes(symbols) -> {symbol: Quote}`.
* Quotes are used only if they are from today's session and no older than
  LIVE_MAX_QUOTE_AGE_MINUTES; otherwise the symbol is skipped (never alerted
  on stale data). Ticks are not stored - only triggered alerts.
* One alert per rule per trading day: recorded in alert_history with metric
  'Intraday' (separate from the end-of-day 'Daily' alerts so the two never
  reset each other); the previous day's intraday alert resets automatically.
* SinceSet rules compare the live price with the rule's base_price (the live
  price when it was saved) and are one-shot: the rule is disabled on firing.
  Emails go through the outbox (group_key alert:<id>, dedup window).
"""
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from .email_format import RED, Cell, money, pct_cell, render_html, render_text
from .logging_setup import log_event
from .outbox import enqueue
from .pgdb import _params, dict_row, service_tx
from .report import SEVERITY_ORDER, report_settings_for, severity
from .rules import EVALUATED_METRICS, ONE_SHOT_METRICS, describe, since_set_change, triggered as rule_triggered
from .timeutil import local_today, to_iso

log = logging.getLogger("alert_system.intraday")
METRIC = "Intraday"
LIVE_LABEL = {"Daily": METRIC, "Price": "Price live", "SinceSet": "SinceSet"}  # alert_history.metric for live alerts
SESSION_OPEN, POLL_END = dtime(9, 15), dtime(15, 50)
LIVE_LOOP_LOCK = 4_673_891  # PostgreSQL advisory lock held by `live-loop` while it runs


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    ts: datetime          # aware, exchange time of the bar
    source: str
    delay_note: str


class YahooQuotes:
    name = "yahoo"
    delay_note = "Yahoo Finance, NSE quotes delayed ~15 min"

    def __init__(self, downloader=None):
        if downloader is None:
            import yfinance as yf
            downloader = yf.download
        self.downloader = downloader

    def get_quotes(self, symbols):
        if not symbols:
            return {}
        data = self.downloader(list(symbols), period="1d", interval="1m", auto_adjust=False, progress=False,
                               threads=True, group_by="ticker", prepost=False, timeout=20)
        out = {}
        if data is None or data.empty:
            return out
        multi = getattr(data.columns, "nlevels", 1) > 1
        present = set(data.columns.get_level_values(0)) if multi else set()
        for s in symbols:
            if multi and s not in present:
                continue
            closes = (data[s] if multi else data)["Close"].dropna()
            closes = closes[closes > 0]
            if closes.empty:
                continue
            ts = closes.index[-1].to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            out[s] = Quote(s, float(closes.iloc[-1]), ts, self.name, self.delay_note)
        return out


PROVIDERS = {"yahoo": YahooQuotes}


@contextmanager
def live_loop_lock(settings):
    """Hold the live-loop advisory lock on a dedicated connection; yields
    False when another live loop already holds it."""
    import psycopg
    conn = psycopg.connect(**_params(settings, "service"), row_factory=dict_row, autocommit=True,
                           keepalives=1, keepalives_idle=60, keepalives_interval=15, keepalives_count=4)
    try:
        held = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LIVE_LOOP_LOCK,)).fetchone()["ok"]
        yield held
    finally:
        conn.close()  # closing the session releases the lock


def live_loop_running(settings):
    with service_tx(settings) as conn:
        return not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (LIVE_LOOP_LOCK,)).fetchone()["ok"]


def market_window(local_now):
    return local_now.weekday() < 5 and SESSION_OPEN <= local_now.time() <= POLL_END


def enabled_rules(conn):
    """Every active user's enabled Daily, Price and SinceSet rules (service role: all users)."""
    return conn.execute("""
        SELECT a.id, a.user_id, u.email, u.prefs, a.symbol, a.metric, a.condition, a.threshold, a.base_price
        FROM user_alerts a JOIN users u ON u.id = a.user_id
        WHERE a.enabled AND u.is_active AND a.metric = ANY(%s)
        ORDER BY a.symbol, u.email""", (list(EVALUATED_METRICS),)).fetchall()


def previous_closes(client, symbols, today):
    rows = client.query("""
        SELECT DISTINCT ON (symbol) symbol, trade_date, close_price
        FROM nse_alerts.daily_stock_prices
        WHERE symbol = ANY(%(s)s) AND trade_date < %(t)s::date AND close_price > 0
        ORDER BY symbol, trade_date DESC
    """, parameters={"s": list(symbols), "t": today.isoformat()}).result_rows
    return {s: (d, float(c)) for s, d, c in rows}


def _email(symbol, metric, condition, threshold, sev, quote, prev, change, detected, base_price=None):
    if metric == "SinceSet":
        ref_rows = [[Cell("Base price", bold=True), money(base_price)],
                    [Cell("Change from base price", bold=True), pct_cell(change)]]
    else:
        ref_rows = [[Cell("Previous close", bold=True), f"{money(prev[1])} ({prev[0]})"],
                    [Cell("Change vs previous close", bold=True), pct_cell(change)]]
    blocks = [
        ("title", f"NSE live alert: {symbol.removesuffix('.NS')}"),
        ("table", ["Field", "Value"], [
            [Cell("Symbol", bold=True), symbol],
            [Cell("Severity", bold=True), Cell(sev, RED if sev == "CRITICAL" else "#b06000", bold=True)],
            [Cell("Rule", bold=True), describe(metric, condition, threshold, base_price)
             + (" (applied to today's live move)" if metric == "Daily" else "")],
            [Cell("Live price", bold=True), money(quote.price)],
            [Cell("Price time", bold=True), f"{quote.ts:%Y-%m-%d %H:%M} IST ({quote.delay_note})"],
            *ref_rows,
            [Cell("Detected at", bold=True), detected],
            [Cell("Signal", bold=True), f"Live price {money(quote.price)} ({change:+.2f}%) met the rule "
                                        f"'{describe(metric, condition, threshold, base_price)}'"
                                        + (". This rule is now switched off." if metric in ONE_SHOT_METRICS else "")],
        ], ["l", "l"]),
        ("note", "Delayed intraday quote from a free source; verify the live price with your broker before "
                 "acting. Not investment advice."),
    ]
    return render_text(blocks), render_html(blocks)


def _check_quote(q, p, local_now, today, tz, max_age):
    if q is None:
        return "no quote"
    if q.ts.astimezone(tz).date() != today:
        return f"quote from {q.ts.date()} (market closed today?)"
    age_min = (local_now - q.ts.astimezone(tz)).total_seconds() / 60
    if age_min > max_age:
        return f"quote {age_min:.0f} min old (stale)"
    if p is None or (today - p[0]).days > 5:
        return "previous close missing or stale in the price table"
    return None


def run_intraday(settings, client, now, provider=None):
    tz = ZoneInfo(settings.timezone)
    local_now = now.astimezone(tz)
    if not market_window(local_now):
        log_event(log, logging.DEBUG, "intraday poll outside market hours", component="intraday")
        return {"status": "market_closed"}
    today = local_today(settings.timezone, now)
    with service_tx(settings) as conn:
        rules = enabled_rules(conn)
    symbols = sorted({r["symbol"] for r in rules})
    if not symbols:
        return {"status": "no_rules"}
    provider = provider or PROVIDERS[settings.live.provider]()
    quotes = provider.get_quotes(symbols)
    if not quotes:
        raise RuntimeError(f"no live quotes returned by {settings.live.provider} for {len(symbols)} symbol(s)")
    prev = previous_closes(client, symbols, today)
    result = {"status": "ok", "checked": 0, "skipped": {}, "triggered": [], "quotes": {}}
    for s in symbols:
        why = _check_quote(quotes.get(s), prev.get(s), local_now, today, tz, settings.live.max_quote_age_minutes)
        if why:
            result["skipped"][s] = why
        else:
            q, p = quotes[s], prev[s]
            result["quotes"][s] = {"price": q.price, "time": q.ts.isoformat(), "prev_close": p[1],
                                   "prev_date": str(p[0]), "change_pct": round((q.price / p[1] - 1) * 100, 2)}
    detected = local_now.isoformat(timespec="seconds")
    # One transaction: alert rows and their emails are committed together.
    with service_tx(settings) as conn:
        # A new trading day starts fresh: earlier days' intraday alerts reset.
        conn.execute("UPDATE alert_history SET status='RESET' WHERE metric = ANY(%s) AND status='ACTIVE' "
                     "AND trigger_session IS DISTINCT FROM %s", (list(LIVE_LABEL.values()), today))
        for r in rules:
            sym = r["symbol"]
            if sym not in result["quotes"]:
                continue
            q, p = quotes[sym], prev[sym]
            one_shot = r["metric"] in ONE_SHOT_METRICS
            change = since_set_change(q.price, r["base_price"]) if one_shot else (q.price / p[1] - 1) * 100
            result["checked"] += 1
            label = LIVE_LABEL[r["metric"]]
            if not rule_triggered(r["metric"], r["condition"], r["threshold"], change, q.price, r["base_price"]):
                continue
            if not one_shot and conn.execute("SELECT 1 FROM alert_history WHERE user_id=%s AND symbol=%s AND metric=%s "
                            "AND condition=%s AND threshold=%s AND status='ACTIVE' AND trigger_session=%s",
                            (r["user_id"], sym, label, r["condition"], r["threshold"], today)).fetchone():
                continue  # this user was already alerted for this rule today
            alert_id = conn.execute(
                "INSERT INTO alert_history (user_id, symbol, metric, condition, threshold, triggered_value, status, "
                "trigger_price, trigger_session) VALUES (%s,%s,%s,%s,%s,%s,'ACTIVE',%s,%s) RETURNING id",
                (r["user_id"], sym, label, r["condition"], r["threshold"], change, q.price, today)).fetchone()["id"]
            if one_shot:  # fires once: switch the rule off in the same transaction
                conn.execute("UPDATE user_alerts SET enabled=false WHERE id=%s", (r["id"],))
            rep = report_settings_for(settings, r["prefs"])
            sev = "WARNING" if r["metric"] == "Price" else severity(change, r["threshold"])
            emailed = False
            if rep.alert_emails and SEVERITY_ORDER[sev] >= SEVERITY_ORDER[rep.min_severity] and settings.email.configured:
                text, html = _email(sym, r["metric"], r["condition"], r["threshold"], sev, q, p, change, detected,
                                    r["base_price"])
                emailed = bool(enqueue(
                    settings, f"alert:{alert_id}", text, (r["email"],), rep.email_max_attempts, now=now,
                    channel="email", subject=f"NSE Live Alert: {sym.removesuffix('.NS')} {change:+.2f}%",
                    dedup_key=f"{sym}|{label}|{r['condition']}|{float(r['threshold'])}",
                    dedup_window_hours=rep.dedup_window_hours, html=html, user_id=r["user_id"], conn=conn))
            entry = {"alert_id": alert_id, "symbol": sym, "change_pct": round(change, 2), "severity": sev,
                     "price": q.price, "emailed": emailed}
            result["triggered"].append(entry)
            log_event(log, logging.WARNING, "intraday alert triggered", component="intraday",
                      user_id=str(r["user_id"]), **entry)
    # Shared market snapshot only (quotes); per-user alert details stay in the RLS-protected database.
    snapshot = {"checked_at": to_iso(now), "provider": settings.live.provider, "quotes": result["quotes"],
                "skipped": result["skipped"], "triggered_count": len(result["triggered"])}
    (settings.state_dir / "live_quotes.json").write_text(json.dumps(snapshot, default=str, indent=1))
    log_event(log, logging.INFO, "intraday poll done", component="intraday", checked=result["checked"],
              triggered=len(result["triggered"]), skipped=result["skipped"])
    return result
