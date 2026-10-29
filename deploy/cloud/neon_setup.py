"""Set up the free cloud database (Neon PostgreSQL) and copy the laptop data.

Run in WSL as root, with the Neon owner connection string from the Neon
console (Dashboard -> Connect; role neondb_owner):

  NEON_URL='postgresql://neondb_owner:...@ep-xxx.aws.neon.tech/neondb?sslmode=require' \\
    /root/nse-alert-venv/bin/python deploy/cloud/neon_setup.py

Steps (idempotent, re-runnable):
  1. create roles nse_owner / nse_app (RLS) / nse_service (BYPASSRLS) with
     fresh passwords (kept in deploy/cloud/out/cloud.env, never printed);
  2. apply sql/postgres migrations;
  3. copy users/holdings/rules/alerts/outbox and the market data
     (nse_alerts.*) from the laptop PostgreSQL (only if the cloud users
     table is still empty);
  4. copy the latest JSON snapshots (recommendations, backtest, ...);
  5. write deploy/cloud/out/cloud.env (GitHub secret ENV_FILE) and
     deploy/cloud/out/streamlit_secrets.toml (dashboard: app role only).
"""
import json
import os
import secrets
import string
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from alert_system.config import load_settings  # noqa: E402

OUT = ROOT / "deploy" / "cloud" / "out"
CLOUD_ENV = OUT / "cloud.env"
SNAPSHOTS = ("recommendations_latest.json", "backtest_latest.json", "intraday_latest.json",
             "intraday_backtest.json", "live_quotes.json")
# never given to the dashboard (Streamlit secrets)
DASHBOARD_EXCLUDE = {"PG_OWNER_PASSWORD", "PG_SERVICE_PASSWORD", "MAIL_APP_PASSWORD", "ANTHROPIC_API_KEY",
                     "WHATSAPP_ACCESS_TOKEN"}


def _password():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))


def cloud_env(url):
    """Laptop .env with the database switched to Neon. Existing cloud
    passwords are reused so re-runs don't break a deployed setup."""
    env = {k: v for k, v in dotenv_values(ROOT / ".env").items() if not k.startswith("CLICKHOUSE_")}
    old = dotenv_values(CLOUD_ENV) if CLOUD_ENV.exists() else {}
    env.update({
        "PG_HOST": url.hostname, "PG_PORT": str(url.port or 5432), "PG_DATABASE": url.path.lstrip("/"),
        "PG_SSLMODE": "require", "STATE_SYNC": "pg",
        # GitHub Actions ticks every 30 min and its cron may start late
        "SCHEDULER_MISFIRE_GRACE_SECONDS": "1800",
    })
    for key in ("PG_OWNER_PASSWORD", "PG_APP_PASSWORD", "PG_SERVICE_PASSWORD"):
        env[key] = old.get(key) or _password()
    return env


def create_roles(url, env):
    with psycopg.connect(url.geturl(), autocommit=True) as c:
        for role, attrs, key in (("nse_owner", "NOBYPASSRLS", "PG_OWNER_PASSWORD"),
                                 ("nse_app", "NOBYPASSRLS", "PG_APP_PASSWORD"),
                                 ("nse_service", "BYPASSRLS", "PG_SERVICE_PASSWORD")):
            if not c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                c.execute(f"CREATE ROLE {role} LOGIN")
            c.execute(sql.SQL("ALTER ROLE {} WITH LOGIN {} PASSWORD {}").format(
                sql.Identifier(role), sql.SQL(attrs), sql.Literal(env[key])))
        db = sql.Identifier(env["PG_DATABASE"])
        c.execute(sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO nse_owner").format(db))
        c.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO nse_app, nse_service").format(db))
        c.execute("GRANT USAGE, CREATE ON SCHEMA public TO nse_owner")
    print("roles ready: nse_owner, nse_app, nse_service")


def copy_app_data(env, settings):
    from alert_system.pgdb import service_tx
    with service_tx(settings) as c:
        if c.execute("SELECT count(*) AS n FROM users").fetchone()["n"]:
            print("cloud users already present: app data not copied again")
            return
    dump = subprocess.run(["sudo", "-u", "postgres", "pg_dump", "--data-only", "--format=custom",
                           "--exclude-table=schema_version", "--exclude-table=app_files", "nse_app"],
                          check=True, capture_output=True).stdout
    restore_env = {**os.environ, "PGPASSWORD": env["PG_SERVICE_PASSWORD"], "PGSSLMODE": "require"}
    subprocess.run(["pg_restore", "--data-only", "--no-owner", "--single-transaction",
                    "-h", env["PG_HOST"], "-p", env["PG_PORT"], "-U", "nse_service", "-d", env["PG_DATABASE"]],
                   input=dump, env=restore_env, check=True)
    with service_tx(settings) as c:
        users = c.execute("SELECT count(*) AS n FROM users").fetchone()["n"]
        prices = c.execute("SELECT count(*) AS n FROM nse_alerts.daily_stock_prices").fetchone()["n"]
    print(f"laptop data copied: {users} users, {prices} daily prices")


def push_snapshots(settings):
    from alert_system.pgdb import service_tx
    local = ROOT / "state"
    with service_tx(settings) as c:
        for name in SNAPSHOTS:
            if (local / name).exists():
                c.execute("INSERT INTO app_files (name, data) VALUES (%s, %s) ON CONFLICT (name) "
                          "DO UPDATE SET data = EXCLUDED.data, updated_at = now()", (name, (local / name).read_bytes()))
    print("JSON snapshots copied")


def write_outputs(env):
    OUT.mkdir(parents=True, exist_ok=True)
    CLOUD_ENV.write_text("".join(f"{k}={v}\n" for k, v in env.items() if v is not None))
    toml = "".join(f"{k} = {json.dumps(v)}\n" for k, v in env.items()
                   if v is not None and k not in DASHBOARD_EXCLUDE)
    (OUT / "streamlit_secrets.toml").write_text(toml)
    for p in (CLOUD_ENV, OUT / "streamlit_secrets.toml"):
        p.chmod(0o600)
    print(f"written: {CLOUD_ENV} and {OUT / 'streamlit_secrets.toml'} (secrets - do not share)")


def main():
    raw = os.environ.get("NEON_URL", "")
    url = urlparse(raw)
    if url.scheme not in ("postgres", "postgresql") or not url.hostname:
        sys.exit("set NEON_URL to the Neon owner connection string")
    env = cloud_env(url)
    write_outputs(env)  # first, so passwords survive a failure further down
    create_roles(url, env)
    settings = load_settings(env={**env, "ALERT_BASE_DIR": str(ROOT)})
    from alert_system.pgdb import migrate
    print("migrations applied:", ", ".join(migrate(settings)) or "none (up to date)")
    copy_app_data(env, settings)
    push_snapshots(settings)
    print(f"done. Neon owner user: {unquote(url.username or '')}")


if __name__ == "__main__":
    main()
