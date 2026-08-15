# Legacy `stockbot` SSH Credential Exposure — CONFIRMED, remediation DEFERRED

Status flags (authoritative):

- `LEGACY_STOCKBOT_CREDENTIAL_EXPOSURE = CONFIRMED`
- `REMEDIATION_STATUS = DEFERRED_BY_OPERATOR`
- `RISK_ACCEPTANCE = TEMPORARY_OPERATOR_ACCEPTED_RISK`
- `LEGACY_CREDENTIAL_REMEDIATION_PENDING = TRUE`

**This does NOT mean the historical credential is safe, revoked, inert, or
remediated.** It means the operator has explicitly, temporarily accepted the risk
and deferred remediation to a later effort.

## What was exposed
- `stockbot.txt` — a real, **unencrypted** `ssh-ed25519` **private** key
  (`openssh-key-v1`, `cipher=none`), public fingerprint
  `SHA256:VXrRFq9sNLdYyA8NBQm6SdO8Ix1MrIdeIGoAnlsQU5o` (comment `stockbot`).
- `stockbot.txt.pub` — two `stockbot` public keys:
  `SHA256:VXrRFq9sNLdYyA8NBQm6SdO8Ix1MrIdeIGoAnlsQU5o` (private half was in
  `stockbot.txt`) and `SHA256:n8NQoOTofHbBQHiGmKyss1QeNWwLrpqIho0oNBI+OGQ`
  (private half disposition unknown).
- Introduced in commit `f5188f5 "adding ssh keys"` (2026-04-28) and **pushed to
  the remote** (`origin/main` ancestor, `https://github.com/pesantezq/v1`).

## Why deleting from HEAD is NOT remediation
A private key that was pushed to a remote is compromised: it persists in git
history, clones, forks, and the host's storage. Removing it from the current
working tree reduces accidental reuse but **does not** un-expose it. Real
remediation is **rotation** (invalidate the key wherever it is authorized).

## Current-tree action taken by the hardening mission
- `stockbot.txt` and `stockbot.txt.pub` **removed from the working tree** (they
  are not referenced by any runtime/test; only this and the cleanup-audit doc
  mention them).
- `.gitignore` extended with private-key / secret patterns.
- Deterministic secret-hygiene gate added (`tools/secret_scan.py` +
  `tests/test_secret_hygiene.py`) to block *new* accidental key commits.
- Git history was **NOT** rewritten (operator directive).

## Recommended operator remediation (deferred — do when resumed)
1. Treat `SHA256:VXrRFq9sNLdYyA8NBQm6SdO8Ix1MrIdeIGoAnlsQU5o` (and precautionarily
   `SHA256:n8NQoO…`) as compromised.
2. Discover where authorized: VPS `authorized_keys`, GitHub account SSH keys and
   repo deploy keys, any other hosts — match on the fingerprints above.
3. Rotate: install replacement key(s), verify, then delete the old public keys
   everywhere they appear.
4. Confirm `pesantezq/v1` visibility; if public, assume automated harvest.
5. Optionally purge from history (`git filter-repo` / BFG + force-push) after
   rotation, coordinating with collaborators.

## Not related to the certified bridge key
The certified Engineer production-evidence key `stockbot_engineer`
(`SHA256:BPNnnxrpQd147uOv45eHGl/UbC65lBncxCwTwuoR3Lw`) is a **separate** key that
lives only on the trusted controller (`~/.ssh/stockbot_engineer`, 0600), is not
in the repo or git history, and is absent from the sandbox / rd-worker. The
legacy `stockbot` key does not touch that certification.
