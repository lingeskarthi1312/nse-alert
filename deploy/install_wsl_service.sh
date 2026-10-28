#!/bin/bash
# Install/refresh the systemd unit inside WSL (run as root):
#   wsl -d Ubuntu -u root -- bash /mnt/e/alert/deploy/install_wsl_service.sh
# Idempotent. Requires /etc/wsl.conf: [boot] systemd=true
set -euo pipefail

APP=/mnt/e/alert
UNIT=/etc/systemd/system/nse-alert.service
PY=/root/nse-alert-venv/bin/python

grep -q '^systemd=true' /etc/wsl.conf || { echo "enable systemd in /etc/wsl.conf first"; exit 1; }
"$PY" -m alert_system check-config >/dev/null

install -m 0644 "$APP/deploy/nse-alert.service" "$UNIT"
systemctl daemon-reload
systemctl enable clickhouse-server.service nse-alert.service

# The service replaces the old crontab entries; comment out any that are
# still active so jobs cannot run twice.
if crontab -l >/dev/null 2>&1; then
    crontab -l > "$APP/crontab.before_service_$(date +%Y%m%d_%H%M%S)"
    crontab -l | sed -E '/^(#|$|CRON_TZ)/!s|^|# disabled-by-nse-alert-service: |' | crontab -
fi

systemctl restart nse-alert.service
sleep 5
systemctl --no-pager status nse-alert.service | head -15
"$PY" -m alert_system health || true
