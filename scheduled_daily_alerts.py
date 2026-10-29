"""End-of-day alert check for every active user's enabled Daily rules.

For each rule: daily change = latest stored close vs the previous stored
close (ClickHouse). A rule triggers once (alert_history ACTIVE) and stays
quiet until the condition clears (RESET), exactly as before - now per user.
The alert row and its email are written in ONE PostgreSQL transaction, and
the email goes to that user's own address (respecting their notification
mode and minimum severity).

SinceSet rules compare the latest close with the rule's base_price, use only a
close from a session that ended after the rule was saved, and are one-shot:
the rule is disabled when it fires.
"""
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from alert_system.clickhouse import get_client
from alert_system.config import load_settings
from alert_system.email_format import RED, Cell, money, pct_cell, render_html, render_text
from alert_system.outbox import enqueue
from alert_system.pgdb import service_tx
from alert_system.report import SEVERITY_ORDER, report_settings_for, severity
from alert_system.rules import (EVALUATED_METRICS, ONE_SHOT_METRICS, describe, since_set_change,
                                triggered as rule_triggered)

LOG_TZ = ZoneInfo("Asia/Kolkata")
MAX_PRICE_AGE_DAYS = 4
SESSION_CLOSE = dtime(15, 30)


def log(message):
    now = datetime.now(LOG_TZ).isoformat(timespec="seconds")
    print(f"[{now}] {message}", flush=True)


def fetch_prices(symbols):
    if not symbols:
        return {}
    client = get_client()
    try:
        rows = client.query("""
            SELECT symbol, trade_date, close_price
            FROM nse_alerts.daily_stock_prices
            WHERE symbol = ANY(%(s)s) AND close_price IS NOT NULL
            ORDER BY symbol, trade_date DESC, recorded_at DESC
        """, parameters={"s": list(symbols)}).result_rows
    finally:
        client.close()
    prices = {}
    for symbol, trade_date, close in rows:
        prices.setdefault(symbol, {}).setdefault(trade_date, float(close))
    return prices


def evaluate(prices, symbol, metric, condition, threshold, today, base_price=None, created_at=None):
    """Returns (triggered, change, (latest_date, latest_close), (prev_date, prev_close)) or a skip reason.
    For SinceSet rules `change` is the move from base_price."""
    sessions = sorted(prices.get(symbol, {}).items(), key=lambda item: item[0], reverse=True)
    if len(sessions) < 2:
        return "fewer than two trading dates"
    (latest_date, latest_close), (previous_date, previous_close) = sessions[0], sessions[1]
    if (previous_date >= latest_date or latest_date > today or previous_date > today
            or (today - latest_date).days > MAX_PRICE_AGE_DAYS or (today - previous_date).days > MAX_PRICE_AGE_DAYS
            or latest_close <= 0 or previous_close <= 0):
        return "price dates or closes invalid/stale"
    change = (latest_close - previous_close) / previous_close * 100
    if condition not in ("Below or equal", "Above or equal"):
        return f"unsupported condition {condition}"
    if metric == "SinceSet":
        if created_at is not None and datetime.combine(latest_date, SESSION_CLOSE, LOG_TZ) <= created_at:
            return "no close since the rule was set yet"
        change = since_set_change(latest_close, base_price)
    triggered = rule_triggered(metric, condition, threshold, change, latest_close, base_price)
    return triggered, change, (latest_date, latest_close), (previous_date, previous_close)


def alert_email(symbol, sev, metric, condition, threshold, change, latest, previous, base_price=None):
    if metric == "SinceSet":
        ref_rows = [[Cell("Change from base price", bold=True), pct_cell(change)],
                    [Cell("Base price", bold=True), money(base_price)]]
        signal = (f"Close {money(latest[1])} ({change:+.2f}%) met the rule "
                  f"'{describe(metric, condition, threshold, base_price)}'. This rule is now switched off.")
    else:
        ref_rows = [[Cell("Daily change", bold=True), pct_cell(change)],
                    [Cell("Previous close", bold=True), f"{money(previous[1])} ({previous[0]})"]]
        signal = (f"Close {money(latest[1])} met the rule '{describe(metric, condition, threshold)}'" if metric == "Price"
                  else f"Daily close-to-close move of {change:+.2f}% met the '{condition} {threshold}%' rule")
    blocks = [
        ("title", f"NSE daily stock alert: {symbol.removesuffix('.NS')}"),
        ("table", ["Field", "Value"], [
            [Cell("Symbol", bold=True), symbol],
            [Cell("Severity", bold=True), Cell(sev, RED if sev == "CRITICAL" else "#b06000", bold=True)],
            [Cell("Rule", bold=True), describe(metric, condition, threshold, base_price)],
            *ref_rows,
            [Cell("Latest stored close", bold=True), f"{money(latest[1])} ({latest[0]})"],
            [Cell("Evaluated at", bold=True), datetime.now(LOG_TZ).isoformat(timespec="seconds")],
            [Cell("Signal", bold=True), signal],
        ], ["l", "l"]),
        ("note", "This alert uses stored daily prices, not live intraday quotes. It is not investment advice."),
    ]
    return render_text(blocks), render_html(blocks)


def main():
    settings = load_settings()
    today = datetime.now(LOG_TZ).date()
    with service_tx(settings) as conn:
        rules = conn.execute("""
            SELECT a.id, a.user_id, u.email, u.role, u.prefs, a.symbol, a.metric, a.condition, a.threshold,
                   a.base_price, a.created_at
            FROM user_alerts a JOIN users u ON u.id = a.user_id
            WHERE a.enabled AND u.is_active AND a.metric = ANY(%s)
            ORDER BY u.email, a.symbol""", (list(EVALUATED_METRICS),)).fetchall()
    log(f"{len(rules)} enabled Daily/Price/SinceSet rule(s) across {len({r['user_id'] for r in rules})} user(s)")
    prices = fetch_prices(sorted({r["symbol"] for r in rules}))

    queued = 0
    with service_tx(settings) as conn:  # alert rows + emails commit (or roll back) together
        for r in rules:
            who, symbol = r["email"], r["symbol"]
            res = evaluate(prices, symbol, r["metric"], r["condition"], r["threshold"], today,
                           r["base_price"], r["created_at"])
            if isinstance(res, str):
                log(f"{who} {symbol}: skipped; {res}")
                continue
            triggered, change, latest, previous = res
            one_shot = r["metric"] in ONE_SHOT_METRICS
            log(f"{who} {symbol}: {previous[0]}={previous[1]:.2f}, {latest[0]}={latest[1]:.2f}, change={change:+.2f}%")
            active = conn.execute("""
                SELECT id FROM alert_history
                WHERE user_id=%s AND symbol=%s AND metric=%s AND condition=%s AND threshold=%s
                  AND status='ACTIVE' LIMIT 1""", (r["user_id"], symbol, r["metric"], r["condition"], r["threshold"])).fetchone()
            if one_shot:
                active = None  # an enabled one-shot rule has not fired yet; firing disables it
            if triggered and not active:
                alert_id = conn.execute("""
                    INSERT INTO alert_history (user_id, symbol, metric, condition, threshold, triggered_value,
                                               status, trigger_price, trigger_session)
                    VALUES (%s, %s, %s, %s, %s, %s, 'ACTIVE', %s, %s) RETURNING id""",
                    (r["user_id"], symbol, r["metric"], r["condition"], r["threshold"], change, latest[1], latest[0])).fetchone()["id"]
                log(f"{who} {symbol}: new alert recorded")
                if one_shot:
                    conn.execute("UPDATE user_alerts SET enabled=false WHERE id=%s", (r["id"],))
                    log(f"{who} {symbol}: one-shot rule #{r['id']} switched off")
                rep = report_settings_for(settings, r["prefs"])
                sev = "WARNING" if r["metric"] == "Price" else severity(change, r["threshold"])
                if not rep.alert_emails or SEVERITY_ORDER[sev] < SEVERITY_ORDER[rep.min_severity]:
                    log(f"{who} {symbol}: alert email not sent (mode {rep.mode}, severity {sev})")
                    continue
                if not settings.email.configured:
                    log(f"{who} {symbol}: MAIL_* not configured; alert recorded without email")
                    continue
                text, html = alert_email(symbol, sev, r["metric"], r["condition"], r["threshold"], change, latest, previous,
                                         r["base_price"])
                n = enqueue(settings, f"alert:{alert_id}", text, (who,), rep.email_max_attempts, channel="email",
                            subject=f"NSE Alert: {symbol}",
                            dedup_key=f"{symbol}|{r['metric']}|{r['condition']}|{float(r['threshold'])}",
                            dedup_window_hours=rep.dedup_window_hours, html=html, user_id=r["user_id"], conn=conn)
                queued += n
                log(f"{who} {symbol}: email " + ("queued" if n else "suppressed (sent within dedup window)"))
                if settings.whatsapp.configured and r["role"] == "admin":  # WhatsApp number belongs to the admin
                    enqueue(settings, f"alert:{alert_id}", text, settings.whatsapp.recipients,
                            settings.whatsapp.max_attempts, channel="whatsapp", user_id=r["user_id"], conn=conn)
            elif triggered:
                log(f"{who} {symbol}: already active; duplicate suppressed")
            elif active:
                conn.execute("UPDATE alert_history SET status='RESET' WHERE id=%s", (active["id"],))
                log(f"{who} {symbol}: alert reset")
            else:
                log(f"{who} {symbol}: no alert")
    log(f"Scheduled alert check complete; {queued} email(s) queued")


if __name__ == "__main__":
    main()
