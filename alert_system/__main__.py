"""Command line entry point.

  python -m alert_system run                 # the long-running service (systemd)
  python -m alert_system check-config        # validate .env + config/alert_system.json
  python -m alert_system status [--limit N]  # recent job runs and outbox
  python -m alert_system health              # exit 0 if the service heartbeat is fresh
  python -m alert_system trigger JOB_ID      # queue a manual run (service executes it)
  python -m alert_system cleanup [--dry-run] # run MTD cleanup now (idempotent)
  python -m alert_system outbox-requeue ID   # retry a FAILED email/WhatsApp item
  python -m alert_system recommend           # run the recommendation engine now (stores snapshot)
  python -m alert_system backtest            # walk-forward backtest -> state/backtest_latest.json
  python -m alert_system report [--preview]  # build the personalised email (preview = print only)
  python -m alert_system live                # one intraday live-alert poll now (market hours only)
"""
import argparse
import json
import sys

from .config import ConfigError, load_settings
from .timeutil import from_iso, utcnow


def _settings_or_exit():
    try:
        return load_settings()
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        sys.exit(2)


def cmd_check_config(settings, args):
    print(f"timezone        : {settings.timezone}")
    print(f"base dir        : {settings.base_dir}")
    print(f"scheduler db    : {settings.scheduler_db}")
    print(f"lock file       : {settings.lock_file}")
    wa_state = ("DISABLED (WHATSAPP_ENABLED=false)" if not settings.whatsapp.enabled
                else "configured" if settings.whatsapp.configured else "NOT configured")
    print(f"whatsapp        : {wa_state}"
          f" (mode={settings.whatsapp.mode}, recipients={len(settings.whatsapp.recipients)})")
    print(f"email (ops)     : {'configured' if settings.email.configured else 'NOT configured'}")
    print(f"clickhouse      : {settings.clickhouse.host}:{settings.clickhouse.port} user={settings.clickhouse.username}")
    rep = settings.report
    print(f"email mode      : {rep.mode} (alert emails={rep.alert_emails}, daily report={rep.daily_report}, "
          f"min severity={rep.min_severity}, dedup window={rep.dedup_window_hours}h)")
    print(f"report sections : top5={rep.include_top5} cap_top5={rep.include_cap_top5} "
          f"cross_asset={rep.include_cross_asset} mtd_rankings_email={rep.include_mtd_rankings_email}")
    print(f"mtd cleanup     : enabled={settings.cleanup_enabled} dry_run={settings.cleanup_dry_run} "
          f"targets={[t.table for t in settings.cleanup_targets]}")
    for job in settings.jobs:
        kind = f"builtin:{job.builtin}" if job.builtin else " && ".join(" ".join(s[1:]) for s in job.steps)
        print(f"job {job.id:<22} {', '.join(job.schedules):<40} max_attempts={job.max_attempts} "
              f"catch_up={job.catch_up} startup={job.run_on_startup} -> {kind}")
    print("OK")
    return 0


def cmd_status(settings, args):
    from .outbox import Outbox
    from .state import JobStore
    store = JobStore(settings.scheduler_db)
    print(f"{'id':>5} {'job':<22} {'status':<16} {'att':>5} {'scheduled_for':<21} {'next_attempt':<21} note/error")
    for r in store.runs(limit=args.limit):
        info = (r.last_error or r.note or "").replace("\n", " ")[:70]
        print(f"{r.id:>5} {r.job_id:<22} {r.status:<16} {r.attempts:>2}/{r.max_attempts:<2} "
              f"{r.scheduled_for:<21} {r.next_attempt_at or '-':<21} {info}")
    outbox = Outbox(settings, client=None)
    print("\nWhatsApp outbox:", outbox.counts() or "empty")
    for item in outbox.items(limit=min(args.limit, 10)):
        print(f"  #{item.id} {item.group_key:<28} ...{item.recipient[-4:]} {item.status:<16} "
              f"att={item.attempts}/{item.max_attempts} {(item.last_error or '')[:80]}")
    if settings.health_file.exists():
        print("\nHealth:", settings.health_file.read_text())
    return 0


def cmd_health(settings, args):
    try:
        data = json.loads(settings.health_file.read_text())
    except (OSError, ValueError) as exc:
        print(f"UNHEALTHY: no health file ({exc})")
        return 1
    age = (utcnow() - from_iso(data["ts"])).total_seconds()
    limit = data.get("tick_seconds", settings.tick_seconds) * 3 + 60
    healthy = age <= limit
    print(f"{'HEALTHY' if healthy else 'UNHEALTHY'}: heartbeat {age:.0f}s old (limit {limit}s); "
          f"online={data.get('online')} current_job={data.get('current_job')} outbox={data.get('outbox')}")
    return 0 if healthy else 1


def cmd_trigger(settings, args):
    from .scheduler import Scheduler
    from .state import JobStore
    if args.job_id not in {j.id for j in settings.jobs}:
        print(f"unknown job {args.job_id}", file=sys.stderr)
        return 2
    scheduler = Scheduler(settings, JobStore(settings.scheduler_db), runners={})
    run_id = scheduler.trigger_manual(args.job_id)
    print(f"queued run {run_id} for {args.job_id}; the running service will execute it within a few seconds")
    return 0


def cmd_cleanup(settings, args):
    from .logging_setup import setup_logging
    from .service import Service
    setup_logging(settings, filename="alert_cli.log")
    svc = Service(settings)
    try:
        summary = svc.mtd_cleanup(None, None, 0, dry_run=args.dry_run or None)
    except Exception as exc:
        print(f"CLEANUP FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, default=str, indent=2))
    return 0


def _service_for_cli(settings):
    from .logging_setup import setup_logging
    from .service import Service
    setup_logging(settings, filename="alert_cli.log", console=False)
    return Service(settings)


def cmd_recommend(settings, args):
    snap = _service_for_cli(settings).recommendations(None, None, 0)
    print(json.dumps({k: snap[k] for k in ("run_date", "market_uptrend", "validation", "universe",
                                           "with_history", "signals")}, indent=2))
    for name, items in snap["rankings"].items():
        print(f"{name:<12} " + (", ".join(f"{a['symbol']}({a['score']})" for a in items) or "-"))
    return 0


def cmd_intraday(settings, args):
    svc = _service_for_cli(settings)
    out = svc.intraday_backtest(None, None, 0) if args.backtest else svc.intraday_reco(None, None, 0)
    if args.backtest:
        print(json.dumps({k: out[k] for k in ("validation", "validation_checks", "period", "overall", "segments",
                                             "by_side", "biases")}, indent=1, default=str))
    else:
        print(f"status={out['status']} data_time={out['data_time']} counts={out['counts']}")
        for side in ("buys", "sells"):
            print(side.upper(), ", ".join(f"{a['symbol']}({a['score']})" for a in out[side]) or "-")
    return 0


def cmd_live(settings, args):
    result = _service_for_cli(settings).intraday_alerts(None, None, 0)
    print(json.dumps(result, indent=1, default=str))
    return 0


def cmd_live_loop(settings, args):
    """Live alerts for the whole market session (GitHub Actions): check the
    rules every --interval seconds and send the emails straight away, until
    the session ends or --max-minutes pass. Only one loop runs at a time;
    meanwhile the scheduler's 30-minute intraday_alerts job stands down."""
    import time
    from zoneinfo import ZoneInfo
    from . import intraday, statesync
    from .logging_setup import setup_logging
    from .service import Service
    setup_logging(settings)
    tz = ZoneInfo(settings.timezone)
    deadline = time.monotonic() + args.max_minutes * 60
    with intraday.live_loop_lock(settings) as held:
        if not held:
            print("another live loop is already running")
            return 0
        svc = Service(settings)
        polls = fired = 0
        while time.monotonic() < deadline:
            local_now = utcnow().astimezone(tz)
            if local_now.weekday() >= 5 or local_now.time() > intraday.POLL_END:
                break
            if local_now.time() >= intraday.SESSION_OPEN:
                try:
                    result = svc.intraday_alerts(None, None, 0)
                    svc._flush_outbox()
                    polls += 1
                    fired += len(result.get("triggered", []))
                    quotes = settings.state_dir / "live_quotes.json"
                    if quotes.exists():
                        statesync.push_files(settings, [quotes])
                except Exception as exc:  # one bad poll (network, Yahoo) must not end the session
                    print(f"poll failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            time.sleep(args.interval)
    print(f"live loop done: {polls} poll(s), {fired} alert(s)")
    return 0


def cmd_backtest(settings, args):
    report = _service_for_cli(settings).backtest(None, None, 0)
    print(json.dumps({k: report[k] for k in ("validation", "validation_checks", "overall", "segments",
                                             "by_category", "by_regime", "biases")}, indent=1, default=str))
    return 0


def cmd_report(settings, args):
    """Preview (default: first admin) or queue the daily report(s)."""
    from .pgdb import service_tx
    user = None
    if args.preview or args.user:
        with service_tx(settings) as conn:
            sql = "SELECT id, email, name, role, prefs FROM users WHERE is_active AND "
            user = (conn.execute(sql + "lower(email)=lower(%s)", (args.user,)).fetchone() if args.user else
                    conn.execute(sql + "role='admin' ORDER BY created_at LIMIT 1").fetchone())
        if user is None:
            print("no such active user", file=sys.stderr)
            return 2
    built = _service_for_cli(settings).daily_report(None, None, 0, send=not args.preview, user=user)
    if not built:
        print("no daily report for this user's notification mode")
        return 0
    for email, (subject, body, html) in built.items():
        print(f"To: {email}\nSubject: {subject}\n\n{body}")
        if args.preview:
            out = settings.state_dir / "report_preview.html"
            out.write_text(html, encoding="utf-8")
            print(f"HTML version (as Gmail shows it) written to {out}")
    print("\n[preview only - not sent]" if args.preview else f"\n[queued for {len(built)} user(s)]")
    return 0


def cmd_db_migrate(settings, args):
    from .pgdb import migrate
    done = migrate(settings)
    print("applied:", ", ".join(done) if done else "nothing (schema up to date)")
    return 0


def cmd_db_import_sqlite(settings, args):
    from .sqlite_import import import_sqlite
    email = args.admin_email or settings.email.recipient.split(",")[0].strip()
    summary, pw = import_sqlite(settings, email, args.name)
    print(json.dumps(summary, indent=1, default=str))
    if pw:
        print(f"\nAdmin account {email} created. One-time temporary password (change it at first login):\n  {pw}")
    return 0


def cmd_users(settings, args):
    """Owner-level user management (no dashboard login needed)."""
    from .auth import hash_password, temp_password
    from .pgdb import service_tx
    with service_tx(settings) as conn:
        if args.action == "list":
            for u in conn.execute("SELECT email, name, role, is_active, must_change_password, failed_logins, "
                                  "last_login_at FROM users ORDER BY created_at"):
                print(f"{u['email']:<35} {u['role']:<7} active={u['is_active']!s:<5} "
                      f"must_change={u['must_change_password']!s:<5} last_login={u['last_login_at']}")
            return 0
        if not args.email:
            print("--email is required", file=sys.stderr)
            return 2
        if args.action == "add":
            pw = temp_password()
            conn.execute("INSERT INTO users (email, name, password_hash, role) VALUES (lower(%s), %s, %s, %s)",
                         (args.email, args.name, hash_password(pw), args.role))
            print(f"created {args.email} ({args.role}); one-time temporary password: {pw}")
        elif args.action == "reset-password":
            pw = temp_password()
            n = conn.execute("UPDATE users SET password_hash=%s, must_change_password=true, failed_logins=0, "
                             "locked_until=NULL WHERE lower(email)=lower(%s)", (hash_password(pw), args.email)).rowcount
            print(f"temporary password for {args.email}: {pw}" if n else "no such user")
        elif args.action in ("deactivate", "activate"):
            n = conn.execute("UPDATE users SET is_active=%s WHERE lower(email)=lower(%s)",
                             (args.action == "activate", args.email)).rowcount
            print(f"{args.action}d {args.email}" if n else "no such user")
    return 0


def cmd_outbox_requeue(settings, args):
    from .outbox import Outbox
    ok = Outbox(settings, client=None).requeue(args.item_id)
    print("requeued" if ok else "not found or not FAILED")
    return 0 if ok else 1


def cmd_run(settings, args):
    from .service import Service
    from .singleton import AlreadyRunning
    try:
        Service(settings).run_forever()
    except AlreadyRunning as exc:
        print(f"already running: {exc}", file=sys.stderr)
        return 3
    return 0


def cmd_cloud_tick(settings, args):
    """One scheduler pass with the state directory kept in PostgreSQL
    (GitHub Actions: fresh machine every run, no long-running service)."""
    from . import statesync
    from .service import Service
    pulled = statesync.pull(settings)
    print(f"state pulled: {', '.join(pulled) or 'none (first run)'}")
    try:
        ran = Service(settings).run_once(budget_seconds=args.budget_minutes * 60)
    finally:
        pushed = statesync.push(settings)
        print(f"state pushed: {', '.join(pushed)}")
    print(f"runs executed: {ran}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m alert_system")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    p = sub.add_parser("cloud-tick")
    p.add_argument("--budget-minutes", type=int, default=25)
    sub.add_parser("check-config")
    p = sub.add_parser("status")
    p.add_argument("--limit", type=int, default=25)
    sub.add_parser("health")
    p = sub.add_parser("trigger")
    p.add_argument("job_id")
    p = sub.add_parser("cleanup")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("outbox-requeue")
    p.add_argument("item_id", type=int)
    sub.add_parser("recommend")
    sub.add_parser("backtest")
    sub.add_parser("live")
    p = sub.add_parser("live-loop")
    p.add_argument("--interval", type=int, default=60, help="seconds between checks")
    p.add_argument("--max-minutes", type=int, default=340, help="stop after this long (GitHub job limit is 6 h)")
    p = sub.add_parser("intraday")
    p.add_argument("--backtest", action="store_true", help="run the 59-day 5-minute backtest instead of a scan")
    sub.add_parser("db-migrate")
    p = sub.add_parser("db-import-sqlite")
    p.add_argument("--admin-email", default=None)
    p.add_argument("--name", default="Admin")
    p = sub.add_parser("users")
    p.add_argument("action", choices=["list", "add", "reset-password", "deactivate", "activate"])
    p.add_argument("--email")
    p.add_argument("--name", default="")
    p.add_argument("--role", choices=["member", "admin"], default="member")
    p = sub.add_parser("report")
    p.add_argument("--preview", action="store_true", help="print the email instead of queueing it")
    p.add_argument("--user", help="email of the user (default for --preview: first admin; else all users)")
    args = parser.parse_args(argv)
    settings = _settings_or_exit()
    handler = {"run": cmd_run, "cloud-tick": cmd_cloud_tick, "check-config": cmd_check_config, "status": cmd_status, "health": cmd_health,
               "trigger": cmd_trigger, "cleanup": cmd_cleanup, "outbox-requeue": cmd_outbox_requeue,
               "recommend": cmd_recommend, "backtest": cmd_backtest, "report": cmd_report,
               "live": cmd_live, "live-loop": cmd_live_loop, "db-migrate": cmd_db_migrate, "db-import-sqlite": cmd_db_import_sqlite,
               "users": cmd_users, "intraday": cmd_intraday}[args.cmd]
    return handler(settings, args)


if __name__ == "__main__":
    sys.exit(main())
