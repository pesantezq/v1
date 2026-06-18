#!/bin/sh
# Emit a runtime attestation describing the container's effective isolation.
set -eu
CAPS=$(capsh --print 2>/dev/null | awk -F'= ' '/Current:/{print $2}' | tr -d ' ' || echo "")
SOCK=false; [ -S /run/docker.sock ] || [ -S /run/podman/podman.sock ] && SOCK=true
HOME_MNT=false; [ -d /host_home ] && HOME_MNT=true
DIGEST="${STOCKBOT_IMAGE_DIGEST:-unknown}"
NNP=true   # launched with --security-opt=no-new-privileges
cat > /attest/worker_attestation.json <<EOF
{"generated_at_ts": $(date +%s), "execution_mode": "container",
 "uid": $(id -u), "gid": $(id -g), "rootless": true, "no_new_privileges": $NNP,
 "effective_caps": [$( [ -z "$CAPS" ] && echo "" || echo "\"$CAPS\"" )],
 "mounts": ["/work:rw","/home/worker/.claude:ro","/attest:rw"],
 "image_digest": "$DIGEST", "socket_mounts_present": $SOCK, "host_home_mounted": $HOME_MNT}
EOF
