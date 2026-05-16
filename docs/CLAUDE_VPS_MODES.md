# Claude Code VPS Modes

This repo runs Claude Code in two environments:

1. **Operator laptop (Windows)** — primary dev environment. Always full
   write access. The operator runs Claude here interactively.
2. **Production VPS (Linux, `/opt/stockbot`)** — runs Claude as well, but
   with permissions that flip between **dev_on_vps** (full write while
   hardening) and **read_only_ops** (validation + diagnostics only, once
   the system is production-grade).

The current production VPS mode is **dev_on_vps**. The end-state mode is
**read_only_ops**.

This doc holds the full `.claude/settings.json` content for each mode plus
the rationale for every allow / deny pattern. To swap, replace the entire
contents of `.claude/settings.json` on the VPS with the matching block
below, then restart the Claude Code session.

## Why have two modes at all

While the system is still being hardened, the VPS is treated as a second
dev environment — Claude needs to edit code, run pytest, commit, and push
directly. Once the advisory layers are confirmed stable and the cron
pipeline is treated as ground truth, the VPS becomes operational and
Claude's role shifts to "read state, run validation, report problems" —
not "edit production."

The mode swap encodes that shift. Read-only mode is enforced at the tool
permission layer, not by convention.

## Mode 1: dev_on_vps (current default)

Full write access. Claude can edit code, write new files, commit, push,
and manage the systemd unit. Only catastrophic / non-recoverable commands
are denied (`rm -rf /`, `dd`, force-push to main, hard-reset to origin).

Copy this entire JSON into `.claude/settings.json` on the VPS to activate:

```json
{
  "_mode": "dev_on_vps",
  "_purpose": "Full write access. Used while hardening the system toward production-grade.",
  "_source": "docs/CLAUDE_VPS_MODES.md",
  "permissions": {
    "allow": [
      "Read(**)",
      "Glob(**)",
      "Grep(**)",
      "Edit(**)",
      "Write(**)",
      "NotebookEdit(**)",
      "Bash(python:*)",
      "Bash(python3:*)",
      "Bash(pytest:*)",
      "Bash(pip:*)",
      "Bash(npm:*)",
      "Bash(node:*)",
      "Bash(git status:*)",
      "Bash(git log:*)",
      "Bash(git diff:*)",
      "Bash(git show:*)",
      "Bash(git add:*)",
      "Bash(git commit:*)",
      "Bash(git push:*)",
      "Bash(git pull:*)",
      "Bash(git fetch:*)",
      "Bash(git branch:*)",
      "Bash(git checkout:*)",
      "Bash(git stash:*)",
      "Bash(git config:*)",
      "Bash(ls:*)",
      "Bash(cat:*)",
      "Bash(tail:*)",
      "Bash(head:*)",
      "Bash(wc:*)",
      "Bash(find:*)",
      "Bash(mkdir:*)",
      "Bash(cp:*)",
      "Bash(mv:*)",
      "Bash(chmod:*)",
      "Bash(systemctl status:*)",
      "Bash(systemctl start:*)",
      "Bash(systemctl stop:*)",
      "Bash(systemctl restart:*)",
      "Bash(systemctl daemon-reload:*)",
      "Bash(journalctl:*)",
      "Bash(crontab -l:*)",
      "Bash(source:*)"
    ],
    "deny": [
      "Bash(rm -rf /:*)",
      "Bash(rm -rf /*:*)",
      "Bash(dd:*)",
      "Bash(mkfs:*)",
      "Bash(shutdown:*)",
      "Bash(reboot:*)",
      "Bash(git push --force main:*)",
      "Bash(git push -f main:*)",
      "Bash(git reset --hard origin/main:*)"
    ]
  }
}
```

### Rationale (dev mode)

- `Edit(**)`, `Write(**)`, `NotebookEdit(**)` — Claude needs to author code
  and tests directly on the VPS while hardening the system.
- `Bash(python:*)` and friends — pytest, validation scripts, ad-hoc REPL.
- Git: full lifecycle (`status`, `log`, `diff`, `add`, `commit`, `push`,
  `pull`, `fetch`, `branch`, `checkout`, `stash`). The deny list still
  blocks force-push to main and hard-reset to origin so a careless command
  cannot blast the remote.
- `systemctl status/start/stop/restart/daemon-reload` — the daily timer
  unit lives in `deploy/systemd/`; you'll want to bounce it as you change
  cron behavior.
- `journalctl` — read systemd logs for the daily timer.
- `Bash(rm -rf /:*)` etc — only truly destructive patterns are denied.
  `rm` for normal paths is allowed under `Bash(mv:*)` / `Bash(rm:*)` is
  not explicitly allowed in this list; Claude will prompt for it if
  needed. If you want to allow it freely, add `Bash(rm:*)` to the allow
  list — leaving it out is intentional because a typo in `rm` is the
  fastest path to data loss.

## Mode 2: read_only_ops (target end state)

Read, observe, validate, report. Cannot edit code, cannot write files,
cannot mutate `outputs/latest/`, cannot push to git, cannot restart
systemd units.

Copy this entire JSON into `.claude/settings.json` on the VPS to activate:

```json
{
  "_mode": "read_only_ops",
  "_purpose": "Production ops. Claude can read state, run pytest, run validation, and report — but cannot edit code or mutate live artifacts.",
  "_source": "docs/CLAUDE_VPS_MODES.md",
  "permissions": {
    "allow": [
      "Read(**)",
      "Glob(**)",
      "Grep(**)",
      "Bash(python -m pytest:*)",
      "Bash(python -m py_compile:*)",
      "Bash(python scripts/validate_pnl_advisors.py:*)",
      "Bash(python scripts/validate_phase1_advisors.py:*)",
      "Bash(python scripts/agent_context_check.py:*)",
      "Bash(pytest:*)",
      "Bash(git status:*)",
      "Bash(git log:*)",
      "Bash(git diff:*)",
      "Bash(git show:*)",
      "Bash(git fetch:*)",
      "Bash(git pull:*)",
      "Bash(ls:*)",
      "Bash(cat:*)",
      "Bash(tail:*)",
      "Bash(head:*)",
      "Bash(wc:*)",
      "Bash(find . -name:*)",
      "Bash(systemctl status:*)",
      "Bash(journalctl:*)",
      "Bash(crontab -l:*)"
    ],
    "deny": [
      "Edit(**)",
      "Write(**)",
      "NotebookEdit(**)",
      "Bash(git commit:*)",
      "Bash(git push:*)",
      "Bash(git add:*)",
      "Bash(git reset --hard:*)",
      "Bash(git checkout --:*)",
      "Bash(git checkout -B:*)",
      "Bash(git branch -D:*)",
      "Bash(git clean -f:*)",
      "Bash(git rebase:*)",
      "Bash(git merge:*)",
      "Bash(git stash drop:*)",
      "Bash(rm:*)",
      "Bash(rmdir:*)",
      "Bash(mv:*)",
      "Bash(cp:*)",
      "Bash(chmod:*)",
      "Bash(chown:*)",
      "Bash(systemctl start:*)",
      "Bash(systemctl stop:*)",
      "Bash(systemctl restart:*)",
      "Bash(systemctl daemon-reload:*)",
      "Bash(systemctl enable:*)",
      "Bash(systemctl disable:*)",
      "Bash(crontab -e:*)",
      "Bash(crontab -r:*)",
      "Bash(pip install:*)",
      "Bash(npm install:*)",
      "Bash(dd:*)",
      "Bash(mkfs:*)",
      "Bash(shutdown:*)",
      "Bash(reboot:*)"
    ]
  }
}
```

### Rationale (read-only mode)

- `Edit(**)`, `Write(**)`, `NotebookEdit(**)` are denied outright — the
  cleanest guarantee Claude cannot touch code or artifacts.
- Bash allow list is narrow: only pytest, py_compile, the project
  validation scripts, and read-only git / shell utilities.
- Bash deny list explicitly blocks anything that could mutate state:
  `git commit/push/add/reset --hard/checkout --/branch -D/clean -f`,
  `rm/mv/cp/chmod/chown`, `systemctl start/stop/restart`, `crontab -e/-r`,
  `pip install`, `npm install`, and destructive system commands.
- `git pull` and `git fetch` are allowed because the operator may want
  Claude to pull recent changes and report on them — those don't mutate
  the working tree without an explicit checkout, which is blocked.
- `journalctl` and `systemctl status` are allowed so Claude can diagnose
  the daily timer and FMP circuit-breaker state without changing
  anything.
- The result: the only thing Claude can change on the production VPS in
  this mode is its own session memory.

## Swap procedure

```bash
# On the VPS — current Claude session
# Read the doc, find the block for the mode you want, save it as:
nano .claude/settings.json
# (paste the JSON block, save, exit)
exit  # exit the current claude REPL
claude  # restart so new permissions take effect
```

Or, from the laptop side, tell Claude on the VPS (in a session that still
has `Write(**)` access — i.e. while still in dev_on_vps mode) to apply
the read-only block:

> "Apply read_only_ops mode by writing the read-only block from
>  docs/CLAUDE_VPS_MODES.md to .claude/settings.json."

Once read-only is active, Claude on the VPS cannot swap itself back to
dev_on_vps because `Write(**)` is denied. The operator switches it back
by editing `.claude/settings.json` manually (`nano` or `vi`).

## How to verify the active mode

```bash
head -3 .claude/settings.json
# Expected:
#   {
#     "_mode": "dev_on_vps",  OR  "_mode": "read_only_ops",
#     ...
```

The `_mode` and `_purpose` keys are advisory only — Claude Code ignores
keys it doesn't recognise — but they make the active mode visible to a
human at a glance.

## When to swap

Switch the VPS to `read_only_ops` when all of these are true:

1. The advisory layers (`exit_advisor`, `cash_deployment_plan`, etc.) have
   produced clean artifacts for at least 30 consecutive daily runs.
2. `outputs/policy/decision_outcomes.jsonl` has ≥ 20 resolved decisions
   per group, so calibration / Kelly / alpha-attribution have something
   to compute.
3. The daily systemd timer has run without error for at least a week.
4. You're ready to stop editing code on the VPS and treat it as
   operations-only.

Until then, dev_on_vps is the correct mode.

## What NOT to do

- Do not commit a `.claude/settings.json` file. The settings file is
  per-environment (laptop vs VPS) and should not be checked in. This doc
  is the source of truth; `.claude/settings.json` is generated from it.
- Do not weaken `read_only_ops` with project-specific allow rules. If a
  workflow needs more access than read_only_ops grants, that workflow
  belongs in dev_on_vps.
- Do not bypass the deny list with shell tricks (`bash -c 'git commit ...'`).
  Claude Code's denylist matches the literal command pattern; clever
  invocations may slip through but doing so deliberately defeats the
  point of having modes at all.
