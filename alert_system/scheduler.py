"""Persistent cron scheduler.

Each tick:
  1. materialise(): turn cron fire times between the persisted cursor and now
     into job_runs rows (UNIQUE per occurrence, so never twice). Fire times
     older than the misfire grace (machine off/asleep, service down) are
     "missed" and handled by the job's catch_up policy:
        latest - run only the most recent missed occurrence (if within
                 max_lateness_minutes), record the others as SKIPPED;
        all    - run every missed occurrence within max_lateness_minutes;
        skip   - record all missed occurrences as SKIPPED.
     A new run supersedes older waiting runs of the same job (supersede=true)
     because each job processes "everything outstanding" (idempotent), so
     retrying a stale occurrence alongside a fresh one would be duplicate work.
  2. run_next(): execute one due run (PENDING / RETRY_SCHEDULED whose
     next_attempt_at <= now). Prerequisites (internet, ClickHouse) are checked
     first; if missing the run is deferred without consuming an attempt.
     On failure: RETRY_SCHEDULED with exponential backoff while
     attempts < max_attempts (default 10), then FAILED. A failure never
     propagates out of run_next, so other jobs keep running.
"""
import logging
from datetime import timedelta

from .jobs import JobFailed, JobInterrupted
from .logging_setup import log_event, redact
from .state import FAILED, PENDING, RETRY_SCHEDULED, SKIPPED, SUCCESS
from .timeutil import from_iso, local_today, to_iso, utcnow

log = logging.getLogger("alert_system.scheduler")
CURSOR_KEY = "cursor"


def retry_delay(job, attempt):
    return min(job.retry_backoff_max_seconds, job.retry_backoff_seconds * (2 ** max(0, attempt - 1)))


class Scheduler:
    def __init__(self, settings, store, runners, online=lambda: True, clickhouse_ok=lambda: True,
                 clock=utcnow, on_failure=None, boot_id="boot"):
        self.settings = settings
        self.store = store
        self.runners = runners
        self.online = online
        self.clickhouse_ok = clickhouse_ok
        self.clock = clock
        self.on_failure = on_failure
        self.boot_id = boot_id
        self.jobs = {j.id: j for j in settings.jobs if j.enabled}
        self.current = None

    # -- recovery & startup ------------------------------------------------
    def startup(self):
        now = self.clock()
        for run in self.store.recover_interrupted(now):
            log_event(log, logging.WARNING, "interrupted run recovered", component="scheduler",
                      job_id=run.job_id, run_id=run.id, attempt=run.attempts, max_attempts=run.max_attempts,
                      status=FAILED if run.attempts >= run.max_attempts else RETRY_SCHEDULED)
        key = (f"startup:{local_today(self.settings.timezone, now)}"
               if self.settings.startup_run_key == "day" else f"startup:{self.boot_id}")
        for job in self.jobs.values():
            if job.run_on_startup:
                run_id = self.store.create_run(job.id, key, "startup", now, job.max_attempts, now)
                if run_id:
                    log_event(log, logging.INFO, "startup run scheduled", component="scheduler",
                              job_id=job.id, run_id=run_id, status=PENDING)
                    self._supersede(job, run_id, now)

    # -- materialisation -----------------------------------------------------
    def materialise(self):
        now = self.clock()
        raw = self.store.get_meta(CURSOR_KEY)
        if raw is None:
            # First ever start: nothing is "missed"; startup runs cover catch-up.
            self.store.set_meta(CURSOR_KEY, to_iso(now))
            log_event(log, logging.INFO, "scheduler cursor initialised", component="scheduler", cursor=to_iso(now))
            return []
        cursor = from_iso(raw)
        if cursor >= now:
            return []
        floor = now - timedelta(days=self.settings.max_catchup_days)
        window_start = max(cursor, floor)
        created = []
        for job in self.jobs.values():
            occurrences = sorted({t for c in job.crons for t in
                                  c.occurrences(window_start, now, self.settings.timezone)})
            created += self._materialise_job(job, occurrences, now)
        self.store.set_meta(CURSOR_KEY, to_iso(now))
        return created

    def _materialise_job(self, job, occurrences, now):
        grace = timedelta(seconds=self.settings.misfire_grace_seconds)
        max_late = timedelta(minutes=job.max_lateness_minutes)
        on_time = [t for t in occurrences if now - t <= grace]
        missed = [t for t in occurrences if now - t > grace]
        to_run, to_skip = list(on_time), []
        eligible = [t for t in missed if now - t <= max_late]
        if job.catch_up == "all":
            to_run = eligible + to_run
            to_skip = [t for t in missed if t not in eligible]
        elif job.catch_up == "latest" and eligible and not on_time:
            to_run = [eligible[-1]]
            to_skip = [t for t in missed if t != eligible[-1]]
        else:
            to_skip = missed
        created = []
        for t in to_skip:
            run_id = self.store.create_run(job.id, f"cron:{to_iso(t)}", "schedule", t, job.max_attempts, now,
                                           status=SKIPPED, note=f"missed while offline; catch_up={job.catch_up}")
            if run_id:
                log_event(log, logging.WARNING, "missed schedule skipped", component="scheduler",
                          job_id=job.id, run_id=run_id, scheduled_for=to_iso(t), status=SKIPPED,
                          catch_up=job.catch_up)
        for t in sorted(to_run):
            trigger = "schedule" if t in on_time else "catch_up"
            run_id = self.store.create_run(job.id, f"cron:{to_iso(t)}", trigger, t, job.max_attempts, now)
            if run_id:
                created.append(run_id)
                log_event(log, logging.INFO, "run scheduled", component="scheduler", job_id=job.id,
                          run_id=run_id, scheduled_for=to_iso(t), trigger=trigger, status=PENDING)
                self._supersede(job, run_id, now)
        return created

    def _supersede(self, job, run_id, now):
        if job.supersede:
            n = self.store.skip_waiting(job.id, run_id, now, f"superseded by run {run_id}")
            if n:
                log_event(log, logging.INFO, "older waiting runs superseded", component="scheduler",
                          job_id=job.id, run_id=run_id, superseded=n, status=SKIPPED)

    def trigger_manual(self, job_id):
        job = self.jobs[job_id]
        now = self.clock()
        run_id = self.store.create_run(job.id, f"manual:{to_iso(now)}", "manual", now, job.max_attempts, now)
        if run_id:
            self._supersede(job, run_id, now)
        return run_id

    # -- execution -------------------------------------------------------------
    def run_next(self, stop_event):
        """Execute at most one due run. Returns True if something ran/deferred."""
        now = self.clock()
        run = self.store.next_due(now)
        if run is None:
            return False
        job = self.jobs.get(run.job_id)
        if job is None:
            self.store.finish(run.id, run.attempts, SKIPPED, now, note="job no longer configured")
            return True
        waiting_for = None
        if job.needs_internet and not self.online():
            waiting_for = "internet"
        elif job.needs_clickhouse and not self.clickhouse_ok():
            waiting_for = "clickhouse"
        if waiting_for:
            next_at = now + timedelta(seconds=self.settings.connectivity_retry_seconds)
            self.store.defer(run.id, next_at, now, f"waiting for {waiting_for}")
            log_event(log, logging.WARNING, "run deferred", component="scheduler", job_id=job.id, run_id=run.id,
                      waiting_for=waiting_for, retry_count=run.attempts, next_attempt_at=to_iso(next_at),
                      status=RETRY_SCHEDULED)
            return True
        attempt = self.store.claim(run.id, now)
        if attempt is None:
            return True
        self.current = (job.id, run.id)
        log_event(log, logging.INFO, "job started", component="scheduler", job_id=job.id, run_id=run.id,
                  scheduled_for=run.scheduled_for, attempt=attempt, retry_count=attempt - 1,
                  max_attempts=run.max_attempts, status="RUNNING")
        started = self.clock()
        try:
            self.runners[job.id](job, run, attempt, stop_event)
        except JobInterrupted as exc:
            self._finish_failure(job, run, attempt, exc, interrupted=True)
        except JobFailed as exc:
            self._finish_failure(job, run, attempt, exc)
        except Exception as exc:  # job bug: record it, keep the scheduler alive
            log.exception("job raised unexpected exception",
                          extra={"fields": {"component": "scheduler", "job_id": job.id, "run_id": run.id}})
            self._finish_failure(job, run, attempt, exc)
        else:
            end = self.clock()
            self.store.finish(run.id, attempt, SUCCESS, end, exit_code=0)
            log_event(log, logging.INFO, "job succeeded", component="scheduler", job_id=job.id, run_id=run.id,
                      attempt=attempt, retry_count=attempt - 1, status=SUCCESS,
                      duration_seconds=round((end - started).total_seconds(), 1))
        finally:
            self.current = None
        return True

    def _finish_failure(self, job, run, attempt, exc, interrupted=False):
        now = self.clock()
        error = redact(f"{type(exc).__name__}: {exc}")[:2000]
        exit_code = getattr(exc, "exit_code", None)
        if interrupted:
            next_at = now
            status = RETRY_SCHEDULED
        elif attempt < run.max_attempts:
            next_at = now + timedelta(seconds=retry_delay(job, attempt))
            status = RETRY_SCHEDULED
        else:
            next_at, status = None, FAILED
        self.store.finish(run.id, attempt, status, now, exit_code=exit_code, error=error, next_at=next_at)
        log_event(log, logging.WARNING if interrupted else logging.ERROR, "job failed", component="scheduler",
                  job_id=job.id, run_id=run.id, attempt=attempt, retry_count=attempt,
                  max_attempts=run.max_attempts, status=status,
                  next_attempt_at=to_iso(next_at) if next_at else None, error=error[:800])
        if self.on_failure and not interrupted:
            self.on_failure(job, run, attempt, status, error)
