"""Structured (one JSON object per line) logging with rotation and secret
redaction. Use `log_event(logger, level, event, **fields)` everywhere so job
ids, run ids, statuses and retry counts are machine-searchable."""
import json
import logging
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"((?:access_token|password|passwd|token)=)[^&\s\"']+", re.I),
    re.compile(r"\bEAA[A-Za-z0-9]{20,}"),  # Meta Graph API tokens
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"),
]
_secrets = []


def register_secrets(values):
    for v in values:
        if v and len(v) >= 6 and v not in _secrets:
            _secrets.append(v)


def redact(text):
    text = str(text)
    for s in _secrets:
        text = text.replace(s, "***")
    for p in _PATTERNS:
        text = p.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
    return text


class JsonFormatter(logging.Formatter):
    def __init__(self, tz_name):
        super().__init__()
        self.tz = ZoneInfo(tz_name)

    def format(self, record):
        payload = {
            "ts": datetime.fromtimestamp(record.created, self.tz).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str, ensure_ascii=False))


def log_event(logger, level, event, **fields):
    logger.log(level, event, extra={"fields": fields})


def setup_logging(settings, filename="alert_service.log", console=True):
    register_secrets(settings.secrets)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("alert_system")
    root.setLevel(settings.log_level)
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    fmt = JsonFormatter(settings.timezone)
    fh = RotatingFileHandler(settings.log_dir / filename, maxBytes=settings.log_max_bytes,
                             backupCount=settings.log_backup_count, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    return root
