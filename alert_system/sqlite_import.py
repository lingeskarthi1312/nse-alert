"""One-time import of the single-user holdings.db into PostgreSQL.

Everything is assigned to one admin account (created if missing). The
SQLite file is only read, never modified, so it remains a backup. Running
again is a no-op once the admin already owns imported data.
"""
import sqlite3
from datetime import datetime, timezone

from .auth import hash_password, temp_password
from .pgdb import service_tx


def _ts(text):
    if not text:
        return None
    t = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(t)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rows(db, sql):
    try:
        return [dict(r) for r in db.execute(sql)]
    except sqlite3.OperationalError:
        return []  # table does not exist in this holdings.db


def import_sqlite(settings, admin_email, admin_name="Admin"):
    """Returns (summary dict, temp_password or None if the admin existed)."""
    src = sqlite3.connect(f"file:{settings.holdings_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    pw = None
    summary = {}
    try:
        with service_tx(settings) as pg:
            admin = pg.execute("SELECT id FROM users WHERE lower(email)=lower(%s)", (admin_email,)).fetchone()
            if admin is None:
                pw = temp_password()
                admin = pg.execute("INSERT INTO users (email, name, password_hash, role) VALUES (lower(%s), %s, %s, "
                                   "'admin') RETURNING id", (admin_email, admin_name, hash_password(pw))).fetchone()
            uid = admin["id"]
            done = pg.execute("SELECT 1 FROM delivery_markers WHERE user_id=%s AND kind='migration' AND ref='sqlite'",
                              (uid,)).fetchone()
            if done:
                return {"skipped": "already imported"}, pw
            hold = _rows(src, "SELECT * FROM holdings")
            for h in hold:
                pg.execute("INSERT INTO holdings (user_id, symbol, buy_date, buy_price, quantity, notes, status, "
                           "sell_date, sell_price) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                           (uid, h["symbol"], h["buy_date"], h["buy_price"], h["quantity"], h.get("notes") or "",
                            h["status"], h.get("sell_date"), h.get("sell_price")))
            rules = _rows(src, "SELECT * FROM user_alerts")
            for r in rules:
                pg.execute("INSERT INTO user_alerts (user_id, symbol, metric, condition, threshold, enabled, created_at) "
                           "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                           (uid, r["symbol"], r["metric"], r["condition"], r["threshold"], bool(r["enabled"]),
                            _ts(r["created_at"]) or datetime.now(timezone.utc)))
            hist = _rows(src, "SELECT * FROM alert_history")
            for a in hist:
                pg.execute("INSERT INTO alert_history (user_id, symbol, metric, condition, threshold, triggered_value, "
                           "triggered_at, status, trigger_price, trigger_session) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                           (uid, a["symbol"], a["metric"], a["condition"], a["threshold"], a.get("triggered_value"),
                            _ts(a["triggered_at"]), a["status"], a.get("trigger_price"), a.get("trigger_session")))
            outbox = _rows(src, "SELECT * FROM notification_outbox")
            for o in outbox:
                pg.execute("INSERT INTO notification_outbox (user_id, group_key, channel, recipient, subject, body, "
                           "body_html, dedup_key, status, attempts, max_attempts, next_attempt_at, send_mode, parts_sent, "
                           "provider_message_ids, last_error, created_at, updated_at, sent_at) VALUES "
                           "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                           (uid if o["channel"] == "email" and not o["group_key"].startswith("ops:") else None,
                            o["group_key"], o["channel"], o["recipient"], o.get("subject"), o["body"], o.get("body_html"),
                            o.get("dedup_key"), o["status"], o["attempts"], o["max_attempts"], _ts(o["next_attempt_at"]),
                            o.get("send_mode"), o.get("parts_sent") or 0, o.get("provider_message_ids"),
                            o.get("last_error"), _ts(o["created_at"]), _ts(o["updated_at"]), _ts(o.get("sent_at"))))
            mtd = _rows(src, "SELECT * FROM mtd_email_delivery")
            for m in mtd:
                pg.execute("INSERT INTO delivery_markers (user_id, kind, ref, status) VALUES (%s,'mtd_rankings',%s,%s) "
                           "ON CONFLICT DO NOTHING", (uid, m["snapshot_date"], m["status"]))
            pg.execute("INSERT INTO delivery_markers (user_id, kind, ref, status) VALUES (%s,'migration','sqlite','DONE')",
                       (uid,))
            summary = {"admin": admin_email, "holdings": len(hold), "alert_rules": len(rules),
                       "alert_history": len(hist), "outbox": len(outbox), "mtd_markers": len(mtd)}
    finally:
        src.close()
    return summary, pw
