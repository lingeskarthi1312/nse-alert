"""Month-to-date rankings message (market-wide top/bottom 5 per category).

Email: to every active user whose prefs.mtd_rankings_email is not false,
once per user per snapshot (delivery_markers kind='mtd_rankings').
WhatsApp: to the configured admin number when WHATSAPP_ENABLED=true.
"""
from zoneinfo import ZoneInfo

from alert_system.clickhouse import get_client
from alert_system.config import load_settings
from alert_system.email_format import Cell, pct_cell, render_html, render_text
from alert_system.outbox import enqueue
from alert_system.pgdb import active_users, service_tx

TZ = ZoneInfo("Asia/Kolkata")


def main():
    client = get_client()

    try:
        latest_price = client.query("""
            SELECT max(trade_date)
            FROM nse_alerts.daily_stock_prices
            WHERE close_price IS NOT NULL
        """).result_rows[0][0]

        rows = client.query("""
            SELECT
                trade_date,
                category,
                ranking_type,
                symbol,
                mtd_change_pct
            FROM nse_alerts.daily_mtd_rankings
            WHERE trade_date = (
                SELECT max(trade_date)
                FROM nse_alerts.daily_mtd_rankings
            )
            ORDER BY category, ranking_type, mtd_change_pct
        """).result_rows
    finally:
        client.close()

    if not rows:
        raise RuntimeError("No saved MTD rankings found.")

    snapshot_date = rows[0][0]
    if latest_price is None or snapshot_date < latest_price:
        raise RuntimeError(
            f"Rankings are stale: snapshot={snapshot_date}, "
            f"latest stored price={latest_price}. Refresh rankings first."
        )

    message = [
        "NSE MONTH-TO-DATE RANKINGS",
        f"Snapshot date: {snapshot_date}",
        "",
    ]

    for category in ("Large-cap", "Mid-cap", "Small-cap"):
        message.append(f"=== {category} ===")

        for ranking_type in ("GAINER", "LOSER"):
            selected = [
                r for r in rows
                if r[1] == category and r[2] == ranking_type
            ]

            if ranking_type == "GAINER":
                selected.sort(key=lambda r: r[4], reverse=True)
            else:
                selected.sort(key=lambda r: r[4])

            message.append(f"{ranking_type.title()} — Top 5")
            for rank, row in enumerate(selected[:5], start=1):
                message.append(
                    f"{rank}. {row[3]}: {float(row[4]):+.2f}%"
                )
            message.append("")

    message.append(
        "Based on saved daily closing prices; not live intraday data. "
        "Informational only, not investment advice."
    )
    text = "\n".join(message)

    # Email version: one table per category, gainers and losers side by side.
    blocks = [("title", f"NSE month-to-date rankings - snapshot {snapshot_date}")]
    for category in ("Large-cap", "Mid-cap", "Small-cap"):
        gainers = sorted((r for r in rows if r[1] == category and r[2] == "GAINER"),
                         key=lambda r: r[4], reverse=True)[:5]
        losers = sorted((r for r in rows if r[1] == category and r[2] == "LOSER"),
                        key=lambda r: r[4])[:5]
        table_rows = []
        for i in range(max(len(gainers), len(losers))):
            g = gainers[i] if i < len(gainers) else None
            lo = losers[i] if i < len(losers) else None
            table_rows.append([
                i + 1,
                Cell(g[3].removesuffix(".NS"), bold=True) if g else "",
                pct_cell(float(g[4])) if g else "",
                Cell(lo[3].removesuffix(".NS"), bold=True) if lo else "",
                pct_cell(float(lo[4])) if lo else "",
            ])
        blocks += [("heading", category),
                   ("table", ["#", "Top gainer", "MTD %", "Top loser", "MTD %"], table_rows,
                    ["r", "l", "r", "l", "r"])]
    blocks.append(("note", "Based on saved daily closing prices; not live intraday data. "
                           "Informational only, not investment advice."))
    email_text, email_html = render_text(blocks), render_html(blocks)


    settings = load_settings()
    if not settings.report.include_mtd_rankings_email and not settings.whatsapp.configured:
        print("Rankings delivery disabled (REPORT_MTD_RANKINGS_EMAIL=false, WhatsApp off); snapshot saved only.")
        return
    queued = 0
    with service_tx(settings) as conn:  # markers + queued emails commit together
        if settings.report.include_mtd_rankings_email and settings.email.configured:
            for u in active_users(conn):
                if (u["prefs"] or {}).get("mtd_rankings_email") is False:
                    continue
                done = conn.execute("INSERT INTO delivery_markers (user_id, kind, ref, status) "
                                    "VALUES (%s, 'mtd_rankings', %s, 'QUEUED') ON CONFLICT DO NOTHING",
                                    (u["id"], str(snapshot_date))).rowcount
                if not done:
                    continue  # this user already got this snapshot
                queued += enqueue(settings, f"mtd:{snapshot_date}", email_text, (u["email"],),
                                  settings.report.email_max_attempts, channel="email",
                                  subject=f"NSE MTD Rankings: {snapshot_date}", html=email_html,
                                  user_id=u["id"], conn=conn)
        if settings.whatsapp.configured:
            enqueue(settings, f"mtd:{snapshot_date}", text, settings.whatsapp.recipients,
                    settings.whatsapp.max_attempts, channel="whatsapp", conn=conn)
    print(f"Ranking emails queued for {snapshot_date}: {queued} user(s)"
          if queued else f"Ranking snapshot {snapshot_date} already delivered to all users")


if __name__ == "__main__":
    main()
