import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alert_system.config import load_settings  # noqa: E402


class Clock:
    """Controllable UTC clock."""

    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        from datetime import timedelta
        self.now = self.now + timedelta(**kw)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def ist(*args):
    from zoneinfo import ZoneInfo
    return datetime(*args, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc)


def make_job(job_id="job", schedules=("0 10 * * 1-5",), **kw):
    job = {"id": job_id, "schedules": list(schedules), "builtin": "noop",
           "needs_internet": False, "needs_clickhouse": False}
    job.update(kw)
    if "steps" in kw:
        job.pop("builtin")
    return job


def make_settings(tmp_path, jobs=None, targets=None, allowed=("nse_alerts",), **env_overrides):
    cfg = {"jobs": jobs or [make_job()],
           "mtd_cleanup": {"allowed_databases": list(allowed), "targets": targets or [],
                           "sqlite_alert_history": True}}
    cfg_path = tmp_path / "alert_system.json"
    cfg_path.write_text(json.dumps(cfg))
    env = {
        "ALERT_BASE_DIR": str(tmp_path),
        "ALERT_STATE_DIR": str(tmp_path / "state"),
        "ALERT_LOG_DIR": str(tmp_path / "logs"),
        "ALERT_LOCK_FILE": str(tmp_path / "state" / "test.lock"),
        "WHATSAPP_ACCESS_TOKEN": "EAAtesttokentesttokentesttoken123",
        "WHATSAPP_PHONE_NUMBER_ID": "1234567890",
        "WHATSAPP_RECIPIENT": "+919999900001,+919999900002",
        "OPS_ALERT_CHANNELS": "log",
    }
    env.update({k: str(v) for k, v in env_overrides.items()})
    settings = load_settings(env=env, config_path=cfg_path)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    return settings


PG_TABLES = ("user_sessions", "delivery_markers", "notification_outbox", "alert_history", "watchlist", "user_alerts", "holdings", "users")


def pg_env():
    """PostgreSQL role passwords from the project .env, pointed at the
    throw-away database nse_app_test (never the real nse_app)."""
    from dotenv import dotenv_values
    vals = dotenv_values(ROOT / ".env")
    keys = ("PG_HOST", "PG_PORT", "PG_APP_PASSWORD", "PG_SERVICE_PASSWORD", "PG_OWNER_PASSWORD")
    env = {k: vals[k] for k in keys if vals.get(k)}
    env["PG_DATABASE"] = "nse_app_test"
    return env


@pytest.fixture
def pg_settings(settings_factory):
    """Settings wired to nse_app_test with all user tables emptied."""
    env = pg_env()
    if "PG_OWNER_PASSWORD" not in env:
        pytest.skip("PostgreSQL not configured (run deploy/postgres_setup.sh)")
    def make(**extra):
        s = settings_factory(**{**env, **extra})
        from alert_system.pgdb import owner_connect
        try:
            with owner_connect(s) as conn:
                conn.execute(f"TRUNCATE {', '.join(PG_TABLES)} RESTART IDENTITY CASCADE")
        except Exception as exc:  # server down / not set up
            pytest.skip(f"PostgreSQL test database unavailable: {exc}")
        return s
    return make


@pytest.fixture
def settings_factory(tmp_path):
    def factory(*a, **kw):
        return make_settings(tmp_path, *a, **kw)
    return factory
