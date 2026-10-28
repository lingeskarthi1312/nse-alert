"""Configuration: secrets and deployment settings from environment / .env,
schedules and retention targets from config/alert_system.json.

Everything is validated at startup so mistakes fail loudly with a clear
message instead of surfacing at 15:30 on a trading day.
"""
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cron import Cron, CronError

BASE_DIR = Path(__file__).resolve().parent.parent
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CATCH_UP_POLICIES = ("latest", "all", "skip")
WHATSAPP_MODES = ("text", "template", "auto")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class JobSpec:
    id: str
    schedules: tuple
    steps: tuple = ()
    builtin: str = None
    description: str = ""
    log_file: str = None
    lock_file: str = None
    timeout_seconds: int = 1800
    max_attempts: int = 10
    retry_backoff_seconds: int = 120
    retry_backoff_max_seconds: int = 1800
    catch_up: str = "latest"
    max_lateness_minutes: int = 360
    run_on_startup: bool = False
    needs_internet: bool = True
    needs_clickhouse: bool = True
    supersede: bool = True
    enabled: bool = True
    failure_alert_after: int = 1   # ops alert only after N consecutive failed runs (frequent jobs)

    @property
    def crons(self):
        return [Cron(s) for s in self.schedules]


@dataclass(frozen=True)
class CleanupTarget:
    table: str
    date_column: str
    baseline_lookback_days: int = 0
    baseline_key_column: str = None

    @property
    def database(self):
        return self.table.split(".", 1)[0]

    @property
    def name(self):
        return self.table.split(".", 1)[1]


@dataclass(frozen=True)
class ClickHouseSettings:
    host: str = "127.0.0.1"
    port: int = 8123
    username: str = "default"
    password: str = ""
    secure: bool = False
    connect_timeout: int = 10
    send_receive_timeout: int = 300
    max_retries: int = 5
    retry_delay_seconds: float = 5.0


@dataclass(frozen=True)
class WhatsAppSettings:
    enabled: bool = True
    access_token: str = ""
    phone_number_id: str = ""
    api_version: str = "v25.0"
    recipients: tuple = ()
    mode: str = "text"
    template_name: str = ""
    template_language: str = "en"
    template_param_max_chars: int = 1000
    timeout_seconds: int = 20
    max_attempts: int = 8
    retry_backoff_seconds: int = 60
    retry_backoff_max_seconds: int = 3600
    resend_unconfirmed: bool = False
    base_url: str = "https://graph.facebook.com"

    @property
    def configured(self):
        return bool(self.enabled and self.access_token and self.phone_number_id and self.recipients)


@dataclass(frozen=True)
class EmailSettings:
    sender: str = ""
    password: str = ""
    recipient: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    timeout_seconds: int = 30

    @property
    def configured(self):
        return bool(self.sender and self.password and self.recipient)


@dataclass(frozen=True)
class PgSettings:
    """PostgreSQL application database (multi-user data, row-level security)."""
    host: str = "127.0.0.1"
    port: int = 5432
    dbname: str = "nse_app"
    app_user: str = "nse_app"
    app_password: str = field(default="", repr=False)
    service_user: str = "nse_service"
    service_password: str = field(default="", repr=False)
    owner_user: str = "nse_owner"
    owner_password: str = field(default="", repr=False)

    @property
    def configured(self):
        return bool(self.app_password and self.service_password)


LIVE_PROVIDERS = ("yahoo",)


@dataclass(frozen=True)
class LiveSettings:
    enabled: bool = True
    provider: str = "yahoo"
    max_quote_age_minutes: int = 30


NOTIFICATION_MODES = ("TRIGGERED_ALERTS_ONLY", "WATCHLIST_DAILY_SUMMARY",
                      "RECOMMENDATIONS_ONLY", "COMBINED_DAILY_REPORT")
SEVERITIES = ("WARNING", "CRITICAL")


@dataclass(frozen=True)
class ReportSettings:
    """Personalised email behaviour (section 6 of the requirements)."""
    mode: str = "COMBINED_DAILY_REPORT"
    watchlist_extra: tuple = ()
    include_top5: bool = True
    include_cap_top5: bool = True
    include_cross_asset: bool = True
    include_mtd_rankings_email: bool = True
    min_severity: str = "WARNING"
    dedup_window_hours: int = 24
    email_max_attempts: int = 8
    email_retry_backoff_seconds: int = 60
    email_retry_backoff_max_seconds: int = 3600
    email_resend_unconfirmed: bool = False
    audit_retention_days: int = 90
    portfolio_capital: float = None

    @property
    def alert_emails(self):
        return self.mode != "RECOMMENDATIONS_ONLY"

    @property
    def daily_report(self):
        return self.mode != "TRIGGERED_ALERTS_ONLY"

    @property
    def watchlist_section(self):
        return self.mode in ("WATCHLIST_DAILY_SUMMARY", "COMBINED_DAILY_REPORT")

    @property
    def recommendation_sections(self):
        return self.mode in ("RECOMMENDATIONS_ONLY", "COMBINED_DAILY_REPORT")


@dataclass(frozen=True)
class Settings:
    base_dir: Path
    state_dir: Path
    log_dir: Path
    holdings_db: Path
    scheduler_db: Path
    lock_file: Path
    health_file: Path
    timezone: str
    python: str
    tick_seconds: int
    misfire_grace_seconds: int
    max_catchup_days: int
    startup_run_key: str
    connectivity_hosts: tuple
    connectivity_timeout: float
    connectivity_retry_seconds: int
    clickhouse: ClickHouseSettings
    whatsapp: WhatsAppSettings
    email: EmailSettings
    jobs: tuple
    cleanup_targets: tuple
    cleanup_allowed_databases: tuple
    cleanup_sqlite: bool
    cleanup_enabled: bool
    cleanup_dry_run: bool
    state_retention_days: int
    log_level: str
    log_max_bytes: int
    log_backup_count: int
    ops_alert_channels: tuple
    daily_summary_channels: tuple
    report: ReportSettings = ReportSettings()
    live: LiveSettings = LiveSettings()
    pg: PgSettings = PgSettings()
    intraday_reco: dict = field(default_factory=dict)
    signup_mode: str = "approval"   # approval | open | closed
    public_url: str = ""            # dashboard address mentioned in emails
    recommendations: dict = field(default_factory=dict)
    secrets: tuple = field(default=(), repr=False)

    def job(self, job_id):
        for j in self.jobs:
            if j.id == job_id:
                return j
        raise KeyError(job_id)

    @property
    def price_lookback_days(self):
        for t in self.cleanup_targets:
            if t.table.endswith(".daily_stock_prices"):
                return t.baseline_lookback_days
        return 10


def _env(name, default=None, env=None):
    value = (env or os.environ).get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _int(name, default, lo, hi, env=None):
    raw = _env(name, None, env)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{name}={value} outside allowed range {lo}..{hi}")
    return value


def _bool(name, default, env=None):
    raw = _env(name, None, env)
    if raw is None:
        return default
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true/false, got {raw!r}")


def _list(name, default, env=None):
    raw = _env(name, None, env)
    if raw is None:
        return tuple(default)
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _normalise_recipient(value):
    digits = value.replace("+", "").replace(" ", "").replace("-", "")
    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        # Show only the tail: recipients are personal phone numbers.
        raise ConfigError(f"WHATSAPP_RECIPIENT entry ending {digits[-4:]!r} is not an E.164 number")
    return digits


def _job_from_dict(raw, defaults):
    if not isinstance(raw, dict) or "id" not in raw:
        raise ConfigError(f"job entry needs an 'id': {raw!r}")
    known = set(JobSpec.__dataclass_fields__)
    data = {k: v for k, v in raw.items() if not k.startswith("_")}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"job {raw['id']}: unknown keys {sorted(unknown)}")
    merged = {**defaults, **data}
    merged["schedules"] = tuple(merged.get("schedules") or ())
    merged["steps"] = tuple(tuple(s) for s in merged.get("steps") or ())
    job = JobSpec(**merged)
    if not _IDENT.match(job.id):
        raise ConfigError(f"job id {job.id!r} must be an identifier")
    if bool(job.steps) == bool(job.builtin):
        raise ConfigError(f"job {job.id}: set exactly one of 'steps' or 'builtin'")
    try:
        job.crons
    except CronError as exc:
        raise ConfigError(f"job {job.id}: {exc}") from exc
    if job.catch_up not in CATCH_UP_POLICIES:
        raise ConfigError(f"job {job.id}: catch_up must be one of {CATCH_UP_POLICIES}")
    if not 1 <= job.max_attempts <= 50:
        raise ConfigError(f"job {job.id}: max_attempts must be 1..50")
    if job.retry_backoff_seconds < 30:
        raise ConfigError(f"job {job.id}: retry_backoff_seconds must be >= 30 (no tight retry loops)")
    if job.retry_backoff_max_seconds < job.retry_backoff_seconds:
        raise ConfigError(f"job {job.id}: retry_backoff_max_seconds < retry_backoff_seconds")
    if not 1 <= job.failure_alert_after <= 100:
        raise ConfigError(f"job {job.id}: failure_alert_after must be 1..100")
    if job.timeout_seconds < 10:
        raise ConfigError(f"job {job.id}: timeout_seconds must be >= 10")
    return job


def _target_from_dict(raw, allowed):
    data = {k: v for k, v in raw.items() if not k.startswith("_")}
    target = CleanupTarget(**data)
    parts = target.table.split(".")
    if len(parts) != 2 or not all(_IDENT.match(p) for p in parts):
        raise ConfigError(f"cleanup target {target.table!r} must be database.table")
    if target.database not in allowed:
        raise ConfigError(f"cleanup target {target.table} is outside allowed_databases {allowed}")
    for col in (target.date_column, target.baseline_key_column):
        if col is not None and not _IDENT.match(col):
            raise ConfigError(f"cleanup target {target.table}: bad column {col!r}")
    if not 0 <= target.baseline_lookback_days <= 31:
        raise ConfigError(f"cleanup target {target.table}: baseline_lookback_days must be 0..31")
    return target


_REC_REQUIRED = {
    "history_days": int, "max_data_age_days": int, "benchmark": str, "top_n": int, "commodities": list,
    "weights": dict, "filters": dict, "signal": dict, "levels": dict, "position_sizing": dict,
    "costs": dict, "backtest": dict,
}


def _validate_recommendations(rec):
    if not rec:
        return {}
    for key, typ in _REC_REQUIRED.items():
        if not isinstance(rec.get(key), typ):
            raise ConfigError(f"recommendations.{key} missing or not a {typ.__name__}")
    from .recommend import COMPONENTS
    missing = [k for k in COMPONENTS if not isinstance(rec["weights"].get(k), (int, float))]
    if missing or sum(rec["weights"][k] for k in COMPONENTS) <= 0:
        raise ConfigError(f"recommendations.weights needs positive numbers for {COMPONENTS}")
    if not 1 <= rec["top_n"] <= 20:
        raise ConfigError("recommendations.top_n must be 1..20")
    if not 1 <= rec["signal"].get("min_confirmations", 0) <= 5:
        raise ConfigError("recommendations.signal.min_confirmations must be 1..5")
    if rec["levels"].get("stop_atr_mult", 0) <= 0 or rec["levels"].get("target_r_multiple", 0) <= 0:
        raise ConfigError("recommendations.levels multipliers must be > 0")
    if not 0 < rec["position_sizing"].get("risk_per_trade_pct", 0) <= 5:
        raise ConfigError("recommendations.position_sizing.risk_per_trade_pct must be in (0, 5]")
    if rec["costs"].get("round_trip_cost_pct", -1) < 0:
        raise ConfigError("recommendations.costs.round_trip_cost_pct must be >= 0")
    for c in rec["commodities"]:
        if not {"symbol", "name", "asset_class"} <= set(c):
            raise ConfigError(f"recommendations.commodities entry needs symbol/name/asset_class: {c}")
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def _report_settings(env):
    mode = _env("EMAIL_NOTIFICATION_MODE", "COMBINED_DAILY_REPORT", env).upper()
    if mode not in NOTIFICATION_MODES:
        raise ConfigError(f"EMAIL_NOTIFICATION_MODE must be one of {NOTIFICATION_MODES}")
    severity = _env("ALERT_MIN_SEVERITY", "WARNING", env).upper()
    if severity not in SEVERITIES:
        raise ConfigError(f"ALERT_MIN_SEVERITY must be one of {SEVERITIES}")
    capital = _env("PORTFOLIO_CAPITAL", None, env)
    try:
        capital = float(capital) if capital else None
    except ValueError as exc:
        raise ConfigError("PORTFOLIO_CAPITAL must be a number (rupees)") from exc
    watch = tuple(s.upper() if "." in s else f"{s.upper()}.NS" for s in _list("WATCHLIST_SYMBOLS", (), env))
    return ReportSettings(
        mode=mode,
        watchlist_extra=watch,
        include_top5=_bool("REPORT_INCLUDE_TOP5", True, env),
        include_cap_top5=_bool("REPORT_INCLUDE_CAP_TOP5", True, env),
        include_cross_asset=_bool("REPORT_INCLUDE_CROSS_ASSET", True, env),
        include_mtd_rankings_email=_bool("REPORT_MTD_RANKINGS_EMAIL", True, env),
        min_severity=severity,
        dedup_window_hours=_int("EMAIL_DEDUP_WINDOW_HOURS", 24, 0, 24 * 31, env),
        email_max_attempts=_int("EMAIL_MAX_ATTEMPTS", 8, 1, 50, env),
        email_retry_backoff_seconds=_int("EMAIL_RETRY_BACKOFF_SECONDS", 60, 30, 86400, env),
        email_retry_backoff_max_seconds=_int("EMAIL_RETRY_BACKOFF_MAX_SECONDS", 3600, 30, 86400, env),
        email_resend_unconfirmed=_bool("EMAIL_RESEND_UNCONFIRMED", False, env),
        audit_retention_days=_int("NOTIFICATION_AUDIT_RETENTION_DAYS", 90, 7, 3650, env),
        portfolio_capital=capital,
    )


def _validate_intraday(cfg):
    if not cfg:
        return {}
    need = ("benchmark", "top_n", "min_confirmations", "volume_surge_min", "max_extension_atr", "stop_atr_mult",
            "target_r", "min_traded_value_cr", "max_quote_age_minutes", "no_new_signals_after", "square_off",
            "weights", "costs", "backtest")
    missing = [k for k in need if k not in cfg]
    if missing:
        raise ConfigError(f"intraday_reco missing keys: {missing}")
    if not 1 <= cfg["min_confirmations"] <= 5 or cfg["stop_atr_mult"] <= 0 or cfg["target_r"] <= 0:
        raise ConfigError("intraday_reco: min_confirmations 1..5, stop_atr_mult and target_r > 0")
    for k in ("no_new_signals_after", "square_off", ):
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", cfg[k]):
            raise ConfigError(f"intraday_reco.{k} must be HH:MM")
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def _signup_mode(env):
    mode = _env("SIGNUP_MODE", "approval", env).lower()
    if mode not in ("approval", "open", "closed"):
        raise ConfigError("SIGNUP_MODE must be approval, open or closed")
    return mode


def _live_settings(env):
    provider = _env("LIVE_QUOTE_PROVIDER", "yahoo", env).lower()
    if provider not in LIVE_PROVIDERS:
        raise ConfigError(f"LIVE_QUOTE_PROVIDER must be one of {LIVE_PROVIDERS}")
    return LiveSettings(enabled=_bool("LIVE_ALERTS_ENABLED", True, env), provider=provider,
                        max_quote_age_minutes=_int("LIVE_MAX_QUOTE_AGE_MINUTES", 30, 5, 240, env))


def _report_time_cron(env):
    raw = _env("DAILY_REPORT_TIME", None, env)
    if raw is None:
        return None
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", raw)
    if not m:
        raise ConfigError("DAILY_REPORT_TIME must be HH:MM (24h, market timezone)")
    return f"{int(m.group(2))} {int(m.group(1))} * * 1-5"


def load_settings(env=None, config_path=None, load_env_file=True):
    """Build validated Settings. `env` overrides os.environ (used by tests)."""
    base = Path(_env("ALERT_BASE_DIR", str(BASE_DIR), env))
    if env is None and load_env_file:
        try:
            from dotenv import load_dotenv
        except ImportError as exc:
            raise ConfigError("python-dotenv is required (pip install python-dotenv)") from exc
        # Real environment (e.g. systemd Environment=) wins over the file.
        load_dotenv(base / ".env", override=False)

    tz = _env("ALERT_TIMEZONE", "Asia/Kolkata", env)
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"ALERT_TIMEZONE {tz!r} is not a valid IANA timezone") from exc

    path = Path(config_path or _env("ALERT_CONFIG_FILE", str(base / "config" / "alert_system.json"), env))
    try:
        raw_cfg = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid JSON: {exc}") from exc

    job_defaults = {
        "max_attempts": _int("JOB_MAX_ATTEMPTS", 10, 1, 50, env),
        "retry_backoff_seconds": _int("JOB_RETRY_BACKOFF_SECONDS", 120, 30, 86400, env),
        "retry_backoff_max_seconds": _int("JOB_RETRY_BACKOFF_MAX_SECONDS", 1800, 30, 86400, env),
    }
    report_cron = _report_time_cron(env)
    raw_jobs = [dict(j, schedules=[report_cron]) if report_cron and j.get("id") == "daily_report" else j
                for j in raw_cfg.get("jobs", [])]
    jobs = tuple(_job_from_dict(j, job_defaults) for j in raw_jobs)
    ids = [j.id for j in jobs]
    if len(set(ids)) != len(ids):
        raise ConfigError(f"duplicate job ids in {path}")
    if not jobs:
        raise ConfigError(f"no jobs configured in {path}")

    cleanup_cfg = raw_cfg.get("mtd_cleanup", {})
    allowed = tuple(cleanup_cfg.get("allowed_databases", ()))
    targets = tuple(_target_from_dict(t, allowed) for t in cleanup_cfg.get("targets", ()))

    state_dir = Path(_env("ALERT_STATE_DIR", str(base / "state"), env))
    log_dir = Path(_env("ALERT_LOG_DIR", str(base / "logs"), env))
    default_lock = "/run/lock/nse-alert.lock" if Path("/run/lock").is_dir() else str(state_dir / "alert_service.lock")

    wa_token = _env("WHATSAPP_ACCESS_TOKEN", "", env)
    recipients = tuple(_normalise_recipient(r) for r in _list("WHATSAPP_RECIPIENT", (), env))
    whatsapp = WhatsAppSettings(
        enabled=_bool("WHATSAPP_ENABLED", True, env),
        access_token=wa_token,
        phone_number_id=_env("WHATSAPP_PHONE_NUMBER_ID", "", env),
        api_version=_env("WHATSAPP_API_VERSION", "v25.0", env),
        recipients=recipients,
        mode=_env("WHATSAPP_SEND_MODE", "text", env),
        template_name=_env("WHATSAPP_TEMPLATE_NAME", "", env),
        template_language=_env("WHATSAPP_TEMPLATE_LANGUAGE", "en", env),
        template_param_max_chars=_int("WHATSAPP_TEMPLATE_PARAM_MAX_CHARS", 1000, 50, 32768, env),
        timeout_seconds=_int("WHATSAPP_TIMEOUT_SECONDS", 20, 3, 120, env),
        max_attempts=_int("WHATSAPP_MAX_ATTEMPTS", 8, 1, 50, env),
        retry_backoff_seconds=_int("WHATSAPP_RETRY_BACKOFF_SECONDS", 60, 30, 86400, env),
        retry_backoff_max_seconds=_int("WHATSAPP_RETRY_BACKOFF_MAX_SECONDS", 3600, 30, 86400, env),
        resend_unconfirmed=_bool("WHATSAPP_RESEND_UNCONFIRMED", False, env),
        base_url=_env("WHATSAPP_API_BASE_URL", "https://graph.facebook.com", env),
    )
    if whatsapp.mode not in WHATSAPP_MODES:
        raise ConfigError(f"WHATSAPP_SEND_MODE must be one of {WHATSAPP_MODES}")
    if whatsapp.mode == "template" and not whatsapp.template_name:
        raise ConfigError("WHATSAPP_SEND_MODE=template requires WHATSAPP_TEMPLATE_NAME")

    mail_password = _env("MAIL_APP_PASSWORD", "", env)
    email = EmailSettings(
        sender=_env("MAIL_SENDER", "", env),
        password=mail_password,
        recipient=_env("MAIL_RECIPIENT", "", env),
        smtp_host=_env("MAIL_SMTP_HOST", "smtp.gmail.com", env),
        smtp_port=_int("MAIL_SMTP_PORT", 587, 1, 65535, env),
    )

    ch_password = _env("CLICKHOUSE_PASSWORD", "", env)
    clickhouse = ClickHouseSettings(
        host=_env("CLICKHOUSE_HOST", "127.0.0.1", env),
        port=_int("CLICKHOUSE_PORT", 8123, 1, 65535, env),
        username=_env("CLICKHOUSE_USER", "default", env),
        password=ch_password,
        secure=_bool("CLICKHOUSE_SECURE", False, env),
        connect_timeout=_int("CLICKHOUSE_CONNECT_TIMEOUT", 10, 1, 300, env),
        send_receive_timeout=_int("CLICKHOUSE_SEND_RECEIVE_TIMEOUT", 300, 5, 3600, env),
        max_retries=_int("CLICKHOUSE_MAX_RETRIES", 5, 1, 50, env),
        retry_delay_seconds=float(_int("CLICKHOUSE_RETRY_DELAY_SECONDS", 5, 1, 300, env)),
    )

    pg = PgSettings(
        host=_env("PG_HOST", "127.0.0.1", env),
        port=_int("PG_PORT", 5432, 1, 65535, env),
        dbname=_env("PG_DATABASE", "nse_app", env),
        app_password=_env("PG_APP_PASSWORD", "", env),
        service_password=_env("PG_SERVICE_PASSWORD", "", env),
        owner_password=_env("PG_OWNER_PASSWORD", "", env),
    )

    channels_ok = {"email", "whatsapp", "log"}
    ops_channels = _list("OPS_ALERT_CHANNELS", ("email", "whatsapp"), env)
    summary_channels = _list("OPS_DAILY_SUMMARY_CHANNELS", ("log",), env)
    for name, chans in (("OPS_ALERT_CHANNELS", ops_channels), ("OPS_DAILY_SUMMARY_CHANNELS", summary_channels)):
        bad = set(chans) - channels_ok
        if bad:
            raise ConfigError(f"{name} has unknown channels {sorted(bad)}; use {sorted(channels_ok)}")

    log_level = _env("ALERT_LOG_LEVEL", "INFO", env).upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("ALERT_LOG_LEVEL must be DEBUG/INFO/WARNING/ERROR")

    startup_key = _env("JOB_STARTUP_RUN_KEY", "day", env)
    if startup_key not in ("day", "boot"):
        raise ConfigError("JOB_STARTUP_RUN_KEY must be 'day' or 'boot'")

    return Settings(
        base_dir=base,
        state_dir=state_dir,
        log_dir=log_dir,
        holdings_db=Path(_env("ALERT_HOLDINGS_DB", str(base / "holdings.db"), env)),
        scheduler_db=Path(_env("ALERT_SCHEDULER_DB", str(state_dir / "scheduler.db"), env)),
        lock_file=Path(_env("ALERT_LOCK_FILE", default_lock, env)),
        health_file=state_dir / "health.json",
        timezone=tz,
        python=_env("ALERT_PYTHON", sys.executable, env),
        tick_seconds=_int("SCHEDULER_TICK_SECONDS", 20, 5, 300, env),
        misfire_grace_seconds=_int("SCHEDULER_MISFIRE_GRACE_SECONDS", 300, 30, 3600, env),
        max_catchup_days=_int("SCHEDULER_MAX_CATCHUP_DAYS", 7, 1, 60, env),
        startup_run_key=startup_key,
        connectivity_hosts=_list(
            "CONNECTIVITY_CHECK_HOSTS",
            ("graph.facebook.com:443", "query1.finance.yahoo.com:443", "1.1.1.1:443"), env),
        connectivity_timeout=float(_int("CONNECTIVITY_TIMEOUT_SECONDS", 5, 1, 60, env)),
        connectivity_retry_seconds=_int("CONNECTIVITY_RETRY_SECONDS", 60, 10, 3600, env),
        clickhouse=clickhouse,
        whatsapp=whatsapp,
        email=email,
        jobs=jobs,
        cleanup_targets=targets,
        cleanup_allowed_databases=allowed,
        cleanup_sqlite=bool(cleanup_cfg.get("sqlite_alert_history", True)),
        cleanup_enabled=_bool("MTD_CLEANUP_ENABLED", True, env),
        cleanup_dry_run=_bool("MTD_CLEANUP_DRY_RUN", False, env),
        state_retention_days=_int("STATE_RETENTION_DAYS", 90, 7, 3650, env),
        log_level=log_level,
        log_max_bytes=_int("LOG_MAX_BYTES", 10 * 1024 * 1024, 64 * 1024, 1024 ** 3, env),
        log_backup_count=_int("LOG_BACKUP_COUNT", 14, 1, 365, env),
        ops_alert_channels=ops_channels,
        daily_summary_channels=summary_channels,
        report=_report_settings(env),
        live=_live_settings(env),
        pg=pg,
        intraday_reco=_validate_intraday(raw_cfg.get("intraday_reco")),
        signup_mode=_signup_mode(env),
        public_url=_env("DASHBOARD_PUBLIC_URL", "", env),
        recommendations=_validate_recommendations(raw_cfg.get("recommendations")),
        secrets=tuple(s for s in (wa_token, mail_password, ch_password, pg.app_password, pg.service_password,
                                  pg.owner_password, _env("ANTHROPIC_API_KEY", "", env)) if s and len(s) >= 6),
    )
