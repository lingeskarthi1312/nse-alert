"""PostgreSQL access with row-level security.

  user_tx(settings, user_id)  dashboard connection (role nse_app). The user id
                              is set for the transaction only
                              (set_config(..., is_local=true)), so RLS policies
                              restrict every statement to that user's rows.
  service_tx(settings)        background jobs (role nse_service, BYPASSRLS).
  owner_connect(settings)     migrations / admin CLI (role nse_owner).
"""
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SQL_DIR = Path(__file__).resolve().parent.parent / "sql" / "postgres"


def _connect(settings, role, autocommit=False, dbname=None):
    pg = settings.pg
    user, password = {"app": (pg.app_user, pg.app_password), "service": (pg.service_user, pg.service_password),
                      "owner": (pg.owner_user, pg.owner_password)}[role]
    if not password:
        raise RuntimeError(f"PostgreSQL password for role {user} missing (PG_*_PASSWORD in .env)")
    return psycopg.connect(host=pg.host, port=pg.port, dbname=dbname or pg.dbname, user=user, password=password,
                           connect_timeout=10, row_factory=dict_row, autocommit=autocommit,
                           application_name=f"nse-alert-{role}")


@contextmanager
def user_tx(settings, user_id):
    if not user_id:
        raise PermissionError("no logged-in user")
    with _connect(settings, "app") as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
            yield conn


@contextmanager
def anon_tx(settings):
    """App role without a user (login only: auth_* functions)."""
    with _connect(settings, "app") as conn:
        with conn.transaction():
            yield conn


@contextmanager
def service_tx(settings):
    with _connect(settings, "service") as conn:
        with conn.transaction():
            yield conn


def owner_connect(settings, autocommit=True, dbname=None):
    return _connect(settings, "owner", autocommit=autocommit, dbname=dbname)


def migrate(settings, dbname=None):
    """Apply sql/postgres/NNN_*.sql files whose version NNN is not yet in
    schema_version, in order, each in its own transaction."""
    applied = []
    with owner_connect(settings, autocommit=False, dbname=dbname) as conn:
        for path in sorted(SQL_DIR.glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            has_table = conn.execute("SELECT to_regclass('public.schema_version') IS NOT NULL AS t").fetchone()["t"]
            if has_table and conn.execute("SELECT 1 FROM schema_version WHERE version=%s", (version,)).fetchone():
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.commit()
            applied.append(path.name)
    return applied


def active_users(conn):
    return conn.execute("SELECT id, email, name, role, prefs FROM users WHERE is_active ORDER BY created_at").fetchall()
