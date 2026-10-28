"""WhatsApp helpers (WhatsApp is optional; WHATSAPP_ENABLED=false = email only).

queue_whatsapp() stores a message in the persistent outbox (PostgreSQL
notification_outbox) for the configured admin number(s); the alert_system
service delivers it with retries. send_whatsapp() sends immediately (manual use).
"""
from alert_system.config import load_settings
from alert_system.outbox import enqueue
from alert_system.whatsapp import WhatsAppClient


def queue_whatsapp(group_key, message):
    """Queue `message` once per recipient under an idempotency key such as
    'alert:42'. Returns the number of newly queued rows (0 = duplicate)."""
    settings = load_settings()
    if not settings.whatsapp.configured:
        raise RuntimeError("Missing WhatsApp token, phone number ID, or recipients in .env")
    return enqueue(settings, group_key, message, settings.whatsapp.recipients, settings.whatsapp.max_attempts)


def send_whatsapp(message):
    """Send a WhatsApp text message to configured recipients."""
    settings = load_settings()
    if not settings.whatsapp.configured:
        raise RuntimeError("Missing WhatsApp token, phone number ID, or recipients in .env")
    client = WhatsAppClient(settings.whatsapp)
    results = []
    for recipient in settings.whatsapp.recipients:
        for part in client.parts_for(message):
            message_id, _mode = client.send_part(recipient, part)
            results.append({"recipient_suffix": recipient[-4:], "message_id": message_id})
    return results
