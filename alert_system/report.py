"""Personalised daily email report (one per user).

All user data is read with the dashboard's RLS role scoped to that user
(pgdb.user_tx), so a report can only ever contain the recipient's own rows.

Allowlist ("watchlist") = symbols of the user's ENABLED rules + ACTIVE
holdings + watchlist entries. Nothing outside it is shown except the
recommendation sections the user enabled. Never included: the scanned
universe, raw ClickHouse rows, logs, other users' data.

Sections by EMAIL_NOTIFICATION_MODE (or the user's prefs.email_mode):
  TRIGGERED_ALERTS_ONLY   - no daily report (only per-alert emails)
  WATCHLIST_DAILY_SUMMARY - watchlist prices, today's triggered alerts, summary
  RECOMMENDATIONS_ONLY    - enabled recommendation sections, summary
  COMBINED_DAILY_REPORT   - everything enabled
"""
import json
from dataclasses import replace
from zoneinfo import ZoneInfo

from .config import NOTIFICATION_MODES
from .email_format import GREEN, RED, Cell, money, pct_cell, render_html, render_text
from .pgdb import user_tx
from .rules import describe
from .timeutil import local_midnight_utc, local_today

SEVERITY_ORDER = {"WARNING": 1, "CRITICAL": 2}
DISCLAIMER = ("Model-generated estimates from end-of-day data, not guaranteed outcomes and not executable "
              "trading instructions or investment advice. Verify prices before acting.")


def severity(change_pct, threshold):
    """CRITICAL when the move is at least twice the rule threshold."""
    if change_pct is None or threshold is None:
        return "WARNING"
    return "CRITICAL" if abs(change_pct) >= 2 * abs(threshold) else "WARNING"


def report_settings_for(settings, prefs):
    """Global report settings with the user's own email_mode, if valid."""
    mode = (prefs or {}).get("email_mode")
    return replace(settings.report, mode=mode) if mode in NOTIFICATION_MODES else settings.report


def watchlist(settings, user_id):
    with user_tx(settings, user_id) as c:
        syms = {r["symbol"] for r in c.execute("SELECT DISTINCT symbol FROM user_alerts WHERE enabled")}
        syms |= {r["symbol"] for r in c.execute("SELECT symbol FROM watchlist")}
        holdings = {}
        for r in c.execute("SELECT symbol, buy_price, quantity FROM holdings WHERE status='ACTIVE'"):
            holdings.setdefault(r["symbol"], []).append((r["buy_price"], r["quantity"]))
            syms.add(r["symbol"])
    return sorted(syms), holdings


def watchlist_prices(client, symbols):
    if not symbols:
        return {}
    rows = client.query("""
        SELECT symbol, trade_date, close_price AS close
        FROM nse_alerts.daily_stock_prices
        WHERE symbol = ANY(%(s)s) AND close_price IS NOT NULL
        ORDER BY symbol, trade_date DESC
    """, parameters={"s": list(symbols)}).result_rows
    out = {}
    for sym, d, close in rows:
        out.setdefault(sym, [])
        if len(out[sym]) < 2:
            out[sym].append((d, float(close)))
    return out


def todays_alerts(settings, user_id, symbols, today, min_severity=None):
    since = local_midnight_utc(today, settings.timezone)
    with user_tx(settings, user_id) as c:
        rows = c.execute("""SELECT id, symbol, metric, condition, threshold, triggered_value, triggered_at, status,
                                   trigger_price, trigger_session
                            FROM alert_history WHERE triggered_at >= %s ORDER BY id""", (since,)).fetchall()
    min_sev = SEVERITY_ORDER[min_severity or settings.report.min_severity]
    out = []
    for r in rows:
        if r["symbol"] not in symbols:
            continue
        sev = severity(r["triggered_value"], r["threshold"])
        if SEVERITY_ORDER[sev] >= min_sev:
            out.append({"id": r["id"], "symbol": r["symbol"], "metric": r["metric"], "condition": r["condition"],
                        "threshold": r["threshold"], "change": r["triggered_value"],
                        "at": r["triggered_at"].astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%dT%H:%M"),
                        "status": r["status"],
                        "trigger_price": r["trigger_price"],
                        "trigger_session": str(r["trigger_session"]) if r["trigger_session"] else "",
                        "severity": sev})
    return out


def load_recommendations(settings, today):
    try:
        snap = json.loads((settings.state_dir / "recommendations_latest.json").read_text())
    except (OSError, ValueError):
        return None, "recommendation job has not produced output yet"
    if snap.get("run_date") != today.isoformat():
        return None, f"latest recommendations are from {snap.get('run_date')} (today's run missing/failed)"
    return snap, None


def _backtest_summary(settings):
    try:
        bt = json.loads((settings.state_dir / "backtest_latest.json").read_text())
    except (OSError, ValueError):
        return []
    oos = bt["segments"]["out_of_sample"]
    out = [f"Backtest {bt['period'][0]} to {bt['period'][1]}, out-of-sample: {oos.get('trades', 0)} trades, "
           f"win rate {oos.get('win_rate_pct')}%, expectancy {oos.get('expectancy_pct')}%/trade after costs, "
           f"profit factor {oos.get('profit_factor')}, max drawdown {oos.get('max_drawdown_pct')}%."]
    bear = bt.get("by_regime", {}).get("bear")
    if bear:
        out.append(f"In weak markets (Nifty below 200-DMA): expectancy {bear.get('expectancy_pct')}%/trade, "
                   f"profit factor {bear.get('profit_factor')}.")
    return out


def _sym(a):
    sym = a["symbol"].removesuffix(".NS")
    return f"{sym} - {a['name']}" if a.get("name") and a["name"] != sym else sym


def _rec_blocks(title, items, empty_note):
    blocks = [("heading", title)]
    if not items:
        return blocks + [("para", empty_note)]
    rows = []
    for i, a in enumerate(items, 1):
        rows.append([i, Cell(_sym(a), bold=True), a["category"], money(a["price"]), a["price_date"],
                     Cell(a["signal"], GREEN if a["signal"] == "BUY CANDIDATE" else None), f"{a['score']:.1f}",
                     f"{money(a['entry_low'])} - {money(a['entry_high'])}", Cell(money(a["stop"]), RED),
                     Cell(money(a["target1"]), GREEN), a["risk_reward"], a["confidence"]])
    blocks.append(("table", ["#", "Symbol", "Category", "Price", "Close date", "Signal", "Score", "Entry zone",
                             "Stop", "Target 1", "R:R", "Confidence"], rows,
                   ["r", "l", "l", "r", "l", "l", "r", "r", "r", "r", "r", "l"]))
    for i, a in enumerate(items, 1):
        comp = ", ".join(f"{k} {v:.0f}" for k, v in a["components"].items())
        blocks += [("note", f"{i}. {_sym(a)} ({a['asset_class']}, {a['exchange']}, {a['horizon']}): score parts "
                            f"{comp}. Why: {'; '.join(a['reasons'])}. Risks: {'; '.join(a['risks'])}. "
                            f"Target basis: {a['target_basis']}. Size: {a['position_size']}.")]
    return blocks


def build_report(settings, client, now, user):
    """user: {"id", "email", "prefs"}. Returns (subject, text, html), or None
    when the user's mode sends no daily report."""
    rep = report_settings_for(settings, user.get("prefs"))
    if not rep.daily_report:
        return None
    today = local_today(settings.timezone, now)
    symbols, holdings = watchlist(settings, user["id"])
    blocks = [("title", f"NSE daily report - {today:%a %d %b %Y}")]
    summary = []
    if rep.watchlist_section:
        prices = watchlist_prices(client, symbols)
        alerts = todays_alerts(settings, user["id"], set(symbols), today, rep.min_severity)
        max_age = settings.recommendations.get("max_data_age_days", 4)
        blocks.append(("heading", "Your watchlist"))
        if not symbols:
            blocks.append(("para", "No tracked stocks (add alert rules or holdings)."))
        else:
            rows = []
            for s in symbols:
                p = prices.get(s, [])
                if not p:
                    rows.append([Cell(s.removesuffix(".NS"), bold=True), "n/a", "no stored price", "", "", "", ""])
                    continue
                (d1, c1), prev = p[0], (p[1] if len(p) > 1 else None)
                change = (c1 / prev[1] - 1) * 100 if prev else None
                held = holdings.get(s, [])
                qty = sum(q for _, q in held)
                cost = sum(b * q for b, q in held)
                rows.append([Cell(s.removesuffix(".NS"), bold=True), money(c1),
                             f"{d1}" + (" (STALE)" if (today - d1).days > max_age else ""),
                             pct_cell(change), f"{qty:g} @ {money(cost / qty)}" if qty else "-",
                             pct_cell((c1 * qty / cost - 1) * 100, 1) if qty else "-",
                             Cell(money((c1 * qty) - cost), GREEN if c1 * qty >= cost else RED) if qty else "-"])
            blocks.append(("table", ["Stock", "Close", "Close date", "Day change", "Holding", "Holding P/L %",
                                     "Holding P/L"], rows, ["l", "r", "l", "r", "r", "r", "r"]))
        blocks.append(("heading", "Triggered alerts today"))
        if not alerts:
            blocks.append(("para", "None. No tracked stock met its alert condition today."))
        else:
            rows = []
            for a in alerts:
                cur = prices.get(a["symbol"], [(None, None)])[0]
                kind = ("Price" if a["metric"].startswith("Price") else
                        "SinceSet" if a["metric"] == "SinceSet" else "Daily")
                rule = describe(kind, a["condition"], a["threshold"]) + ("" if kind != "Daily" else f" ({a['metric']})")
                # move from the trigger price to the latest close (the move that fired it is "Move at trigger")
                since = (cur[1] / a["trigger_price"] - 1) * 100 if cur[1] and a["trigger_price"] else None
                rows.append([Cell(a["severity"], RED if a["severity"] == "CRITICAL" else "#b06000", bold=True),
                             Cell(a["symbol"].removesuffix(".NS"), bold=True), rule, pct_cell(a["change"]),
                             money(a["trigger_price"]) if a["trigger_price"] is not None else "not recorded",
                             a["trigger_session"] or "", money(cur[1]) + (f" ({cur[0]})" if cur[0] else ""),
                             pct_cell(since) if since is not None else "-",
                             a["at"][:16].replace("T", " ") + " IST", a["status"]])
            blocks.append(("table", ["Severity", "Stock", "Rule", "Move at trigger", "Trigger price", "Session",
                                     "Current", "Change since alert", "Triggered at", "Status"], rows,
                           ["l", "l", "l", "r", "r", "l", "r", "r", "l", "l"]))
        summary.append(f"{len(symbols)} tracked stock(s), {len(alerts)} triggered alert(s) today")

    if rep.recommendation_sections:
        snap, why = load_recommendations(settings, today)
        if snap is None:
            blocks += [("heading", "Recommendations"), ("para", f"Unavailable: {why}.")]
            summary.append("recommendations unavailable")
        else:
            r = snap["rankings"]
            none = "No instrument met all quality, risk and confirmation criteria today."
            if snap["validation"] == "FAILED":
                blocks.append(("warning", "This strategy FAILED its backtest (negative expectancy after costs). "
                                          "The lists below are research output only, not trade recommendations."))
                blocks += [("note", t) for t in _backtest_summary(settings)]
            n = settings.recommendations.get("top_n", 10)
            if rep.include_top5:
                blocks += _rec_blocks(f"Top {n} overall (stocks)", r["overall"], none)
            if rep.include_cap_top5:
                for cat in ("Large-cap", "Mid-cap", "Small-cap"):
                    blocks += _rec_blocks(f"Top {n} {cat}", r[cat], none)
            if rep.include_cross_asset:
                blocks += _rec_blocks(f"Top {n} cross-asset (stocks + gold/silver ETFs)", r["cross_asset"], none)
                blocks.append(("heading", "Gold / silver status"))
                blocks.append(("table", ["Instrument", "Name", "Exchange", "Currency", "Close", "Close date",
                                         "Signal", "Detail"],
                               [[c["symbol"], c["name"], c["exchange"], c["currency"], money(c["price"]),
                                 c["price_date"] or "", c["signal"],
                                 c["excluded_reason"] or f"score {c['score']}"] for c in snap["commodities"]],
                               ["l", "l", "l", "l", "r", "l", "l", "l"]))
                blocks.append(("note", "MCX gold/silver futures are not available from the data provider and are "
                                       "not analysed; ETF prices are not physical bullion prices."))
            regime = "uptrend" if snap["market_uptrend"] else "below 200-DMA (stricter rules applied)"
            blocks.append(("note", f"Model: {snap['universe']} instruments scored, Nifty 50 {regime}. "
                                   f"Strategy validation: {snap['validation']}."))
            summary.append(f"{len(r['cross_asset'])} cross-asset buy candidate(s); validation {snap['validation']}")
    blocks += [("heading", "Summary")] + [("para", s) for s in summary]
    if rep.recommendation_sections:
        blocks.append(("note", DISCLAIMER))
    subject = f"NSE daily report {today}: " + "; ".join(summary) if summary else f"NSE daily report {today}"
    return subject[:180], render_text(blocks), render_html(blocks)
