"""Service wiring: single-instance lock, scheduler worker thread, WhatsApp
outbox delivery, built-in jobs (MTD cleanup, daily summary), health file."""
import json
import logging
import os
import signal
import threading
import uuid
from datetime import timedelta

from . import __version__, clickhouse
from .cleanup import MTDCleaner, RetentionError, cleanup_appdb
from .config import ConfigError
from .connectivity import ConnectivityMonitor
from .jobs import run_command_job
from .logging_setup import log_event, setup_logging
from .notify import OpsNotifier
from .outbox import Outbox
from .scheduler import Scheduler
from .singleton import InstanceLock
from .state import FAILED, JobStore
from .timeutil import local_today, to_iso, utcnow
from .whatsapp import WhatsAppClient

log = logging.getLogger("alert_system.service")
BUILTINS = ("mtd_cleanup", "daily_summary", "recommendations", "daily_report", "backtest", "intraday_alerts",
            "intraday_reco", "intraday_backtest")


class Service:
    def __init__(self, settings, clock=utcnow, online=None, clickhouse_ok=None, whatsapp_client=None):
        self.settings = settings
        self.clock = clock
        self.boot_id = str(uuid.uuid4())
        self.stop_event = threading.Event()
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = JobStore(settings.scheduler_db)
        self.connectivity = ConnectivityMonitor(settings)
        self._online = online or self.connectivity.online
        self._clickhouse_ok = clickhouse_ok or (lambda: clickhouse.ping(settings))
        self.outbox = Outbox(settings, whatsapp_client or WhatsAppClient(settings.whatsapp), clock=clock,
                             on_failed=self._outbox_failed)
        self.notifier = OpsNotifier(settings, self.outbox, clock=clock)
        runners = {}
        for job in settings.jobs:
            if job.builtin:
                if job.builtin not in BUILTINS:
                    raise ConfigError(f"job {job.id}: unknown builtin {job.builtin!r}")
                runners[job.id] = getattr(self, job.builtin)
            else:
                runners[job.id] = lambda j, r, a, stop: run_command_job(self.settings, j, r, a, stop)
        self.scheduler = Scheduler(settings, self.store, runners, online=self._online,
                                   clickhouse_ok=self._clickhouse_ok, clock=clock,
                                   on_failure=self._job_failed, boot_id=self.boot_id)

    # -- failure hooks ---------------------------------------------------------
    def _job_failed(self, job, run, attempt, status, error):
        # Frequent jobs (e.g. 5-minute live polls) alert once per day, only
        # after `failure_alert_after` consecutive runs ended FAILED.
        if job.failure_alert_after > 1:
            if status != FAILED:
                return
            recent = self.store.runs(job.id, limit=job.failure_alert_after + 5)
            finished = [r for r in recent if r.status in ("SUCCESS", FAILED)][:job.failure_alert_after]
            if len(finished) == job.failure_alert_after and all(r.status == FAILED for r in finished):
                day = local_today(self.settings.timezone, self.clock())
                self.notifier.alert(f"job:{job.id}:{day}:streak", f"{job.id} failing repeatedly",
                                    f"The last {job.failure_alert_after} runs of {job.id} failed "
                                    f"(latest run {run.id}).\n\n{error[:1500]}")
            return
        # Alert on the first failure of a run and when it is finally FAILED;
        # cleanup failures always alert on the first attempt (data safety).
        if attempt == 1 or status == FAILED:
            what = "FAILED permanently" if status == FAILED else f"failed (attempt {attempt}/{run.max_attempts}), retrying"
            self.notifier.alert(f"job:{job.id}:{run.id}:{status}", f"{job.id} {what}",
                                f"Job {job.id} run {run.id} scheduled for {run.scheduled_for} {what}.\n\n{error[:1500]}")

    def _outbox_failed(self, item, error):
        if item.group_key.startswith("ops:"):
            return  # never alert about failed alerts (loop)
        # Report a failure on the *other* channel(s); an email failure is
        # still attempted by direct SMTP (it may be recipient-specific).
        channels = tuple(c for c in self.settings.ops_alert_channels if c != item.channel) or ("log",)
        self.notifier.alert(f"outbox:{item.id}", f"{item.channel} delivery failed",
                            f"Message {item.group_key} to recipient ending {item.recipient[-4:]} was not delivered "
                            f"after {item.attempts} attempt(s): {error}\nIt remains in notification_outbox.",
                            channels=channels)

    # -- recommendation / report jobs ---------------------------------------------------
    def recommendations(self, job, run, attempt, stop_event=None):
        from .reco_job import run_recommendations
        client = clickhouse.get_client(self.settings)
        try:
            return run_recommendations(self.settings, client, self.clock())
        finally:
            client.close()

    def intraday_alerts(self, job, run, attempt, stop_event=None, provider=None):
        if not self.settings.live.enabled:
            return {"status": "disabled"}
        from .intraday import run_intraday
        client = clickhouse.get_client(self.settings)
        try:
            return run_intraday(self.settings, client, self.clock(), provider=provider)
        finally:
            client.close()

    def intraday_reco(self, job, run, attempt, stop_event=None):
        from .intraday_reco import run_intraday_reco
        client = clickhouse.get_client(self.settings)
        try:
            return run_intraday_reco(self.settings, client, self.clock())
        finally:
            client.close()

    def intraday_backtest(self, job, run, attempt, stop_event=None):
        from .intraday_reco import run_backtest_job
        client = clickhouse.get_client(self.settings)
        try:
            return run_backtest_job(self.settings, client, self.clock())
        finally:
            client.close()

    def backtest(self, job, run, attempt, stop_event=None):
        from .reco_job import run_backtest_job
        client = clickhouse.get_client(self.settings)
        try:
            return run_backtest_job(self.settings, client, self.clock())
        finally:
            client.close()

    def daily_report(self, job, run, attempt, stop_event=None, send=True, user=None):
        """Build each active user's personalised report and queue it to that
        user's own email, once per user per day. With `user`, build only that
        user's report (CLI preview). Returns {email: (subject, text, html)}."""
        from .pgdb import active_users, service_tx
        from .report import build_report
        if send and not self.settings.email.configured:
            raise RuntimeError("MAIL_* email settings missing; cannot send the daily report")
        if user is None:
            with service_tx(self.settings) as conn:
                users = active_users(conn)
        else:
            users = [user]
        day = local_today(self.settings.timezone, self.clock())
        out, failures = {}, []
        client = clickhouse.get_client(self.settings)
        try:
            for u in users:
                try:
                    built = build_report(self.settings, client, self.clock(), u)
                    if built is None:
                        log_event(log, logging.INFO, "daily report skipped by mode", component="report",
                                  user=u["email"])
                        continue
                    subject, body, html = built
                    if send:
                        self.outbox.enqueue_email(f"report:{day}", subject, body, html=html,
                                                  recipients=(u["email"],), user_id=u["id"])
                    out[u["email"]] = built
                except Exception as exc:  # one user's failure must not block the others
                    failures.append(f"{u['email']}: {type(exc).__name__}: {exc}")
                    log.exception("daily report failed for user", extra={"fields": {"component": "report"}})
        finally:
            client.close()
        if failures:
            raise RuntimeError("daily report failed for " + "; ".join(failures))
        return out

    # -- built-in jobs -----------------------------------------------------------
    def mtd_cleanup(self, job, run, attempt, stop_event=None, dry_run=None):
        settings = self.settings
        if not settings.cleanup_enabled:
            log_event(log, logging.INFO, "mtd cleanup disabled (MTD_CLEANUP_ENABLED=false)", component="cleanup")
            return {"status": "disabled"}
        dry = settings.cleanup_dry_run if dry_run is None else dry_run
        today = local_today(settings.timezone, self.clock())
        started = self.clock()
        log_event(log, logging.INFO, "mtd cleanup started", component="cleanup", run_id=getattr(run, "id", None),
                  attempt=attempt, today=str(today), dry_run=dry,
                  targets=[t.table for t in settings.cleanup_targets])
        client = clickhouse.get_client(settings)
        try:
            results = MTDCleaner(settings, client, today, dry_run=dry).run()
        finally:
            client.close()
        sqlite_result = cleanup_appdb(settings, today, dry_run=dry) if settings.cleanup_sqlite else None
        pruned = 0 if dry else self.store.prune(self.clock() - timedelta(days=settings.state_retention_days))
        audit_pruned = 0 if dry else self.outbox.prune(
            self.clock() - timedelta(days=settings.report.audit_retention_days))
        errors = [r for r in results if r.status == "ERROR"]
        summary = {
            "run_id": getattr(run, "id", None), "started_at": to_iso(started), "finished_at": to_iso(self.clock()),
            "tables": [r.__dict__ for r in results], "app_db": sqlite_result, "scheduler_runs_pruned": pruned, "notification_audit_pruned": audit_pruned,
            "verification": "failed" if errors else "passed",
        }
        log_event(log, logging.ERROR if errors else logging.INFO, "mtd cleanup finished", component="cleanup",
                  **summary)
        if not dry:
            (settings.state_dir / "last_cleanup.json").write_text(json.dumps(summary, default=str, indent=2))
        if errors:
            raise RetentionError("; ".join(f"{r.table}: {r.error}" for r in errors))
        return summary

    def daily_summary(self, job, run, attempt, stop_event=None):
        now = self.clock()
        since = now - timedelta(hours=24)
        lines = [f"NSE alert system daily summary ({local_today(self.settings.timezone, now)})", ""]
        per_job = {}
        for job_id, status, count in self.store.status_counts(since):
            per_job.setdefault(job_id, []).append(f"{status}={count}")
        for job_id in sorted(per_job):
            lines.append(f"{job_id}: {', '.join(per_job[job_id])}")
        if not per_job:
            lines.append("No job runs in the last 24h.")
        failed = self.store.runs(statuses=(FAILED,), limit=5)
        if failed:
            lines.append("")
            lines.append("Last FAILED runs (any day): " + ", ".join(f"{r.job_id}#{r.id}" for r in failed))
        counts = self.outbox.counts()
        lines.append("")
        lines.append("WhatsApp outbox: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "empty"))
        text = "\n".join(lines)
        log_event(log, logging.INFO, "daily summary", component="summary", text=text)
        (self.settings.state_dir / "daily_summary.txt").write_text(text + "\n", encoding="utf-8")
        self.notifier.summary(str(local_today(self.settings.timezone, now)), "Daily summary", text)
        return text

    # -- health ------------------------------------------------------------------
    def write_health(self, online):
        data = {"ts": to_iso(self.clock()), "pid": os.getpid(), "boot_id": self.boot_id, "version": __version__,
                "online": online, "current_job": self.scheduler.current, "outbox": self.outbox.counts(),
                "tick_seconds": self.settings.tick_seconds}
        tmp = self.settings.health_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.settings.health_file)

    # -- main loop -------------------------------------------------------------
    def _worker(self):
        while not self.stop_event.is_set():
            try:
                did = self.scheduler.run_next(self.stop_event)
            except Exception:  # state DB error etc.: log, back off, keep going
                log.exception("scheduler worker error", extra={"fields": {"component": "scheduler"}})
                did = False
                self.stop_event.wait(30)
            if not did:
                self.stop_event.wait(2)

    def tick(self):
        self.scheduler.materialise()
        online = self._online()
        if online:
            self.outbox.process_due(should_stop=self.stop_event.is_set)
        self.write_health(online)

    def run_forever(self):
        settings = self.settings
        setup_logging(settings)
        lock = InstanceLock(settings.lock_file).acquire()
        log_event(log, logging.INFO, "service starting", component="service", version=__version__,
                  pid=os.getpid(), boot_id=self.boot_id, jobs=sorted(self.scheduler.jobs),
                  whatsapp_configured=settings.whatsapp.configured, whatsapp_mode=settings.whatsapp.mode,
                  cleanup_dry_run=settings.cleanup_dry_run, timezone=settings.timezone)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop_event.set())
        try:
            self.outbox.recover()
            self.scheduler.startup()
            worker = threading.Thread(target=self._worker, name="job-worker", daemon=True)
            worker.start()
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    log.exception("service tick error", extra={"fields": {"component": "service"}})
                self.stop_event.wait(settings.tick_seconds)
            worker.join(timeout=45)
        finally:
            log_event(log, logging.INFO, "service stopped", component="service", pid=os.getpid())
            lock.release()
