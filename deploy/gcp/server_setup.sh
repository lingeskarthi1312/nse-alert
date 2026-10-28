#!/bin/bash
# One-time (and re-runnable) setup of the NSE alert system on a Google Cloud
# e2-micro or Oracle Cloud Always Free VM (x86 or Ampere ARM) running
# Ubuntu 24.04 LTS. Run on the VM:
#   tar -xzf nse-alert-bundle.tar.gz
#   sudo bash bundle/deploy/gcp/server_setup.sh "$PWD/bundle"
#
# Installs: 4 GB swap, Asia/Kolkata timezone, ClickHouse (low-memory, localhost
# only, no system logs), PostgreSQL (multi-user data with row-level security),
# Python venv, the app in /opt/nse-alert (user nsealert), your data, systemd
# services (scheduler + dashboard), Caddy (HTTPS in front of the dashboard at
# https://<ip>.sslip.io; users sign in with their own email + password).
set -euo pipefail

BUNDLE=${1:?usage: server_setup.sh /path/to/bundle}
APP=/opt/nse-alert
APP_USER=nsealert
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo "== timezone + swap"
timedatectl set-timezone Asia/Kolkata
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -w vm.swappiness=60 >/dev/null
echo 'vm.swappiness=60' > /etc/sysctl.d/99-nse-alert.conf

echo "== packages"
apt-get update -q
apt-get install -y -q python3-venv python3-pip sqlite3 curl ca-certificates gnupg apt-transport-https \
  debian-keyring debian-archive-keyring unattended-upgrades

echo "== ClickHouse"
if ! command -v clickhouse-server >/dev/null; then
  curl -fsSL 'https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key' \
    | gpg --dearmor --yes -o /usr/share/keyrings/clickhouse-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg arch=$(dpkg --print-architecture)] https://packages.clickhouse.com/deb stable main" \
    > /etc/apt/sources.list.d/clickhouse.list
  apt-get update -q
  apt-get install -y -q clickhouse-server clickhouse-client
fi
# memory caps only on small VMs (GCP e2-micro, Oracle E2.1.Micro); the Oracle
# Ampere A1 free shape has 6-24 GB and runs ClickHouse with its defaults
if [ "$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)" -lt 3500 ]; then
  install -m 0644 "$BUNDLE/deploy/gcp/clickhouse-low-memory.xml" /etc/clickhouse-server/config.d/low-memory.xml
  install -m 0644 "$BUNDLE/deploy/gcp/clickhouse-users-low-memory.xml" /etc/clickhouse-server/users.d/low-memory.xml
else
  rm -f /etc/clickhouse-server/config.d/low-memory.xml /etc/clickhouse-server/users.d/low-memory.xml
fi
systemctl enable clickhouse-server
systemctl restart clickhouse-server
for i in $(seq 1 30); do clickhouse-client -q "SELECT 1" >/dev/null 2>&1 && break; sleep 2; done
clickhouse-client -q "SELECT version()"

echo "== application"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/$APP_USER --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP"
# code/config refresh; data files are only copied the first time
( cd "$BUNDLE" && tar -cf - --exclude=data --exclude=state . ) | tar -xf - -C "$APP"
mkdir -p "$APP/state" "$APP/logs"
[ -f "$APP/holdings.db" ] || cp "$BUNDLE/data/holdings.db" "$APP/holdings.db"
for f in "$BUNDLE"/state/*; do [ -e "$f" ] && { [ -f "$APP/state/$(basename "$f")" ] || cp "$f" "$APP/state/"; }; done
chmod 600 "$APP/.env"
chown -R "$APP_USER:$APP_USER" "$APP"

echo "== python venv (several minutes on e2-micro)"
[ -x "$APP/venv/bin/python" ] || sudo -u "$APP_USER" python3 -m venv "$APP/venv"
sudo -u "$APP_USER" "$APP/venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "== ClickHouse data import (only tables that do not exist yet)"
clickhouse-client -q "CREATE DATABASE IF NOT EXISTS nse_alerts"
for sql in "$BUNDLE"/data/clickhouse/*.sql; do
  t=$(basename "$sql" .sql)
  if [ "$(clickhouse-client -q "EXISTS TABLE nse_alerts.$t")" = 1 ]; then
    echo "  $t exists: kept as is"
    continue
  fi
  clickhouse-client --multiquery < "$sql"
  clickhouse-client -q "INSERT INTO nse_alerts.$t FORMAT Native" < "$BUNDLE/data/clickhouse/$t.native"
  echo "  $t: $(clickhouse-client -q "SELECT count() FROM nse_alerts.$t") rows imported"
done

echo "== PostgreSQL (roles, database, RLS schema) + app data"
apt-get install -y -q postgresql postgresql-contrib
systemctl enable --now postgresql
bash "$APP/deploy/postgres_setup.sh" "$APP" "$APP/venv/bin/python"
chown "$APP_USER:$APP_USER" "$APP/.env" && chmod 600 "$APP/.env"
if [ "$(sudo -u postgres psql -tAc 'SELECT count(*) FROM users' nse_app)" = 0 ] && [ -f "$BUNDLE/data/app_db.dump" ]; then
  # via stdin: the postgres user cannot read files inside the (private) home folder
  sudo -u postgres pg_restore --data-only --disable-triggers -d nse_app < "$BUNDLE/data/app_db.dump"
  echo "  restored $(sudo -u postgres psql -tAc 'SELECT count(*) FROM users' nse_app) user(s) with their data"
else
  echo "  users already present (or no dump): kept as is"
fi

echo "== systemd services"
install -m 0644 "$BUNDLE/deploy/gcp/nse-alert.service" /etc/systemd/system/nse-alert.service
install -m 0644 "$BUNDLE/deploy/gcp/nse-dashboard.service" /etc/systemd/system/nse-dashboard.service
systemctl daemon-reload
sudo -u "$APP_USER" bash -c "cd $APP && $APP/venv/bin/python -m alert_system check-config"
systemctl enable nse-alert nse-dashboard
systemctl restart nse-alert nse-dashboard

echo "== Caddy (HTTPS for the dashboard; login is handled by the app)"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi
# Oracle Ubuntu images ship iptables rules that REJECT everything except SSH;
# open 80 (certificate challenge) and 443 and keep them across reboots.
# The VCN security list must allow the same ports (deploy/oracle/README.md).
if [ -f /etc/iptables/rules.v4 ] && iptables -S INPUT | grep -q -- '-j REJECT'; then
  for port in 443 80; do
    iptables -C INPUT -p tcp -m state --state NEW --dport $port -j ACCEPT 2>/dev/null \
      || iptables -I INPUT 5 -p tcp -m state --state NEW --dport $port -j ACCEPT
  done
  netfilter-persistent save
fi
# public IP: Google metadata, else Oracle, else an external lookup
IP=$(curl -fs -m 3 -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip \
  || curl -fs -m 10 https://ifconfig.me || curl -fs -m 10 https://api.ipify.org)
[[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "could not detect the public IP (got '$IP')"; exit 1; }
HOST="${IP//./-}.sslip.io"
cat > /etc/caddy/Caddyfile <<EOF
$HOST {
    header Strict-Transport-Security "max-age=31536000"
    reverse_proxy 127.0.0.1:8501
}
EOF
systemctl enable caddy
systemctl restart caddy

echo "== unattended security updates"
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null

sleep 15
echo
systemctl --no-pager --lines=0 status postgresql clickhouse-server nse-alert nse-dashboard caddy | grep -E '●|Active:'
sudo -u "$APP_USER" bash -c "cd $APP && $APP/venv/bin/python -m alert_system health" || true
free -m | head -2
echo
echo "Dashboard: https://$HOST  (sign in with your email + password; same accounts as on the laptop)"
echo "Users:     sudo -u $APP_USER bash -c 'cd $APP && venv/bin/python -m alert_system users list'"
echo "Status:    sudo -u $APP_USER bash -c 'cd $APP && venv/bin/python -m alert_system status'"
echo "Next: stop the laptop copy so emails are not sent twice (deploy/gcp/README.md, step 6)."
