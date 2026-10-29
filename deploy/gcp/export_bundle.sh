#!/bin/bash
# Build the migration bundle on the laptop (run inside WSL as root):
#   wsl -u root -- bash /mnt/e/alert/deploy/gcp/export_bundle.sh
# Output: E:\alert\deploy\gcp\out\nse-alert-bundle.tar.gz
# Contains: code, config, .env (secrets incl. PostgreSQL role passwords!), the
# PostgreSQL app data (users with password hashes, holdings, rules, alerts,
# email history), a consistent copy of the legacy holdings.db, the nse_alerts
# ClickHouse schema + data (read-only export), latest state files.
# Nothing on the laptop is modified.
set -euo pipefail

APP=/mnt/e/alert
PY=/root/nse-alert-venv/bin/python
OUT=$APP/deploy/gcp/out
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
B=$WORK/bundle
mkdir -p "$B/data/clickhouse" "$B/state" "$OUT"

echo "== code"
( cd "$APP" && tar -cf - \
    --exclude='*.bak*' --exclude='*.backup*' --exclude='*.before_*' --exclude='__pycache__' \
    --exclude='deploy/gcp/out' \
    alert_system config deploy sql tests requirements.txt AUTOMATION.md .env.example \
    $(ls *.py | grep -v -E '\.bak|\.backup') ) | tar -xf - -C "$B"
cp "$APP/.env" "$B/.env"
# ngrok authtoken, so the server keeps the same public link
[ -f /root/.config/ngrok/ngrok.yml ] && cp /root/.config/ngrok/ngrok.yml "$B/ngrok.yml"

echo "== holdings.db (sqlite online backup = consistent copy)"
"$PY" - "$APP/holdings.db" "$B/data/holdings.db" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
print("tables:", [r[0] for r in dst.execute("select name from sqlite_master where type='table'")])
EOF

echo "== ClickHouse nse_alerts (schema + data)"
for t in $(clickhouse-client -q "SHOW TABLES FROM nse_alerts"); do
  clickhouse-client -q "SHOW CREATE TABLE nse_alerts.$t" --format TSVRaw > "$B/data/clickhouse/$t.sql"
  clickhouse-client -q "SELECT * FROM nse_alerts.$t FORMAT Native" > "$B/data/clickhouse/$t.native"
  echo "  $t: $(clickhouse-client -q "SELECT count() FROM nse_alerts.$t") rows"
done

echo "== PostgreSQL app data (users, holdings, rules, alerts, emails) - data only"
sudo -u postgres pg_dump --data-only --format=custom --exclude-table=schema_version nse_app > "$B/data/app_db.dump"
echo "  users: $(sudo -u postgres psql -tAc 'SELECT count(*) FROM users' nse_app)"

echo "== state"
for f in recommendations_latest.json backtest_latest.json last_cleanup.json; do
  [ -f "$APP/state/$f" ] && cp "$APP/state/$f" "$B/state/"
done

tar -czf "$OUT/nse-alert-bundle.tar.gz" -C "$WORK" bundle
chmod 600 "$OUT/nse-alert-bundle.tar.gz"
ls -la "$OUT/nse-alert-bundle.tar.gz"
echo "Bundle contains your .env secrets: upload it only to your own server and delete it afterwards."
