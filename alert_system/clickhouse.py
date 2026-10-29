"""Market-data client. The data used to live in ClickHouse; since 2026-10-29
it is in PostgreSQL schema nse_alerts (same table names), so the system can
run on free hosting. The module keeps its name and the small client surface
the callers use (query / command / insert / close) on top of psycopg.

Background jobs connect as the service role; the dashboard passes role="app"
(read-only on nse_alerts). Parameters use the %(name)s style.
"""
import logging
import time
from dataclasses import dataclass

import psycopg
from psycopg.rows import tuple_row

from .logging_setup import log_event, redact

log = logging.getLogger("alert_system.clickhouse")

# primary keys used by insert() to upsert (re-loading a day replaces it)
TABLE_KEYS = {
    "nse_alerts.daily_stock_prices": ("symbol", "trade_date"),
    "nse_alerts.daily_mtd_rankings": ("trade_date", "category", "ranking_type", "symbol"),
    "nse_alerts.stock_categories": ("category", "symbol"),
    "nse_alerts.recommendations": ("run_date", "ranking", "rank"),
}


@dataclass
class QueryResult:
    result_rows: list
    column_names: tuple


class Client:
    def __init__(self, conn):
        self.conn = conn

    def query(self, sql, parameters=None):
        cur = self.conn.execute(sql, parameters or None)
        rows = cur.fetchall() if cur.description else []
        return QueryResult(rows, tuple(c.name for c in cur.description or ()))

    def command(self, sql, parameters=None):
        """Run a statement; returns the first value of the first row, if any."""
        cur = self.conn.execute(sql, parameters or None)
        if cur.description:
            row = cur.fetchone()
            return row[0] if row else None
        return None

    def insert(self, table, rows, column_names):
        if table not in TABLE_KEYS:
            raise ValueError(f"insert into unknown table {table}")
        key = TABLE_KEYS[table]
        cols = ", ".join(column_names)
        marks = ", ".join(["%s"] * len(column_names))
        update = ", ".join(f"{c} = EXCLUDED.{c}" for c in column_names if c not in key)
        sql = (f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT ({', '.join(key)}) "
               + (f"DO UPDATE SET {update}" if update else "DO NOTHING"))
        with self.conn.cursor() as cur:
            cur.executemany(sql, [tuple(r) for r in rows])

    def close(self):
        self.conn.close()


def _transient_errors():
    return (psycopg.OperationalError, ConnectionError, TimeoutError, OSError)


def _connect(settings, role):
    from .pgdb import _connect as pg_connect
    return Client(pg_connect(settings, role, autocommit=True, row_factory=tuple_row))


def get_client(settings=None, retries=None, sleep=time.sleep, role="service"):
    """Connect, retrying transient failures (database waking up, network not
    up yet) with linear backoff. Raises the last error when exhausted."""
    if settings is None:
        from .config import load_settings
        settings = load_settings()
    ch = settings.clickhouse
    attempts = retries or ch.max_retries
    transient = _transient_errors()
    for attempt in range(1, attempts + 1):
        try:
            client = _connect(settings, role)
            if attempt > 1:
                log_event(log, logging.INFO, "market db reconnected", component="clickhouse", attempt=attempt)
            return client
        except transient as exc:
            log_event(log, logging.WARNING, "market db connection failed", component="clickhouse",
                      attempt=attempt, max_attempts=attempts, host=settings.pg.host,
                      error=redact(f"{type(exc).__name__}: {exc}")[:300])
            if attempt == attempts:
                raise
            sleep(ch.retry_delay_seconds * attempt)


def ping(settings):
    """True if the market database answers SELECT 1 (single quick attempt)."""
    try:
        client = get_client(settings, retries=1)
    except Exception as exc:  # any failure here simply means "not available"
        log_event(log, logging.DEBUG, "market db ping failed", component="clickhouse",
                  error=redact(f"{type(exc).__name__}: {exc}")[:200])
        return False
    try:
        return client.command("SELECT 1") == 1
    except Exception as exc:
        log_event(log, logging.DEBUG, "market db ping query failed", component="clickhouse",
                  error=redact(f"{type(exc).__name__}: {exc}")[:200])
        return False
    finally:
        client.close()
