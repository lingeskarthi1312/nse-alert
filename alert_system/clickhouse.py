"""Shared ClickHouse client factory (same server/user defaults the scripts
always used: 127.0.0.1:8123, user 'default') with timeouts and controlled
reconnection. Credentials come from CLICKHOUSE_* environment variables."""
import logging
import time

from .logging_setup import log_event, redact

log = logging.getLogger("alert_system.clickhouse")


def _transient_errors():
    from clickhouse_connect.driver.exceptions import OperationalError
    return (OperationalError, ConnectionError, TimeoutError, OSError)


def _connect(ch):
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host=ch.host,
        port=ch.port,
        username=ch.username,
        password=ch.password,
        secure=ch.secure,
        connect_timeout=ch.connect_timeout,
        send_receive_timeout=ch.send_receive_timeout,
    )


def get_client(settings=None, retries=None, sleep=time.sleep):
    """Connect, retrying transient failures (server restarting, WSL network
    not up yet) with linear backoff. Raises the last error when exhausted."""
    if settings is None:
        from .config import load_settings
        settings = load_settings()
    ch = settings.clickhouse
    attempts = retries or ch.max_retries
    transient = _transient_errors()
    for attempt in range(1, attempts + 1):
        try:
            client = _connect(ch)
            if attempt > 1:
                log_event(log, logging.INFO, "clickhouse reconnected", component="clickhouse", attempt=attempt)
            return client
        except transient as exc:
            log_event(log, logging.WARNING, "clickhouse connection failed", component="clickhouse",
                      attempt=attempt, max_attempts=attempts, host=ch.host, port=ch.port,
                      error=redact(f"{type(exc).__name__}: {exc}")[:300])
            if attempt == attempts:
                raise
            sleep(ch.retry_delay_seconds * attempt)


def ping(settings):
    """True if ClickHouse answers SELECT 1 (single quick attempt)."""
    try:
        client = get_client(settings, retries=1)
    except Exception as exc:  # any failure here simply means "not available"
        log_event(log, logging.DEBUG, "clickhouse ping failed", component="clickhouse",
                  error=redact(f"{type(exc).__name__}: {exc}")[:200])
        return False
    try:
        return client.command("SELECT 1") == 1
    except Exception as exc:
        log_event(log, logging.DEBUG, "clickhouse ping query failed", component="clickhouse",
                  error=redact(f"{type(exc).__name__}: {exc}")[:200])
        return False
    finally:
        client.close()
