# NSE alert system: automation, WhatsApp queue and MTD retention

## 1. Architecture

### What already existed (unchanged logic)
| Script | Does | Data |
|---|---|---|
| `load_all_stock_prices.py` | Downloads daily OHLCV from Yahoo Finance for every symbol in `nse_alerts.stock_categories`; inserts only missing `(symbol, trade_date)` keys | ClickHouse `nse_alerts.daily_stock_prices` |
| `scheduled_daily_alerts.py` | Evaluates `user_alerts` (daily % change rules), records `alert_history` (ACTIVE/RESET dedup), emails and WhatsApps new alerts | SQLite `holdings.db` + ClickHouse read |
| `save_mtd_rankings.py` | Top/bottom 25 MTD movers per category (latest close vs last close **before** the 1st) | ClickHouse `nse_alerts.daily_mtd_rankings` |
| `whatsapp_mtd_rankings.py` | Formats the top 5 rankings and sends them on WhatsApp once per snapshot | `whatsapp_mtd_delivery` |
| `whatsapp_notify.py` | Meta WhatsApp Cloud API text send | – |
| `dashboard.py` | Streamlit dashboard (not scheduled) | – |

Runtime: **WSL 2 Ubuntu (systemd enabled)** on Windows 11. ClickHouse 26.7 runs in WSL at 127.0.0.1:8123. Python 3.10 venv: `/root/nse-alert-venv`.

### Issues found (2026-10-19)
1. **The `alert_system/` package source had been deleted.** `nse-alert.service` was still running old code from memory, and every crontab line was disabled. The next reboot or restart would have run nothing. Rebuilt and redeployed.
2. `load_all_stock_prices.py` had a hard-coded `START_DATE = "2026-09-25"`. It didn't follow the month and wasn't aligned with retention.
3. WhatsApp was sent inline. A failed send marked the row PENDING with no backoff and no attempt limit.
4. ClickHouse clients had no timeouts or reconnects. Host, port and user were hard-coded in 4 places.
5. Scripts used `date.today()` with no explicit market timezone (WSL happens to be IST).
6. `.env` `WHATSAPP_RECIPIENT` had a stray trailing `n` (`…254n`). It was fixed; the backup is `state/.env.bak_before_recipient_fix`.
7. No MTD retention existed. A strict month-only cut on prices would break the MTD baseline (the last close before the 1st).

### Service layout (`alert_system/`)
| Module | Responsibility |
|---|---|
| `config.py` | Loads `.env` and `config/alert_system.json`; validates everything at startup |
| `cron.py` | 5-field cron, evaluated in Asia/Kolkata |
| `state.py` | SQLite `state/scheduler.db`: `job_runs`, `job_attempts`, cursor |
| `scheduler.py` | Materialises occurrences, catch-up, retries, supersede |
| `jobs.py` | Runs the existing scripts as subprocesses (timeout, lock, log tail) |
| `whatsapp.py` | Cloud API client: timeouts, error classification, splitting, templates |
| `outbox.py` | Persistent WhatsApp queue (`holdings.db.notification_outbox`) |
| `cleanup.py` | MTD retention for ClickHouse and SQLite alert history |
| `clickhouse.py` | Shared client with timeouts and reconnect |
| `connectivity.py` | Internet probe |
| `notify.py` | Ops alerts by email/WhatsApp, deduplicated |
| `service.py` | Wiring, worker thread, health file, built-in jobs |
| `__main__.py` | CLI |

## 2. Schedules (`config/alert_system.json`, IST)
| Job | When | Notes |
|---|---|---|
| `price_import` | 09:45, 13:15, 15:15, 16:15 Mon–Fri (15 min before each consumer job), and once at startup each day | Incremental, batched: about 30 s for a full re-download, about 2 s when up to date. Lock `load_all_stock_prices.lock` |
| `daily_alerts` | 10:00, 13:30, 15:30 Mon–Fri | |
| `mtd_rankings_whatsapp` | 16:30 Mon–Fri | `save_mtd_rankings.py`, then `whatsapp_mtd_rankings.py` emails the rankings (WhatsApp only if `WHATSAPP_ENABLED=true`) |
| `mtd_cleanup` | 00:20 daily, and at startup | built-in |
| `daily_summary` | 18:00 daily | built-in |

Since 2026-10-19 each download runs 15 min before the job that uses it. Delivery is email-only (`WHATSAPP_ENABLED=false` in `.env`); WhatsApp sections below apply only if you turn it back on.

## 3. How it works

**Scheduler.** Every 20 s, cron fire times between the persisted cursor and now become `job_runs` rows. `UNIQUE(job_id, occurrence_key)` means an occurrence is created at most once, whatever restarts or reboots happen. One worker thread runs due runs one at a time, so the import finishes before alerts read prices.

**Statuses.** `PENDING → RUNNING → SUCCESS`. A failed run goes to `RETRY_SCHEDULED` and then `FAILED`. `SKIPPED` covers missed occurrences under the catch-up policy and superseded runs.

**Retries.** Each scheduled run gets at most `max_attempts` attempts (default **10**; `JOB_MAX_ATTEMPTS` or per job). The backoff is 2, 4, 8, 16 min, then 30 min (configurable, minimum 30 s), so there is never a tight loop. The error text, exit code and output tail are stored on the run and on each `job_attempts` row. When a newer occurrence of the same job is created, it supersedes older waiting retries. Each job processes everything outstanding, so nothing is lost. A failing job never stops other jobs or the service.

**Offline or ClickHouse down.** The run is deferred every `CONNECTIVITY_RETRY_SECONDS` (60 s) **without using up an attempt**.

**Missed schedules (power off).** An occurrence more than 5 min late counts as missed. Policies:
- `latest` (default): run only the newest missed occurrence, if it is within `max_lateness_minutes`. The others are recorded as SKIPPED.
- `all`: run every missed occurrence within `max_lateness_minutes`.
- `skip`: run none of them.

Catch-up looks back at most 7 days.

**Restart recovery.** Runs left RUNNING by a crash or power loss count that attempt and are requeued immediately (or marked FAILED if no attempts remain). A graceful stop terminates the child process and requeues the run.

**WhatsApp queue.** Scripts call `queue_whatsapp(group_key, text)`, which inserts one row per recipient with `UNIQUE(group_key, recipient)`. The group keys are `alert:<alert_history.id>` and `mtd:<snapshot_date>`, so a re-run never queues a second copy. The service delivers due items every tick while online:
- **Retries:** up to `WHATSAPP_MAX_ATTEMPTS` (8), with backoff from 60 s up to 1 h.
- **Permanent errors** (invalid number, template problems) are marked FAILED straight away and you get an email.
- **Multi-part messages:** each accepted part is saved as it goes, so a retry never resends parts that already went out.
- **Crash mid-send:** if the service dies while a message is SENDING, the outcome is unknown. By default the message is **not** resent, to avoid a duplicate. It is marked FAILED and you are emailed. Requeue it with `outbox-requeue ID` if it didn't arrive.

A WhatsApp failure never blocks alert generation, ClickHouse writes or other jobs.

**MTD cleanup.** Runs at startup and daily at 00:20. On days 2–31 it is a no-op; on the 1st it removes the previous month. Before anything is deleted, it checks:
- the target is listed in config and its database is in `allowed_databases`;
- the table exists, is a MergeTree engine, and the date column is a Date type;
- the cutoff is within 62 days;
- the local date matches the ClickHouse server's date (so a wrong clock can't wipe the current month);
- rows to keep are counted before and verified after.

Strategy, chosen from what the table actually is:
- `daily_stock_prices` (partitioned by month): `DROP PARTITION` for whole old months, then a lightweight `DELETE` for older rows in the boundary month. Cutoff is the 1st minus 10 days (the MTD baseline close), moved earlier if a symbol's last pre-month close is older. `load_all_stock_prices.py` imports from the same date, so deleted rows aren't re-downloaded.
- `daily_mtd_rankings` (not partitioned): lightweight `DELETE WHERE trade_date < 1st`.
- `stock_categories` is reference data and is never touched.
- In `holdings.db`, old RESET `alert_history` rows and their delivery rows are removed, plus SENT/FAILED outbox rows and old snapshot markers. ACTIVE alerts (the dedup state), `user_alerts`, `holdings` and `processed_sessions` are kept.

If cleanup fails, it is retried by the scheduler and you are emailed. The result is written to `state/last_cleanup.json`.

**Duplicate data in ClickHouse.** The tables are plain MergeTree, so they do **not** de-duplicate rows. The scripts' own idempotency is relied on and kept:
- the price import skips existing `(symbol, trade_date)` keys and holds an exclusive lock;
- rankings delete the month's snapshot before inserting;
- the price window is regenerated from Yahoo on retry, so a failed insert loses no data;
- alerts live in SQLite and are committed before notification.

## 4. WhatsApp: account requirements, restrictions and costs
- You are using the official **WhatsApp Business Platform (Cloud API)**: a Meta Business account, a WhatsApp Business Account (WABA), a registered phone number ID and an access token. A personal WhatsApp account cannot be automated through this API.
- **Token:** use a **System User permanent token**. Temporary tokens from the developer dashboard expire after about 24 h, and then every send fails with code 190. You are emailed, but the messages stay queued until the token is fixed.
- **24-hour window:** free-form text (`WHATSAPP_SEND_MODE=text`, the current setting) is delivered only if the recipient messaged your business number in the last 24 h. Otherwise Meta returns error **131047**.
  - For reliable daily alerts, create and get approved a **Utility template** with one body variable `{{1}}`.
  - Then set `WHATSAPP_SEND_MODE=auto` (or `template`) and `WHATSAPP_TEMPLATE_NAME`.
  - In template mode the message is flattened to one line, because template variables cannot contain newlines.
- **Recipients:** while the app is in development/test mode, only numbers added as test recipients in the Meta app can receive messages. Group chats are **not** supported by the Cloud API; use individual numbers.
- **Cost:** Meta charges per delivered template message; Utility rates for India are a few paise each. Messages inside an open customer-service window are free. Check Meta's current rate card.
- **Not verified live:** I didn't send a real WhatsApp message during this work, to avoid messaging your phone unasked. The API path was tested against a local mock Graph API. `holdings.db` shows the 2026-10-13 rankings message was marked SENT by the old code. To test live: `python -m alert_system trigger mtd_rankings_whatsapp`, or queue a test message.

## 5. Startup on this machine
Chain: Windows logon → Task Scheduler `\NSE Alert System (WSL)` → `wsl.exe` (kept alive with `sleep infinity`) → systemd → `clickhouse-server` + `nse-alert.service` (`Restart=always`).
- Install or refresh the unit: `wsl -d Ubuntu -u root -- bash /mnt/e/alert/deploy/install_wsl_service.sh`
- Recreate the Windows task: `powershell -ExecutionPolicy Bypass -File E:\alert\deploy\register_windows_task.ps1`
- **Starting without signing in:** the task triggers at logon, because WSL is not reliable in a non-interactive boot task. For true power-on start, enable Windows automatic sign-in (Sysinternals *Autologon*, or `netplwiz`) and disable sleep (`powercfg /change standby-timeout-ac 0`).
- The two older boot tasks (`\Start NSE Alert System` and `\Microsoft\Windows\NSE Alert System`) only run `service cron start && service clickhouse-server start`. They are now redundant (cron has no active lines; systemd starts ClickHouse) but harmless. You can disable them.
- Single instance: an exclusive `flock` on `/run/lock/nse-alert.lock`. A second `run` exits with code 3.

## 6. Operations
```
wsl -u root -- bash -lc 'cd /mnt/e/alert && /root/nse-alert-venv/bin/python -m alert_system status'
... -m alert_system health                 # exit 0 if heartbeat fresh (state/health.json)
... -m alert_system check-config
... -m alert_system trigger daily_alerts   # manual run, executed by the service
... -m alert_system cleanup --dry-run
... -m alert_system outbox-requeue 12
journalctl -u nse-alert -f                 # or logs/alert_service.log (JSON lines, rotating 10 MB x 14)
```
Script output is still appended to the original files: `load_all_stock_prices.log`, `scheduled_daily_alerts.log` and `daily_whatsapp_rankings.log`.

## 7. Tests
`wsl -u root -- bash -lc 'cd /mnt/e/alert && /root/nse-alert-venv/bin/python -m pytest -q tests'`

There are 79 tests covering:
- cron and config validation;
- the scheduler: retry limit, backoff, catch-up, supersede, restart dedup, power-loss recovery, offline deferral;
- the outbox: dedup, retry, permanent errors, multi-part resume, crash recovery, template fallback, mock Graph API over HTTP;
- cleanup: IST month boundaries, safety refusals, SQLite retention;
- ClickHouse integration against a throw-away `alert_test_*` database: partition drop, lightweight delete, baseline guard, dry run, idempotency;
- service wiring and the subprocess runner: timeout, shutdown, lock.

## 8. Deployment and recovery checklist
- [ ] `check-config` prints OK.
- [ ] `systemctl is-enabled nse-alert clickhouse-server` shows both enabled; `crontab -l` has no active lines.
- [ ] The Windows task `\NSE Alert System (WSL)` is Ready/Running; auto sign-in is enabled if you need start without login.
- [ ] The WhatsApp token is a permanent System User token, and template mode is set if recipients are outside the 24 h window.
- [ ] Reboot test: restart Windows, sign in, wait 2 min, then `health` shows HEALTHY and `status` shows the startup runs.
- [ ] Offline test: disconnect the network, and runs show `waiting for internet` with attempts unchanged. Reconnect and they run.
- [ ] After the 1st of the month: `state/last_cleanup.json` shows `verification: passed`, and `sql/mtd_retention.sql` section 1 shows the expected min dates.
- [ ] If a job is FAILED: read the error in `status`, fix the cause, then `trigger <job>`.
- [ ] If WhatsApp is FAILED: fix the token or template, then `outbox-requeue <id>`.
- [ ] Rollback of the script edits: the `*.bak_automation_20261019` files are the pre-change versions.

## 9. Recommendations, backtest and personalised email (added 2026-10-19)

### Jobs (IST, weekdays unless noted)
| Job | When | Output |
|---|---|---|
| `recommendations` | 16:25 | Scores ~500 stocks + GOLDBEES/SILVERBEES ETFs. Writes ClickHouse `nse_alerts.recommendations` (top 10 rankings) and `state/recommendations_latest.json` |
| `daily_report` | 16:45 (`DAILY_REPORT_TIME`) | One personalised email per day (`report:<date>`) |
| `backtest` | Saturday 07:00 | `state/backtest_latest.json`. Sets the validation label shown in every report |

To run them by hand: `python -m alert_system recommend | backtest | report --preview`

### Data
- **Source:** the existing provider (Yahoo Finance via yfinance). About 420 days of daily bars are downloaded in batches into memory for each run (about 40 s).
- **Storage:** raw history is **not** stored, because ClickHouse keeps only month-to-date data. Only the ranked snapshots are stored, and they follow the same MTD cleanup.
- **Completed bars only:** before 15:45 IST today's partial bar is dropped. A last bar more than 4 days old is marked STALE or UNAVAILABLE, never presented as a live signal.
- **Market-cap categories:** taken from `nse_alerts.stock_categories` (Nifty 100 = Large-cap, Midcap 150 = Mid-cap, Smallcap 250 = Small-cap).
- **Gold and silver:** NSE ETFs `GOLDBEES.NS` and `SILVERBEES.NS` (INR, no expiry), configurable under `recommendations.commodities`. MCX futures and physical bullion prices are not available from the provider, so they are not analysed.
- **Not assessed:** fundamentals, news, corporate actions and sector strength. No reliable source exists in this project, and every recommendation says so.

### Model (`config/alert_system.json` → `recommendations`)
1. **Filters:** at least 220 bars, price of at least ₹20, minimum liquidity (median 20-day traded value: ₹20 cr large-cap, ₹10 cr mid-cap, ₹5 cr small-cap and ETFs), and ATR of at most 6% of price.
2. **Score (0–100):** a weighted sum of trend 25, momentum 20, relative strength vs Nifty 50 20, volume 15, MACD 10 and low volatility 10. Each component is shown in the email.
3. **Five independent confirmations:** trend (close > 50-DMA > 200-DMA), momentum (3-month return > 0 and RSI 45–75), volume (20-day > 50-day average with up-day dominance), relative strength > 0, and MACD above its signal line.
4. **BUY CANDIDATE** needs all of:
   - at least 3 confirmations (4 when the Nifty 50 is below its 200-DMA);
   - a score of at least 60;
   - reward/risk of at least 2;
   - not overbought (RSI ≤ 75 and at most 3 ATR above the 20-DMA).

   Otherwise the signal is **HOLD** (trend intact but overheated), **WATCHLIST**, **AVOID** (downtrend, filtered out, or score below 45) or **UNAVAILABLE** (missing or stale data).
5. **Levels (BUY CANDIDATE only):**
   - **Entry zone:** the 20-DMA (or the close − 0.5 ATR) up to the close.
   - **Stop:** the close − 2 × ATR(14).
   - **Target 1:** the nearest 60-day or 52-week high if it is at least 1R away. Otherwise a 2R projection, with a "needs breakout" risk note.
   - **Position size:** (capital × 1%) ÷ (entry − stop).
6. **Rankings** are kept separate: overall stocks, Large-cap, Mid-cap, Small-cap, and cross-asset (stocks plus gold/silver). Only BUY CANDIDATEs are ranked, and a list can be shorter than 5. It is never padded.
7. **Confidence:** MEDIUM with 4+ confirmations, otherwise LOW. HIGH is possible only after the backtest has VALIDATED the strategy.

The intended horizon is a **swing trade (2–6 weeks)**, from end-of-day signals. Intraday is not supported because the system uses daily bars.

### Backtest and validation
- **Same engine, replayed:** every 5 sessions over about 3 years, the same `analyze()` runs on data up to that day.
- **Trade rules:** entry at the **next** open. Exit at the stop, the target or after 20 days. If the stop and target are both hit in one bar, the stop is assumed.
- **Costs:** 0.45% round trip (STT, fees, stamp duty, slippage), deducted from every trade.
- **Metrics:** win rate, average win and loss, expectancy, profit factor, maximum drawdown, R-multiple distribution, and results by category and by market regime.
- **Segments:** trades are split by date into in-sample (60%), validation (20%) and out-of-sample (20%). Weights are not tuned on this data.
- **Label:**
  - **VALIDATED** only if the out-of-sample segment has at least 30 trades, positive expectancy after costs, a profit factor of at least 1.2 and a drawdown of at most 25%;
  - **FAILED** if it has enough trades but misses a criterion;
  - otherwise **UNVALIDATED**.
- **Known bias:** survivorship, because the universe is today's index constituents.

### Personalised email (`EMAIL_NOTIFICATION_MODE`)
| Mode | Per-alert emails | Daily report contains |
|---|---|---|
| `TRIGGERED_ALERTS_ONLY` | yes | no report |
| `WATCHLIST_DAILY_SUMMARY` | yes | watchlist prices, today's triggered alerts, summary |
| `RECOMMENDATIONS_ONLY` | no | enabled recommendation sections, summary |
| `COMBINED_DAILY_REPORT` (default) | yes | all of the above |

- **Watchlist:** enabled `user_alerts` symbols, plus ACTIVE `holdings`, plus `WATCHLIST_SYMBOLS`. Other stocks never appear.
- **Severity:** WARNING, or CRITICAL when the move is at least 2× the rule threshold. `ALERT_MIN_SEVERITY` filters both alert emails and the report.
- **Delivery:**
  - all emails go through the persistent outbox (`notification_outbox`, channel `email`), with bounded backoff (60 s doubling to 1 h, at most 8 attempts);
  - a rejected recipient is a permanent failure; auth and network errors are retried;
  - an email is never resent after a crash unless `EMAIL_RESEND_UNCONFIRMED=true`.
- **Duplicates:**
  - one email per alert event (`alert:<id>`) and per daily report (`report:<date>`);
  - the same stock and rule is not re-emailed within `EMAIL_DEDUP_WINDOW_HOURS`, even after a reset and re-trigger.
- **Retention:** delivery audit and dedup rows are kept for `NOTIFICATION_AUDIT_RETENTION_DAYS` (90). They are not deleted at month-end, so duplicate protection still works across the 1st.

### First backtest result (run 2026-10-19, period 2023-10-16 to 2026-10-16, 501 instruments): **FAILED**
| Segment | Trades | Win rate | Expectancy/trade (after 0.45% costs) | Profit factor | Max drawdown |
|---|---|---|---|---|---|
| In-sample | 309 | 38.5% | −0.45% | 0.88 | 54.3% |
| Validation | 103 | 45.6% | −0.23% | 0.94 | 38.6% |
| Out-of-sample | 104 | 38.5% | −0.43% | 0.89 | 35.5% |
| Weak market (Nifty below 200-DMA) | 231 | 39.0% | −0.92% | 0.79 | 57.8% |
| Strong market | 285 | 40.7% | +0.02% | 1.00 | 43.0% |

By category: Mid-cap +0.28%/trade (profit factor 1.08), Large-cap −1.03%, Small-cap −0.60%, gold/silver ETFs +0.38% (only 6 trades, too few to judge).

**Conclusion:** the model shows **no edge after costs**. Because of this, the email puts a warning banner above the recommendation lists and confidence is forced to LOW. Do not trade these lists as recommendations. The weights were deliberately **not** tuned to make the backtest pass, because tuning on the same data would only overfit it.

## 10. Live intraday alerts (added 2026-10-19)
- **Job:** `intraday_alerts` polls every 5 minutes, 09:15–15:45 IST on weekdays. The polls run past the 15:30 close because the free feed lags about 15 minutes.
- **Source:** Yahoo Finance 1-minute bars via `yfinance`. It's free and needs no account, but **NSE quotes are delayed about 15 minutes** and the source is unofficial. `LIVE_QUOTE_PROVIDER` selects the provider; a broker API (Upstox, Angel One and so on) can be added later by implementing `get_quotes()` in `alert_system/intraday.py`.
- **Rules:** your enabled `user_alerts` rules (daily % change), applied to *today's live price vs the previous session's close* (from ClickHouse).
- **Duplicates:** at most **one alert per rule per trading day**. Alerts are stored in `alert_history` with metric `Intraday`, separate from the end-of-day `Daily` alerts so the two never reset each other. The previous day's intraday alert resets automatically. Emails go through the outbox in the same table format, with the 24-hour dedup window, and they respect `EMAIL_NOTIFICATION_MODE` and `ALERT_MIN_SEVERITY`.
- **Safety:** quotes older than 30 minutes, or not from today's session (holidays), are skipped and never alerted on. Ticks are not stored; only the latest poll is kept, in `state/live_quotes.json`.
- **Failures:** a failed poll retries once after 60 s, and missed polls are skipped, not caught up. You're emailed only after **3 consecutive failed polls**, at most once per day.
- **Manual run:** `python -m alert_system live` runs one poll now (it does nothing outside market hours).

## 11. Login, users and row-level security (added 2026-10-19)
**Model:** a small invited group. Each user signs in with email + password and sees only their own holdings, alert rules, watchlist, alert history and emails. Market data (prices, rankings, recommendations) is shared.

**Where data lives:**
| Data | Store | Isolation |
|---|---|---|
| users, holdings, user_alerts, watchlist, alert_history, notification_outbox, delivery_markers | PostgreSQL `nse_app` | **Row-level security** on every table |
| Prices, MTD rankings, recommendations | ClickHouse `nse_alerts` | Shared, read-only market data |
| Scheduler state, health | `state/` | System only |

`holdings.db` (SQLite) is no longer used. It was imported once into an admin account and is kept as a backup.

**Database roles** (created by `deploy/postgres_setup.sh`; passwords generated into `.env`):
- `nse_app` is the dashboard role. It has NOBYPASSRLS, and every query runs in a transaction with `app.user_id` set to the signed-in user, so the database only returns or accepts that user's rows.
  - **Password hashes:** it cannot read them (column grants).
  - **Your own role:** it cannot change it.
  - **Alert history:** it cannot insert alert rows.
  - **Email:** it cannot queue mail to any address except the caller's own (via `request_email()`, max 10 per hour).
- `nse_service` is the background jobs role. It has BYPASSRLS, so it can evaluate every user's rules and send each user their own emails. Reports are built through `nse_app` scoped to the recipient, so a report can never contain another user's rows.
- `nse_owner` owns the schema and is used only for migrations: `python -m alert_system db-migrate`.

**Passwords and sessions:**
- **Hashing:** passwords are hashed with scrypt.
- **Strength:** at least 10 characters, mixing letters and digits/symbols.
- **Lockout:** 5 wrong attempts lock the account for 15 minutes.
- **Email enumeration:** prevented, because unknown emails take the same time as wrong passwords.
- **New accounts:** temporary passwords must be changed at first login.
- **Sessions:** a session ends after 8 hours idle, or when the browser tab is reloaded (sign in again).

**Managing users** (admins), in the dashboard under **Users**: invite (shows a one-time temporary password), reset password, activate or deactivate, change role. Or from the CLI:
```
python -m alert_system users list
python -m alert_system users add --email friend@example.com --name "Friend"
python -m alert_system users reset-password --email friend@example.com
python -m alert_system users deactivate --email friend@example.com
```

**Per-user notifications:**
- **Alerts and reports:** go to each user's own email.
- **Report mode:** each user can choose their own on the **Account** page.
- **MTD rankings:** go to every user who hasn't opted out.
- **WhatsApp (if re-enabled):** goes only to the admin number.

**New rule type: Price.** Alert when the price is ≥ or ≤ a rupee level. It's checked end-of-day and live every 5 minutes, at most once per day per rule. Set it on the Alerts page, or via the ♥ button on Top 10 Opportunities.

**Tests:** `tests/test_rls.py` proves cross-user isolation against a separate database, `nse_app_test`. `test_report_email.py` and `test_intraday.py` include cross-user report and alert checks.

**Sign-up** (`SIGNUP_MODE`, default `approval`). The sign-in screen has a **Create account** tab.
- **`approval`:** the account is created *pending* and every admin gets an email. Admins approve or reject it on **Users → Sign-up requests**. A pending user who enters the correct password sees "awaiting approval"; anyone else just gets "invalid email or password".
- **`open`:** accounts are active immediately, always as `member` (never admin).
- **`closed`:** the tab is hidden and accounts are invite-only.

Sign-ups go through the `auth_signup()` database function, limited to 20 per hour. The dashboard role still cannot create or edit other users directly.
