import os
import time
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

from alert_system.clickhouse import get_client
from alert_system.config import load_settings
from alert_system.timeutil import local_today, price_window_start

SETTINGS = load_settings()
TODAY = local_today(SETTINGS.timezone)
# Import window = MTD retention window: the current month plus the short
# pre-month baseline the MTD % change needs. It must match the cleanup cutoff
# (config/alert_system.json) or cleaned rows would be re-imported.
# PRICE_IMPORT_START_DATE overrides it for one-off backfills.
START_DATE = os.getenv("PRICE_IMPORT_START_DATE") or price_window_start(
    TODAY, SETTINGS.price_lookback_days
).isoformat()
# Exclusive end. Today's bar is included only once the NSE session is over
# (15:45 IST, allowing for the feed delay); during market hours it would be a
# partial, still-changing bar, so it is skipped until the next run.
_NOW_LOCAL = datetime.now(ZoneInfo(SETTINGS.timezone))
SESSION_CLOSED = _NOW_LOCAL.weekday() >= 5 or _NOW_LOCAL.time() >= dtime(15, 45)
END_DATE = (TODAY + timedelta(days=1) if SESSION_CLOSED else TODAY).isoformat()
# Incremental + batched: each symbol is only fetched from the day after its
# latest stored trade_date, and up to BATCH_SIZE tickers are requested in one
# Yahoo call (instead of one call per symbol).
BATCH_SIZE = 100
BATCH_RETRIES = 2
DRY_RUN = False  # Review first; no inserts while True


def number(value):
    try:
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError, OverflowError):
        return None


def weekdays_between(start, end):
    """Mon-Fri dates in [start, end)."""
    d, n = start, 0
    while d < end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def download_batch(tickers, start, end):
    """One Yahoo request for many tickers. Returns {symbol: DataFrame}."""
    for attempt in range(1, BATCH_RETRIES + 1):
        try:
            data = yf.download(
                tickers,
                start=start,
                end=end,
                auto_adjust=False,
                progress=False,
                threads=True,
                group_by="ticker",
                timeout=30,
            )
            break
        except Exception as exc:
            if attempt == BATCH_RETRIES:
                raise
            print(f"Batch download failed ({type(exc).__name__}); retrying")
            time.sleep(5 * attempt)

    if data.empty:
        return {}
    if getattr(data.columns, "nlevels", 1) == 1:
        return {tickers[0]: data}
    present = set(data.columns.get_level_values(0))
    return {t: data[t] for t in tickers if t in present}


def main():
    started = time.monotonic()
    client = get_client()
    try:
        symbols = [
            row[0] for row in client.query("""
                SELECT DISTINCT symbol
                FROM nse_alerts.stock_categories
                ORDER BY symbol
            """).result_rows
        ]

        existing = set(client.query(f"""
            SELECT symbol, trade_date
            FROM nse_alerts.daily_stock_prices
            WHERE trade_date >= toDate('{START_DATE}')
              AND trade_date < toDate('{END_DATE}')
        """).result_rows)

        latest = {}
        for symbol, trade_date in existing:
            if trade_date > latest.get(symbol, date.min):
                latest[symbol] = trade_date

        # Group symbols by the first date they still need.
        window_start = date.fromisoformat(START_DATE)
        end = date.fromisoformat(END_DATE)
        by_start = {}
        for symbol in symbols:
            need_from = (latest[symbol] + timedelta(days=1)) if symbol in latest else window_start
            if need_from < end:
                by_start.setdefault(need_from, []).append(symbol)

        to_fetch = sum(len(v) for v in by_start.values())
        print(f"Unique symbols: {len(symbols)}")
        print(f"Existing price keys in date range: {len(existing)}")
        print(f"Symbols needing data: {to_fetch} "
              f"(already up to date: {len(symbols) - to_fetch})")

        records = []
        no_data = []
        errors = []
        weekday_requests = 0

        for need_from, group in sorted(by_start.items()):
            if not weekdays_between(need_from, end):
                print(f"{len(group)} symbols only miss weekend dates; nothing to fetch")
                continue
            weekday_requests += len(group)
            for start in range(0, len(group), BATCH_SIZE):
                batch = group[start:start + BATCH_SIZE]
                print(f"Batch {need_from} -> {END_DATE}: {len(batch)} symbols")
                try:
                    frames = download_batch(batch, need_from.isoformat(), END_DATE)
                except Exception as exc:
                    errors.extend((s, type(exc).__name__) for s in batch)
                    print(f"Batch ERROR {type(exc).__name__}: {str(exc)[:200]}")
                    continue

                for symbol in batch:
                    frame = frames.get(symbol)
                    added = 0
                    if frame is not None:
                        for timestamp, row in frame.iterrows():
                            trade_date = timestamp.date()
                            if (symbol, trade_date) in existing:
                                continue
                            ohlc = [number(row.get(k)) for k in ("Open", "High", "Low", "Close")]
                            if all(v is None for v in ohlc):
                                continue  # date belongs to another ticker in the batch
                            volume_value = number(row.get("Volume"))
                            volume = (
                                int(volume_value)
                                if volume_value is not None and volume_value >= 0
                                else None
                            )
                            records.append((symbol, trade_date, *ohlc, volume))
                            existing.add((symbol, trade_date))
                            added += 1
                    if not added:
                        no_data.append(symbol)

        print("\n--- LOAD PREVIEW ---")
        print("Symbols:", len(symbols))
        print("New price rows prepared:", len(records))
        print("No new data:", len(no_data), no_data[:15])
        print("Errors:", len(errors), errors[:15])
        print("DRY_RUN:", DRY_RUN)
        print(f"Download time: {time.monotonic() - started:.1f}s")

        for row in records[:10]:
            print(row)

        if DRY_RUN:
            print("DRY RUN: no ClickHouse price inserts performed.")
            return

        # Report failure (so the scheduler retries) when downloads errored
        # for every symbol, or when the requested range spans 2+ weekdays and
        # Yahoo returned nothing at all (e.g. internet dropped mid-run).
        # Weekends and single-day holidays legitimately return no rows.
        if weekday_requests and len(errors) == weekday_requests:
            raise RuntimeError(f"Price download failed for all {weekday_requests} symbols")
        if weekday_requests and not records and all(
            weekdays_between(d, end) >= 2 for d in by_start
        ):
            raise RuntimeError(
                "No price data returned for a range covering 2+ weekdays"
            )

        if records:
            client.insert(
                "nse_alerts.daily_stock_prices",
                records,
                column_names=[
                    "symbol", "trade_date", "open_price",
                    "high_price", "low_price", "close_price", "volume",
                ],
            )
            print(f"Inserted {len(records)} new price rows.")
        else:
            print("No new price rows to insert.")

    finally:
        client.close()


if __name__ == "__main__":
    main()
