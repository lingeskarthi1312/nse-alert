"""Persistent job state (SQLite). This — not the in-memory scheduler — is the
record of what ran, so restarts and reboots neither lose nor repeat work.

job_runs has one row per (job_id, occurrence_key). The UNIQUE constraint is
the duplicate-execution guard: a scheduled occurrence can only be
materialised once no matter how many times the scheduler restarts.
"""
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from .timeutil import to_iso

PENDING, RUNNING, SUCCESS, FAILED, SKIPPED, RETRY_SCHEDULED = (
    "PENDING", "RUNNING", "SUCCESS", "FAILED", "SKIPPED", "RETRY_SCHEDULED")
STATUSES = (PENDING, RUNNING, SUCCESS, FAILED, SKIPPED, RETRY_SCHEDULED)
WAITING = (PENDING, RETRY_SCHEDULED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    occurrence_key TEXT NOT NULL,
    trigger TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('PENDING','RUNNING','SUCCESS','FAILED','SKIPPED','RETRY_SCHEDULED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    next_attempt_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    last_exit_code INTEGER,
    last_error TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, occurrence_key)
);
CREATE INDEX IF NOT EXISTS idx_job_runs_due ON job_runs (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs (job_id, scheduled_for);
CREATE TABLE IF NOT EXISTS job_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES job_runs(id),
    attempt INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    exit_code INTEGER,
    error TEXT,
    UNIQUE (run_id, attempt)
);
CREATE TABLE IF NOT EXISTS scheduler_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class JobRun:
    id: int
    job_id: str
    occurrence_key: str
    trigger: str
    scheduled_for: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: str
    last_error: str
    note: str


_RUN_COLS = ("id, job_id, occurrence_key, trigger, scheduled_for, status, attempts, "
             "max_attempts, next_attempt_at, last_error, note")


class JobStore:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=30)
        try:
            db.executescript(SCHEMA)  # executescript manages its own transaction
        finally:
            db.close()

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        finally:
            db.close()

    # -- meta -------------------------------------------------------------
    def get_meta(self, key):
        with self.tx() as db:
            row = db.execute("SELECT value FROM scheduler_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        with self.tx() as db:
            db.execute("INSERT INTO scheduler_meta(key, value) VALUES (?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # -- runs -------------------------------------------------------------
    def create_run(self, job_id, occurrence_key, trigger, scheduled_for, max_attempts,
                   now, status=PENDING, note=None):
        """Insert a run; returns its id, or None if this occurrence already exists."""
        ts = to_iso(now)
        with self.tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO job_runs (job_id, occurrence_key, trigger, scheduled_for, "
                "status, max_attempts, next_attempt_at, note, created_at, updated_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, occurrence_key, trigger, to_iso(scheduled_for), status, max_attempts,
                 ts if status == PENDING else None, note, ts, ts, ts if status == SKIPPED else None))
            return cur.lastrowid if cur.rowcount else None

    def get(self, run_id):
        with self.tx() as db:
            row = db.execute(f"SELECT {_RUN_COLS} FROM job_runs WHERE id=?", (run_id,)).fetchone()
        return JobRun(*row) if row else None

    def runs(self, job_id=None, statuses=None, limit=50):
        sql = f"SELECT {_RUN_COLS} FROM job_runs WHERE 1=1"
        args = []
        if job_id:
            sql += " AND job_id=?"
            args.append(job_id)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args.extend(statuses)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.tx() as db:
            return [JobRun(*r) for r in db.execute(sql, args).fetchall()]

    def next_due(self, now, exclude_jobs=()):
        sql = (f"SELECT {_RUN_COLS} FROM job_runs WHERE status IN ('PENDING','RETRY_SCHEDULED') "
               "AND next_attempt_at <= ?")
        args = [to_iso(now)]
        if exclude_jobs:
            sql += f" AND job_id NOT IN ({','.join('?' * len(exclude_jobs))})"
            args.extend(exclude_jobs)
        sql += " ORDER BY next_attempt_at, id LIMIT 1"
        with self.tx() as db:
            row = db.execute(sql, args).fetchone()
        return JobRun(*row) if row else None

    def claim(self, run_id, now):
        """PENDING/RETRY_SCHEDULED -> RUNNING, counting one attempt. Returns
        the attempt number, or None if someone else changed the run."""
        ts = to_iso(now)
        with self.tx() as db:
            cur = db.execute(
                "UPDATE job_runs SET status='RUNNING', attempts=attempts+1, started_at=?, "
                "updated_at=?, next_attempt_at=NULL WHERE id=? AND status IN ('PENDING','RETRY_SCHEDULED')",
                (ts, ts, run_id))
            if not cur.rowcount:
                return None
            attempt = db.execute("SELECT attempts FROM job_runs WHERE id=?", (run_id,)).fetchone()[0]
            db.execute("INSERT OR REPLACE INTO job_attempts (run_id, attempt, started_at, status) "
                       "VALUES (?, ?, ?, 'RUNNING')", (run_id, attempt, ts))
            return attempt

    def defer(self, run_id, next_at, now, note):
        """Push a waiting run later without consuming an attempt (e.g. offline)."""
        with self.tx() as db:
            db.execute("UPDATE job_runs SET status='RETRY_SCHEDULED', next_attempt_at=?, note=?, "
                       "updated_at=? WHERE id=? AND status IN ('PENDING','RETRY_SCHEDULED')",
                       (to_iso(next_at), note, to_iso(now), run_id))

    def finish(self, run_id, attempt, status, now, exit_code=None, error=None, next_at=None, note=None):
        ts = to_iso(now)
        terminal = status in (SUCCESS, FAILED, SKIPPED)
        with self.tx() as db:
            db.execute(
                "UPDATE job_runs SET status=?, last_exit_code=?, last_error=?, next_attempt_at=?, "
                "finished_at=?, updated_at=?, note=COALESCE(?, note) WHERE id=?",
                (status, exit_code, error, to_iso(next_at) if next_at else None,
                 ts if terminal else None, ts, note, run_id))
            db.execute("UPDATE job_attempts SET finished_at=?, status=?, exit_code=?, error=? "
                       "WHERE run_id=? AND attempt=?",
                       (ts, SUCCESS if status == SUCCESS else FAILED, exit_code, error, run_id, attempt))

    def skip_waiting(self, job_id, keep_run_id, now, note):
        """Mark older waiting runs of a job SKIPPED (superseded by keep_run_id)."""
        ts = to_iso(now)
        with self.tx() as db:
            cur = db.execute(
                "UPDATE job_runs SET status='SKIPPED', note=?, finished_at=?, updated_at=?, "
                "next_attempt_at=NULL WHERE job_id=? AND id<>? AND id<? "
                "AND status IN ('PENDING','RETRY_SCHEDULED')",
                (note, ts, ts, job_id, keep_run_id, keep_run_id))
            return cur.rowcount

    def recover_interrupted(self, now):
        """Runs left RUNNING by a crash/power loss: their attempt counts as
        used; requeue immediately if attempts remain, else FAILED."""
        ts = to_iso(now)
        with self.tx() as db:
            rows = db.execute(f"SELECT {_RUN_COLS} FROM job_runs WHERE status='RUNNING'").fetchall()
            for r in rows:
                run = JobRun(*r)
                err = "interrupted (service stopped, crashed or machine powered off)"
                if run.attempts >= run.max_attempts:
                    db.execute("UPDATE job_runs SET status='FAILED', last_error=?, finished_at=?, "
                               "updated_at=? WHERE id=?", (err, ts, ts, run.id))
                else:
                    db.execute("UPDATE job_runs SET status='RETRY_SCHEDULED', last_error=?, "
                               "next_attempt_at=?, updated_at=? WHERE id=?", (err, ts, ts, run.id))
                db.execute("UPDATE job_attempts SET status='FAILED', finished_at=?, error=? "
                           "WHERE run_id=? AND attempt=? AND status='RUNNING'",
                           (ts, err, run.id, run.attempts))
        return [JobRun(*r) for r in rows]

    def status_counts(self, since):
        with self.tx() as db:
            return db.execute(
                "SELECT job_id, status, COUNT(*) FROM job_runs WHERE updated_at >= ? "
                "GROUP BY job_id, status ORDER BY job_id, status", (to_iso(since),)).fetchall()

    def prune(self, older_than):
        cutoff = to_iso(older_than)
        with self.tx() as db:
            db.execute("DELETE FROM job_attempts WHERE run_id IN (SELECT id FROM job_runs WHERE "
                       "status IN ('SUCCESS','FAILED','SKIPPED') AND updated_at < ?)", (cutoff,))
            cur = db.execute("DELETE FROM job_runs WHERE status IN ('SUCCESS','FAILED','SKIPPED') "
                             "AND updated_at < ?", (cutoff,))
            return cur.rowcount
