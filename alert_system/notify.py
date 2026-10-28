"""Operational alerts to the owner (job/cleanup/WhatsApp failures).

Email uses the SMTP settings the project already had (MAIL_*), which still
works when WhatsApp itself is the thing that is broken. WhatsApp ops alerts
go through the outbox with an 'ops:' group key. Each alert key is sent at
most once (dedup table ops_alerts in scheduler.db), so a flapping job cannot
spam you.
"""
import logging
import smtplib
import sqlite3
from email.message import EmailMessage

from .logging_setup import log_event, redact
from .timeutil import to_iso, utcnow

log = logging.getLogger("alert_system.notify")


def send_email(email_settings, subject, body, html=None):
    """Plain-text body, plus an HTML alternative (tables) when given."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_settings.sender
    msg["To"] = email_settings.recipient
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(email_settings.smtp_host, email_settings.smtp_port,
                      timeout=email_settings.timeout_seconds) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(email_settings.sender, email_settings.password)
        smtp.send_message(msg)


class OpsNotifier:
    def __init__(self, settings, outbox, clock=utcnow, email_sender=send_email):
        self.settings = settings
        self.outbox = outbox
        self.clock = clock
        self.email_sender = email_sender
        db = sqlite3.connect(settings.scheduler_db, timeout=30)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS ops_alerts (key TEXT PRIMARY KEY, subject TEXT, "
                       "created_at TEXT NOT NULL)")
            db.commit()
        finally:
            db.close()

    def _first_time(self, key, subject):
        db = sqlite3.connect(self.settings.scheduler_db, timeout=30)
        try:
            cur = db.execute("INSERT OR IGNORE INTO ops_alerts (key, subject, created_at) VALUES (?, ?, ?)",
                             (key, subject, to_iso(self.clock())))
            db.commit()
            return bool(cur.rowcount)
        finally:
            db.close()

    def alert(self, key, subject, text, channels=None):
        channels = self.settings.ops_alert_channels if channels is None else channels
        text = redact(text)
        log_event(log, logging.WARNING, "ops alert", component="ops", key=key, subject=subject,
                  channels=list(channels), text=text[:500])
        if not self._first_time(key, subject):
            return False
        self._dispatch(key, subject, text, channels)
        return True

    def _dispatch(self, key, subject, text, channels):
        if "email" in channels:
            if self.settings.email.configured:
                try:
                    self.email_sender(self.settings.email, f"[NSE alert system] {subject}", text)
                    log_event(log, logging.INFO, "ops email sent", component="ops", key=key)
                except (smtplib.SMTPException, OSError) as exc:
                    log_event(log, logging.ERROR, "ops email failed", component="ops", key=key,
                              error=redact(f"{type(exc).__name__}: {exc}")[:300])
            else:
                log_event(log, logging.WARNING, "ops email skipped: MAIL_* not configured", component="ops")
        if "whatsapp" in channels and self.settings.whatsapp.configured:
            try:
                self.outbox.enqueue(f"ops:{key}", f"NSE ALERT SYSTEM: {subject}\n\n{text}"[:3500])
            except (sqlite3.Error, RuntimeError, ValueError) as exc:
                log_event(log, logging.ERROR, "ops whatsapp enqueue failed", component="ops", key=key,
                          error=f"{type(exc).__name__}: {exc}"[:300])

    def summary(self, day_key, subject, text):
        channels = tuple(c for c in self.settings.daily_summary_channels if c != "log")
        if channels and self._first_time(f"summary:{day_key}", subject):
            self._dispatch(f"summary:{day_key}", subject, text, channels)
