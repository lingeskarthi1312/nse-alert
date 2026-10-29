# Free cloud hosting (no card): Neon + GitHub Actions + Streamlit Cloud

Runs the whole system with the laptop switched off, at no cost.

| Part | Where | Free limit |
|---|---|---|
| Database (users, rules, alerts, prices, rankings, job state) | **Neon** PostgreSQL | 0.5 GB storage (we use a few MB) |
| Jobs: price import, alerts, rankings, reports, emails | **GitHub Actions** `.github/workflows/scheduler.yml` | 2000 min/month on a private repo (we use ~800–1000) |
| Dashboard with login | **Streamlit Community Cloud** | Sleeps after a long idle period, and takes ~30 s to wake |

What changed compared with the laptop:
- **Market data:** ClickHouse is gone. The market data now lives in the PostgreSQL schema `nse_alerts`, under the same table names.
- **Scheduler:** there is no always-on scheduler. GitHub runs `python -m alert_system cloud-tick` every 30 minutes on weekdays from 08:50 to 18:20 IST, plus a few daily and weekend ticks. Each tick runs every job that is due in `config/alert_system.json`, then sends the queued emails.
- **Intraday alerts:** they run every 30 minutes instead of every 5, to stay inside the free minutes. GitHub may start a tick 5–15 minutes late.
- **Job state:** the scheduler history (`scheduler.db`) and the JSON snapshots are stored in the table `app_files`.

## 1. Neon database
1. Sign up at https://console.neon.tech with GitHub (no card).
2. Create a project:
   - Name: `nse-alert`
   - PostgreSQL: 17
   - Region: **AWS Asia Pacific (Singapore)**
3. In **Dashboard → Connect**, copy the connection string. It looks like `postgresql://neondb_owner:...@ep-....neon.tech/neondb?sslmode=require`.
4. On the laptop, in WSL as root, run:
   ```
   cd /mnt/e/alert
   NEON_URL='<connection string>' /root/nse-alert-venv/bin/python deploy/cloud/neon_setup.py
   ```
   This creates the roles, applies the migrations, and copies the users, rules, alerts, prices and snapshots. It also writes two files:
   - `deploy/cloud/out/cloud.env`
   - `deploy/cloud/out/streamlit_secrets.toml`

   Both files are secrets: they are git-ignored, and you should never share them.

## 2. GitHub Actions
1. Go to the repo → **Settings → Secrets and variables → Actions → New repository secret**.
   - Name: `ENV_FILE`
   - Value: the whole content of `deploy/cloud/out/cloud.env`
2. Go to **Actions → scheduler → Run workflow** and run it once to test. The log ends with `runs executed: N`.

## 3. Streamlit Community Cloud
1. Go to https://share.streamlit.io, sign in with GitHub, and allow access to the private repo.
2. Click **Create app** and fill in:
   - Repository: `lingeskarthi1312/nse-alert`
   - Branch: `main`
   - Main file: `dashboard.py`
   - App URL: choose one, e.g. `nse-alert`
3. Open **Advanced settings**:
   - Python: **3.12**
   - Secrets: paste the content of `deploy/cloud/out/streamlit_secrets.toml`. It contains the dashboard role only, with no service or email passwords.
4. Click **Deploy**.
5. Go to **Settings → Sharing** and make the app public, so others can open it. Users still have to sign in.
6. Put the app URL into `DASHBOARD_PUBLIC_URL` in the `ENV_FILE` secret. Emails link to it.

## 4. Switch the laptop off
Only after a cloud tick has worked:
```
powershell -ExecutionPolicy Bypass -File E:\alert\deploy\gcp\laptop_switch.ps1 off
```
This stops the laptop scheduler, dashboard and ngrok tunnel. Without it, emails would go out twice.

## Day to day
- **Logs:** GitHub → Actions → scheduler → a run.
- **Run now:** **Run workflow**.
- **Change a setting:** edit the `ENV_FILE` secret (jobs) or the Streamlit secrets (dashboard).
- **Change code:** push to `main`. Actions use it at the next tick, and Streamlit redeploys automatically.
