# Streamlit Retirement Runbook

Status: ready to retire whenever the operator chooses.

`gui/app.py` (the original 7000+ line Streamlit dashboard) has been
superseded by `gui_v2/` (FastAPI + HTMX + Jinja2 + Tailwind, port 8502).
All read-only pages were migrated. The remaining Streamlit-only behaviour
was the write surface, which has been replaced with small, atomic CLI
tools that mirror the same semantics with an audit trail.

This document is the complete operator runbook for retiring Streamlit.

## Decision: write surface = CLIs

The roadmap step `gui_v2_write_surface_decision` resolved to **CLI
replacements**, not "write mode in gui_v2." Rationale:

- gui_v2 is read-only by spec invariant. Adding a write surface would
  expand the security and accidental-mutation surface.
- The operator already runs from a terminal. CLI tools fit that workflow.
- Each CLI tool is small, atomic, audited, and individually testable.

## Page-by-page replacement map

| Streamlit page | Replacement | Notes |
|---|---|---|
| Dashboard | gui_v2 **Today** (`/`) | Top widgets + decisions + capital + memo |
| Decision Center | gui_v2 **Today** (`/`) | Full decision queue + AI validation + perf |
| Automatic Promotion | gui_v2 **Research** (`/research`) | Candidate triage by status + safety flags |
| Watchlist Manager (View tab) | gui_v2 **Portfolio** (`/portfolio`) | Holdings + symbols + tags + signals |
| Watchlist Manager (Add/Remove/Import) | `python -m tools.watchlist_edit ...` | See command examples below |
| Run History | gui_v2 **Operations** (`/operations`) | Run history + snapshots + portfolio peaks |
| Run Controls | `python main.py --run-mode daily` | The Streamlit page was just a button wrapping this command |
| Outputs (browse/preview) | gui_v2 **Operations** + `scp`/`curl` | Log tail in Operations; file download via shell |
| Logs | gui_v2 **Operations** (`/operations`) | Log tail with mode column + error counts |
| API Health | gui_v2 **Health** (`/health`) | All probes |
| Production Health | gui_v2 **Health** (`/health`) | Already migrated last session |
| Config Editor | direct edit of `config.json` or `tools.manual_portfolio_update` | See "Config edits" below |
| Prompts | direct edit of `data/prompts.json` | Rare; no CLI wrapper warranted |
| Diagnostics | gui_v2 **Health** (`/health`) | All probes |

## CLI tools shipped with this track

| Tool | Purpose |
|---|---|
| `python -m tools.watchlist_edit` | Add / remove / bulk-replace symbols; set tags / notes / enabled; import/export. Atomic writes, audit log at `outputs/policy/watchlist_edits.jsonl`. |
| `python -m tools.manual_portfolio_update` | Update portfolio holdings + cash. Requires `--approve` flag. Atomic; audit log + config backup. |
| `python -m tools.notify_status` | SMTP alert on production FAIL/WARN. Throttled; opt-in via `STATUS_ALERT_ENABLED=1`. Reuses `MEMO_EMAIL_*` SMTP config. |
| `python -m tools.backup_portfolio_db` | SQLite online backup with retention. Cron-ready. |
| `python -m tools.status` | Read-only health check (per-probe). Used by daily chain + ad-hoc. |
| `python -m tools.smoke_test` | Read-only artifact-shape validation. Used by preflight. |
| `python -m tools.cleanup_orphan_outputs` | One-shot remediation of orphan `/opt/outputs/` from the parents[2] regression. |
| `python -m tools.daily_sandbox_run` | Sandbox / research lane orchestrator (already present). |
| `python -m portfolio_automation.env --check` | Env-var registry inspection (with redaction). |

## Command examples for the operator

### Watchlist edits

```bash
# List current watchlist with tags + enabled state
python -m tools.watchlist_edit --list

# Add symbols
python -m tools.watchlist_edit --add NVDA,AAPL,MSFT

# Remove symbols (also strips orphaned tags)
python -m tools.watchlist_edit --remove AAPL

# Replace the whole list
python -m tools.watchlist_edit --bulk-replace QQQ,SPY,GLD

# Per-symbol metadata
python -m tools.watchlist_edit --set-tag NVDA AI,Semis
python -m tools.watchlist_edit --set-note NVDA "AI bellwether"
python -m tools.watchlist_edit --disable AAPL
python -m tools.watchlist_edit --enable AAPL

# Backup + restore
python -m tools.watchlist_edit --export watchlist_backup.json
python -m tools.watchlist_edit --import watchlist_backup.json

# Preview without writing
python -m tools.watchlist_edit --dry-run --add NVDA,AAPL
```

### Trigger a daily run on demand

```bash
# Production cron does this automatically; this is the ad-hoc path
cd /opt/stockbot && source .venv/bin/activate
python main.py --run-mode daily
```

### Production status + alerting

```bash
# Inspect production state right now (read-only)
python -m tools.status

# Strict mode for cron (exits non-zero on WARN/FAIL)
python -m tools.status --strict

# Email alert on failure (opt-in; reuses MEMO_EMAIL_* SMTP config)
# Set STATUS_ALERT_ENABLED=1 in /opt/stockbot/.env, then:
*/15 * * * *  cd /opt/stockbot && /opt/stockbot/.venv/bin/python -m tools.notify_status \
              >> logs/notify_status.log 2>&1
```

### DB backup

```bash
# One-shot
python -m tools.backup_portfolio_db --retain 30

# Cron — daily at 04:00
0 4 * * *  cd /opt/stockbot && /opt/stockbot/.venv/bin/python -m tools.backup_portfolio_db \
           --retain 30 >> logs/backup.log 2>&1
```

### Config edits

For portfolio holdings + cash use the existing structured tool:

```bash
# Edit data/holdings_update.csv first, then
python -m tools.manual_portfolio_update \
    --input data/holdings_update.csv \
    --cash 1000.00 \
    --as-of 2026-05-15 \
    --approve
```

For other config fields (scoring weights, signal thresholds, etc) edit
`config.json` directly with a text editor. Restart the daily timer only
if a config change should take effect immediately:

```bash
sudo systemctl restart stockbot-daily.timer
```

## Retirement procedure

When you're satisfied with gui_v2, disable Streamlit on the VPS:

```bash
# 1. Confirm gui_v2 has every page you use day-to-day
python -m tools.status                          # quick health check
curl -s http://localhost:8502/                  # Today renders
curl -s http://localhost:8502/portfolio         # Portfolio renders
curl -s http://localhost:8502/research          # Research renders
curl -s http://localhost:8502/health            # Health renders
curl -s http://localhost:8502/operations        # Operations renders

# 2. Stop and disable the Streamlit unit (does NOT touch the daily timer)
sudo systemctl disable --now stockbot-streamlit.service

# 3. Optional: free port 8501 in your firewall
#    (only if you opened it explicitly; the daily timer doesn't use 8501)

# 4. Optional: remove the unit file
sudo rm /etc/systemd/system/stockbot-streamlit.service
sudo systemctl daemon-reload
```

The daily pipeline (`stockbot-daily.timer` → `run_daily.sh` → `main.py`),
the sandbox timer (`stockbot-sandbox-daily.timer`), and the new GUI
(`stockbot-dashboard.service` on 8502) are **unaffected** by Streamlit
retirement.

## Reversibility

The Streamlit code is still in the repo at `gui/app.py`. To re-enable:

```bash
sudo systemctl enable --now stockbot-streamlit.service
```

No code change required. The unit file is still in
`deploy/systemd/stockbot-streamlit.service` (verify it lives in
`/etc/systemd/system/` if you removed it in step 4 above; if not,
copy it back from the repo).

## Hard constraints preserved

Everything in this track has honored the system's hard invariants:

- Advisory only, no broker integration, no auto-trading
- Read-only consumer pattern in gui_v2 (write actions live in CLI tools
  with explicit `--approve` flags and audit trails)
- Run-mode governance preserved (CLIs do not bypass `assert_can_*`)
- No business-logic changes to scoring / allocation / recommendations /
  decision engine
- Observe-only / no-trade flags preserved in every output artifact
- Two-lane separation: official lane (daily/manual/weekly) and research
  lane (discovery/backtest/historical_replay) untouched
