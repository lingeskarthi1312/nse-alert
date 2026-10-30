"""State directory kept in PostgreSQL (table app_files) for cloud runs.

GitHub Actions starts every run on a fresh machine, so `cloud-tick` pulls the
state files (scheduler.db with the job history, the *.json snapshots) before
running due jobs and pushes them back afterwards. The dashboard on Streamlit
Community Cloud reads the same snapshots with read_json().
"""
import json
import sqlite3

import psycopg

SYNCED_SUFFIXES = (".json", ".db", ".txt")


def _synced(path):
    return path.is_file() and path.suffix in SYNCED_SUFFIXES and not path.name.endswith(".tmp")


def pull(settings):
    from .pgdb import service_tx
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    with service_tx(settings) as conn:
        rows = conn.execute("SELECT name, data FROM app_files").fetchall()
    for r in rows:
        (settings.state_dir / r["name"]).write_bytes(bytes(r["data"]))
    return [r["name"] for r in rows]


def push(settings):
    db = settings.scheduler_db
    if db.exists():  # fold any WAL into the main file before copying it
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    files = sorted(p for p in settings.state_dir.iterdir() if _synced(p))
    return push_files(settings, files)


def push_files(settings, files):
    """Upload just these state files (the live loop must not overwrite scheduler.db)."""
    from .pgdb import service_tx
    with service_tx(settings) as conn:
        for p in files:
            conn.execute("INSERT INTO app_files (name, data, updated_at) VALUES (%s, %s, now()) "
                         "ON CONFLICT (name) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                         (p.name, p.read_bytes()))
    return [p.name for p in files]


def read_bytes(settings, name, role="app"):
    from .pgdb import _connect
    with _connect(settings, role) as conn:
        row = conn.execute("SELECT data FROM app_files WHERE name = %s", (name,)).fetchone()
    return bytes(row["data"]) if row else None


def read_json(settings, name):
    """JSON snapshot from app_files (state_sync) or the local state dir."""
    try:
        if settings.state_sync:
            raw = read_bytes(settings, name)
            return json.loads(raw) if raw else None
        return json.loads((settings.state_dir / name).read_text())
    except (OSError, ValueError, psycopg.Error):
        return None
