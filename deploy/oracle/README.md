# Run the NSE alert system on Oracle Cloud (Always Free)

Same result as the Google Cloud setup (`deploy/gcp/README.md`): the scheduler, alerts, emails, ClickHouse, PostgreSQL and the dashboard at `https://<server-ip>.sslip.io` run 24x7 and the laptop can stay off. It uses the same bundle and the same `server_setup.sh`.

## Limits and costs (please read)
- **Free:** Oracle's Always Free resources (Ampere A1 up to 4 CPUs / 24 GB RAM in total, 200 GB disk, 10 TB/month traffic, 1 public IP) are not charged. A card is needed at sign-up for verification only. Stay on the **Free Tier** account and you cannot be charged.
- **Home region is permanent:** Always Free machines can only be created in the home region you pick at sign-up. Pick **India South (Hyderabad)** or **India West (Mumbai)**.
- **"Out of capacity":** free Ampere A1 machines are often sold out. Retry later or try another availability domain. The fallback is **VM.Standard.E2.1.Micro** (1 GB, also free; it will be slow, like the Google e2-micro).
- **Idle reclaim:** Oracle may stop Always Free machines that stay almost idle (CPU under 20%) for 7 days. If that happens, start the machine again in the console. Upgrading the account to *Pay As You Go* removes this rule, and Always Free resources are still free on that plan.
- **Yahoo data:** Yahoo sometimes rate-limits cloud IP addresses. The jobs retry, and you're emailed if downloads keep failing.

## Steps

### 1. Sign up
Go to https://signup.cloud.oracle.com, choose the home region (see above), and finish the card verification.

### 2. Create the VM
**Menu → Compute → Instances → Create instance**:

| Setting | Value |
|---|---|
| Name | `nse-alert` |
| Image | **Canonical Ubuntu 24.04** (click *Change image*) |
| Shape | *Change shape* → **Ampere** → **VM.Standard.A1.Flex**, **2 OCPUs, 12 GB** memory |
| Networking | create a new VCN + **public** subnet, **Assign a public IPv4 address** = yes |
| SSH keys | **Generate a key pair** → **Save private key** (keep this file; it is the only way in) |
| Boot volume | default (47 GB) is fine |

Click **Create** and wait until the instance is **Running**. Copy its **Public IP address**.

### 3. Open ports 80 and 443
On the instance page, open the **subnet** → **Security Lists** → *Default Security List* → **Add Ingress Rules**, twice:

| Source CIDR | IP protocol | Destination port |
|---|---|---|
| `0.0.0.0/0` | TCP | `80` |
| `0.0.0.0/0` | TCP | `443` |

(The VM's own firewall is opened by the setup script.)

### 4. Build the bundle on the laptop
```
wsl -u root -- bash /mnt/e/alert/deploy/gcp/export_bundle.sh
```
This creates `E:\alert\deploy\gcp\out\nse-alert-bundle.tar.gz`. It contains your `.env` secrets, so keep it private.

### 5. Upload and install (from Windows PowerShell)
Replace `<key>` with the saved private key file and `<ip>` with the public IP:
```
icacls <key> /inheritance:r /grant:r "$($env:USERNAME):R"
scp -i <key> E:\alert\deploy\gcp\out\nse-alert-bundle.tar.gz ubuntu@<ip>:~
ssh -i <key> ubuntu@<ip>
```
The `icacls` line stops SSH refusing the key with "UNPROTECTED PRIVATE KEY FILE". Then, on the VM:
```
tar -xzf nse-alert-bundle.tar.gz
sudo bash bundle/deploy/gcp/server_setup.sh "$PWD/bundle"
rm -rf nse-alert-bundle.tar.gz bundle
```
Setup takes about 10-15 minutes and ends by printing the dashboard address.

### 6. Check, then switch the laptop off
Follow steps 5 and 6 of `deploy/gcp/README.md` (health check, then `laptop_switch.ps1 off` so emails are not sent twice).

## Updating the server later
Rebuild the bundle (step 4), upload it again and re-run the install commands in step 5. Code and config are refreshed and server data is kept.
