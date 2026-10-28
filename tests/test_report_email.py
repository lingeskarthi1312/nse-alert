"""Personalised (per-user) email: modes, allowlist, severity, dedup window,
SMTP retry policy, restart safety, report content - against PostgreSQL
(nse_app_test). Includes cross-user isolation of report contents."""
import json
import smtplib
from datetime import date, datetime, timezone

import pytest

from alert_system import service as service_module
from alert_system.auth import hash_password
from alert_system.outbox import Outbox, classify_smtp_error, enqueue
from alert_system.pgdb import service_tx, user_tx
from alert_system.report import build_report, severity, todays_alerts, watchlist
from conftest import Clock, ist, make_job, utc


class FakeCH:
    def __init__(self, prices):
        self.prices = prices

    def query(self, sql, parameters=None):
        class R:
            pass
        r = R()
        r.result_rows = [row for row in self.prices if row[0] in parameters["s"]]
        return r

    def close(self):
        pass


PRICES = [("RELIANCE.NS", date(2026, 10, 16), 1400.0), ("RELIANCE.NS", date(2026, 10, 15), 1500.0),
          ("SUNTV.NS", date(2026, 10, 16), 630.0), ("SUNTV.NS", date(2026, 10, 15), 620.0),
          ("TCS.NS", date(2026, 10, 16), 3000.0)]


def add_user(s, email, prefs=None, role="member"):
    with service_tx(s) as c:
        return c.execute("INSERT INTO users (email, name, password_hash, role, prefs, must_change_password) "
                         "VALUES (%s, %s, %s, %s, %s, false) RETURNING id, email, name, role, prefs",
                         (email, email.split("@")[0], hash_password("Password-123"), role,
                          json.dumps(prefs or {}))).fetchone()


def seed_me(s, uid, alerts=True):
    with user_tx(s, uid) as c:
        c.execute("INSERT INTO user_alerts (symbol, metric, condition, threshold) VALUES "
                  "('RELIANCE.NS','Daily','Below or equal',5)")
        c.execute("INSERT INTO user_alerts (symbol, metric, condition, threshold, enabled) VALUES "
                  "('OFF.NS','Daily','Below or equal',5,false)")
        c.execute("INSERT INTO holdings (symbol, buy_date, buy_price, quantity) VALUES ('SUNTV.NS','2026-01-01',600,10)")
        c.execute("INSERT INTO holdings (symbol, buy_date, buy_price, quantity, status, sell_date, sell_price) "
                  "VALUES ('SOLD.NS','2026-01-01',100,1,'SOLD','2026-02-01',120)")
    if alerts:
        with service_tx(s) as c:
            for sym, chg, at, status in (("RELIANCE.NS", -6.67, datetime(2026, 10, 19, 4, 35, tzinfo=timezone.utc), "ACTIVE"),
                                         ("TCS.NS", -9.0, datetime(2026, 10, 19, 4, 35, tzinfo=timezone.utc), "ACTIVE"),
                                         ("RELIANCE.NS", -7.0, datetime(2026, 10, 17, 4, 35, tzinfo=timezone.utc), "RESET")):
                c.execute("INSERT INTO alert_history (user_id, symbol, metric, condition, threshold, triggered_value, "
                          "triggered_at, status, trigger_price, trigger_session) VALUES "
                          "(%s,%s,'Daily','Below or equal',5,%s,%s,%s,1400,'2026-10-16')", (uid, sym, chg, at, status))


@pytest.fixture
def email_settings(pg_settings):
    def make(jobs=None, **env):
        base = dict(MAIL_SENDER="me@example.com", MAIL_APP_PASSWORD="app-password-xyz",
                    MAIL_RECIPIENT="ops@example.com", WHATSAPP_ENABLED="false")
        base.update(env)
        s = pg_settings(jobs=jobs, **base)
        object.__setattr__(s, "recommendations", {"max_data_age_days": 4})
        return s
    return make


def test_watchlist_is_enabled_rules_plus_active_holdings_plus_own_watchlist(email_settings):
    s = email_settings()
    me = add_user(s, "me@example.com")
    seed_me(s, me["id"], alerts=False)
    with user_tx(s, me["id"]) as c:
        c.execute("INSERT INTO watchlist (symbol) VALUES ('INFY.NS')")
    syms, holdings = watchlist(s, me["id"])
    assert syms == ["INFY.NS", "RELIANCE.NS", "SUNTV.NS"]  # OFF (disabled rule) and SOLD excluded
    assert holdings == {"SUNTV.NS": [(600.0, 10.0)]}


def test_only_tracked_and_todays_alerts_reported(email_settings):
    s = email_settings()
    me = add_user(s, "me@example.com")
    seed_me(s, me["id"])
    alerts = todays_alerts(s, me["id"], {"RELIANCE.NS", "SUNTV.NS"}, date(2026, 10, 19))
    assert [a["symbol"] for a in alerts] == ["RELIANCE.NS"]  # TCS untracked; the RESET one is from an earlier day


def test_min_severity_filter(email_settings):
    s = email_settings(ALERT_MIN_SEVERITY="CRITICAL")
    me = add_user(s, "me@example.com")
    seed_me(s, me["id"])
    assert todays_alerts(s, me["id"], {"RELIANCE.NS"}, date(2026, 10, 19)) == []  # -6.67% < 2x5% threshold
    assert severity(-10.0, 5.0) == "CRITICAL" and severity(-6.0, 5.0) == "WARNING"


def test_combined_report_content_is_personal(email_settings):
    s = email_settings(REPORT_INCLUDE_CROSS_ASSET="false")
    me = add_user(s, "me@example.com")
    seed_me(s, me["id"])
    subject, body, html = build_report(s, FakeCH(PRICES), ist(2026, 10, 19, 16, 45), me)
    assert "RELIANCE" in body and "SUNTV" in body
    assert "TCS" not in body  # untracked stock never appears
    alert_row = next(line for line in body.splitlines() if line.startswith("WARNING") and "RELIANCE" in line)
    assert "Below or equal 5.0% (Daily)" in alert_row and "-6.67%" in alert_row and "₹1,400.00" in alert_row
    suntv_row = next(line for line in body.splitlines() if line.startswith("SUNTV"))
    assert "10 @ ₹600.00" in suntv_row and "+5.0%" in suntv_row and "₹300.00" in suntv_row
    assert html.count("<table") == 2 and "TCS" not in html
    assert "1 triggered alert(s)" in subject


def test_reports_never_contain_another_users_data(email_settings):
    s = email_settings(EMAIL_NOTIFICATION_MODE="WATCHLIST_DAILY_SUMMARY")
    alice, bob = add_user(s, "alice@example.com"), add_user(s, "bob@example.com")
    seed_me(s, alice["id"])  # Alice: RELIANCE rule + alert, SUNTV holding
    with user_tx(s, bob["id"]) as c:
        c.execute("INSERT INTO holdings (symbol, buy_date, buy_price, quantity) VALUES ('TCS.NS','2026-01-01',2900,2)")
    _, bob_body, bob_html = build_report(s, FakeCH(PRICES), ist(2026, 10, 19, 16, 45), bob)
    assert "TCS" in bob_body
    for leaked in ("RELIANCE", "SUNTV", "600.00"):
        assert leaked not in bob_body and leaked not in bob_html


def test_per_user_mode_preference(email_settings):
    s = email_settings(EMAIL_NOTIFICATION_MODE="COMBINED_DAILY_REPORT")
    quiet = add_user(s, "quiet@example.com", prefs={"email_mode": "TRIGGERED_ALERTS_ONLY"})
    assert build_report(s, FakeCH(PRICES), ist(2026, 10, 19, 16, 45), quiet) is None


def test_report_with_recommendations_snapshot(email_settings):
    s = email_settings(EMAIL_NOTIFICATION_MODE="RECOMMENDATIONS_ONLY")
    me = add_user(s, "me@example.com")
    pick = {"symbol": "ABC.NS", "name": "ABC", "asset_class": "Equity", "category": "Mid-cap", "exchange": "NSE",
            "signal": "BUY CANDIDATE", "score": 71.2, "components": {"trend": 100, "momentum": 80},
            "price": 250.0, "price_date": "2026-10-19", "freshness": "FRESH", "horizon": "Swing (2-6 weeks)",
            "entry_low": 245.0, "entry_high": 250.0, "stop": 240.0, "target1": 270.0, "target2": None,
            "target_basis": "2R projection", "risk_reward": 2.0, "position_size": "units = ...",
            "reasons": ["trend ok"], "risks": ["volatility"], "confidence": "LOW"}
    snap = {"run_date": "2026-10-19", "market_uptrend": True, "validation": "UNVALIDATED", "universe": 503,
            "rankings": {"overall": [pick], "Large-cap": [], "Mid-cap": [pick], "Small-cap": [], "cross_asset": [pick]},
            "commodities": [{"symbol": "GOLDBEES.NS", "name": "Gold", "exchange": "NSE", "currency": "INR",
                             "signal": "WATCHLIST", "excluded_reason": None, "score": 52.0, "price": 120.0,
                             "price_date": "2026-10-19"}]}
    (s.state_dir / "recommendations_latest.json").write_text(json.dumps(snap))
    subject, body, html = build_report(s, FakeCH(PRICES), ist(2026, 10, 19, 16, 45), me)
    assert "YOUR WATCHLIST" not in body
    assert "TOP 10 OVERALL" in body and "TOP 10 LARGE-CAP" in body and "No instrument met" in body
    assert "GOLDBEES.NS" in body and "MCX gold/silver futures are not available" in body
    assert "UNVALIDATED" in body and "not guaranteed" in body


def test_failed_backtest_banner(email_settings):
    s = email_settings(EMAIL_NOTIFICATION_MODE="RECOMMENDATIONS_ONLY")
    me = add_user(s, "me@example.com")
    (s.state_dir / "recommendations_latest.json").write_text(json.dumps({
        "run_date": "2026-10-19", "market_uptrend": False, "validation": "FAILED", "universe": 10,
        "rankings": {k: [] for k in ("overall", "Large-cap", "Mid-cap", "Small-cap", "cross_asset")},
        "commodities": []}))
    (s.state_dir / "backtest_latest.json").write_text(json.dumps({
        "period": ["2023-10-16", "2026-10-16"],
        "segments": {"out_of_sample": {"trades": 104, "win_rate_pct": 38.5, "expectancy_pct": -0.43,
                                       "profit_factor": 0.89, "max_drawdown_pct": 35.48}},
        "by_regime": {"bear": {"expectancy_pct": -0.916, "profit_factor": 0.79}}}))
    _, body, _ = build_report(s, FakeCH(PRICES), ist(2026, 10, 19, 16, 45), me)
    assert "FAILED its backtest" in body and "expectancy -0.43%/trade" in body and "-0.916" in body


def test_stale_recommendations_not_presented_as_today(email_settings):
    s = email_settings(EMAIL_NOTIFICATION_MODE="RECOMMENDATIONS_ONLY")
    me = add_user(s, "me@example.com")
    (s.state_dir / "recommendations_latest.json").write_text(json.dumps({"run_date": "2026-10-16"}))
    _, body, _ = build_report(s, FakeCH(PRICES), ist(2026, 10, 19, 16, 45), me)
    assert "latest recommendations are from 2026-10-16" in body and "TOP 10" not in body


def test_triggered_alerts_only_mode_sends_no_report(email_settings):
    s = email_settings(EMAIL_NOTIFICATION_MODE="TRIGGERED_ALERTS_ONLY")
    me = add_user(s, "me@example.com")
    assert build_report(s, FakeCH(PRICES), ist(2026, 10, 19, 16, 45), me) is None
    assert s.report.alert_emails and not s.report.daily_report
    r = email_settings(EMAIL_NOTIFICATION_MODE="RECOMMENDATIONS_ONLY").report
    assert not r.alert_emails and r.recommendation_sections and not r.watchlist_section


def test_dedup_window_suppresses_same_rule(email_settings):
    s = email_settings(EMAIL_DEDUP_WINDOW_HOURS=24)
    args = dict(channel="email", subject="NSE Alert", dedup_key="RELIANCE.NS|Daily|Below|5.0", dedup_window_hours=24)
    assert enqueue(s, "alert:1", "b", ("me@x",), 3, now=utc(2026, 10, 19, 5), **args) == 1
    assert enqueue(s, "alert:1", "b", ("me@x",), 3, now=utc(2026, 10, 19, 6), **args) == 0  # retry/restart
    assert enqueue(s, "alert:2", "b", ("me@x",), 3, now=utc(2026, 10, 19, 10), **args) == 0  # re-trigger in window
    assert enqueue(s, "alert:3", "b", ("me@x",), 3, now=utc(2026, 10, 20, 6), **args) == 1  # after the window
    assert enqueue(s, "alert:2", "b", ("other@x",), 3, now=utc(2026, 10, 19, 10), **args) == 1  # other recipient


def _box(s, sender, clock=None):
    return Outbox(s, client=None, clock=clock or Clock(utc(2026, 10, 19, 11)), email_sender=sender)


def test_email_delivery_retry_then_success(email_settings):
    s = email_settings(EMAIL_RETRY_BACKOFF_SECONDS=60)
    calls = []

    def flaky(cfg, subject, body, html=None):
        calls.append((cfg.recipient, subject))
        if len(calls) == 1:
            raise smtplib.SMTPServerDisconnected("dropped")

    clock = Clock(utc(2026, 10, 19, 11))
    box = _box(s, flaky, clock)
    box.enqueue_email("report:2026-10-19", "Daily", "body", recipients=("me@example.com",))
    assert box.process_due() == (0, 1)
    assert box.process_due() == (0, 0)  # backoff respected, no tight loop
    clock.advance(seconds=61)
    assert box.process_due() == (1, 0)
    assert calls == [("me@example.com", "Daily")] * 2
    assert box.process_due() == (0, 0)  # never resent after success


def test_email_permanent_failure_and_bounded_attempts(email_settings):
    s = email_settings(EMAIL_MAX_ATTEMPTS=3, EMAIL_RETRY_BACKOFF_SECONDS=30, EMAIL_RETRY_BACKOFF_MAX_SECONDS=30)
    refused = smtplib.SMTPRecipientsRefused({"me@example.com": (550, b"no such user")})
    assert classify_smtp_error(refused) is False
    assert classify_smtp_error(smtplib.SMTPAuthenticationError(535, b"bad creds")) is True
    box = _box(s, lambda *a: (_ for _ in ()).throw(refused))
    box.enqueue_email("k1", "s", "b")
    box.process_due()
    assert box.items(channel="email")[0].status == "FAILED"
    attempts = []
    clock = Clock(utc(2026, 10, 19, 11))

    def down(*a):
        attempts.append(1)
        raise OSError("network unreachable")
    box2 = _box(s, down, clock)
    box2.enqueue_email("k2", "s", "b")
    for _ in range(10):
        box2.process_due()
        clock.advance(seconds=31)
    assert len(attempts) == 3


def test_email_not_resent_after_crash_mid_send(email_settings):
    s = email_settings()
    sent = []
    box = _box(s, lambda *a: sent.append(a))
    box.enqueue_email("alert:5", "s", "b")
    box._claim(box.items()[0].id)
    box.recover()
    assert box.items()[0].status == "FAILED" and box.process_due() == (0, 0) and sent == []


def test_credentials_not_in_stored_errors(email_settings):
    s = email_settings()
    from alert_system.logging_setup import register_secrets
    register_secrets(s.secrets)
    box = _box(s, lambda *a: (_ for _ in ()).throw(smtplib.SMTPAuthenticationError(535, b"app-password-xyz bad")))
    box.enqueue_email("k", "s", "b")
    box.process_due()
    assert "app-password-xyz" not in box.items()[0].last_error


def test_daily_report_job_queues_one_per_user_once_per_day(email_settings, monkeypatch):
    s = email_settings(jobs=[make_job("daily_report", builtin="daily_report")])
    a, b = add_user(s, "alice@example.com"), add_user(s, "bob@example.com")
    seed_me(s, a["id"])
    with service_tx(s) as c:
        c.execute("UPDATE users SET is_active=false WHERE email='bob@example.com'")
        c.execute("INSERT INTO users (email, password_hash) VALUES ('carol@example.com', 'x')")
    monkeypatch.setattr(service_module.clickhouse, "get_client", lambda st: FakeCH(PRICES))
    svc = service_module.Service(s, clock=Clock(ist(2026, 10, 19, 16, 45)), online=lambda: True,
                                 clickhouse_ok=lambda: True)
    svc.daily_report(None, None, 1)
    svc.daily_report(None, None, 2)  # retry/restart same day
    items = svc.outbox.items(channel="email")
    assert sorted(i.recipient for i in items) == ["alice@example.com", "carol@example.com"]  # bob inactive
    assert all(i.group_key == "report:2026-10-19" and i.user_id for i in items)
    assert b["id"] not in {i.user_id for i in items}


def test_report_time_override():
    from pathlib import Path
    from alert_system.config import load_settings
    root = Path(__file__).resolve().parent.parent
    s = load_settings(env={"ALERT_BASE_DIR": str(root), "DAILY_REPORT_TIME": "18:05"})
    assert s.job("daily_report").schedules == ("5 18 * * 1-5",)
