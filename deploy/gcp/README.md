# Run the NSE alert system on Google Cloud (free e2-micro)

Moves the alert system off the laptop so alerts, reports and data loading keep running 24×7. The laptop can then stay off.

## What you get
- Everything that runs on the laptop today, on one small Google Cloud VM: the scheduler, live alerts, email reports, ClickHouse and the recommendation engine.
- The **dashboard** at the same fixed ngrok link as on the laptop, https://genetics-upcoming-placate.ngrok-free.dev (the bundle carries the ngrok authtoken). Without an ngrok config the setup falls back to Caddy at `https://<server-ip>.sslip.io`. Each person signs in with their own email + password and sees only their own data (PostgreSQL row-level security).
- Automatic restart after crashes and VM reboots, plus automatic security updates.

## Limits and costs (please read)
- **RAM:** e2-micro has only **1 GB**. The setup adds 4 GB of swap and caps ClickHouse at 512 MB. Expect the dashboard to be slow, and the weekly backtest takes several minutes. If the server struggles, move to e2-small (2 GB, about US$13/month) with no other changes.
- **Free-tier rules:** one e2-micro, in **us-west1, us-central1 or us-east1 only**, a 30 GB *standard* persistent disk, and 1 GB/month of outbound traffic. Other regions or disk types are charged.
- **Public IP:** Google may charge a small fee for the VM's public IPv4 address. Check *Billing → Reports* after the first days. A **budget alert** (e.g. ₹200) under *Billing → Budgets & alerts* is strongly recommended.
- **Time zone:** the US region doesn't matter. The server is set to Asia/Kolkata and all schedules stay in IST.
- **Yahoo data:** Yahoo sometimes rate-limits cloud IP addresses. The jobs retry, and you're emailed if downloads keep failing.

## Steps

### 1. Create the VM (Google Cloud Console)
Go to https://console.cloud.google.com, create a project, and enable billing. A card is needed even for free tier.

Then **Compute Engine → VM instances → Create instance**:

| Setting | Value |
|---|---|
| Name | `nse-alert` |
| Region | `us-central1` (or us-west1 / us-east1) |
| Machine type | **e2-micro** |
| Boot disk | **Ubuntu 24.04 LTS** (x86/64), **Standard persistent disk**, **30 GB** |
| Firewall | tick **Allow HTTP traffic** and **Allow HTTPS traffic** |

Then click **Create**.

### 2. Make the IP permanent (recommended)
**VPC network → IP addresses** → on the VM's external IP, click **Promote to static**. Otherwise the dashboard address changes whenever the VM is stopped and started.

### 3. Build the bundle on the laptop
```
wsl -u root -- bash /mnt/e/alert/deploy/gcp/export_bundle.sh
```
This creates `E:\alert\deploy\gcp\out\nse-alert-bundle.tar.gz`. It contains your `.env` secrets and the ngrok authtoken, so keep it private.

### 4. Upload and install on the VM
In the console, click **SSH** next to the VM. A browser terminal opens. Use **Upload file** (top right) to upload `nse-alert-bundle.tar.gz`, then run:
```
tar -xzf nse-alert-bundle.tar.gz
sudo bash bundle/deploy/gcp/server_setup.sh "$PWD/bundle"
```
It takes about 10–15 minutes and ends by printing the dashboard address. After that, delete the uploaded bundle with `rm -rf nse-alert-bundle.tar.gz bundle`.

### 5. Check the server
```
sudo -u nsealert bash -c 'cd /opt/nse-alert && venv/bin/python -m alert_system health'
sudo -u nsealert bash -c 'cd /opt/nse-alert && venv/bin/python -m alert_system report --preview'
```
Open the dashboard address and sign in with your own email and password (the same accounts as on the laptop; PostgreSQL data is copied by the bundle).

### 6. Switch the laptop off (avoid double emails)
Only **after** step 5 works, run this on the laptop. It also stops the laptop's dashboard and ngrok tunnel; the server's tunnel retries every 10 s and takes over the link within a minute.
```
powershell -ExecutionPolicy Bypass -File E:\alert\deploy\gcp\laptop_switch.ps1 off
```
To go back to the laptop later, run the same command with `on`, and stop the server copy with `sudo systemctl disable --now nse-alert`.

## Updating the server later
Rebuild the bundle on the laptop (step 3), upload it, and re-run step 4. Code and config are refreshed. Your server data (holdings.db, ClickHouse tables, state) is **kept**, never overwritten.

## Useful commands on the VM
```
sudo systemctl status nse-alert nse-dashboard clickhouse-server caddy
sudo journalctl -u nse-alert -f
sudo -u nsealert bash -c 'cd /opt/nse-alert && venv/bin/python -m alert_system status'
free -m
```
