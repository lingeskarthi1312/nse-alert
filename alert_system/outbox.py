"""Persistent notification outbox (PostgreSQL table notification_outbox) for
both channels: 'email' (Gmail SMTP) and 'whatsapp' (Meta Cloud API).

Producers *enqueue* (optionally inside their own transaction, so an alert and
its email are committed atomically); the service delivers due items with
bounded exponential backoff, so a mail/WhatsApp outage never blocks scanning,
alert generation or ClickHouse writes.

Every row carries user_id (NULL = system/ops message). Dashboard users can
read only their own rows (RLS) and cannot insert directly; the background
service (BYPASSRLS role) does all delivery.

Duplicate protection:
  * UNIQUE(group_key, channel, recipient): re-running a job re-enqueues the
    same group_key (e.g. 'alert:42', 'report:2026-10-19'), which is ignored.
  * dedup_key + window: the same rule/instrument is not emailed again within
    EMAIL_DEDUP_WINDOW_HOURS even if it reset and re-triggered.
  * Items are claimed as SENDING before the network call; WhatsApp parts_sent
    is persisted after every accepted part.
  * Items found in SENDING after a crash have an unknown outcome; by default
    they are NOT resent (*_RESEND_UNCONFIRMED=false) and are marked FAILED
    for manual review (`python -m alert_system outbox-requeue ID`).
Rows are an audit trail kept NOTIFICATION_AUDIT_RETENTION_DAYS (not MTD).
"""
import json
import logging
import smtplib
from dataclasses import dataclass, replace
from datetime import timedelta

from .logging_setup import log_event, redact
from .pgdb import service_tx
from .timeutil import utcnow
from .whatsapp import WhatsAppError

log = logging.getLogger("alert_system.outbox")
CHANNELS = ("email", "whatsapp")


class EmailError(Exception):
    def __init__(self, message, retryable):
        super().__init__(message)
        self.retryable = retryable
        self.code = None


@dataclass(frozen=True)
class OutboxItem:
    id: int
    group_key: str
    recipient: str
    body: str
    status: str
    attempts: int
    max_attempts: int
    provider_message_ids: str
    parts_sent: int
    last_error: str
    channel: str = "whatsapp"
    subject: str = None
    body_html: str = None
    user_id: object = None


_COLS = ("id, group_key, recipient, body, status, attempts, max_attempts, provider_message_ids, parts_sent, "
         "last_error, channel, subject, body_html, user_id")


def _item(row):
    return OutboxItem(**{k: row[k] for k in OutboxItem.__dataclass_fields__})


def enqueue(settings, group_key, body, recipients, max_attempts, now=None, channel="whatsapp", subject=None,
            dedup_key=None, dedup_window_hours=0, html=None, user_id=None, conn=None):
    """Queue `body` for each recipient. Returns how many rows were new
    (0 = already queued/sent, or suppressed by the dedup window).
    Pass `conn` to enqueue inside the caller's transaction."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    if not recipients:
        raise RuntimeError(f"No {channel} recipients configured")
    if not body.strip():
        raise ValueError("refusing to enqueue an empty message")
    now = now or utcnow()
    if conn is None:
        with service_tx(settings) as c:
            return enqueue(settings, group_key, body, recipients, max_attempts, now, channel, subject, dedup_key,
                           dedup_window_hours, html, user_id, conn=c)
    inserted = suppressed = 0
    window_start = now - timedelta(hours=dedup_window_hours)
    for r in recipients:
        if dedup_key and dedup_window_hours > 0 and conn.execute(
                "SELECT 1 FROM notification_outbox WHERE channel=%s AND recipient=%s AND dedup_key=%s "
                "AND created_at >= %s AND group_key<>%s AND status<>'FAILED' LIMIT 1",
                (channel, r, dedup_key, window_start, group_key)).fetchone():
            suppressed += 1
            continue
        cur = conn.execute(
            "INSERT INTO notification_outbox (user_id, group_key, channel, recipient, body, body_html, subject, "
            "dedup_key, status, max_attempts, next_attempt_at, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (user_id, group_key, channel, r, body, html, subject, dedup_key, max_attempts, now, now, now))
        inserted += cur.rowcount
    log_event(log, logging.INFO, f"{channel} message queued" if inserted else f"{channel} duplicate suppressed",
              component=channel, group_key=group_key, new_rows=inserted, recipients=len(recipients),
              suppressed_by_window=suppressed, user_id=str(user_id) if user_id else None)
    return inserted


def backoff_seconds(attempts, base, cap):
    return min(cap, base * (2 ** max(0, attempts - 1)))


def classify_smtp_error(exc):
    """Permanent: recipient/sender rejected or 5xx data error. Everything
    else (auth, disconnects, timeouts, 4xx) is retried with backoff."""
    if isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused)):
        return False
    if isinstance(exc, smtplib.SMTPDataError) and 500 <= exc.smtp_code < 600:
        return False
    return True


def email_recipients(settings):
    """System recipients (ops alerts): MAIL_RECIPIENT, comma-separated."""
    return tuple(r.strip() for r in settings.email.recipient.split(",") if r.strip())


class Outbox:
    def __init__(self, settings, client, clock=utcnow, on_failed=None, email_sender=None):
        self.settings = settings
        self.client = client
        self.clock = clock
        self.on_failed = on_failed
        if email_sender is None:
            from .notify import send_email as email_sender
        self.email_sender = email_sender

    # -- producers -------------------------------------------------------------
    def enqueue(self, group_key, body, recipients=None):
        """WhatsApp to the configured (system) recipients."""
        return enqueue(self.settings, group_key, body,
                       recipients if recipients is not None else self.settings.whatsapp.recipients,
                       self.settings.whatsapp.max_attempts, self.clock())

    def enqueue_email(self, group_key, subject, body, dedup_key=None, recipients=None, html=None, user_id=None,
                      conn=None):
        rep = self.settings.report
        return enqueue(self.settings, group_key, body,
                       recipients if recipients is not None else email_recipients(self.settings),
                       rep.email_max_attempts, self.clock(), channel="email", subject=subject,
                       dedup_key=dedup_key, dedup_window_hours=rep.dedup_window_hours if dedup_key else 0,
                       html=html, user_id=user_id, conn=conn)

    # -- inspection ----------------------------------------------------------------
    def items(self, statuses=None, limit=50, channel=None):
        sql = f"SELECT {_COLS} FROM notification_outbox WHERE true"
        args = []
        if statuses:
            sql += " AND status = ANY(%s)"
            args.append(list(statuses))
        if channel:
            sql += " AND channel=%s"
            args.append(channel)
        sql += " ORDER BY id DESC LIMIT %s"
        args.append(limit)
        with service_tx(self.settings) as c:
            return [_item(r) for r in c.execute(sql, args).fetchall()]

    def counts(self, channel=None):
        sql = "SELECT status, count(*) AS n FROM notification_outbox"
        args = ()
        if channel:
            sql += " WHERE channel=%s"
            args = (channel,)
        with service_tx(self.settings) as c:
            return {r["status"]: r["n"] for r in c.execute(sql + " GROUP BY status", args)}

    # -- recovery ------------------------------------------------------------------
    def _resend_unconfirmed(self, channel):
        return (self.settings.report.email_resend_unconfirmed if channel == "email"
                else self.settings.whatsapp.resend_unconfirmed)

    def recover(self):
        """Handle SENDING rows left by a crash (outcome unknown)."""
        now = self.clock()
        with service_tx(self.settings) as c:
            rows = [_item(r) for r in c.execute(f"SELECT {_COLS} FROM notification_outbox WHERE status='SENDING'")]
            for item in rows:
                if self._resend_unconfirmed(item.channel):
                    c.execute("UPDATE notification_outbox SET status='RETRY_SCHEDULED', next_attempt_at=%s, "
                              "last_error=%s, updated_at=%s WHERE id=%s",
                              (now, "interrupted while sending; resending (RESEND_UNCONFIRMED=true)", now, item.id))
                else:
                    c.execute("UPDATE notification_outbox SET status='FAILED', last_error=%s, updated_at=%s WHERE id=%s",
                              ("interrupted while sending; delivery unconfirmed so not resent to avoid a duplicate. "
                               "Check your inbox/phone, then `python -m alert_system outbox-requeue "
                               f"{item.id}` if it did not arrive.", now, item.id))
        for item in rows:
            resend = self._resend_unconfirmed(item.channel)
            log_event(log, logging.WARNING, "outbox item recovered after interruption", component=item.channel,
                      outbox_id=item.id, group_key=item.group_key,
                      action="requeued" if resend else "failed_unconfirmed")
            if not resend and self.on_failed:
                self.on_failed(item, "delivery unconfirmed after interruption")
        return rows

    def requeue(self, item_id):
        now = self.clock()
        with service_tx(self.settings) as c:
            return bool(c.execute("UPDATE notification_outbox SET status='RETRY_SCHEDULED', attempts=0, "
                                  "next_attempt_at=%s, updated_at=%s WHERE id=%s AND status='FAILED'",
                                  (now, now, item_id)).rowcount)

    def prune(self, older_than):
        with service_tx(self.settings) as c:
            return c.execute("DELETE FROM notification_outbox WHERE status IN ('SENT','FAILED') AND created_at < %s",
                             (older_than,)).rowcount

    # -- delivery ------------------------------------------------------------------
    def enabled_channels(self):
        chans = []
        if self.settings.email.configured:
            chans.append("email")
        if self.settings.whatsapp.configured and self.client is not None:
            chans.append("whatsapp")
        return chans

    def _claim(self, item_id):
        now = self.clock()
        with service_tx(self.settings) as c:
            row = c.execute("UPDATE notification_outbox SET status='SENDING', attempts=attempts+1, updated_at=%s "
                            "WHERE id=%s AND status IN ('PENDING','RETRY_SCHEDULED') "
                            f"RETURNING {_COLS}", (now, item_id)).fetchone()
            return _item(row) if row else None

    def _update(self, item_id, **fields):
        fields["updated_at"] = self.clock()
        cols = ", ".join(f"{k}=%s" for k in fields)
        with service_tx(self.settings) as c:
            c.execute(f"UPDATE notification_outbox SET {cols} WHERE id=%s", (*fields.values(), item_id))

    def process_due(self, limit=20, should_stop=lambda: False, channels=None):
        """Deliver due items on enabled channels. Returns (sent, failed_or_retry)."""
        channels = list(channels if channels is not None else self.enabled_channels())
        if not channels:
            return 0, 0
        with service_tx(self.settings) as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM notification_outbox WHERE status IN ('PENDING','RETRY_SCHEDULED') "
                "AND next_attempt_at <= %s AND channel = ANY(%s) ORDER BY id LIMIT %s",
                (self.clock(), channels, limit))]
        sent = failed = 0
        for item_id in ids:
            if should_stop():
                break
            item = self._claim(item_id)
            if item is None:
                continue
            try:
                ok = self._deliver(item)
            except Exception as exc:  # unexpected bug/IO error: never leave the row stuck in SENDING
                self._update(item.id, status="RETRY_SCHEDULED", last_error=redact(repr(exc))[:500],
                             next_attempt_at=self.clock() + timedelta(seconds=self._backoff(item)[0]))
                log.exception("outbox delivery crashed", extra={"fields": {
                    "component": item.channel, "outbox_id": item.id, "status": "RETRY_SCHEDULED"}})
                ok = False
            if ok:
                sent += 1
            else:
                failed += 1
        return sent, failed

    def _backoff(self, item):
        if item.channel == "email":
            rep = self.settings.report
            return rep.email_retry_backoff_seconds, rep.email_retry_backoff_max_seconds
        cfg = self.settings.whatsapp
        return cfg.retry_backoff_seconds, cfg.retry_backoff_max_seconds

    def _send_email(self, item):
        cfg = replace(self.settings.email, recipient=item.recipient)
        try:
            self.email_sender(cfg, item.subject or "NSE alert", item.body, item.body_html)
        except (smtplib.SMTPException, OSError) as exc:
            raise EmailError(f"{type(exc).__name__}: {exc}", classify_smtp_error(exc)) from exc
        return [], "smtp"

    def _send_whatsapp(self, item):
        ids = json.loads(item.provider_message_ids) if item.provider_message_ids else []
        parts = self.client.parts_for(item.body)
        mode_used = None
        for i in range(item.parts_sent, len(parts)):
            message_id, mode_used = self.client.send_part(item.recipient, parts[i])
            ids.append(message_id)
            self._update(item.id, parts_sent=i + 1, provider_message_ids=json.dumps(ids), send_mode=mode_used)
        return ids, mode_used

    def _deliver(self, item):
        try:
            ids, mode_used = self._send_email(item) if item.channel == "email" else self._send_whatsapp(item)
        except (WhatsAppError, EmailError) as exc:
            error = redact(str(exc))[:500]
            final = not exc.retryable or item.attempts >= item.max_attempts
            if final:
                self._update(item.id, status="FAILED", last_error=error)
            else:
                base, cap = self._backoff(item)
                delay = backoff_seconds(item.attempts, base, cap)
                self._update(item.id, status="RETRY_SCHEDULED", last_error=error,
                             next_attempt_at=self.clock() + timedelta(seconds=delay))
            log_event(log, logging.ERROR if final else logging.WARNING,
                      f"{item.channel} delivery failed" if final else f"{item.channel} delivery retry scheduled",
                      component=item.channel, outbox_id=item.id, group_key=item.group_key,
                      recipient_suffix=item.recipient[-4:], attempt=item.attempts, retry_count=item.attempts,
                      max_attempts=item.max_attempts, retryable=exc.retryable, error_code=exc.code,
                      status="FAILED" if final else "RETRY_SCHEDULED", error=error)
            if final and self.on_failed:
                self.on_failed(item, error)
            return False
        self._update(item.id, status="SENT", last_error=None, sent_at=self.clock(), send_mode=mode_used)
        log_event(log, logging.INFO, f"{item.channel} delivered", component=item.channel, outbox_id=item.id,
                  group_key=item.group_key, recipient_suffix=item.recipient[-4:], attempt=item.attempts,
                  status="SENT", message_ids=ids)
        return True
