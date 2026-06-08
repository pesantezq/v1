# GUI Portfolio Config Edit

## Overview

`/dashboard/portfolio-config` is the only write surface in the StockBot Dashboard v2.
It provides a gated, operator-approved path for updating the local portfolio holdings
and cash balance in `config.json`. It does **not** place trades, call a broker API, or
modify any decision-core artifact.

---

## Gating: Three Conditions Required

The edit form is rendered (and the save endpoint accepts submissions) only when **all
three** of the following conditions are true at request time:

1. `GUI_V2_AUTH_USER` environment variable is set (non-empty).
2. `GUI_V2_AUTH_PASS` environment variable is set (non-empty).
3. `GUI_V2_PORTFOLIO_EDIT=1` environment variable is set.

If any condition is unmet:
- `GET /dashboard/portfolio-config` renders a read-only "editing disabled" state
  (no form fields, clear disabled indicator).
- `POST /dashboard/portfolio-config/save` returns HTTP 403 with an error message
  explaining which conditions are unmet.

The gate is evaluated at **request time** (not startup), so toggling
`GUI_V2_PORTFOLIO_EDIT` in the environment takes effect on the next request after a
service restart.

**Default state:** All three conditions are unset; editing is disabled by default.
The dashboard can be read without any of these variables. The edit capability is
opt-in.

---

## Enabling the Edit (Deployment)

Add the following to `/opt/stockbot/.env` and restart the service:

```bash
GUI_V2_AUTH_USER=operator
GUI_V2_AUTH_PASS=<strong-random-password>
GUI_V2_PORTFOLIO_EDIT=1
```

```bash
sudo systemctl restart stockbot-dashboard
```

Never hardcode credentials in source files.

---

## The Validate → Dry-Run Diff → Confirm → Save Flow

### Step 1: Validate (HTMX, no write)

Submitting the form triggers `POST /dashboard/portfolio-config/validate`. This
endpoint:
- Parses the submitted holdings and cash.
- Runs `gui_v2/portfolio_config_writer.validate_config_edit()` — checks symbol
  format, numeric bounds, optional target-weight sum, holding count cap.
- If validation fails: returns an error fragment via HTMX with the exact errors
  listed. Nothing is written.
- If validation passes: computes a dry-run diff (old vs. proposed holdings) and
  renders it in the page. Still nothing is written.

### Step 2: Review the diff

The operator reviews the diff inline. The diff shows which holdings are added,
removed, or changed (shares, target_weight, asset_class, leverage).

### Step 3: Confirm → Save

A separate "Confirm and Save" submit button sends the same form to
`POST /dashboard/portfolio-config/save`. This endpoint:
1. Re-validates (server-side, independent of the HTMX preview).
2. Checks the edit gate (`_edit_enabled()`); returns 403 if not enabled.
3. If both pass, calls `gui_v2/portfolio_config_writer.apply_config_edit()`.

`apply_config_edit()` performs the following in order:
1. Takes a **backup** of the current `config.json` to
   `outputs/policy/portfolio_backups/config.<YYYYMMDD_HHMMSS>.json`.
2. Writes the updated holdings and cash to `config.json` (atomic write via
   `tools.manual_portfolio_update._atomic_write_json`).
3. Appends an **audit record** to
   `outputs/policy/manual_portfolio_updates.jsonl`.

---

## Backup and Audit

Every save operation (successful or not) leaves a recoverable trail:

| Artifact | Location | Purpose |
|---|---|---|
| Pre-save backup | `outputs/policy/portfolio_backups/config.<timestamp>.json` | Restore to previous state |
| Audit record | `outputs/policy/manual_portfolio_updates.jsonl` | Append-only log of every change |

The backup is taken **before** `config.json` is written, so a crash during write
leaves the original backup intact.

To restore a previous config, copy the backup file over `config.json`:
```bash
cp outputs/policy/portfolio_backups/config.<timestamp>.json config.json
```

---

## "Updates Local Config Only — No Trades"

The audit record includes:
```json
{
  "observe_only": true,
  "no_trade": true,
  "not_recommendation": true,
  "disclaimer": "GUI operator update of holdings and cash. No broker trade placed. No recommendation emitted. ..."
}
```

The GUI edit surface:
- Does **not** call the Schwab API or any broker.
- Does **not** modify `outputs/latest/` or `decision_plan.json`.
- Does **not** modify `signal_registry.yaml`.
- Does **not** rerun the scoring or allocation pipeline.

The pipeline will pick up the updated `config.json` on its next scheduled run
(daily cron via `stockbot-daily.timer`).

---

## Schwab Portfolio-Sync and the Config Edit

The `/dashboard/portfolio-sync` view shows a read-only reconcile proposal that
compares the live Schwab account (when the `schwab-readonly-sync` feature is
available) against the local `config.json`. The proposal is an artifact only; it
does not mutate anything.

If the operator wants to apply the proposal, they must manually review the diff and
use the gated `/dashboard/portfolio-config` edit form to submit the changes. The
save flow described above applies identically.

---

## Safe-Write Primitives

`gui_v2/portfolio_config_writer.py` imports and reuses the safe-write primitives
from `tools.manual_portfolio_update`:
- `_atomic_write_json` — write via a temp file + rename for crash safety.
- `_write_backup` — creates the timestamped backup in `outputs/policy/portfolio_backups/`.
- `_append_audit_record` — appends the JSON audit line to `manual_portfolio_updates.jsonl`.

These primitives are tested independently in `tests/test_manual_portfolio_update.py`
and reused here without reimplementation.

---

## Related Docs

- `docs/gui_observe_only_safety.md` — full observe-only model
- `docs/gui_usage.md` — dashboard routes overview
- `docs/gui_remote_access.md` — secure remote access before enabling the edit gate
- `CLAUDE.md` — hard boundaries (no recompute outside decision-core)
