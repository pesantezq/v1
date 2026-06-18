#!/usr/bin/env bash
# =============================================================================
# worker_container_setup.sh — Operator provisioning guide for the
# stockbot-worker container isolation layer.
#
# IMPORTANT: This script is a GUIDED CHECKLIST, not an auto-installer.
#   - Sourcing this file or running it without a subcommand does nothing.
#   - Every step that modifies the system PRINTS the required command for
#     the operator to review and run manually with sudo.
#   - No step auto-executes system-mutating commands.
#
# Usage:
#   bash scripts/worker_container_setup.sh <subcommand>
#
# Subcommands (run in order):
#   check        — verify prerequisites (OS, podman presence, user existence)
#   account      — print commands to create the stockbot-worker system account
#   subid        — print commands to add /etc/subuid + /etc/subgid ranges
#   linger       — print command to enable loginctl linger
#   install      — print apt-get install command for podman
#   build        — build the container image (runs as current user, rootless)
#   digest       — capture and display the sha256 image digest
#   pin          — print the config.json fragment to pin the digest
#   creddir      — print commands to establish the worker credential directory
#   attest       — run a smoke attestation against the built image
#   all          — print the full ordered sequence of operator steps (dry run)
#   help         — show this help
# =============================================================================

set -euo pipefail

IMAGE_REF="localhost/stockbot-worker"
CONTAINERFILE="docker/Containerfile"
WORKER_USER="stockbot-worker"
WORKER_UID=2000
WORKER_GID=2000
SUBUID_RANGE="2000:65536"
SUBGID_RANGE="2000:65536"
CREDS_DIR="/opt/stockbot-worker-creds"
ATTEST_OUT="outputs/operator_control/worker_attestation.json"

_header() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
_info()   { printf '    %s\n' "$*"; }
_cmd()    { printf '\n    \033[1;32m$ %s\033[0m\n' "$*"; }
_warn()   { printf '  \033[1;33mWARN:\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
# subcommand: check
# ---------------------------------------------------------------------------
cmd_check() {
    _header "Prerequisite check"
    _info "OS:  $(uname -sr 2>/dev/null || echo unknown)"
    if command -v podman >/dev/null 2>&1; then
        _info "podman: $(podman --version)"
    else
        _warn "podman not found — run the 'install' subcommand first"
    fi
    if id "$WORKER_USER" >/dev/null 2>&1; then
        _info "System account '$WORKER_USER' EXISTS (uid=$(id -u "$WORKER_USER"))"
    else
        _warn "System account '$WORKER_USER' not found — run the 'account' subcommand"
    fi
    if grep -q "^${WORKER_USER}:" /etc/subuid 2>/dev/null; then
        _info "/etc/subuid entry found for $WORKER_USER"
    else
        _warn "/etc/subuid entry missing — run the 'subid' subcommand"
    fi
    if grep -q "^${WORKER_USER}:" /etc/subgid 2>/dev/null; then
        _info "/etc/subgid entry found for $WORKER_USER"
    else
        _warn "/etc/subgid entry missing — run the 'subid' subcommand"
    fi
    if loginctl show-user "$WORKER_USER" 2>/dev/null | grep -q "^Linger=yes"; then
        _info "loginctl linger: ENABLED for $WORKER_USER"
    else
        _warn "loginctl linger not enabled — run the 'linger' subcommand"
    fi
}

# ---------------------------------------------------------------------------
# subcommand: account
# ---------------------------------------------------------------------------
cmd_account() {
    _header "Create the stockbot-worker system account (operator-run commands)"
    _info "Run the following as root/sudo:"
    _cmd "sudo useradd --system --uid ${WORKER_UID} --gid ${WORKER_GID} --no-create-home --shell /usr/sbin/nologin ${WORKER_USER}"
    _info ""
    _info "If gid ${WORKER_GID} does not yet exist, first create the group:"
    _cmd "sudo groupadd --gid ${WORKER_GID} ${WORKER_USER}"
    _info ""
    _info "Verify:"
    _cmd "id ${WORKER_USER}"
}

# ---------------------------------------------------------------------------
# subcommand: subid
# ---------------------------------------------------------------------------
cmd_subid() {
    _header "Add /etc/subuid + /etc/subgid ranges (operator-run commands)"
    _info "These ranges allow the stockbot-worker account to run rootless Podman."
    _info "Append the following lines (as root):"
    _cmd "echo '${WORKER_USER}:${SUBUID_RANGE}' | sudo tee -a /etc/subuid"
    _cmd "echo '${WORKER_USER}:${SUBGID_RANGE}' | sudo tee -a /etc/subgid"
    _info ""
    _info "Verify:"
    _cmd "grep '${WORKER_USER}' /etc/subuid /etc/subgid"
}

# ---------------------------------------------------------------------------
# subcommand: linger
# ---------------------------------------------------------------------------
cmd_linger() {
    _header "Enable loginctl linger for ${WORKER_USER} (operator-run command)"
    _info "Linger keeps the user's systemd session alive after logout so"
    _info "rootless Podman can manage its own cgroups."
    _cmd "sudo loginctl enable-linger ${WORKER_USER}"
    _info ""
    _info "Verify:"
    _cmd "loginctl show-user ${WORKER_USER} | grep Linger"
}

# ---------------------------------------------------------------------------
# subcommand: install
# ---------------------------------------------------------------------------
cmd_install() {
    _header "Install Podman (operator-run command)"
    _info "Podman is the only required system package."
    _cmd "sudo apt-get update && sudo apt-get install -y podman"
    _info ""
    _info "After installation verify:"
    _cmd "podman --version"
    _cmd "podman info --format '{{.Host.Security.Rootless}}'"
}

# ---------------------------------------------------------------------------
# subcommand: build
# ---------------------------------------------------------------------------
cmd_build() {
    _header "Build the worker container image"
    _info "This runs as the CURRENT user (rootless build). Run from /opt/stockbot."
    _info "Building now..."
    echo ""
    podman build -t "${IMAGE_REF}" -f "${CONTAINERFILE}" .
    echo ""
    _info "Build complete. Run 'digest' subcommand to capture the sha256."
}

# ---------------------------------------------------------------------------
# subcommand: digest
# ---------------------------------------------------------------------------
cmd_digest() {
    _header "Capture the image sha256 digest"
    DIGEST=$(podman inspect --format '{{index .RepoDigests 0}}' "${IMAGE_REF}" 2>/dev/null \
             || podman image inspect --format '{{.Digest}}' "${IMAGE_REF}" 2>/dev/null \
             || echo "")
    if [ -z "$DIGEST" ]; then
        # Fallback: use the manifest digest directly
        DIGEST=$(podman images --no-trunc --format '{{.Digest}}' "${IMAGE_REF}" 2>/dev/null | head -1 || echo "")
    fi
    if [ -z "$DIGEST" ] || [ "$DIGEST" = "<none>" ]; then
        _warn "Could not read digest. Is the image built? Run 'build' first."
        _info "Manual alternative:"
        _cmd "podman image inspect --format '{{.Digest}}' ${IMAGE_REF}"
        exit 1
    fi
    # Normalise: ensure it starts with sha256:
    DIGEST="${DIGEST#*@}"   # strip any "ref@" prefix if present
    _info "Image digest:"
    printf '\n    %s\n\n' "$DIGEST"
    BUILD_TS=$(date +%s)
    _info "image_build_ts (epoch): ${BUILD_TS}"
    _info ""
    _info "Copy BOTH values into config.json (see 'pin' subcommand for the fragment)."
}

# ---------------------------------------------------------------------------
# subcommand: pin
# ---------------------------------------------------------------------------
cmd_pin() {
    _header "config.json fragment to pin the digest"
    _info "Add / merge this block into config.json under operator_control:"
    cat <<'FRAGMENT'

  "worker_container": {
    "enabled": false,
    "podman_path": "/usr/bin/podman",
    "image_ref": "localhost/stockbot-worker",
    "image_digest": "sha256:<PASTE_DIGEST_HERE>",
    "image_build_ts": <PASTE_EPOCH_HERE>,
    "run_as_user": "stockbot-worker",
    "container_uid": 2000,
    "container_gid": 2000,
    "attestation_path": "outputs/operator_control/worker_attestation.json",
    "attestation_max_age_days": 30,
    "env_allowlist": ["OPENAI_API_KEY", "FMP_API_KEY"],
    "cap_drop_exceptions": [],
    "resource_limits": {
      "pids": 256,
      "memory": "2g",
      "cpus": "1.0",
      "tmpfs_size": "512m",
      "timeout_seconds": 600
    }
  }

FRAGMENT
    _info "Set enabled=true ONLY after all gates pass (see 'attest' + check auth status)."
}

# ---------------------------------------------------------------------------
# subcommand: creddir
# ---------------------------------------------------------------------------
cmd_creddir() {
    _header "Establish the worker credential directory (operator-run commands)"
    _info "The credential directory holds the ~/.claude login state for the"
    _info "worker account. It is mounted read-only inside the container."
    _info ""
    _cmd "sudo mkdir -p ${CREDS_DIR}"
    _cmd "sudo chown ${WORKER_USER}:${WORKER_USER} ${CREDS_DIR}"
    _cmd "sudo chmod 0700 ${CREDS_DIR}"
    _info ""
    _info "Then, as the stockbot-worker user, run 'claude' once to complete"
    _info "interactive OAuth so ~/.claude credentials are written:"
    _cmd "sudo -u ${WORKER_USER} XDG_CONFIG_HOME=${CREDS_DIR} claude --version"
    _info ""
    _info "The resulting ${CREDS_DIR}/.claude directory is the ro creds_dir"
    _info "passed to build_container_launch_spec."
}

# ---------------------------------------------------------------------------
# subcommand: attest
# ---------------------------------------------------------------------------
cmd_attest() {
    _header "Smoke attestation"
    _info "Runs the worker_attest.sh inside the container and writes"
    _info "${ATTEST_OUT}."
    _info ""
    _info "Requires: image built + pinned digest in config.json."
    _info ""
    DIGEST=$(podman image inspect --format '{{.Digest}}' "${IMAGE_REF}" 2>/dev/null | head -1 || echo "unknown")
    ATTEST_DIR="$(pwd)/$(dirname ${ATTEST_OUT})"
    mkdir -p "${ATTEST_DIR}"
    podman run --rm \
        --user=2000:2000 \
        --read-only \
        --security-opt=no-new-privileges \
        --cap-drop=ALL \
        --pids-limit=64 \
        --memory=256m \
        --cpus=0.5 \
        --tmpfs=/tmp:size=64m \
        -v "${ATTEST_DIR}:/attest:rw" \
        -e "STOCKBOT_IMAGE_DIGEST=${DIGEST}" \
        "${IMAGE_REF}" \
        /usr/local/bin/worker_attest.sh
    _info ""
    _info "Attestation written to: ${ATTEST_OUT}"
    _info "Contents:"
    cat "${ATTEST_OUT}"
    _info ""
    _info "Now enable the container in config.json (enabled=true) and verify"
    _info "operator_worker_readiness returns auth=green."
}

# ---------------------------------------------------------------------------
# subcommand: all  (dry-run overview)
# ---------------------------------------------------------------------------
cmd_all() {
    _header "Full operator provisioning sequence (overview)"
    _info "Run each subcommand in order. Sudo/system steps are printed, not run."
    _info ""
    _info "  1. bash scripts/worker_container_setup.sh check"
    _info "  2. bash scripts/worker_container_setup.sh install"
    _info "  3. bash scripts/worker_container_setup.sh account"
    _info "  4. bash scripts/worker_container_setup.sh subid"
    _info "  5. bash scripts/worker_container_setup.sh linger"
    _info "  6. bash scripts/worker_container_setup.sh build"
    _info "  7. bash scripts/worker_container_setup.sh digest"
    _info "  8. bash scripts/worker_container_setup.sh pin     # edit config.json"
    _info "  9. bash scripts/worker_container_setup.sh creddir"
    _info " 10. bash scripts/worker_container_setup.sh attest"
    _info " 11. Set worker_container.enabled=true in config.json"
    _info " 12. Verify: python -c \\"
    _info "       from portfolio_automation.operator_worker_readiness import operator_worker_readiness"
    _info "       import json; print(json.dumps(operator_worker_readiness('.'), indent=2))\""
    _info "     Expected: gates.auth.status == 'green'"
    _info ""
    _info "See docs/operator_worker_container.md for the full prose runbook."
}

# ---------------------------------------------------------------------------
# Main dispatcher — GUARDED: sourcing or running without args is a no-op
# ---------------------------------------------------------------------------
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    # Being sourced — do nothing
    return 0 2>/dev/null || true
fi

case "$1" in
    check)   cmd_check ;;
    account) cmd_account ;;
    subid)   cmd_subid ;;
    linger)  cmd_linger ;;
    install) cmd_install ;;
    build)   cmd_build ;;
    digest)  cmd_digest ;;
    pin)     cmd_pin ;;
    creddir) cmd_creddir ;;
    attest)  cmd_attest ;;
    all)     cmd_all ;;
    help|--help|-h) bash "$0" all; echo ""; _info "Subcommands: check account subid linger install build digest pin creddir attest all help" ;;
    "")
        echo ""
        echo "worker_container_setup.sh — operator provisioning guide"
        echo ""
        echo "Run with a subcommand. No subcommand = no action (safe to source)."
        echo "  bash scripts/worker_container_setup.sh help"
        echo ""
        exit 0
        ;;
    *)
        echo "Unknown subcommand: ${1:-}" >&2
        echo "Run: bash scripts/worker_container_setup.sh help" >&2
        exit 1
        ;;
esac
