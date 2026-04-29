# Systemd Deployment

## Purpose

This deployment path runs the daily portfolio system on a VPS with `systemd`.

It is designed to work with:

```bash
git pull
sudo bash deploy/install_systemd.sh
```

The daily command executed by the service is:

```bash
python3 main.py --run-mode daily
```

The wrapper uses the repository virtualenv directly at `/opt/stockbot/.venv/bin/python3`.

## Files

| Path | Purpose |
| --- | --- |
| `scripts/run_daily.sh` | Daily wrapper invoked by systemd |
| `scripts/verify_run.sh` | Local health check for decision-plan outputs and logs |
| `deploy/install_systemd.sh` | Idempotent installer for service and timer |
| `deploy/stockbot-daily.service` | systemd oneshot service |
| `deploy/stockbot-daily.timer` | systemd timer scheduled for 06:30 daily |

## Install

Run on the VPS after the repo is available at `/opt/stockbot`:

```bash
sudo bash deploy/install_systemd.sh
```

What it does:

- creates `/opt/stockbot/logs` if missing
- ensures `scripts/run_daily.sh`, `scripts/verify_run.sh`, and `deploy/install_systemd.sh` are executable
- installs `stockbot-daily.service`
- installs `stockbot-daily.timer`
- runs `systemctl daemon-reload`
- enables and starts the timer

## Test

Run the service once manually:

```bash
sudo systemctl start stockbot-daily.service
```

Then confirm outputs:

```bash
bash /opt/stockbot/scripts/verify_run.sh
```

## Check Logs

systemd journal:

```bash
journalctl -u stockbot-daily.service -n 100
```

Wrapper log file:

```bash
tail -n 100 /opt/stockbot/logs/daily.log
```

## Disable

Stop and disable the timer:

```bash
systemctl disable --now stockbot-daily.timer
```

## Notes

- `run_daily.sh` uses absolute paths only.
- The wrapper calls the virtualenv interpreter directly and does not source `activate`.
- `decision_plan.json`, `decision_plan.md`, and `data/last_success.json` are included in the verification check.
- The timer is configured for `06:30` daily with `Persistent=true` and `RandomizedDelaySec=5min`.

## Next Implementation Step

After deployment, validate one full timer-driven run on the VPS and confirm that `/opt/stockbot/logs/daily.log` and the decision-plan artifacts update as expected.
