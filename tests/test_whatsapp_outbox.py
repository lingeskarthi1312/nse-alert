import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from alert_system.outbox import Outbox, backoff_seconds, enqueue
from alert_system.whatsapp import WhatsAppClient, WhatsAppError, split_message, template_parameter
from conftest import Clock, utc


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Scripted Graph API: list of (status, payload) or exceptions, consumed in order."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.posts = []

    def post(self, url, headers, json, timeout):
        self.posts.append({"url": url, "json": json, "timeout": timeout, "auth": headers["Authorization"]})
        item = self.script.pop(0) if self.script else (200, {"messages": [{"id": f"wamid.{len(self.posts)}"}]})
        if isinstance(item, Exception):
            raise item
        return FakeResponse(*item)


def err(status, code, msg="error"):
    return (status, {"error": {"code": code, "type": "OAuthException", "message": msg}})


def make_outbox(settings, session, clock=None, failed=None):
    clock = clock or Clock(utc(2026, 10, 19, 5, 0))
    client = WhatsAppClient(settings.whatsapp, session=session)
    hook = (lambda item, error: failed.append((item, error))) if failed is not None else None
    return Outbox(settings, client, clock=clock, on_failed=hook), clock


def test_enqueue_is_idempotent_per_group_key(pg_settings):
    s = pg_settings()
    box, _ = make_outbox(s, FakeSession())
    assert box.enqueue("alert:1", "hello") == 2  # two recipients
    assert box.enqueue("alert:1", "hello again") == 0  # retry/restart: suppressed
    assert box.counts() == {"PENDING": 2}


def test_delivery_success_and_no_resend(pg_settings):
    s = pg_settings()
    session = FakeSession()
    box, _ = make_outbox(s, session)
    box.enqueue("alert:1", "NSE DAILY STOCK ALERT")
    assert box.process_due() == (2, 0)
    assert box.process_due() == (0, 0)
    assert len(session.posts) == 2
    assert session.posts[0]["json"]["type"] == "text"
    assert session.posts[0]["json"]["to"] == "919999900001"
    assert session.posts[0]["timeout"] == 20
    assert box.counts() == {"SENT": 2}


def test_transient_failure_retries_with_backoff(pg_settings):
    s = pg_settings(WHATSAPP_RECIPIENT="+919999900001", WHATSAPP_RETRY_BACKOFF_SECONDS=60)
    session = FakeSession([requests.Timeout(), err(500, 1, "temporarily unavailable")])
    box, clock = make_outbox(s, session)
    box.enqueue("alert:1", "x")
    assert box.process_due() == (0, 1)
    assert box.items()[0].status == "RETRY_SCHEDULED"
    assert box.process_due() == (0, 0)  # not due yet
    clock.advance(seconds=61)
    assert box.process_due() == (0, 1)  # HTTP 500
    clock.advance(seconds=121)
    assert box.process_due() == (1, 0)
    item = box.items()[0]
    assert item.status == "SENT" and item.attempts == 3


def test_permanent_failure_marks_failed_and_alerts(pg_settings):
    s = pg_settings(WHATSAPP_RECIPIENT="+919999900001")
    failed = []
    box, _ = make_outbox(s, FakeSession([err(400, 131026, "undeliverable")]), failed=failed)
    box.enqueue("alert:1", "x")
    box.process_due()
    item = box.items()[0]
    assert item.status == "FAILED" and "131026" in item.last_error
    assert len(failed) == 1


def test_max_attempts_respected(pg_settings):
    s = pg_settings(WHATSAPP_RECIPIENT="+919999900001", WHATSAPP_MAX_ATTEMPTS=3,
                         WHATSAPP_RETRY_BACKOFF_SECONDS=30, WHATSAPP_RETRY_BACKOFF_MAX_SECONDS=30)
    session = FakeSession([err(503, 2)] * 10)
    box, clock = make_outbox(s, session)
    box.enqueue("alert:1", "x")
    for _ in range(10):
        box.process_due()
        clock.advance(seconds=31)
    assert len(session.posts) == 3 and box.items()[0].status == "FAILED"


def test_token_never_logged_in_errors(pg_settings):
    s = pg_settings(WHATSAPP_RECIPIENT="+919999900001")
    token = s.whatsapp.access_token
    box, _ = make_outbox(s, FakeSession([err(401, 190, f"Invalid token {token}")]))
    from alert_system.logging_setup import register_secrets
    register_secrets(s.secrets)
    box.enqueue("alert:1", "x")
    box.process_due()
    assert token not in (box.items()[0].last_error or "")


def test_multipart_resumes_without_resending_sent_parts(pg_settings):
    s = pg_settings(WHATSAPP_RECIPIENT="+919999900001")
    body = "\n".join("line %04d " % i + "x" * 90 for i in range(100))  # ~10k chars -> 3 parts
    parts = split_message(body)
    assert len(parts) == 3 and all(len(p) <= 4096 for p in parts) and "".join(parts) == body
    session = FakeSession([(200, {"messages": [{"id": "m1"}]}), requests.ConnectionError()])
    box, clock = make_outbox(s, session)
    box.enqueue("mtd:2026-10-13", body)
    box.process_due()
    assert box.items()[0].parts_sent == 1
    clock.advance(hours=1)
    box.process_due()
    item = box.items()[0]
    assert item.status == "SENT" and item.parts_sent == 3
    sent_bodies = [p["json"]["text"]["body"] for p in session.posts]
    assert sent_bodies.count(parts[0]) == 1  # part 1 never re-sent


def test_crash_while_sending_not_resent_by_default(pg_settings):
    s = pg_settings(WHATSAPP_RECIPIENT="+919999900001")
    session = FakeSession()
    failed = []
    box, _ = make_outbox(s, session, failed=failed)
    box.enqueue("alert:1", "x")
    box._claim(box.items()[0].id)  # process died after claiming
    box.recover()
    assert box.items()[0].status == "FAILED" and len(failed) == 1
    assert box.process_due() == (0, 0) and session.posts == []
    assert box.requeue(box.items()[0].id)
    assert box.process_due() == (1, 0)


def test_crash_while_sending_resent_when_configured(pg_settings):
    s = pg_settings(WHATSAPP_RECIPIENT="+919999900001", WHATSAPP_RESEND_UNCONFIRMED="true")
    box, _ = make_outbox(s, FakeSession())
    box.enqueue("alert:1", "x")
    box._claim(box.items()[0].id)
    box.recover()
    assert box.process_due() == (1, 0)


def test_auto_mode_falls_back_to_template_outside_24h_window(pg_settings):
    s = pg_settings(WHATSAPP_RECIPIENT="+919999900001", WHATSAPP_SEND_MODE="auto",
                         WHATSAPP_TEMPLATE_NAME="stock_alert")
    session = FakeSession([err(400, 131047, "Re-engagement message")])
    box, _ = make_outbox(s, session)
    box.enqueue("alert:1", "NSE DAILY STOCK ALERT\n\nSymbol: X")
    assert box.process_due() == (1, 0)
    tmpl = session.posts[1]["json"]
    assert tmpl["type"] == "template" and tmpl["template"]["name"] == "stock_alert"
    param = tmpl["template"]["components"][0]["parameters"][0]["text"]
    assert "\n" not in param and param.startswith("NSE DAILY STOCK ALERT | Symbol")


def test_text_mode_reengagement_error_is_retryable_not_template(pg_settings):
    s = pg_settings(WHATSAPP_RECIPIENT="+919999900001")
    box, _ = make_outbox(s, FakeSession([err(400, 131047)]))
    box.enqueue("alert:1", "x")
    box.process_due()
    assert box.items()[0].status == "RETRY_SCHEDULED"


def test_template_parameter_flattening():
    assert template_parameter("a\n\nb\t\tc    d", 100) == "a | b c d"
    assert len(template_parameter("x" * 2000, 1000)) == 1000


def test_backoff_caps():
    assert [backoff_seconds(n, 60, 3600) for n in (1, 2, 3, 7, 8)] == [60, 120, 240, 3600, 3600]


def test_enqueue_without_recipients_raises():
    with pytest.raises(RuntimeError, match="recipients"):
        enqueue(None, "k", "body", (), 3)


def test_system_messages_have_no_user_and_enqueue_joins_caller_transaction(pg_settings):
    from alert_system.pgdb import service_tx
    s = pg_settings()
    assert enqueue(s, "ops:x", "hi", ("911",), 3) == 1
    with service_tx(s) as c:
        assert c.execute("SELECT user_id FROM notification_outbox WHERE group_key='ops:x'").fetchone()["user_id"] is None
    with pytest.raises(ZeroDivisionError):
        with service_tx(s) as c:  # caller's transaction fails -> the queued message is rolled back too
            enqueue(s, "alert:9", "hi", ("911",), 3, conn=c)
            1 / 0
    with service_tx(s) as c:
        assert c.execute("SELECT count(*) AS n FROM notification_outbox WHERE group_key='alert:9'").fetchone()["n"] == 0


# --- integration: real HTTP round trip against a local mock Graph API -----------

class _GraphHandler(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _GraphHandler.received.append((self.path, self.headers.get("Authorization"), body))
        if body["to"].endswith("0002"):
            payload, status = {"error": {"code": 131026, "message": "Message undeliverable"}}, 400
        else:
            payload, status = {"messages": [{"id": "wamid.TEST"}]}, 200
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_integration_mock_graph_api_over_http(pg_settings):
    server = HTTPServer(("127.0.0.1", 0), _GraphHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        s = pg_settings(WHATSAPP_API_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                             WHATSAPP_API_VERSION="v25.0", WHATSAPP_PHONE_NUMBER_ID="555")
        box = Outbox(s, WhatsAppClient(s.whatsapp))
        box.enqueue("alert:42", "NSE DAILY STOCK ALERT")
        assert box.process_due() == (1, 1)
        by_recipient = {i.recipient[-4:]: i for i in box.items()}
        assert by_recipient["0001"].status == "SENT" and "wamid.TEST" in by_recipient["0001"].provider_message_ids
        assert by_recipient["0002"].status == "FAILED"
        path, auth, body = _GraphHandler.received[0]
        assert path == "/v25.0/555/messages" and auth.startswith("Bearer ")
        assert body["messaging_product"] == "whatsapp" and body["text"]["body"] == "NSE DAILY STOCK ALERT"
    finally:
        server.shutdown()


def test_whatsapp_error_classification():
    c = WhatsAppClient.__new__(WhatsAppClient)
    e = WhatsAppError("x", retryable=True, code=131047)
    assert e.needs_template
    assert not WhatsAppError("x", retryable=False, code=100).needs_template
    assert c is not None
