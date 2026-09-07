# StockBot Backup & Disaster Recovery

Three separate steps, on purpose. A nightly snapshot must not fail because
GitHub was unreachable, and a restore drill must not need the network at all.

```
backup_state.sh          → local plaintext snapshot + manifest v2
backup_offbox_push.sh    → one encrypted artifact  (upload is a separate mode)
backup_restore_verify.sh → offline restore proof    (scratch tree only)
```

## ⚠ The one thing that will actually lose your data

The encryption passphrase lives at `/root/.stockbot_backup_key`.

**If that file is lost together with the VPS, every encrypted backup is
unrecoverable.** No amount of off-box replication helps — the artifacts are
AES-256 blobs and the key is the only way in.

The key must exist somewhere that is not the VPS. This document does **not**
claim an off-box copy exists; verify that yourself. It is the single highest-value
five minutes of work in this whole system.

## Local backup

| | |
|---|---|
| Producer | `scripts/backup_state.sh` |
| Location | `/var/backups/stockbot/<UTC stamp>/` (`STOCKBOT_BACKUP_ROOT`) |
| Schedule | `30 23 * * *` (existing production cron — unchanged by this work) |
| Retention | 14 snapshots (`STOCKBOT_BACKUP_RETAIN`) |
| Permissions | artifacts `0600`, snapshot dir `0700` |

Each snapshot contains one gzip per database, one `state_files.tar.gz`, and
`manifest.json`.

Databases are copied with the SQLite **online backup API**, so a copy taken
while the pipeline is writing is still consistent. Each copy then gets
`PRAGMA integrity_check` plus a **row signature** — sorted per-table row counts,
hashed. The signature is what lets a restore prove the database still holds what
it held at backup time; `integrity_check` alone only proves the file is
well-formed SQLite.

## What is backed up, and what is not

The set is an explicit allowlist in `portfolio_automation/backup/recovery_set.py`,
with a reason on every entry. It replaced `ls data/*.json data/*.jsonl`, which
silently changed the backup policy whenever a new file appeared — it could omit
new nested state, or sweep in a new secret-bearing file, with nobody reviewing it.

**BACKUP_REQUIRED** — 6 databases (`portfolio`, `crowd_intelligence`,
`fmp_budget`, `sim_governance_watchlist`, `rd_control`,
`institutional_intelligence`) and `data/finance_history.json`.

`rd_control.db` is worth calling out: its own documentation says *"SQLite
(`data/rd_control.db`) is the single authoritative store"*, and it was **absent
from the previous four-database backup set** because it was created after that
set was written. That is exactly the failure mode an allowlist plus a test
prevents.

**SOURCE_CONTROL_DURABLE** — `.agent/`, `config/`, `docs/`, `evals/`. These carry
the protected roadmap, authority configuration and frozen experiment evidence,
and they are **deliberately not in the artifact**: they are tracked in Git, and
duplicating them nightly would create a staler second copy of record.

That reasoning has one failure mode, and it is not hypothetical — this system
already had 12 commits that existed only on the VPS. Git durability is a property
of **pushed** commits. So every manifest records `git_provenance`:

```json
"git_provenance": {
  "head": "...", "head_contained_in_origin_main": "yes|no",
  "tracked_recovery_state_dirty": false, "warning": "..."
}
```

If `head_contained_in_origin_main` is not `yes`, or `tracked_recovery_state_dirty`
is true, that state exists only on that host. `backup_state.sh` prints a warning
to stderr in that case. Fix it by pushing, not by widening the backup.

**REGENERABLE** — vendor caches, `outputs/latest/`, the 5-year price archive
(re-fetchable by the weekend backfill, though refetching costs quota and returns
the vendor's then-current adjustment vintage), VS-002 evidence, agent-export
snapshots.

**SECRET_EXCLUDED** — `.env`, tokens, SSH keys, the backup passphrase itself.
Enforced by name at both ends: the producer refuses to add a secret-shaped file,
and the verifier refuses an archive containing one.

**EPHEMERAL_EXCLUDED** — logs, and `data/stockbot.db`, which is a phantom:
referenced only by `docs/PROD_EVIDENCE_DIRECT_V0.md` and the stale
`ops/prod_evidence/stockbot-observe` script (whose own comment says `# adjust`).
No production code creates it. It is recorded in the allowlist so nobody adds it
on the strength of those two references.

## Encrypted artifact

```bash
scripts/backup_offbox_push.sh --backup-root /var/backups/stockbot
```

Produces `stockbot-state-<stamp>.tar.gz.enc` next to the snapshot and prints its
SHA-256. **Local-only by default** — no network, no GitHub, no credentials.

```
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass file:<key>
```

Unchanged from the scheme that already completed an end-to-end restore proof on
real data. It is **not** authenticated encryption; AES-GCM or `age` would be
better, and that is a deliberate future hardening mission — changing the cipher
in the same commit that repairs the format gap would have thrown away the only
real evidence we have that the chain works.

## Off-box (operator-authorized)

Upload is a **distinct explicit mode**, never implied:

```bash
STOCKBOT_OFFBOX_UPLOAD=1 scripts/backup_offbox_push.sh --backup-root ... \
  --gh-repo pesantezq/stockbot-backups
```

Publishes the blob as a private GitHub release asset tagged `backup-<stamp>`.
Retention only ever considers tags with that prefix and **fails closed** if the
release listing cannot be parsed, so an unrelated release can never be deleted.
No credential is stored in code — `gh` supplies its own.

**Nothing here has been deployed.** No off-box cron exists, no backup repository
has been created, and no upload has been performed.

## Restore proof

```bash
scripts/backup_restore_verify.sh /var/backups/stockbot/<stamp>            # plaintext
scripts/backup_restore_verify.sh /path/stockbot-state-<stamp>.tar.gz.enc  # encrypted
```

Restores into a temporary scratch tree and **never writes to the live
repository**. `RESTORE_PROOF_OK` requires all of:

1. the encrypted artifact decrypts
2. the archive unpacks **safely** — absolute paths, `../`, links, device nodes
   and secret-shaped members are all refused before anything is written
3. the manifest schema is recognized
4. snapshot identity is present and well-formed
5. the artifact set **exactly** matches the manifest — nothing missing, nothing extra
6. every SHA-256 matches **before** decompression
7. every database opens and passes `integrity_check`
8. every database reproduces its recorded row signature
9. every `BACKUP_REQUIRED` state component is present and digest-valid
10. every required surface appears in the manifest

Anything less prints `RESTORE_PROOF_FAILED` with the specific reason.

The distinction that matters: *"archive readable"* and *"archive integrity
matches the frozen manifest"* are different claims. Only the second is worth
anything after a disaster, and only the second is what this prints OK for.
