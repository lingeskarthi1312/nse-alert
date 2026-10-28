"""Intraday live alerts for multiple users (fake quote provider, fake
ClickHouse, real PostgreSQL test database)."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from alert_system import service as service_module
from alert_system.auth import hash_password
from alert_system.intraday import Quote, YahooQuotes, market_window, run_intraday
from alert_system.pgdb import service_tx, user_tx
from conftest import Clock, ist

IST = ZoneInfo("Asia/Kolkata")
NOW = ist(2026, 10, 19, 11, 0)
PREV = {"CUPID.NS": (date(2026, 10, 16), 350.0), "SUNTV.NS": (date(2026, 10, 16), 600.0)}


class FakeCH:
    def __init__(self, prev):
        self.prev = prev

    def query(self, sql, parameters=None):
        class R:
            pass
        r = R()
        r.result_rows = [(s, d, c) for s, (d, c) in self.prev.items() if s in parameters["s"]]
        return r

    def close(self):
        pass


class FakeQuotes:
    def __init__(self, quotes):
        self.quotes = quotes
        self.calls = 0

    def get_quotes(self, symbols):
        self.calls += 1
        return {s: q for s, q in self.quotes.items() if s in symbols}


def q(sym, price, minutes_ago=15, day_offset=0, now=NOW):
    return Quote(sym, price, now.astimezone(IST) - timedelta(minutes=minutes_ago, days=day_offset), "fake", "test feed")


def add_user(s, email, rules, prefs="{}"):
    with service_tx(s) as c:
        uid = c.execute("INSERT INTO users (email, password_hash, prefs) VALUES (%s, %s, %s) RETURNING id",
                        (email, hash_password("Password-123"), prefs)).fetchone()["id"]
    with user_tx(s, uid) as c:
        for sym, cond, thr, enabled in rules:
            c.execute("INSERT INTO user_alerts (symbol, metric, condition, threshold, enabled) "
                      "VALUES (%s, 'Daily', %s, %s, %s)", (sym, cond, thr, enabled))
    return uid


@pytest.fixture
def live_settings(pg_settings):
    def make(**env):
        s = pg_settings(MAIL_SENDER="me@example.com", MAIL_APP_PASSWORD="pw-123456", MAIL_RECIPIENT="ops@example.com",
                        WHATSAPP_ENABLED="false", **env)
        uid = add_user(s, "me@example.com", [("CUPID.NS", "Below or equal", 10.0, True),
                                             ("SUNTV.NS", "Above or equal", 5.0, True),
                                             ("OFF.NS", "Below or equal", 1.0, False)])
        return s, uid
    return make


def history(s, uid=None):
    with service_tx(s) as c:
        rows = c.execute("SELECT user_id, symbol, metric, status, trigger_price, trigger_session FROM alert_history "
                         "ORDER BY id").fetchall()
    return [(r["symbol"], r["metric"], r["status"], r["trigger_price"], str(r["trigger_session"]))
            for r in rows if uid is None or r["user_id"] == uid]


def emails(s):
    with service_tx(s) as c:
        return [(r["group_key"], r["recipient"], r["subject"], r["body_html"] is not None) for r in c.execute(
            "SELECT group_key, recipient, subject, body_html FROM notification_outbox WHERE channel='email' ORDER BY id")]


def test_market_window():
    assert market_window(ist(2026, 10, 19, 9, 15).astimezone(IST))
    assert market_window(ist(2026, 10, 19, 15, 45).astimezone(IST))
    assert not market_window(ist(2026, 10, 19, 9, 10).astimezone(IST))
    assert not market_window(ist(2026, 10, 19, 16, 0).astimezone(IST))
    assert not market_window(ist(2026, 10, 18, 11, 0).astimezone(IST))  # Sunday


def test_trigger_once_per_day_and_email_table(live_settings):
    s, uid = live_settings()
    prov = FakeQuotes({"CUPID.NS": q("CUPID.NS", 310.0), "SUNTV.NS": q("SUNTV.NS", 612.0)})
    r = run_intraday(s, FakeCH(PREV), NOW, provider=prov)
    assert r["checked"] == 2 and [t["symbol"] for t in r["triggered"]] == ["CUPID.NS"]  # -11.43%; SUNTV +2%
    assert history(s) == [("CUPID.NS", "Intraday", "ACTIVE", 310.0, "2026-10-19")]
    [(key, to, subject, has_html)] = emails(s)
    assert key.startswith("alert:") and to == "me@example.com" and subject == "NSE Live Alert: CUPID -11.43%" and has_html
    run_intraday(s, FakeCH(PREV), NOW + timedelta(minutes=5), provider=prov)
    run_intraday(s, FakeCH(PREV), NOW + timedelta(minutes=10), provider=prov)
    assert len(history(s)) == 1 and len(emails(s)) == 1


def test_each_user_gets_only_own_alerts(live_settings):
    s, me = live_settings()
    other = add_user(s, "other@example.com", [("SUNTV.NS", "Above or equal", 1.0, True)])
    prov = FakeQuotes({"CUPID.NS": q("CUPID.NS", 310.0), "SUNTV.NS": q("SUNTV.NS", 612.0)})
    run_intraday(s, FakeCH(PREV), NOW, provider=prov)
    assert [h[0] for h in history(s, me)] == ["CUPID.NS"]
    assert [h[0] for h in history(s, other)] == ["SUNTV.NS"]  # +2% met only the other user's 1% rule
    assert sorted((e[1], e[2].split(":")[1].split()[0]) for e in emails(s)) == [
        ("me@example.com", "CUPID"), ("other@example.com", "SUNTV")]
    with user_tx(s, other) as c:  # and RLS shows each user only their own history
        assert [r["symbol"] for r in c.execute("SELECT symbol FROM alert_history")] == ["SUNTV.NS"]


def test_new_day_resets_and_can_alert_again(live_settings):
    s, _ = live_settings(EMAIL_DEDUP_WINDOW_HOURS=0)
    run_intraday(s, FakeCH(PREV), NOW, provider=FakeQuotes({"CUPID.NS": q("CUPID.NS", 310.0)}))
    tomorrow = NOW + timedelta(days=1)
    run_intraday(s, FakeCH({"CUPID.NS": (date(2026, 10, 19), 340.0)}), tomorrow,
                 provider=FakeQuotes({"CUPID.NS": q("CUPID.NS", 300.0, now=tomorrow)}))
    h = history(s)
    assert [x[2] for x in h] == ["RESET", "ACTIVE"] and h[1][4] == "2026-10-20"
    assert len(emails(s)) == 2


def test_stale_or_old_session_quotes_never_alert(live_settings):
    s, _ = live_settings()
    prov = FakeQuotes({"CUPID.NS": q("CUPID.NS", 300.0, minutes_ago=45), "SUNTV.NS": q("SUNTV.NS", 700.0, day_offset=3)})
    r = run_intraday(s, FakeCH(PREV), NOW, provider=prov)
    assert r["triggered"] == [] and "stale" in r["skipped"]["CUPID.NS"] and "market closed" in r["skipped"]["SUNTV.NS"]
    assert history(s) == []


def test_missing_previous_close_skips(live_settings):
    s, _ = live_settings()
    r = run_intraday(s, FakeCH({}), NOW, provider=FakeQuotes({"CUPID.NS": q("CUPID.NS", 300.0)}))
    assert r["triggered"] == [] and "previous close" in r["skipped"]["CUPID.NS"]


def test_outside_hours_does_nothing(live_settings):
    s, _ = live_settings()
    prov = FakeQuotes({"CUPID.NS": q("CUPID.NS", 300.0)})
    assert run_intraday(s, FakeCH(PREV), ist(2026, 10, 19, 17, 0), provider=prov) == {"status": "market_closed"}
    assert prov.calls == 0


def test_no_quotes_raises_for_retry(live_settings):
    s, _ = live_settings()
    with pytest.raises(RuntimeError, match="no live quotes"):
        run_intraday(s, FakeCH(PREV), NOW, provider=FakeQuotes({}))


def test_mode_and_severity_respected(live_settings):
    s, _ = live_settings(ALERT_MIN_SEVERITY="CRITICAL")
    r = run_intraday(s, FakeCH(PREV), NOW, provider=FakeQuotes({"CUPID.NS": q("CUPID.NS", 310.0)}))
    assert r["triggered"][0]["emailed"] is False and emails(s) == []  # -11.4% < 2x10% => WARNING


def test_user_preference_recommendations_only_records_but_does_not_email(live_settings):
    s, _ = live_settings()
    with service_tx(s) as c:
        c.execute("""UPDATE users SET prefs = '{"email_mode": "RECOMMENDATIONS_ONLY"}'""")
    run_intraday(s, FakeCH(PREV), NOW, provider=FakeQuotes({"CUPID.NS": q("CUPID.NS", 250.0)}))
    assert emails(s) == [] and len(history(s)) == 1


def test_daily_and_intraday_alerts_do_not_interfere(live_settings):
    s, uid = live_settings()
    with service_tx(s) as c:
        c.execute("INSERT INTO alert_history (user_id, symbol, metric, condition, threshold, triggered_value, status) "
                  "VALUES (%s,'CUPID.NS','Daily','Below or equal',10,-12,'ACTIVE')", (uid,))
    run_intraday(s, FakeCH(PREV), NOW, provider=FakeQuotes({"CUPID.NS": q("CUPID.NS", 310.0)}))
    rows = history(s)
    assert ("CUPID.NS", "Daily", "ACTIVE") == rows[0][:3] and rows[1][1] == "Intraday"


def test_shared_snapshot_contains_no_user_data(live_settings):
    import json
    s, _ = live_settings()
    run_intraday(s, FakeCH(PREV), NOW, provider=FakeQuotes({"CUPID.NS": q("CUPID.NS", 310.0)}))
    snap = json.loads((s.state_dir / "live_quotes.json").read_text())
    assert set(snap) == {"checked_at", "provider", "quotes", "skipped", "triggered_count"}
    assert "me@example.com" not in json.dumps(snap)


def test_yahoo_provider_parses_multi_ticker_frame():
    idx = pd.DatetimeIndex(["2026-10-19 10:44", "2026-10-19 10:45"]).tz_localize("Asia/Kolkata")
    cols = pd.MultiIndex.from_product([["A.NS", "B.NS"], ["Open", "High", "Low", "Close", "Volume"]])
    frame = pd.DataFrame([[1, 1, 1, 100.0, 5, 1, 1, 1, None, 5], [1, 1, 1, 101.0, 5, 1, 1, 1, None, 5]],
                         index=idx, columns=cols)
    quotes = YahooQuotes(downloader=lambda *a, **k: frame).get_quotes(["A.NS", "B.NS", "C.NS"])
    assert set(quotes) == {"A.NS"} and quotes["A.NS"].price == 101.0
    assert quotes["A.NS"].ts.hour == 10 and quotes["A.NS"].ts.minute == 45


def test_streak_alert_only_after_consecutive_failures(live_settings):
    s, _ = live_settings()
    object.__setattr__(s, "jobs", ())
    svc = service_module.Service(s, clock=Clock(NOW), online=lambda: True, clickhouse_ok=lambda: True)
    keys = []
    svc.notifier.alert = lambda key, subject, text, channels=None: keys.append(key)
    from alert_system.config import JobSpec
    job = JobSpec(id="intraday_alerts", schedules=("*/5 9-15 * * 1-5",), builtin="intraday_alerts",
                  failure_alert_after=3)
    runs = []
    for i in range(3):
        rid = svc.store.create_run(job.id, f"k{i}", "schedule", NOW, 2, NOW)
        svc.store.claim(rid, NOW)
        svc.store.finish(rid, 1, "FAILED", NOW, error="boom")
        runs.append(svc.store.get(rid))
        svc._job_failed(job, runs[-1], 2, "FAILED", "boom")
        if i < 2:
            assert keys == []
    assert keys == ["job:intraday_alerts:2026-10-19:streak"]
    svc._job_failed(job, runs[-1], 1, "RETRY_SCHEDULED", "boom")
    assert len(keys) == 1


def test_price_level_rule_live(live_settings):
    s, uid = live_settings()
    with user_tx(s, uid) as c:
        c.execute("INSERT INTO user_alerts (symbol, metric, condition, threshold) VALUES ('SUNTV.NS','Price','Above or equal',610)")
    prov = FakeQuotes({"SUNTV.NS": q("SUNTV.NS", 612.0)})  # +2%: % rule silent, price rule >= 610 fires
    r = run_intraday(s, FakeCH(PREV), NOW, provider=prov)
    assert [(t["symbol"]) for t in r["triggered"]] == ["SUNTV.NS"]
    assert history(s)[0][:2] == ("SUNTV.NS", "Price live")
    [(_, to, subject, has_html)] = emails(s)
    assert to == "me@example.com" and subject.startswith("NSE Live Alert: SUNTV") and has_html
    run_intraday(s, FakeCH(PREV), NOW + timedelta(minutes=5), provider=prov)
    assert len(history(s)) == 1  # once per day


def test_rule_semantics():
    from alert_system.rules import describe, triggered
    assert triggered("Price", "Above or equal", 900, None, 900.0)
    assert not triggered("Price", "Below or equal", 800, None, 801.0)
    assert triggered("Daily", "Below or equal", 5, -5.0, 1) and not triggered("Daily", "Below or equal", 5, 5.0, 1)
    assert triggered("Daily", "Above or equal", 5, 6.0, 1)
    assert describe("Price", "Below or equal", 800) == "price ≤ ₹800.00"


def test_since_set_rule_live_fires_once_and_switches_off(live_settings):
    s, uid = live_settings()
    with user_tx(s, uid) as c:  # base 620 when set; -3% => fires at <= 601.40
        rule_id = c.execute("INSERT INTO user_alerts (symbol, metric, condition, threshold, base_price) "
                            "VALUES ('SUNTV.NS','SinceSet','Below or equal',3,620) RETURNING id").fetchone()["id"]
    r = run_intraday(s, FakeCH(PREV), NOW, provider=FakeQuotes({"SUNTV.NS": q("SUNTV.NS", 605.0)}))
    assert r["triggered"] == []  # -2.42% from 620 (and +0.83% on the day)
    r = run_intraday(s, FakeCH(PREV), NOW + timedelta(minutes=5), provider=FakeQuotes({"SUNTV.NS": q("SUNTV.NS", 600.0)}))
    assert [(t["symbol"], t["change_pct"]) for t in r["triggered"]] == [("SUNTV.NS", -3.23)]  # vs 620, not prev close
    assert history(s)[0][:3] == ("SUNTV.NS", "SinceSet", "ACTIVE")
    [(_, to, subject, _)] = emails(s)
    assert to == "me@example.com" and subject == "NSE Live Alert: SUNTV -3.23%"
    with user_tx(s, uid) as c:
        assert c.execute("SELECT enabled FROM user_alerts WHERE id=%s", (rule_id,)).fetchone()["enabled"] is False
    run_intraday(s, FakeCH(PREV), NOW + timedelta(minutes=10), provider=FakeQuotes({"SUNTV.NS": q("SUNTV.NS", 590.0)}))
    assert len(history(s)) == 1  # switched off: never fires again


def test_since_set_rule_semantics():
    from alert_system.rules import describe, triggered
    assert triggered("SinceSet", "Above or equal", 5, None, 105.0, 100.0)
    assert not triggered("SinceSet", "Above or equal", 5, 50.0, 104.0, 100.0)  # daily change is ignored
    assert triggered("SinceSet", "Below or equal", 5, None, 95.0, 100.0)
    assert describe("SinceSet", "Below or equal", 5, 1000) == "-5% from ₹1,000.00 (once)"
