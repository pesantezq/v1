#!/bin/bash
# Integrated OS-exec backend for the R&D sandbox runner (jail_wrapper target).
#
#   Invoked as:  sandbox-run <netns> -- <worker argv...>
#
# It reads RD_JOB_ID / RD_WORKSPACE_DIR / RD_OUTPUT_DIR / RD_INPUT_DIR from the
# environment (set by the trusted Python runner, run_job) and executes the
# worker inside a TRANSIENT systemd SERVICE that enforces, technically (not by
# trust, prompt, or expected behaviour):
#
#   P0.1/P0.2  per-job filesystem isolation: an empty tmpfs is mounted over the
#              whole jobs root (TemporaryFileSystem=), hiding every sibling job,
#              then ONLY this job's workspace/ + output/ are bound back writable
#              and input/ read-only. (ProtectSystem=strict makes the rest of the
#              host tree read-only.) Sibling jobs are invisible + unreachable.
#   P0.3       complete process-tree containment: the worker is a systemd
#              service cgroup with KillMode=control-group, so setsid/double-fork
#              descendants are reaped when the service stops or is killed.
#   P1.1       resource caps: TasksMax / MemoryMax / CPUQuota (fork-bomb, OOM,
#              CPU spin are bounded).
#   P1.3/P1.4  least privilege: User=rd-worker, NoNewPrivileges=yes, empty
#              capability set, clean --setenv-only environment.
#   P0.4       network jail: runs in the OFFLINE_LOCAL netns; only the
#              inference-only Ollama proxy is reachable.
#
# RuntimeMaxSec is optional (env RDSBX_RUNTIME_MAX_SEC); when set, systemd is a
# second, independent timeout enforcer alongside the runner's own timeout.
set -euo pipefail
NS="${1:?netns required}"; shift
[ "${1:-}" = "--" ] && shift
: "${RD_JOB_ID:?}"; : "${RD_WORKSPACE_DIR:?}"; : "${RD_OUTPUT_DIR:?}"; : "${RD_INPUT_DIR:?}"
JOB_ROOT="$(dirname "$(dirname "$RD_WORKSPACE_DIR")")"   # <jobs_root>/<id>/workspace -> <jobs_root>
UNIT="rdsbx-job-${RD_JOB_ID}"
RT=()
[ -n "${RDSBX_RUNTIME_MAX_SEC:-}" ] && RT=(-p "RuntimeMaxSec=${RDSBX_RUNTIME_MAX_SEC}")

exec systemd-run --quiet --pipe --wait --collect --unit="$UNIT" \
  -p Type=exec \
  -p User=rd-worker -p Group=rd-worker \
  -p NoNewPrivileges=yes \
  -p CapabilityBoundingSet= -p AmbientCapabilities= \
  -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes \
  -p ProtectKernelTunables=yes -p ProtectKernelModules=yes -p ProtectControlGroups=yes \
  -p RestrictSUIDSGID=yes -p LockPersonality=yes -p RestrictRealtime=yes \
  -p TemporaryFileSystem="$JOB_ROOT" \
  -p BindReadOnlyPaths="$RD_INPUT_DIR" \
  -p BindPaths="$RD_WORKSPACE_DIR" \
  -p BindPaths="$RD_OUTPUT_DIR" \
  -p WorkingDirectory="$RD_WORKSPACE_DIR" \
  -p TasksMax=64 -p MemoryMax=2G -p CPUQuota=200% \
  -p NetworkNamespacePath="/run/netns/${NS}" \
  -p KillMode=control-group -p KillSignal=SIGKILL \
  "${RT[@]}" \
  --setenv=RD_JOB_ID="$RD_JOB_ID" \
  --setenv=RD_INPUT_DIR="$RD_INPUT_DIR" \
  --setenv=RD_WORKSPACE_DIR="$RD_WORKSPACE_DIR" \
  --setenv=RD_OUTPUT_DIR="$RD_OUTPUT_DIR" \
  --setenv=RD_NETWORK_PROFILE="${RD_NETWORK_PROFILE:-offline_local}" \
  --setenv=HOME="$RD_WORKSPACE_DIR" \
  --setenv=PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  --setenv=RD_OLLAMA_URL="${RD_OLLAMA_URL:-http://10.201.0.1:11435}" \
  -- "$@"
