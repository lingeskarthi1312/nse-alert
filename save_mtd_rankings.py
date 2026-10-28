from alert_system.clickhouse import get_client
from alert_system.config import load_settings
from alert_system.timeutil import local_today

TODAY = local_today(load_settings().timezone)  # market date (Asia/Kolkata)
AS_OF_DATE = TODAY.isoformat()
MONTH_START = TODAY.replace(day=1).isoformat()

client = get_client()

try:
    # Use the latest available close in the current month and
    # the latest close strictly before the month starts. Today's row is
    # included: load_all_stock_prices.py only stores it after the session
    # closes, and whatsapp_mtd_rankings.py rejects a snapshot older than
    # the latest stored price.
    rows = client.query("""
        WITH prices AS (
            SELECT
                symbol,
                trade_date,
                close_price
            FROM nse_alerts.daily_stock_prices
            WHERE close_price > 0
              AND trade_date <= toDate(%(as_of)s)
        ),
        baseline AS (
            SELECT
                symbol,
                argMax(close_price, trade_date) AS baseline_close
            FROM prices
            WHERE trade_date < toDate(%(month_start)s)
            GROUP BY symbol
        ),
        latest AS (
            SELECT
                symbol,
                argMax(close_price, trade_date) AS latest_close,
                max(trade_date) AS latest_date
            FROM prices
            WHERE trade_date >= toDate(%(month_start)s)
            GROUP BY symbol
        )
        SELECT
            l.symbol,
            c.category,
            l.latest_date,
            l.latest_close,
            b.baseline_close,
            100.0 * (l.latest_close - b.baseline_close)
                / b.baseline_close AS mtd_pct
        FROM latest l
        INNER JOIN baseline b ON l.symbol = b.symbol
        INNER JOIN nse_alerts.stock_categories c ON l.symbol = c.symbol
        WHERE b.baseline_close > 0
    """, parameters={
        "as_of": AS_OF_DATE,
        "month_start": MONTH_START,
    }).result_rows

    if not rows:
        raise RuntimeError("No stocks have both baseline and current-month prices.")

    # Keep one category per symbol in this output. If a symbol appears in
    # multiple universes, prefer the category order shown below.
    category_order = {"Large-cap": 0, "Mid-cap": 1, "Small-cap": 2}
    by_symbol = {}
    for symbol, category, latest_date, latest_close, baseline, pct in rows:
        candidate = (
            symbol, category, latest_date, latest_close, baseline, float(pct)
        )
        if symbol not in by_symbol or category_order.get(category, 99) < \
                category_order.get(by_symbol[symbol][1], 99):
            by_symbol[symbol] = candidate

    rankings = []
    for category in ("Large-cap", "Mid-cap", "Small-cap"):
        group = [r for r in by_symbol.values() if r[1] == category]
        group.sort(key=lambda r: r[5], reverse=True)

        for rank, r in enumerate(group[:25], 1):
            rankings.append((
                r[2], r[0], r[1], r[3], r[5], "GAINER"
            ))

        group.sort(key=lambda r: r[5])
        for rank, r in enumerate(group[:25], 1):
            rankings.append((
                r[2], r[0], r[1], r[3], r[5], "LOSER"
            ))

    print("As-of date:", AS_OF_DATE)
    print("Month start:", MONTH_START)
    print("Stocks with valid baseline and current-month prices:", len(by_symbol))
    print("Ranking rows prepared:", len(rankings))
    print("\nPreview:")
    for row in rankings[:10]:
        print(row)

    # Insert rows for the latest available date per stock. This script
    # appends a snapshot; do not run repeatedly for the same snapshot.
    # Replace the current month's snapshot before inserting fresh results.
    client.command("""
        ALTER TABLE nse_alerts.daily_mtd_rankings
        DELETE WHERE trade_date >= toDate(%(month_start)s)
    """, parameters={"month_start": MONTH_START})

    client.command("OPTIMIZE TABLE nse_alerts.daily_mtd_rankings FINAL")

    client.insert(
        "nse_alerts.daily_mtd_rankings",
        rankings,
        column_names=[
            "trade_date", "symbol", "category", "close_price",
            "mtd_change_pct", "ranking_type",
        ],
    )
    print("\nSaved MTD rankings to ClickHouse.")

    print("\nSaved ranking counts:")
    for row in client.query("""
        SELECT category, ranking_type, count()
        FROM nse_alerts.daily_mtd_rankings
        WHERE trade_date >= toDate(%(month_start)s)
        GROUP BY category, ranking_type
        ORDER BY category, ranking_type
    """, parameters={"month_start": MONTH_START}).result_rows:
        print(row)

finally:
    client.close()
