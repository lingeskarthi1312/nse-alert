import json
import sys
import threading

import pytest

from alert_system import service as service_module
from alert_system.cleanup import RetentionError, TableResult
from alert_system.jobs import JobFailed, JobInterrupted, run_command_job
from alert_system.singleton import AlreadyRunning, InstanceLock
from alert_system.state import JobStore
from conftest import Clock, ist, make_job


class NullClient:
    def parts_for(self, body):
        return [body]

    def send_part(self, recipient, body):
        return "wamid.1", "text"


@pytest.fixture
def svc(pg_settings):
    s = pg_settings(jobs=[make_job("daily_summary", ["0 18 * * *"], builtin="daily_summary"),
                               make_job("mtd_cleanup", ["20 0 * * *"], builtin="mtd_cleanup")])
    return service_module.Service(s, clock=Clock(ist(2026, 10, 19, 18, 0)), online=lambda: True,
                                  clickhouse_ok=lambda: True, whatsapp_client=NullClient())


def test_daily_summary_reports_runs_and_outbox(svc):
    svc.store.create_run("daily_summary", "k1", "schedule", svc.clock(), 10, svc.clock())
    svc.outbox.enqueue("alert:1", "x")
    text = svc.daily_summary(None, None, 1)
    assert "daily_summary: PENDING=1" in text and "PENDING=2" in text
    assert (svc.settings.state_dir / "daily_summary.txt").exists()


def test_mtd_cleanup_error_raises_for_retry(svc, monkeypatch):
    class FakeCH:
        def close(self):
            pass

    class BrokenCleaner:
        def __init__(self, *a, **k):
            pass

        def run(self):
            return [TableResult("nse_alerts.t", "2026-10-01", status="ERROR", error="boom")]

    monkeypatch.setattr(service_module.clickhouse, "get_client", lambda s: FakeCH())
    monkeypatch.setattr(service_module, "MTDCleaner", BrokenCleaner)
    with pytest.raises(RetentionError, match="boom"):
        svc.mtd_cleanup(None, None, 1)


def test_job_failure_alerts_once_on_first_and_final(svc):
    keys = []
    svc.notifier.alert = lambda key, subject, text, channels=None: keys.append(key)
    run = svc.store.get(svc.store.create_run("daily_summary", "k", "manual", svc.clock(), 10, svc.clock()))
    job = svc.settings.job("daily_summary")
    for attempt, status in [(1, "RETRY_SCHEDULED"), (2, "RETRY_SCHEDULED"), (10, "FAILED")]:
        svc._job_failed(job, run, attempt, status, "err")
    assert len(keys) == 2


def test_ops_alert_dedup(svc):
    sent = []
    svc.notifier.email_sender = lambda *a: sent.append(a)
    object.__setattr__(svc.settings, "ops_alert_channels", ("whatsapp",))
    assert svc.notifier.alert("job:x:1:FAILED", "x failed", "details") is True
    assert svc.notifier.alert("job:x:1:FAILED", "x failed", "details") is False
    assert svc.outbox.counts() == {"PENDING": 2}


def test_outbox_failure_for_ops_message_does_not_loop(svc):
    calls = []
    svc.notifier.alert = lambda key, subject, text, channels=None: calls.append(key)
    from alert_system.outbox import OutboxItem
    item = OutboxItem(1, "alert:9", "919999900001", "b", "FAILED", 1, 1, None, 0, "")
    svc._outbox_failed(item, "err")
    svc._outbox_failed(OutboxItem(2, "ops:job:1", "919999900001", "b", "FAILED", 1, 1, None, 0, ""), "err")
    assert calls == ["outbox:1"]


def test_tick_materialises_delivers_and_writes_health(svc):
    svc.outbox.enqueue("alert:1", "x")
    svc.tick()
    health = json.loads(svc.settings.health_file.read_text())
    assert health["online"] is True and health["outbox"] == {"SENT": 2}


def test_offline_tick_keeps_messages_queued(pg_settings):
    s = pg_settings(jobs=[make_job("daily_summary", builtin="daily_summary")])
    svc = service_module.Service(s, online=lambda: False, clickhouse_ok=lambda: True, whatsapp_client=NullClient())
    svc.outbox.enqueue("alert:1", "x")
    svc.tick()
    assert svc.outbox.counts() == {"PENDING": 2}


# --- command job runner -----------------------------------------------------------

def _cmd_job(settings_factory, code, **kw):
    s = settings_factory(jobs=[make_job("cmd", ["0 10 * * *"], steps=[[sys.executable, "-c", code]],
                                        log_file="cmd.log", **kw)])
    store = JobStore(s.scheduler_db)
    run = store.get(store.create_run("cmd", "k", "manual", ist(2026, 10, 19, 10, 0), 10, ist(2026, 10, 19, 10, 0)))
    return s, s.job("cmd"), run


def test_command_success_logs_to_existing_log_file(settings_factory):
    s, job, run = _cmd_job(settings_factory, "import os; print('hello', os.environ['TZ'])")
    run_command_job(s, job, run, 1, threading.Event())
    text = (s.base_dir / "cmd.log").read_text()
    assert "hello Asia/Kolkata" in text and "exit=0" in text


def test_command_failure_captures_output_tail(settings_factory):
    s, job, run = _cmd_job(settings_factory, "import sys; print('bad thing'); sys.exit(3)")
    with pytest.raises(JobFailed) as info:
        run_command_job(s, job, run, 1, threading.Event())
    assert info.value.exit_code == 3 and "bad thing" in str(info.value)


def test_command_timeout_kills_process(settings_factory):
    s, job, run = _cmd_job(settings_factory, "import time; time.sleep(60)", timeout_seconds=10)
    object.__setattr__(job, "timeout_seconds", 2)
    with pytest.raises(JobFailed, match="timed out"):
        run_command_job(s, job, run, 1, threading.Event())


def test_command_interrupted_on_shutdown(settings_factory):
    s, job, run = _cmd_job(settings_factory, "import time; time.sleep(60)")
    stop = threading.Event()
    stop.set()
    with pytest.raises(JobInterrupted):
        run_command_job(s, job, run, 1, stop)


def test_command_lock_prevents_overlap_with_manual_run(settings_factory):
    s, job, run = _cmd_job(settings_factory, "print(1)", lock_file="x.lock")
    with InstanceLock(s.base_dir / "x.lock"):
        with pytest.raises(JobFailed, match="held by another process"):
            run_command_job(s, job, run, 1, threading.Event())


def test_single_instance_lock(tmp_path):
    first = InstanceLock(tmp_path / "svc.lock").acquire()
    try:
        with pytest.raises(AlreadyRunning):
            InstanceLock(tmp_path / "svc.lock").acquire()
    finally:
        first.release()
    InstanceLock(tmp_path / "svc.lock").acquire().release()


def test_redaction_of_secrets():
    from alert_system.logging_setup import redact, register_secrets
    register_secrets(["supersecretpassword"])
    out = redact("login failed for supersecretpassword; Authorization: Bearer EAAabc.def access_token=xyz")
    assert "supersecretpassword" not in out and "EAAabc" not in out and "xyz" not in out


def test_project_scripts_compile():
    import py_compile
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for name in ("load_all_stock_prices.py", "scheduled_daily_alerts.py", "save_mtd_rankings.py",
                 "whatsapp_mtd_rankings.py", "whatsapp_notify.py"):
        py_compile.compile(str(root / name), doraise=True)


def test_email_only_mode_skips_whatsapp(pg_settings):
    s = pg_settings(jobs=[make_job("daily_summary", builtin="daily_summary")],
                         WHATSAPP_ENABLED="false", OPS_ALERT_CHANNELS="email,whatsapp")
    svc = service_module.Service(s, online=lambda: True, clickhouse_ok=lambda: True, whatsapp_client=NullClient())
    emails = []
    svc.notifier.email_sender = lambda cfg, subject, body: emails.append(subject)
    object.__setattr__(s, "email", s.email.__class__(sender="a@x", password="p", recipient="a@x"))
    svc.notifier.alert("job:x:1:FAILED", "x failed", "details")
    svc.tick()
    assert emails == ["[NSE alert system] x failed"]
    assert svc.outbox.counts() == {}  # nothing queued for WhatsApp


def test_daily_alert_script_price_and_daily_rules(pg_settings, monkeypatch):
    """scheduled_daily_alerts.main(): per-user rules, one transaction, own email address."""
    from datetime import date as d
    import scheduled_daily_alerts as sda
    from alert_system.auth import hash_password
    from alert_system.pgdb import service_tx, user_tx
    s = pg_settings(MAIL_SENDER="me@example.com", MAIL_APP_PASSWORD="pw-123456", MAIL_RECIPIENT="ops@example.com")
    with service_tx(s) as c:
        a = c.execute("INSERT INTO users (email, password_hash) VALUES ('a@example.com', %s) RETURNING id",
                      (hash_password("x-123456789"),)).fetchone()["id"]
        b = c.execute("INSERT INTO users (email, password_hash) VALUES ('b@example.com', %s) RETURNING id",
                      (hash_password("x-123456789"),)).fetchone()["id"]
    with user_tx(s, a) as c:
        c.execute("INSERT INTO user_alerts (symbol, metric, condition, threshold) VALUES ('X.NS','Daily','Below or equal',5)")
    with user_tx(s, b) as c:
        c.execute("INSERT INTO user_alerts (symbol, metric, condition, threshold) VALUES ('X.NS','Price','Below or equal',95)")
        c.execute("INSERT INTO user_alerts (symbol, metric, condition, threshold) VALUES ('X.NS','Price','Above or equal',99)")
    today = sda.datetime.now(sda.LOG_TZ).date()
    monkeypatch.setattr(sda, "load_settings", lambda: s)
    monkeypatch.setattr(sda, "fetch_prices", lambda syms: {"X.NS": {today: 90.0, today - __import__("datetime").timedelta(days=1): 100.0}})
    sda.main()
    sda.main()  # second run: nothing new (ACTIVE suppresses duplicates)
    with service_tx(s) as c:
        hist = c.execute("SELECT user_id, metric, condition FROM alert_history ORDER BY id").fetchall()
        mails = c.execute("SELECT recipient, user_id FROM notification_outbox ORDER BY id").fetchall()
    assert [(h["user_id"], h["metric"]) for h in hist] == [(a, "Daily"), (b, "Price")]  # -10% and 90 <= 95
    assert [(m["recipient"], m["user_id"]) for m in mails] == [("a@example.com", a), ("b@example.com", b)]


def test_daily_alert_script_since_set_rule(pg_settings, monkeypatch):
    """SinceSet: close vs base_price, only closes after the rule was set, one-shot."""
    from datetime import timedelta
    import scheduled_daily_alerts as sda
    from alert_system.auth import hash_password
    from alert_system.pgdb import service_tx, user_tx
    s = pg_settings(MAIL_SENDER="me@example.com", MAIL_APP_PASSWORD="pw-123456", MAIL_RECIPIENT="ops@example.com")
    with service_tx(s) as c:
        a = c.execute("INSERT INTO users (email, password_hash) VALUES ('a@example.com', %s) RETURNING id",
                      (hash_password("x-123456789"),)).fetchone()["id"]
    now = sda.datetime.now(sda.LOG_TZ)
    today = now.date()
    with user_tx(s, a) as c:  # base 100; the stored closes are 95 (-5%) today, 100 yesterday (daily move -5%)
        c.execute("INSERT INTO user_alerts (symbol, metric, condition, threshold, base_price, created_at) "
                  "VALUES ('X.NS','SinceSet','Below or equal',5,100,%s)", (now,))
    monkeypatch.setattr(sda, "load_settings", lambda: s)
    monkeypatch.setattr(sda, "fetch_prices", lambda syms: {"X.NS": {today: 95.0, today - timedelta(days=1): 100.0}})
    with service_tx(s) as c:
        c.execute("UPDATE user_alerts SET created_at=%s", (sda.datetime.combine(today, sda.SESSION_CLOSE, sda.LOG_TZ),))
    sda.main()  # set exactly at today's close: today's close is not "after" the rule yet
    with service_tx(s) as c:
        assert c.execute("SELECT count(*) n FROM alert_history").fetchone()["n"] == 0
        c.execute("UPDATE user_alerts SET created_at=%s", (sda.datetime.combine(today, sda.SESSION_CLOSE, sda.LOG_TZ)
                                                           - timedelta(hours=2),))
    sda.main()
    sda.main()  # rule is off now: nothing new
    with service_tx(s) as c:
        hist = c.execute("SELECT metric, triggered_value FROM alert_history").fetchall()
        enabled = c.execute("SELECT enabled FROM user_alerts").fetchone()["enabled"]
        mails = c.execute("SELECT recipient FROM notification_outbox").fetchall()
    assert [(h["metric"], round(h["triggered_value"], 2)) for h in hist] == [("SinceSet", -5.0)]
    assert enabled is False and [m["recipient"] for m in mails] == ["a@example.com"]
