"""Month-to-date (MTD) retention for the configured datasets.

Rule: keep rows dated on/after the first calendar day of the current month in
the market timezone (Asia/Kolkata). A target may keep a short pre-month
window (`baseline_lookback_days`) when the MTD calculation itself needs it:
save_mtd_rankings.py compares the latest close against the last close
*before* the 1st, so daily_stock_prices keeps month_start - 10 days, extended
further back if needed so no symbol loses its baseline close.

Safety checks before anything is deleted:
  * target must be listed in config and inside allowed_databases;
  * table must exist, be a *MergeTree engine, and the date column must be a
    Date/DateTime type;
  * cutoff must be within [month_start - 62 days, month_start];
  * local date must agree with the ClickHouse server's date (guards against
    a wrong system clock wiping the current month);
  * rows to keep are counted before and verified after.

Strategy (decided per table from system.tables, not assumed):
  * PARTITION BY toYYYYMM(<date_column>): DROP PARTITION for months
    entirely older than the cutoff (cheap metadata operation), then a
    lightweight DELETE for leftover rows in the boundary partition;
  * anything else: lightweight DELETE WHERE <date_column> < cutoff.
Every step is idempotent: re-running finds nothing old and reports NOOP.
"""
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from .logging_setup import log_event, redact
from .timeutil import local_midnight_utc, month_start

log = logging.getLogger("alert_system.cleanup")

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PARTITION_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_DATE_TYPE = re.compile(r"^(Date|Date32|DateTime(64)?(\(.*\))?)$")
MAX_CUTOFF_AGE_DAYS = 62
BASELINE_FLOOR_DAYS = 45


class RetentionError(Exception):
    pass


@dataclass
class TableResult:
    table: str
    cutoff: str
    status: str = "PENDING"  # NOOP | DRY_RUN | CLEANED | ERROR
    strategy: str = "none"
    rows_old_before: int = 0
    rows_kept_before: int = 0
    rows_old_after: int = 0
    rows_kept_after: int = 0
    dropped_partitions: list = field(default_factory=list)
    error: str = None


def _q(name):
    if not _IDENT.match(name):
        raise RetentionError(f"unsafe identifier {name!r}")
    return f"`{name}`"


class MTDCleaner:
    def __init__(self, settings, client, today, dry_run=None):
        self.settings = settings
        self.client = client
        self.today = today
        self.month_start = month_start(today)
        self.dry_run = settings.cleanup_dry_run if dry_run is None else dry_run

    # -- helpers ----------------------------------------------------------
    def _scalar(self, sql, params=None):
        rows = self.client.query(sql, parameters=params or {}).result_rows
        return rows[0][0] if rows else None

    def _counts(self, fq, col, cutoff):
        row = self.client.query(
            f"SELECT countIf({col} < toDate(%(c)s)), countIf({col} >= toDate(%(c)s)) FROM {fq}",
            parameters={"c": cutoff.isoformat()}).result_rows[0]
        return int(row[0]), int(row[1])

    def check_clock(self):
        server_today = self._scalar("SELECT toDate(now(), %(tz)s)", {"tz": self.settings.timezone})
        if server_today is None or abs((server_today - self.today).days) > 1:
            raise RetentionError(
                f"clock mismatch: local date {self.today} vs ClickHouse date {server_today}; "
                "refusing to delete anything")

    def cutoff_for(self, target, fq, col):
        cutoff = self.month_start - timedelta(days=target.baseline_lookback_days)
        if target.baseline_key_column:
            key = _q(target.baseline_key_column)
            earliest_baseline = self._scalar(
                f"SELECT minOrNull(last_pre) FROM (SELECT {key}, max({col}) AS last_pre FROM {fq} "
                f"WHERE {col} < toDate(%(ms)s) AND {key} IN "
                f"(SELECT DISTINCT {key} FROM {fq} WHERE {col} >= toDate(%(ms)s)) GROUP BY {key})",
                {"ms": self.month_start.isoformat()})
            floor = self.month_start - timedelta(days=BASELINE_FLOOR_DAYS)
            if earliest_baseline is not None and earliest_baseline < cutoff:
                new_cutoff = max(earliest_baseline, floor)
                log_event(log, logging.INFO, "mtd cutoff extended to keep baseline closes",
                          component="cleanup", table=target.table, configured_cutoff=str(cutoff),
                          effective_cutoff=str(new_cutoff), earliest_baseline=str(earliest_baseline))
                cutoff = new_cutoff
        if not (self.month_start - timedelta(days=MAX_CUTOFF_AGE_DAYS) <= cutoff <= self.month_start):
            raise RetentionError(f"{target.table}: cutoff {cutoff} outside safe range")
        return cutoff

    def inspect(self, target):
        if target.database not in self.settings.cleanup_allowed_databases:
            raise RetentionError(f"{target.table} not in allowed databases")
        info = self.client.query(
            "SELECT engine, partition_key FROM system.tables WHERE database=%(d)s AND name=%(t)s",
            parameters={"d": target.database, "t": target.name}).result_rows
        if not info:
            raise RetentionError(f"{target.table} does not exist")
        engine, partition_key = info[0]
        if "MergeTree" not in engine:
            raise RetentionError(f"{target.table}: engine {engine} not supported for retention")
        col_type = self._scalar(
            "SELECT type FROM system.columns WHERE database=%(d)s AND table=%(t)s AND name=%(c)s",
            {"d": target.database, "t": target.name, "c": target.date_column})
        if col_type is None:
            raise RetentionError(f"{target.table}: column {target.date_column} missing")
        if not _DATE_TYPE.match(col_type):
            raise RetentionError(f"{target.table}.{target.date_column} has type {col_type}, expected Date/DateTime")
        return engine, (partition_key or "").replace(" ", "")

    # -- main -------------------------------------------------------------
    def clean_table(self, target):
        fq = f"{_q(target.database)}.{_q(target.name)}"
        col = _q(target.date_column)
        result = TableResult(target.table, cutoff="?")
        try:
            engine, partition_key = self.inspect(target)
            cutoff = self.cutoff_for(target, fq, col)
            result.cutoff = cutoff.isoformat()
            result.rows_old_before, result.rows_kept_before = self._counts(fq, col, cutoff)
            log_event(log, logging.INFO, "mtd cleanup table inspected", component="cleanup",
                      table=target.table, engine=engine, partition_key=partition_key,
                      cutoff=result.cutoff, rows_to_remove=result.rows_old_before,
                      rows_to_keep=result.rows_kept_before, dry_run=self.dry_run)
            if result.rows_old_before == 0:
                result.status = "NOOP"
            elif self.dry_run:
                monthly = partition_key == f"toYYYYMM({target.date_column})"
                result.status, result.strategy = "DRY_RUN", "drop_partition+delete" if monthly else "delete"
            else:
                self._delete(target, fq, col, cutoff, partition_key, result)
            result.rows_old_after, result.rows_kept_after = self._counts(fq, col, cutoff)
            if not self.dry_run:
                if result.rows_old_after:
                    raise RetentionError(f"{result.rows_old_after} expired rows still visible after cleanup")
                if result.rows_kept_after < result.rows_kept_before:
                    raise RetentionError(
                        f"kept rows dropped from {result.rows_kept_before} to {result.rows_kept_after}")
        except Exception as exc:
            result.status = "ERROR"
            result.error = redact(f"{type(exc).__name__}: {exc}")[:500]
        log_event(log, logging.ERROR if result.status == "ERROR" else logging.INFO,
                  "mtd cleanup table result", component="cleanup", **asdict(result))
        return result

    def _delete(self, target, fq, col, cutoff, partition_key, result):
        strategies = []
        if partition_key == f"toYYYYMM({target.date_column})":
            parts = self.client.query(
                f"SELECT _partition_id, max({col}) FROM {fq} GROUP BY _partition_id").result_rows
            for pid, max_date in parts:
                if max_date < cutoff:
                    if not _PARTITION_ID.match(str(pid)):
                        raise RetentionError(f"unexpected partition id {pid!r}")
                    self.client.command(f"ALTER TABLE {fq} DROP PARTITION ID '{pid}'")
                    result.dropped_partitions.append(str(pid))
            if result.dropped_partitions:
                strategies.append("drop_partition")
        remaining, _ = self._counts(fq, col, cutoff)
        if remaining:
            self.client.command(f"DELETE FROM {fq} WHERE {col} < toDate(%(c)s)",
                                parameters={"c": cutoff.isoformat()})
            strategies.append("lightweight_delete")
        result.strategy = "+".join(strategies) or "none"
        result.status = "CLEANED"

    def run(self):
        self.check_clock()
        return [self.clean_table(t) for t in self.settings.cleanup_targets]


def cleanup_appdb(settings, today, dry_run=False):
    """MTD retention for per-user alert history in the PostgreSQL app DB.

    Only *finished* history is removed: alert_history rows with status RESET
    (ACTIVE rows are the duplicate-suppression state and must stay) and
    monthly-report delivery markers from earlier months. Holdings, alert
    rules, watchlists and users are never touched. notification_outbox is the
    delivery audit/dedup store and is pruned by NOTIFICATION_AUDIT_RETENTION_DAYS.
    """
    from .pgdb import service_tx
    cutoff = local_midnight_utc(month_start(today), settings.timezone)
    plan = [("alert_history", "FROM alert_history WHERE status='RESET' AND triggered_at < %s", (cutoff,)),
            ("delivery_markers", "FROM delivery_markers WHERE kind='mtd_rankings' AND ref < %s",
             (month_start(today).isoformat(),))]
    result = {"cutoff_utc": cutoff.isoformat(), "dry_run": dry_run}
    with service_tx(settings) as c:
        for name, where, args in plan:
            result[name] = c.execute(f"SELECT count(*) AS n {where}", args).fetchone()["n"]
            if not dry_run:
                c.execute(f"DELETE {where}", args)
    log_event(log, logging.INFO, "app db alert history mtd cleanup", component="cleanup", **result)
    return result
