"""Internet connectivity probe: TCP connect to well-known endpoints the jobs
actually use (Meta Graph API, Yahoo Finance) plus a public anycast IP."""
import logging
import socket

from .logging_setup import log_event

log = logging.getLogger("alert_system.connectivity")


def _probe(host_port, timeout):
    host, _, port = host_port.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=timeout):
        return True


def is_online(hosts, timeout=5.0):
    errors = []
    for hp in hosts:
        try:
            if _probe(hp, timeout):
                return True
        except OSError as exc:
            errors.append(f"{hp}: {type(exc).__name__}")
    log_event(log, logging.DEBUG, "connectivity probe failed", component="connectivity", errors=errors)
    return False


class ConnectivityMonitor:
    """Caches the probe result briefly and logs only on state changes."""

    def __init__(self, settings, probe=is_online, clock=None):
        import time
        self.settings = settings
        self.probe = probe
        self.clock = clock or time.monotonic
        self._state = None
        self._checked_at = None

    def online(self, max_age=15.0):
        now = self.clock()
        if self._checked_at is not None and now - self._checked_at < max_age:
            return self._state
        state = self.probe(self.settings.connectivity_hosts, self.settings.connectivity_timeout)
        self._checked_at = now
        if state != self._state:
            log_event(log, logging.INFO if state else logging.WARNING,
                      "internet connectivity available" if state else "internet connectivity lost",
                      component="connectivity", online=state,
                      retry_seconds=None if state else self.settings.connectivity_retry_seconds)
        self._state = state
        return state
