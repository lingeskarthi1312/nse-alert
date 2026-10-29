"""One-time copy of the ClickHouse market data (nse_alerts.*) into the
PostgreSQL schema nse_alerts of the database configured in .env (or the
PG_* environment variables). Re-runnable: rows are upserted.

  python deploy/cloud/copy_market_data.py            # ClickHouse at 127.0.0.1:8123
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import clickhouse_connect  # noqa: E402  (only needed for this migration)

from alert_system.clickhouse import TABLE_KEYS, get_client  # noqa: E402
from alert_system.config import load_settings  # noqa: E402

SKIP_COLUMNS = {"recorded_at", "updated_at"}  # PostgreSQL fills these


def copy_all(settings):
    src = clickhouse_connect.get_client(host="127.0.0.1", port=8123, username="default")
    dst = get_client(settings)
    try:
        for table in TABLE_KEYS:
            cols = [c for c in src.query(f"SELECT * FROM {table} LIMIT 0").column_names if c not in SKIP_COLUMNS]
            key = TABLE_KEYS[table]
            # newest version of each key wins (ClickHouse may hold duplicates)
            order = "recorded_at" if table.endswith(("prices", "rankings")) else (
                "updated_at" if table.endswith("categories") else "generated_at")
            rows = src.query(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order}").result_rows
            latest = {tuple(r[cols.index(k)] for k in key): r for r in rows}
            dst.insert(table, list(latest.values()), column_names=cols)
            n = dst.command(f"SELECT count(*) FROM {table}")
            print(f"{table}: {len(rows)} source rows -> {n} rows in PostgreSQL")
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    copy_all(load_settings())
