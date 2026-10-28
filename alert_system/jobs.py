"""Job runners. Existing scripts run as subprocesses (exactly as cron ran
them) so their alert logic is untouched, a crash in one cannot take down the
scheduler, and a hung job can be killed at its timeout."""
import os
import signal
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .logging_setup import redact
from .singleton import AlreadyRunning, InstanceLock


class JobFailed(Exception):
    def __init__(self, message, exit_code=None):
        super().__init__(message)
        self.exit_code = exit_code


class JobInterrupted(JobFailed):
    pass


def _tail(path, offset, lines=15):
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()[-8000:]
    except OSError:
        return ""
    return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])


def _stop_process(proc):
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=20)
    except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()


def _stamp(tz):
    return datetime.now(tz).isoformat(timespec="seconds")


def run_command_job(settings, job, run, attempt, stop_event):
    base = settings.base_dir
    log_path = base / (job.log_file or f"logs/{job.id}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lock = None
    if job.lock_file:
        try:
            lock = InstanceLock(base / job.lock_file).acquire(blocking=False)
        except AlreadyRunning as exc:
            raise JobFailed(f"{job.lock_file} is held by another process (manual run?)") from exc
    env = dict(os.environ, TZ=settings.timezone, PYTHONUNBUFFERED="1", ALERT_JOB_ID=job.id,
               ALERT_RUN_ID=str(run.id), ALERT_ATTEMPT=str(attempt))
    tz = ZoneInfo(settings.timezone)
    deadline = time.monotonic() + job.timeout_seconds
    try:
        for index, step in enumerate(job.steps, 1):
            cmd = [a.replace("{python}", settings.python) for a in step]
            label = " ".join(step[1:])
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"[{_stamp(tz)}] [alert_system] job={job.id} run={run.id} attempt={attempt} "
                         f"step={index}/{len(job.steps)} start: {label}\n")
                fh.flush()
                offset = fh.tell()
                proc = subprocess.Popen(cmd, cwd=base, stdout=fh, stderr=subprocess.STDOUT, env=env,
                                        start_new_session=(os.name != "nt"))
                while True:
                    try:
                        rc = proc.wait(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if stop_event.is_set():
                        _stop_process(proc)
                        raise JobInterrupted("service shutting down; step terminated")
                    if time.monotonic() > deadline:
                        _stop_process(proc)
                        raise JobFailed(f"timed out after {job.timeout_seconds}s in step {index}", exit_code=-1)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"[{_stamp(tz)}] [alert_system] job={job.id} run={run.id} step={index} exit={rc}\n")
            if rc != 0:
                tail = redact(_tail(log_path, offset))
                raise JobFailed(f"step {index} ({label}) exited {rc}; output tail:\n{tail}"[-1500:], exit_code=rc)
    finally:
        if lock:
            lock.release()
    return 0
