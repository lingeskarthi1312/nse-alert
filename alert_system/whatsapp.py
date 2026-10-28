"""WhatsApp Business Platform (Meta Cloud API) client.

Same endpoint and payload the project already used
(POST {base}/{version}/{phone_number_id}/messages, type=text), plus:
  * timeouts and error classification (retryable vs permanent);
  * message splitting at the 4096-char text limit;
  * optional approved-template sending, needed for business-initiated
    messages outside the 24-hour customer-service window.
"""
import re

import requests

TEXT_LIMIT = 4096

# Graph API error codes that will not succeed on retry without a code/config
# change (invalid parameter, recipient not on WhatsApp, template issues...).
PERMANENT_CODES = {100, 131008, 131009, 131021, 131026, 131051,
                   132000, 132001, 132005, 132007, 132012, 132015, 132016}
# "Re-engagement message": >24h since the recipient last messaged the
# business number; only an approved template can be delivered.
REENGAGEMENT_CODES = {131047, 470}


class WhatsAppError(Exception):
    def __init__(self, message, retryable, code=None, http_status=None):
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.http_status = http_status

    @property
    def needs_template(self):
        return self.code in REENGAGEMENT_CODES


def split_message(text, limit=TEXT_LIMIT):
    """Split on line boundaries into chunks <= limit characters."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return parts


def template_parameter(text, max_chars):
    """Template body parameters may not contain newlines/tabs or >4 spaces."""
    flat = re.sub(r"\s*\n\s*", " | ", text.strip())
    flat = re.sub(r"[\t ]{2,}", " ", flat)
    return flat if len(flat) <= max_chars else flat[: max_chars - 3] + "..."


class WhatsAppClient:
    def __init__(self, settings, session=None):
        self.cfg = settings
        self.session = session or requests.Session()

    @property
    def url(self):
        return f"{self.cfg.base_url}/{self.cfg.api_version}/{self.cfg.phone_number_id}/messages"

    def _post(self, payload, recipient):
        if not (self.cfg.access_token and self.cfg.phone_number_id):
            raise WhatsAppError("WhatsApp token or phone number id missing in configuration", retryable=False)
        headers = {"Authorization": f"Bearer {self.cfg.access_token}", "Content-Type": "application/json"}
        try:
            resp = self.session.post(self.url, headers=headers, json=payload, timeout=self.cfg.timeout_seconds)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise WhatsAppError(f"network error: {type(exc).__name__}", retryable=True) from exc
        except requests.RequestException as exc:
            raise WhatsAppError(f"request error: {type(exc).__name__}", retryable=True) from exc
        if resp.ok:
            try:
                messages = resp.json().get("messages", [])
            except ValueError:
                messages = []
            return messages[0].get("id") if messages else None
        code, detail = None, resp.text[:300]
        try:
            err = resp.json().get("error", {})
            code = err.get("code")
            detail = f"{err.get('type', '')} {err.get('message', '')}".strip()[:300]
            sub = (err.get("error_data") or {}).get("details")
            if sub:
                detail += f" ({str(sub)[:150]})"
        except ValueError:
            pass
        retryable = resp.status_code >= 500 or resp.status_code == 429 or code not in PERMANENT_CODES
        raise WhatsAppError(
            f"HTTP {resp.status_code} code={code} for recipient ending {recipient[-4:]}: {detail}",
            retryable=retryable, code=code, http_status=resp.status_code)

    def send_text(self, recipient, body):
        return self._post({"messaging_product": "whatsapp", "recipient_type": "individual",
                           "to": recipient, "type": "text", "text": {"body": body}}, recipient)

    def send_template(self, recipient, body):
        param = template_parameter(body, self.cfg.template_param_max_chars)
        return self._post({
            "messaging_product": "whatsapp", "recipient_type": "individual", "to": recipient,
            "type": "template",
            "template": {"name": self.cfg.template_name,
                         "language": {"code": self.cfg.template_language},
                         "components": [{"type": "body", "parameters": [{"type": "text", "text": param}]}]},
        }, recipient)

    def send_part(self, recipient, body, mode=None):
        """Send one part. Returns (message_id, mode_used)."""
        mode = mode or self.cfg.mode
        if mode == "template":
            return self.send_template(recipient, body), "template"
        try:
            return self.send_text(recipient, body), "text"
        except WhatsAppError as exc:
            if mode == "auto" and exc.needs_template and self.cfg.template_name:
                return self.send_template(recipient, body), "template"
            raise

    def parts_for(self, body, mode=None):
        """Templates carry the whole message in one parameter; text is split."""
        if (mode or self.cfg.mode) == "template":
            return [body]
        return split_message(body)
