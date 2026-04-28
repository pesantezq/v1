# StockBot — Ubuntu VPS Deployment Guide

This guide covers a clean deployment of the StockBot investment decision system
on a fresh Ubuntu 22.04 or 24.04 VPS. Windows / local workflow is unchanged.

---

## A. Recommended Server Specs

| Resource | Minimum |
|----------|---------|
| vCPU     | 2       |
| RAM      | 4 GB    |
| Disk     | 40+ GB  |
| OS       | Ubuntu 22.04 LTS or 24.04 LTS |

---

## B. Clone the Repository

```bash
git clone <your-repo-url> /opt/stockbot
cd /opt/stockbot
```

---

## C. Run Setup

The setup script is idempotent — safe to re-run.

```bash
bash scripts/server_setup.sh
```

This will:
- Install system packages (`python3`, `python3-venv`, `git`, `ufw`, `curl`)
- Create `.venv` and install `requirements.txt`
- Create `logs/`, `data/`, `outputs/latest/`, `outputs/performance/`, `outputs/policy/`
- Copy `.env.example` → `.env` (only if `.env` does not already exist)
- Make all scripts in `scripts/` executable

---

## D. Configure `.env`

```bash
nano /opt/stockbot/.env
```

Fill in every blank value. Required fields:

```
FMP_API_KEY=<your key>
ALPHA_VANTAGE_API_KEY=<your key>
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
EMAIL_USER=you@example.com
EMAIL_PASS=<app password>
EMAIL_TO=recipient@example.com
STOCKBOT_ENV=production
PYTHONUNBUFFERED=1
```

> **Never commit `.env` to version control.**

---

## E. Validate the Installation

```bash
cd /opt/stockbot

# Run the pipeline in debug mode (no email sent)
.venv/bin/python run_daily_pipeline.py --debug

# Send a test email to verify SMTP credentials
.venv/bin/python -m watchlist_scanner.daily_memo --test-email
```

Both commands should exit with code 0.

---

## F. Start the GUI Manually

```bash
bash /opt/stockbot/scripts/server_start_streamlit.sh
```

Open `http://<server-ip>:8501` in a browser. Use Ctrl-C to stop.

---

## G. Install the Systemd Service (Persistent GUI)

```bash
cp /opt/stockbot/deploy/stockbot-streamlit.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable stockbot-streamlit
systemctl start stockbot-streamlit

# Verify it is running
systemctl status stockbot-streamlit
```

---

## H. Open the Firewall

```bash
ufw allow OpenSSH
ufw allow 8501/tcp
ufw enable
ufw status
```

---

## I. Install the Daily Pipeline Cron

```bash
bash /opt/stockbot/scripts/install_cron.sh
```

This installs a cron job that runs `server_run_pipeline.sh` at **09:00 server
time** every day and appends output to `logs/cron.log`. The script is
idempotent — re-running it will not add duplicate entries.

To change the schedule, edit your crontab manually:

```bash
crontab -e
```

Reference example: `deploy/stockbot-cron.example`

---

## J. Update the Server Remotely

```bash
cd /opt/stockbot
git pull
source .venv/bin/activate
pip install -r requirements.txt
systemctl restart stockbot-streamlit

# Smoke-test after update
python run_daily_pipeline.py --debug
```

---

## K. Troubleshooting

**Streamlit service logs:**
```bash
journalctl -u stockbot-streamlit -f
```

**Pipeline / cron logs:**
```bash
tail -f /opt/stockbot/logs/server_pipeline.log
tail -f /opt/stockbot/logs/cron.log
```

**Test email delivery:**
```bash
cd /opt/stockbot
.venv/bin/python -m watchlist_scanner.daily_memo --test-email
```

**Check service status:**
```bash
systemctl status stockbot-streamlit
```

**Restart after crash:**
```bash
systemctl restart stockbot-streamlit
```

---

## Notes

- The `.env` file is never overwritten by setup scripts — your credentials are safe during updates.
- Local Windows scripts (`scripts/*.ps1`) are unaffected by this deployment.
- The systemd service runs as `root`; scope to a dedicated user if your security policy requires it.
