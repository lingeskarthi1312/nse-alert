"""MTD boundary + cleanup safety tests.

The ClickHouse tests create a throw-away database alert_test_<random> with
the production table definitions, and drop it afterwards. They never touch
nse_alerts. Skipped automatically when no ClickHouse server is reachable.
"""
import uuid
from datetime import date, timedelta

import pytest

from alert_system.cleanup import MTDCleaner, RetentionError, cleanup_appdb
from alert_system.timeutil import local_midnight_utc, local_today, month_start, price_window_start
from conftest import utc


# --- pure MTD boundary tests -------------------------------------------------

def test_month_start_and_price_window():
    assert month_start(date(2026, 10, 19)) == date(2026, 10, 1)
    assert price_window_start(date(2026, 10, 19), 10) == date(2026, 9, 21)
    assert price_window_start(date(2027, 1, 1), 10) == date(2026, 12, 22)  # year boundary
    assert price_window_start(date(2026, 3, 31), 0) == date(2026, 3, 1)


def test_ist_date_boundary_not_utc():
    # 2026-10-31 19:00 UTC is already 1 Nov 00:30 in India: new month in IST
    assert local_today("Asia/Kolkata", utc(2026, 10, 31, 19, 0)) == date(2026, 11, 1)
    assert local_today("Asia/Kolkata", utc(2026, 10, 31, 18, 29)) == date(2026, 10, 31)
    assert local_midnight_utc(date(2026, 11, 1), "Asia/Kolkata") == utc(2026, 10, 31, 18, 30)


def test_mtd_is_calendar_month_not_30_days():
    # 31-day and 28-day months: cutoff is always the 1st, never today-30
    assert month_start(date(2026, 3, 31)) == date(2026, 3, 1)
    assert month_start(date(2027, 2, 28)) == date(2027, 2, 1)



# --- PostgreSQL alert-history retention (per user) -------------------------------

def _seed_history(s):
    from datetime import datetime, timezone
    from alert_system.pgdb import service_tx
    with service_tx(s) as c:
        uid = c.execute("INSERT INTO users (email, password_hash) VALUES ('u@example.com','x') RETURNING id").fetchone()["id"]
        c.execute("INSERT INTO user_alerts (user_id, symbol, metric, condition, threshold) "
                  "VALUES (%s,'CUPID.NS','Daily','Below or equal',10)", (uid,))
        for sym, at, status in (("A", datetime(2026, 9, 10, 5, tzinfo=timezone.utc), "RESET"),
                                ("B", datetime(2026, 9, 12, 5, tzinfo=timezone.utc), "ACTIVE"),
                                ("C", datetime(2026, 9, 30, 18, 29, 59, tzinfo=timezone.utc), "RESET"),
                                ("D", datetime(2026, 9, 30, 18, 30, tzinfo=timezone.utc), "RESET")):
            c.execute("INSERT INTO alert_history (user_id, symbol, metric, condition, threshold, triggered_at, status) "
                      "VALUES (%s,%s,'Daily','Below or equal',10,%s,%s)", (uid, sym, at, status))
        for ref in ("2026-09-30", "2026-10-13"):
            c.execute("INSERT INTO delivery_markers (user_id, kind, ref, status) VALUES (%s,'mtd_rankings',%s,'SENT')",
                      (uid, ref))
    return uid


def test_appdb_cleanup_keeps_active_and_current_month(pg_settings):
    from alert_system.outbox import enqueue
    from alert_system.pgdb import service_tx
    s = pg_settings()
    _seed_history(s)
    enqueue(s, "alert:old", "x", ("911",), 3, now=utc(2026, 9, 15))
    dry = cleanup_appdb(s, date(2026, 10, 19), dry_run=True)
    assert dry["alert_history"] == 2 and dry["delivery_markers"] == 1
    res = cleanup_appdb(s, date(2026, 10, 19))
    assert res["alert_history"] == 2
    with service_tx(s) as c:
        # 18:30 UTC on 30 Sep is 00:00 IST on 1 Oct: that row belongs to October and stays
        assert [r["symbol"] for r in c.execute("SELECT symbol FROM alert_history ORDER BY id")] == ["B", "D"]
        assert [r["ref"] for r in c.execute("SELECT ref FROM delivery_markers")] == ["2026-10-13"]
        assert c.execute("SELECT count(*) AS n FROM notification_outbox").fetchone()["n"] == 1  # audit kept
        assert c.execute("SELECT count(*) AS n FROM user_alerts").fetchone()["n"] == 1  # rules never touched
    assert cleanup_appdb(s, date(2026, 10, 19))["alert_history"] == 0  # idempotent


def test_outbox_audit_retention_prune(pg_settings):
    from alert_system.outbox import Outbox, enqueue
    from alert_system.pgdb import service_tx
    s = pg_settings()
    box = Outbox(s, client=None, clock=lambda: utc(2026, 10, 19))
    enqueue(s, "old", "x", ("a@x",), 3, now=utc(2026, 6, 1), channel="email", subject="s")
    enqueue(s, "recent", "x", ("a@x",), 3, now=utc(2026, 9, 1), channel="email", subject="s")
    enqueue(s, "pending-old", "x", ("a@x",), 3, now=utc(2026, 6, 1), channel="email", subject="s")
    with service_tx(s) as c:
        c.execute("UPDATE notification_outbox SET status='SENT' WHERE group_key <> 'pending-old'")
    assert box.prune(utc(2026, 10, 19) - timedelta(days=90)) == 1
    with service_tx(s) as c:
        assert sorted(r["group_key"] for r in c.execute("SELECT group_key FROM notification_outbox")) == [
            "pending-old", "recent"]  # undelivered items are never pruned


# --- fake-client safety checks (no server needed) --------------------------------

class _Result:
    def __init__(self, rows):
        self.result_rows = rows


class ScriptedClient:
    def __init__(self, tables=None, column_type="Date", server_today=date(2026, 10, 19)):
        self.tables = tables or {}
        self.column_type = column_type
        self.server_today = server_today
        self.commands = []

    def query(self, sql, parameters=None):
        if "FROM system.tables" in sql:
            t = self.tables.get((parameters["d"], parameters["t"]))
            return _Result([t] if t else [])
        if "FROM system.columns" in sql:
            return _Result([(self.column_type,)])
        if "toDate(now()" in sql:
            return _Result([(self.server_today,)])
        if "countIf" in sql:
            return _Result([(5, 5)])
        return _Result([(None,)])

    def command(self, sql, parameters=None):
        self.commands.append(sql)


def _target_settings(settings_factory, **kw):
    return settings_factory(targets=[{"table": "nse_alerts.daily_mtd_rankings", "date_column": "trade_date", **kw}])


def test_missing_table_is_error_and_nothing_deleted(settings_factory):
    s = _target_settings(settings_factory)
    client = ScriptedClient()
    [res] = MTDCleaner(s, client, date(2026, 10, 19)).run()
    assert res.status == "ERROR" and "does not exist" in res.error and client.commands == []


def test_non_date_column_is_refused(settings_factory):
    s = _target_settings(settings_factory)
    client = ScriptedClient({("nse_alerts", "daily_mtd_rankings"): ("MergeTree", "")}, column_type="String")
    [res] = MTDCleaner(s, client, date(2026, 10, 19)).run()
    assert res.status == "ERROR" and "expected Date" in res.error and client.commands == []


def test_non_mergetree_engine_is_refused(settings_factory):
    s = _target_settings(settings_factory)
    client = ScriptedClient({("nse_alerts", "daily_mtd_rankings"): ("Log", "")})
    [res] = MTDCleaner(s, client, date(2026, 10, 19)).run()
    assert res.status == "ERROR" and client.commands == []


def test_clock_mismatch_refuses_everything(settings_factory):
    s = _target_settings(settings_factory)
    client = ScriptedClient({("nse_alerts", "daily_mtd_rankings"): ("MergeTree", "")},
                            server_today=date(2026, 10, 19))
    with pytest.raises(RetentionError, match="clock mismatch"):
        MTDCleaner(s, client, date(2027, 3, 1)).run()
    assert client.commands == []


# --- real ClickHouse integration ---------------------------------------------------

PRICES_DDL = """CREATE TABLE {db}.daily_stock_prices (
    symbol String, trade_date Date, open_price Nullable(Float64), high_price Nullable(Float64),
    low_price Nullable(Float64), close_price Nullable(Float64), volume Nullable(UInt64),
    recorded_at DateTime DEFAULT now())
ENGINE = MergeTree PARTITION BY toYYYYMM(trade_date) ORDER BY (symbol, trade_date)"""
RANKINGS_DDL = """CREATE TABLE {db}.daily_mtd_rankings (
    trade_date Date, symbol String, category String, close_price Nullable(Float64),
    mtd_change_pct Nullable(Float64), ranking_type String, recorded_at DateTime DEFAULT now())
ENGINE = MergeTree ORDER BY (trade_date, category, ranking_type, symbol)"""
CATEGORIES_DDL = """CREATE TABLE {db}.stock_categories (symbol String, category String,
    updated_at DateTime DEFAULT now()) ENGINE = ReplacingMergeTree(updated_at) ORDER BY (category, symbol)"""


@pytest.fixture
def ch(settings_factory):
    clickhouse_connect = pytest.importorskip("clickhouse_connect")
    try:
        client = clickhouse_connect.get_client(host="127.0.0.1", port=8123, username="default",
                                               connect_timeout=3)
        client.command("SELECT 1")
    except Exception as exc:
        pytest.skip(f"ClickHouse not reachable: {exc}")
    db = f"alert_test_{uuid.uuid4().hex[:10]}"
    client.command(f"CREATE DATABASE {db}")
    for ddl in (PRICES_DDL, RANKINGS_DDL, CATEGORIES_DDL):
        client.command(ddl.format(db=db))
    s = settings_factory(allowed=(db,), targets=[
        {"table": f"{db}.daily_stock_prices", "date_column": "trade_date",
         "baseline_lookback_days": 10, "baseline_key_column": "symbol"},
        {"table": f"{db}.daily_mtd_rankings", "date_column": "trade_date"}])
    try:
        yield client, db, s
    finally:
        client.command(f"DROP DATABASE IF EXISTS {db}")
        client.close()


def _days(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _load_prices(client, db, symbols, start, end):
    rows = [(s, d, 1.0, 1.0, 1.0, 100.0, 10) for s in symbols for d in _days(start, end)]
    client.insert(f"{db}.daily_stock_prices", rows, column_names=[
        "symbol", "trade_date", "open_price", "high_price", "low_price", "close_price", "volume"])


def _count(client, sql):
    return client.query(sql).result_rows[0][0]


def test_ch_prices_partition_drop_plus_boundary_delete_idempotent(ch):
    client, db, s = ch
    _load_prices(client, db, ["A.NS", "B.NS"], date(2026, 8, 20), date(2026, 10, 19))
    client.insert(f"{db}.stock_categories", [("A.NS", "Large-cap")], column_names=["symbol", "category"])
    kept_expected = _count(client, f"SELECT count() FROM {db}.daily_stock_prices WHERE trade_date >= '2026-09-21'")

    cleaner = MTDCleaner(s, client, date(2026, 10, 19))
    res = cleaner.clean_table(s.cleanup_targets[0])
    assert res.status == "CLEANED", res.error
    assert res.cutoff == "2026-09-21"
    assert res.dropped_partitions == ["202608"]
    assert res.strategy == "drop_partition+lightweight_delete"
    assert _count(client, f"SELECT min(trade_date) FROM {db}.daily_stock_prices") == date(2026, 9, 21)
    assert _count(client, f"SELECT count() FROM {db}.daily_stock_prices") == kept_expected
    assert _count(client, f"SELECT count() FROM {db}.stock_categories") == 1  # untouched

    again = cleaner.clean_table(s.cleanup_targets[0])
    assert again.status == "NOOP" and again.rows_kept_after == kept_expected


def test_ch_baseline_guard_extends_cutoff(ch):
    client, db, s = ch
    _load_prices(client, db, ["A.NS"], date(2026, 9, 1), date(2026, 10, 19))
    # C.NS last traded 2026-09-15 before the month, then again in October
    _load_prices(client, db, ["C.NS"], date(2026, 9, 1), date(2026, 9, 15))
    _load_prices(client, db, ["C.NS"], date(2026, 10, 5), date(2026, 10, 19))
    res = MTDCleaner(s, client, date(2026, 10, 19)).clean_table(s.cleanup_targets[0])
    assert res.status == "CLEANED" and res.cutoff == "2026-09-15"
    assert _count(client, f"SELECT max(trade_date) FROM {db}.daily_stock_prices "
                          "WHERE symbol='C.NS' AND trade_date < '2026-10-01'") == date(2026, 9, 15)


def test_ch_rankings_unpartitioned_lightweight_delete_on_new_month(ch):
    client, db, s = ch
    rows = [(d, "X.NS", "Large-cap", 1.0, 2.0, "GAINER") for d in _days(date(2026, 10, 1), date(2026, 10, 30))]
    rows += [(date(2026, 11, 2), "X.NS", "Large-cap", 1.0, 2.0, "GAINER")]
    client.insert(f"{db}.daily_mtd_rankings", rows, column_names=[
        "trade_date", "symbol", "category", "close_price", "mtd_change_pct", "ranking_type"])
    res = MTDCleaner(s, client, date(2026, 11, 2)).clean_table(s.cleanup_targets[1])
    assert res.status == "CLEANED" and res.strategy == "lightweight_delete" and res.cutoff == "2026-11-01"
    assert res.rows_old_before == 30 and res.rows_kept_after == 1
    assert _count(client, f"SELECT count() FROM {db}.daily_mtd_rankings") == 1


def test_ch_dry_run_deletes_nothing(ch):
    client, db, s = ch
    _load_prices(client, db, ["A.NS"], date(2026, 8, 1), date(2026, 10, 19))
    before = _count(client, f"SELECT count() FROM {db}.daily_stock_prices")
    res = MTDCleaner(s, client, date(2026, 10, 19), dry_run=True).clean_table(s.cleanup_targets[0])
    assert res.status == "DRY_RUN" and res.rows_old_before > 0
    assert _count(client, f"SELECT count() FROM {db}.daily_stock_prices") == before


def test_ch_full_run_with_clock_check(ch):
    client, db, s = ch
    _load_prices(client, db, ["A.NS"], date(2026, 9, 1), local_today("Asia/Kolkata"))
    results = MTDCleaner(s, client, local_today("Asia/Kolkata")).run()
    assert all(r.status in ("CLEANED", "NOOP") for r in results), [r.error for r in results]


def test_ch_disallowed_database_never_touched(ch, settings_factory):
    client, db, _ = ch
    s = settings_factory(allowed=("nse_alerts",), targets=[
        {"table": "nse_alerts.daily_mtd_rankings", "date_column": "trade_date"}])
    object.__setattr__(s, "cleanup_allowed_databases", ("somewhere_else",))
    res = MTDCleaner(s, client, date(2026, 10, 19)).clean_table(s.cleanup_targets[0])
    assert res.status == "ERROR" and "allowed" in res.error
