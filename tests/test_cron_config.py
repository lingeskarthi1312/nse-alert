import pytest

from alert_system.config import ConfigError
from alert_system.cron import Cron, CronError
from conftest import ist, make_job, utc


def test_weekday_cron_in_ist():
    c = Cron("45 9 * * 1-5")
    # Fri 2026-10-16 .. Mon 2026-10-19 (IST): fires Fri 09:45 and Mon 09:45 only
    occ = c.occurrences(ist(2026, 10, 16, 0, 0), ist(2026, 10, 19, 23, 0), "Asia/Kolkata")
    assert [o.strftime("%a %H:%M") for o in occ] == ["Fri 09:45", "Mon 09:45"]
    assert occ[0].utcoffset().total_seconds() == 5.5 * 3600


def test_cron_window_is_exclusive_start_inclusive_end():
    c = Cron("0 10 * * *")
    t = ist(2026, 10, 19, 10, 0)
    assert c.occurrences(t, t, "Asia/Kolkata") == []
    from datetime import timedelta
    assert len(c.occurrences(t - timedelta(seconds=1), t, "Asia/Kolkata")) == 1


@pytest.mark.parametrize("expr", ["* * *", "61 * * * *", "*/0 * * * *", "a * * * *", "5-1 * * * *"])
def test_bad_cron_rejected(expr):
    with pytest.raises(CronError):
        Cron(expr)


def test_steps_lists_and_sunday_7():
    c = Cron("*/30 9-10 * * 0,7")
    occ = c.occurrences(utc(2026, 10, 17, 0, 0), utc(2026, 10, 19, 0, 0), "UTC")
    assert len(occ) == 4 and all(o.weekday() == 6 for o in occ)


def test_config_defaults_and_validation(settings_factory):
    s = settings_factory()
    assert s.timezone == "Asia/Kolkata"
    assert s.jobs[0].max_attempts == 10
    assert s.whatsapp.recipients == ("919999900001", "919999900002")
    assert "EAAtesttokentesttokentesttoken123" in s.secrets


@pytest.mark.parametrize("job_kw, message", [
    ({"catch_up": "sometimes"}, "catch_up"),
    ({"max_attempts": 0}, "max_attempts"),
    ({"retry_backoff_seconds": 1}, "tight retry"),
    ({"schedules": ["bad"]}, "cron"),
    ({"surprise": 1}, "unknown keys"),
])
def test_invalid_job_config(settings_factory, job_kw, message):
    with pytest.raises(ConfigError, match=message):
        settings_factory(jobs=[make_job(**job_kw)])


def test_cleanup_target_must_be_in_allowed_database(settings_factory):
    with pytest.raises(ConfigError, match="allowed_databases"):
        settings_factory(targets=[{"table": "other_app.events", "date_column": "d"}])


def test_bad_env_values(settings_factory):
    with pytest.raises(ConfigError, match="ALERT_TIMEZONE"):
        settings_factory(ALERT_TIMEZONE="Mars/Base")
    with pytest.raises(ConfigError, match="WHATSAPP_SEND_MODE=template"):
        settings_factory(WHATSAPP_SEND_MODE="template")
    with pytest.raises(ConfigError, match="E.164"):
        settings_factory(WHATSAPP_RECIPIENT="not-a-number")


def test_real_project_config_is_valid():
    from pathlib import Path
    from alert_system.config import load_settings
    root = Path(__file__).resolve().parent.parent
    s = load_settings(env={"ALERT_BASE_DIR": str(root)})
    assert {j.id for j in s.jobs} == {"price_import", "daily_alerts", "mtd_rankings_whatsapp",
                                       "mtd_cleanup", "daily_summary", "recommendations",
                                       "daily_report", "backtest", "intraday_alerts", "intraday_reco",
                                       "intraday_backtest"}
    assert s.recommendations["top_n"] == 5
    assert "nse_alerts.recommendations" in [t.table for t in s.cleanup_targets]
    assert s.job("daily_alerts").schedules == ("0 10 * * 1-5", "30 13 * * 1-5", "30 15 * * 1-5")
    assert s.price_lookback_days == 10


def test_whatsapp_can_be_disabled(settings_factory):
    s = settings_factory(WHATSAPP_ENABLED="false")
    assert s.whatsapp.enabled is False and s.whatsapp.configured is False
